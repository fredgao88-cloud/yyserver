# -*- coding: utf-8 -*-
"""
私立医院智能业务管理 Agent 系统 - FastAPI 规则引擎服务 (v1.2)
严格依照《开发设计文档 v1.2》架构规范：
- 零硬编码点值/系数，动态直连 his_agent_db.dbo.dip_rules；
- 全流程高精度 Decimal 交叉相乘判灯；
- 自费标 NOT_APPLICABLE，多候选/缺参/床日缺天数标 PENDING；
- 严格遵循隐私保护红线：审计日志严禁记录患者自由文本、姓名与病案明细；
- 支持渠道分流（钉钉渠道患者级查询自动激活隐私盾牌）。
"""
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from decimal import Decimal
import sys
import os
import re

# 确保能加载项目内部模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.dip_calculator import match_dip_rule, evaluate_traffic_light
from tools.analytics_reporter import get_doctor_performance_report
from tools.dip_importer import import_dip_rules_records, parse_csv_bytes
from agent.llm_router import parse_local_intent
from agent.tool_dispatcher import extract_tool_call, dispatch_tool_call
from api.admin_ui import ADMIN_OPERATORS_HTML
from api.workbench_ui import (
    INPATIENT_ALERTS_HTML, ANALYTICS_HTML, DIP_RULES_ADMIN_HTML, AUDIT_LOGS_ADMIN_HTML, RECONCILIATION_HTML
)
from tools.reconciliation_reporter import run_settled_reconciliation_report
from db.local_db import record_audit_log, get_local_db_connection
from db.his_reader import HISReader
from auth.rbac import (
    get_authorized_dept_ids,
    authenticate, issue_token, operator_from_optional_token, require_roles,
    ensure_auth_schema, TOKEN_TTL_MINUTES, PATIENT_QUERY_ROLES, ALERTS_ROLES,
    ROLE_DOCTOR, ROLE_DEPT_HEAD, ROLE_INSURANCE, ROLE_ADMIN, ALL_ROLES,
    get_operator, list_operators, create_operator, set_operator_status
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FIX-10: 使用 lifespan 替代已废弃的 @app.on_event('startup')"""
    try:
        ensure_auth_schema()
    except Exception as e:
        print(f"[WARN] 鉴权账号表自检失败: {e}")
    yield

# FIX-09: 生产环境默认关闭 /docs 与 /redoc，仅在显式设置 ENABLE_API_DOCS=1/true 时开放
_ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").strip().lower() in ("1", "true", "yes")

app = FastAPI(
    title="私立医院智能业务管理 Agent 系统 (FastAPI 规则引擎)",
    version="1.2.0",
    docs_url="/docs" if _ENABLE_API_DOCS else None,
    redoc_url="/redoc" if _ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_API_DOCS else None,
    lifespan=lifespan
)

# ----------------- 数据模型定义 -----------------

class QueryDipRuleRequest(BaseModel):
    disease: str = Field(..., description="诊断名称或ICD编码，如 M17.9、膝关节病、K63.5")
    age: Optional[int] = Field(None, description="患者周岁年龄")
    ins_type: Optional[str] = Field(None, description="参保类型：职工/居民/自费，缺失时触发待定追问")
    operation: Optional[str] = Field(None, description="拟行或已执行手术操作关键词")
    bed_days: Optional[int] = Field(None, description="在院床日天数（针对按床日付费病种必填）")
    rule_version: str = Field("2026_SAMPLE_V1", description="DIP 规则版本号")

class CheckPatientDipRequest(BaseModel):
    patient_id: Optional[str] = Field(None, description="患者唯一流水号，如 zy2025042901")
    disease: str = Field(..., description="主要诊断编码或名称")
    current_cost: Decimal = Field(..., description="当前住院累计费用（元）")
    age: Optional[int] = Field(None, description="患者周岁年龄")
    ins_type: Optional[str] = Field(None, description="参保类型：职工/居民/自费，缺失时触发待定追问")
    operation: Optional[str] = Field(None, description="已完成手术或操作名称")
    bed_days: Optional[int] = Field(None, description="在院床日天数（按床日付费必填）")
    rule_version: str = Field("2026_SAMPLE_V1", description="DIP 规则版本号")

class ChatRequest(BaseModel):
    text: str = Field(..., description="自然语言查询内容")
    channel: Optional[str] = Field("web", description="来源渠道：dingtalk / web")

class LoginRequest(BaseModel):
    operator_id: str = Field(..., description="操作员工号")
    password: str = Field(..., description="操作员口令")

class OperatorUpsertRequest(BaseModel):
    operator_id: str = Field(..., min_length=1, max_length=64, description="操作员工号")
    display_name: str = Field(..., min_length=1, max_length=64, description="显示姓名")
    password: str = Field(..., min_length=8, description="口令（至少 8 位）")
    role_type: str = Field(..., description="角色类型")
    dept_scope: str = Field("", description="科室范围或科室 ID")
    doctor_id: str = Field("", description="绑定的 HIS 医生工号")
    dingtalk_userid: str = Field("", description="钉钉 userid 映射")
    can_export_case: bool = Field(False, description="是否允许导出病例")

class OperatorStatusRequest(BaseModel):
    status: str = Field(..., description="ACTIVE 或 DISABLED")

class DipRuleImportRequest(BaseModel):
    rule_version: str = Field(..., description="目标规则版本号，如 2026_V2")
    source_doc: str = Field(..., description="来源文件或发文编号")
    effective_date: Optional[str] = Field(None, description="生效日期 YYYY-MM-DD")
    approve: bool = Field(False, description="是否直接置为 APPROVED")
    replace_version: bool = Field(False, description="是否覆盖同版本号旧规则")
    dry_run: bool = Field(False, description="是否仅预检不入库")
    csv_text: Optional[str] = Field(None, description="CSV 文本内容（与 records 二选一）")
    records: Optional[List[Dict[str, Any]]] = Field(None, description="结构化规则行列表")

# ----------------- 启动自检 -----------------

# ----------------- 路由定义 -----------------

@app.get("/health", summary="开机自愈与健康检查接口")
async def health_check():
    """轻量级健康检查，独立返回 HTTP 200 与 ok 状态"""
    return {"status": "ok", "version": "1.2.0"}

@app.post("/api/auth/login", summary="操作员登录签发访问令牌")
async def login(req: LoginRequest):
    """
    工号 + 口令本地校验（PBKDF2 哈希），签发 HMAC 签名令牌；
    连续失败 5 次锁定 10 分钟。审计日志绝不记录口令与令牌本体。
    """
    op, reason = authenticate(req.operator_id, req.password)
    if op is None:
        record_audit_log(
            operator_id=re.sub(r'[^a-zA-Z0-9_-]', '', req.operator_id or "unknown")[:64] or "unknown",
            role_type="UNKNOWN", channel="API", action_type="LOGIN",
            target_ref="LOGIN", auth_result="DENIED"
        )
        raise HTTPException(status_code=401, detail=reason)
    token = issue_token(op["operator_id"], op["role_type"], op.get("dept_scope", ""), cred_version=int(op.get("cred_version") or 0))
    record_audit_log(
        operator_id=op["operator_id"], role_type=op["role_type"], channel="API",
        action_type="LOGIN", target_ref="LOGIN", auth_result="PERMITTED"
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in_minutes": TOKEN_TTL_MINUTES,
        "operator_id": op["operator_id"],
        "display_name": op["display_name"],
        "role_type": op["role_type"]
    }

@app.post("/api/tools/query_dip_rule", summary="DIP 规则检索与基准测算接口")
async def query_dip_rule(req: QueryDipRuleRequest, authorization: Optional[str] = Header(None)):
    """
    根据疾病、年龄、医保类型、手术操作等参数，从数据库检索匹配生效 DIP 规则并测算基准。
    科室级基准不含患者数据，保持开放；身份仅取自有效令牌，绝不信任自报工号请求头。
    """
    op = operator_from_optional_token(authorization)
    result = match_dip_rule(
        disease=req.disease,
        age=req.age,
        ins_type=req.ins_type,
        op_keyword=req.operation,
        bed_days=req.bed_days,
        rule_version=req.rule_version
    )
    
    # 严格合规审计日志：仅记录校验通过的标准规则ID或标准ICD，绝不截取记录用户自由文本
    if result.get("matched"):
        clean_target = f"RULE_ID:{result.get('rule_id')}"
    elif result.get("disease_code"):
        clean_target = f"ICD:{result['disease_code']}"
    else:
        clean_target = "QUERY:UNMATCHED"
    record_audit_log(
        operator_id=op["sub"],
        role_type=op["role"],
        channel="API",
        action_type="QUERY_DIP_RULE",
        target_ref=clean_target,
        auth_result="PERMITTED",
        rule_version=req.rule_version
    )
    
    return result

@app.post("/api/tools/check_patient_dip", summary="在院患者费用判灯与控费分析接口")
async def check_patient_dip(req: CheckPatientDipRequest, operator: Dict[str, Any] = Depends(require_roles(*PATIENT_QUERY_ROLES))):
    """
    根据患者累计费用与数据库生效 DIP 规则，执行高精度未舍入 Decimal 交叉相乘判灯分析。
    患者级接口：强制有效令牌 + 角色校验（医生/医保办/管理员），操作人身份只取自令牌。
    """
    # 实时管床医生与科主任数据范围穿透核验（严禁使用 15 分钟快照授权）
    role = operator.get("role")
    if role in (ROLE_DOCTOR, ROLE_DEPT_HEAD) and req.patient_id:
        reader = HISReader()
        adm = reader.get_patient_realtime_admission(req.patient_id)
        if not adm:
            record_audit_log(
                operator_id=operator["sub"],
                role_type=operator["role"],
                channel="API",
                action_type="CHECK_PATIENT_DIP",
                target_ref=f"PATIENT:{re.sub(r'[^a-zA-Z0-9_-]', '', req.patient_id or '')[:30]}",
                auth_result="DENIED"
            )
            raise HTTPException(
                status_code=403,
                detail="越权访问：管床鉴权未通过"
            )

        if role == ROLE_DOCTOR:
            cur_doc_id = (adm.get("attending_doctor_id") or "").strip()
            op_rec = get_operator(operator["sub"])
            if not op_rec or op_rec.get("status") != "ACTIVE":
                # AUTH-SCOPE-001：操作员账号不存在或已停用，一律拒绝，绝不回退登录名
                valid_doc_ids = set()
            else:
                mapped_doc_id = (op_rec.get("doctor_id") or "").strip()
                if mapped_doc_id:
                    # 存在显式 HIS 工号映射时，仅采信当前有效映射的 HIS 工号，严禁同时放行登录名以防工号碰撞越权
                    valid_doc_ids = {mapped_doc_id}
                else:
                    # 有效旧账号未配置显式映射时兼容使用操作员表内 operator_id
                    fallback_id = (op_rec.get("operator_id") or "").strip()
                    valid_doc_ids = {fallback_id} if fallback_id else set()
            # 严格使用唯一工号和有效在院关系精确授权，关系缺失时拒绝；严禁根据科室放行，对外统一拒绝信息
            is_attending = bool(cur_doc_id and cur_doc_id in valid_doc_ids)
            if not is_attending:
                record_audit_log(
                    operator_id=operator["sub"],
                    role_type=operator["role"],
                    channel="API",
                    action_type="CHECK_PATIENT_DIP",
                    target_ref=f"PATIENT:{re.sub(r'[^a-zA-Z0-9_-]', '', req.patient_id or '')[:30]}",
                    auth_result="DENIED"
                )
                raise HTTPException(
                    status_code=403,
                    detail="越权访问：管床鉴权未通过"
                )

        elif role == ROLE_DEPT_HEAD:
            cur_dept_id = str(adm.get("current_dept_id") or "").strip()
            auth_dept_ids = get_authorized_dept_ids(operator.get("dept") or "")
            # AUTH-SCOPE-002：仅用可信科室 ID 裁决，缺少科室 ID 或映射不明确一律拒绝
            is_authorized_dept = bool(cur_dept_id and cur_dept_id in auth_dept_ids)
            if not is_authorized_dept:
                record_audit_log(
                    operator_id=operator["sub"],
                    role_type=operator["role"],
                    channel="API",
                    action_type="CHECK_PATIENT_DIP",
                    target_ref=f"PATIENT:{re.sub(r'[^a-zA-Z0-9_-]', '', req.patient_id or '')[:30]}",
                    auth_result="DENIED"
                )
                raise HTTPException(
                    status_code=403,
                    detail="越权访问：管床鉴权未通过"
                )

    cost_dec = Decimal(str(req.current_cost))

    # 负费用拦截（未闭环冲正退费明细）
    if cost_dec < Decimal("0"):
        record_audit_log(
            operator_id=operator["sub"],
            role_type=operator["role"],
            channel="API",
            action_type="CHECK_PATIENT_DIP",
            target_ref=f"PATIENT:{re.sub(r'[^a-zA-Z0-9_-]', '', req.patient_id or '')[:30]}",
            auth_result="PERMITTED",
            rule_version=req.rule_version
        )
        return {
            "success": False,
            "patient_id": req.patient_id,
            "status": "PENDING",
            "pending_reason": "NEGATIVE_FEE_UNRECONCILED",
            "message": "当前费用为负数（可能存在冲正退费未闭环或结算重算明细），暂停判灯并进入待定核查",
            "current_cost": float(cost_dec),
            "evaluation": {
                "light": "无",
                "status": "PENDING",
                "pending_reason": "NEGATIVE_FEE_UNRECONCILED",
                "message": "当前费用为负数（可能存在冲正退费未闭环或结算重算明细），暂停判灯并进入待定核查",
                "ratio_percent": None,
                "boundary_note": None
            }
        }

    rule_res = match_dip_rule(
        disease=req.disease,
        age=req.age,
        ins_type=req.ins_type,
        op_keyword=req.operation,
        bed_days=req.bed_days,
        rule_version=req.rule_version
    )
    
    # 严格合规审计日志：仅记录去标识化患者唯一流水号或规则ID，绝不记录任何患者文本
    if req.patient_id:
        clean_target = f"PATIENT:{re.sub(r'[^a-zA-Z0-9_-]', '', req.patient_id)[:30]}"
    elif rule_res.get("matched"):
        clean_target = f"RULE_ID:{rule_res.get('rule_id')}"
    else:
        clean_target = "PATIENT:UNSPECIFIED"
    record_audit_log(
        operator_id=operator["sub"],
        role_type=operator["role"],
        channel="API",
        action_type="CHECK_PATIENT_DIP",
        target_ref=clean_target,
        auth_result="PERMITTED",
        rule_version=req.rule_version
    )
    
    status = rule_res.get("status")

    # 自费患者
    if status == "NOT_APPLICABLE":
        return {
            "success": True,
            "patient_id": req.patient_id,
            "status": "NOT_APPLICABLE",
            "message": "自费患者不适用医保 DIP 支付规则",
            "current_cost": req.current_cost,
            "evaluation": {
                "light": "无",
                "status": "NOT_APPLICABLE",
                "message": "自费患者不适用医保 DIP 支付规则",
                "ratio_percent": None,
                "boundary_note": None
            }
        }

    # 无法唯一定位（待补充信息）
    if status == "PENDING":
        return {
            "success": False,
            "patient_id": req.patient_id,
            "status": "PENDING",
            "pending_reason": rule_res.get("pending_reason"),
            "message": rule_res.get("message"),
            "candidates": rule_res.get("candidates")
        }

    # 未找到规则
    if not rule_res.get("matched"):
        return {
            "success": False,
            "patient_id": req.patient_id,
            "status": "NOT_FOUND",
            "message": rule_res.get("message", "未唯一定位到 DIP 规则基准"),
            "rule_detail": rule_res
        }

    # 唯一定位成功，进行交叉相乘判灯计算
    bm = Decimal(str(rule_res.get("benchmark_amount") or 0))
    if bm <= Decimal("0"):
        return {
            "success": False,
            "patient_id": req.patient_id,
            "status": "PENDING",
            "pending_reason": "INVALID_BENCHMARK",
            "message": "命中规则基准金额为零或无效（<=0），无法进行有效控费判灯测算"
        }
    low_th = Decimal(str(rule_res["low_threshold"]))
    high_th = Decimal(str(rule_res["high_threshold"]))
    
    light_eval = evaluate_traffic_light(
        current_cost=cost_dec,
        benchmark=bm,
        low_threshold=low_th,
        high_threshold=high_th,
        ins_type=rule_res.get("ins_type") or "职工"
    )
    if light_eval.get("status") == "PENDING":
        return {
            "success": False,
            "patient_id": req.patient_id,
            "status": "PENDING",
            "pending_reason": light_eval.get("pending_reason"),
            "message": light_eval.get("message"),
            "current_cost": float(cost_dec),
            "evaluation": light_eval
        }
    
    return {
        "success": True,
        "patient_id": req.patient_id,
        "status": "MATCHED",
        "rule_id": rule_res["rule_id"],
        "rule_version": rule_res["rule_version"],
        "disease_name": rule_res["disease_name"],
        "group_name": rule_res["group_name"],
        "billing_unit": rule_res["billing_unit"],
        "score": rule_res["score"],
        "benchmark_amount": rule_res["benchmark_amount"],
        "current_cost": req.current_cost,
        "evaluation": light_eval
    }

@app.get("/api/inpatient/alerts", summary="在院科室预警列表看板接口")
async def get_inpatient_alerts(dept: Optional[str] = Query(None, description="科室筛选，如 中医科"),
                               operator: Dict[str, Any] = Depends(require_roles(*ALERTS_ROLES))):
    """
    获取当前住院患者红黄绿灯控费预警清单（穿透读取 dbo.inpatient_dip_snapshot 最新批次）。
    患者级接口：强制有效令牌，仅科室主任、医保办与管理员角色可访问。
    支持按角色实施科室范围 (dept_scope) 行级数据隔离及快照过期检测。
    """
    from datetime import datetime, timedelta, timezone
    role = operator.get("role")
    op_info = get_operator(operator["sub"])
    user_dept = operator.get("dept") or (op_info.get("dept_scope") if op_info else "")

    # 行级权限过滤：科室主任仅允许查看本科室数据
    effective_dept = dept
    if role == ROLE_DEPT_HEAD:
        if not user_dept:
            raise HTTPException(status_code=403, detail="科室主任账号未配置科室范围 (dept_scope)，无法访问预警看板")
        effective_dept = user_dept

    cursor, conn = get_local_db_connection(as_dict=True)
    try:
        cursor.execute("""
        SELECT TOP 1 batch_id, data_cutoff_time, synced_at
        FROM dbo.inpatient_dip_snapshot
        ORDER BY synced_at DESC
        """)
        latest_batch = cursor.fetchone()

        if not latest_batch:
            return {
                "is_demo_baseline": False,
                "total": 0,
                "batch_id": None,
                "batch_time": None,
                "is_stale": True,
                "stale_warning": "系统尚未生成任何在院快照批次，请先触发在院扫描任务",
                "items": []
            }

        batch_id = latest_batch["batch_id"]
        cutoff_time = latest_batch["data_cutoff_time"]
        synced_at = latest_batch["synced_at"]

        # SNAP-TIME-001：统一转换为 UTC 再比较（无偏移数据库时间明确按东八区 UTC+8 解析）
        _DB_TZ = timezone(timedelta(hours=8))
        now_utc = datetime.now(timezone.utc)
        is_stale = False
        stale_warning = None
        if synced_at:
            synced_aware = synced_at if getattr(synced_at, "tzinfo", None) is not None else synced_at.replace(tzinfo=_DB_TZ)
            synced_utc = synced_aware.astimezone(timezone.utc)
            if (now_utc - synced_utc) > timedelta(hours=24):
                is_stale = True
                synced_local = synced_aware.astimezone(_DB_TZ)
                stale_warning = f"快照数据生成于 {synced_local.strftime('%Y-%m-%d %H:%M:%S')}，已超过 24 小时未更新，请及时运行定时巡检任务"

        sql = """
        SELECT visit_id, ward_name, bed_no, dept_id, dept_name, doctor_id, age,
               ins_type, primary_icd, matched_rule_id, current_fee, target_base,
               fee_ratio, alert_level, pending_reason, data_cutoff_time
        FROM dbo.inpatient_dip_snapshot
        WHERE batch_id = %s
        """
        params = [batch_id]
        if role == ROLE_DEPT_HEAD:
            auth_dept_ids = get_authorized_dept_ids(user_dept)
            if not auth_dept_ids:
                raise HTTPException(status_code=403, detail="科室主任账号未配置有效科室 ID 或存在同名歧义，无法访问预警看板")
            dept_list = sorted(auth_dept_ids)
            placeholders = ', '.join(['%s'] * len(dept_list))
            # AUTH-SCOPE-002 & SNAP-QUERY-001：严格按可信 dept_id 过滤，缺失 dept_id 的历史行对科主任不可见
            sql += f" AND dept_id IN ({placeholders})"
            params.extend(dept_list)
        elif dept:
            # FIX-07: 优先按 dept_id 过滤；若传入的是名称，同时匹配 dept_name 作为兼容；
            # 管理员路径不受科室 ID 授权约束，但仍建议前端传 dept_id 而非名称
            sql += " AND (dept_id = %s OR dept_name = %s)"
            params.extend([dept, dept])
        sql += " ORDER BY alert_level ASC, current_fee DESC"

        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()

        items = []
        for r in rows:
            level = r["alert_level"]
            light_map = {"GREEN": "绿灯", "YELLOW": "黄灯", "RED": "红灯", "PENDING": "待定"}
            status_desc_map = {
                "GREEN": "费用正常（<90%）",
                "YELLOW": "预警观察（90%~110%）",
                "RED": "费用超标（>110%）",
                "PENDING": f"待定核查（{r.get('pending_reason') or '信息待补充'}）"
            }
            # FIX-12: 先用 Decimal quantize 消除浮点舍入误差；保留原数值字段兼容旧调用方，同时提供高精度字符串字段
            fee_dec = Decimal(str(r["current_fee"])).quantize(Decimal("0.01"))
            bm_dec = Decimal(str(r["target_base"])).quantize(Decimal("0.01")) if r["target_base"] is not None else None
            ratio_dec = (r["fee_ratio"] * Decimal("100")).quantize(Decimal("0.01")) if r["fee_ratio"] is not None else None
            items.append({
                "zyh": r["visit_id"],
                "dept_id": r.get("dept_id"),
                "dept_name": r["dept_name"],
                "dept": r["dept_name"],
                "ward": r["ward_name"],
                "bed": r["bed_no"],
                "doctor_id": r["doctor_id"],
                "disease": r["primary_icd"] or "未明确",
                "ins_type": r["ins_type"],
                "current_cost": float(fee_dec),
                "current_cost_str": f"{fee_dec:.2f}",
                "benchmark": float(bm_dec) if bm_dec is not None else None,
                "benchmark_str": f"{bm_dec:.2f}" if bm_dec is not None else None,
                "light": light_map.get(level, level),
                "alert_level": level,
                "status": status_desc_map.get(level, level),
                "pending_reason": r.get("pending_reason"),
                "ratio_percent": float(ratio_dec) if ratio_dec is not None else None
            })

        return {
            "is_demo_baseline": False,
            "total": len(items),
            "batch_id": batch_id,
            "batch_time": cutoff_time.strftime("%Y-%m-%d %H:%M:%S") if cutoff_time else None,
            "is_stale": is_stale,
            "stale_warning": stale_warning,
            "items": items
        }
    finally:
        conn.close()


@app.get("/api/analytics/doctor-performance", summary="医生 DIP 控费绩效分析与历史趋势聚合报表")
async def get_doctor_performance(
    days: int = Query(30, ge=1, le=365, description="统计最近天数，默认 30 天"),
    dept_id: Optional[str] = Query(None, description="科室 ID 筛选（管理员/医保办可用）"),
    doctor_id: Optional[str] = Query(None, description="医生工号筛选"),
    operator: Dict[str, Any] = Depends(require_roles(*PATIENT_QUERY_ROLES)),
):
    """
    基于快照历史批次聚合医生绩效指标与控费趋势。
    严格执行行级权限隔离：
    - 医生仅可查看本人名下数据；
    - 科主任仅可查看授权科室 ID 范围内的数据；
    - 医保办与管理员可查看全院或按参数筛选。
    """
    role = operator.get("role")
    authorized_dept_ids = None
    enforced_doctor_id = None

    if role == ROLE_DOCTOR:
        op_rec = get_operator(operator["sub"])
        if not op_rec or op_rec.get("status") != "ACTIVE":
            raise HTTPException(status_code=403, detail="越权访问：操作员账号状态异常")
        mapped_doc_id = (op_rec.get("doctor_id") or "").strip()
        enforced_doctor_id = mapped_doc_id or (op_rec.get("operator_id") or "").strip()
        if not enforced_doctor_id:
            raise HTTPException(status_code=403, detail="越权访问：未配置有效医生工号")
        if doctor_id and doctor_id.strip() != enforced_doctor_id:
            raise HTTPException(status_code=403, detail="越权访问：医生仅可查看本人绩效报表")
    elif role == ROLE_DEPT_HEAD:
        user_dept = operator.get("dept") or ""
        authorized_dept_ids = get_authorized_dept_ids(user_dept)
        if not authorized_dept_ids:
            raise HTTPException(status_code=403, detail="科室主任账号未配置有效科室 ID 或存在同名歧义，无法访问报表")
        if dept_id and dept_id.strip() not in authorized_dept_ids:
            raise HTTPException(status_code=403, detail="越权访问：无权查询非本科室绩效数据")

    report = get_doctor_performance_report(
        days=days,
        dept_id=dept_id,
        doctor_id=doctor_id,
        authorized_dept_ids=authorized_dept_ids,
        enforced_doctor_id=enforced_doctor_id,
    )
    record_audit_log(
        operator_id=operator["sub"],
        role_type=role or "UNKNOWN",
        channel="API",
        action_type="DOCTOR_PERFORMANCE_REPORT",
        target_ref=f"DAYS:{days}",
        auth_result="PERMITTED",
    )
    return report



@app.get("/inpatient", response_class=HTMLResponse, summary="在院患者 DIP 控费预警看板 Web 工作台")
async def inpatient_workbench_page():
    """纯本地静态工作台页面，所有患者数据通过 Bearer Token 调用 /api/inpatient/alerts 获取。"""
    return HTMLResponse(content=INPATIENT_ALERTS_HTML)

@app.get("/analytics", response_class=HTMLResponse, summary="医生绩效与历史趋势看板 Web 工作台")
async def analytics_workbench_page():
    """纯本地静态工作台页面，所有绩效数据通过 Bearer Token 调用 /api/analytics/doctor-performance 获取。"""
    return HTMLResponse(content=ANALYTICS_HTML)

@app.get("/admin/dip-rules", response_class=HTMLResponse, summary="DIP 规则目录批量导入 Web 控制台")
async def dip_rules_admin_page():
    """纯本地静态工作台页面，通过 Bearer Token 调用 /api/admin/dip-rules/import 执行预检与导入。"""
    return HTMLResponse(content=DIP_RULES_ADMIN_HTML)

@app.get("/reconciliation", response_class=HTMLResponse, summary="出院决算账实对账看板 Web 工作台")
async def reconciliation_workbench_page():
    """纯本地静态页面，通过 Bearer Token 调用 /api/reconciliation/settled 执行核对。"""
    return HTMLResponse(content=RECONCILIATION_HTML)

@app.get("/api/reconciliation/settled", summary="出院决算账实自动对账接口（医保办/管理员）")
async def api_settled_reconciliation(
    limit: int = Query(30, ge=1, le=100, description="抽检最近已结算病例数"),
    dept: Optional[str] = Query(None, description="按科室名称筛选"),
    operator: Dict[str, Any] = Depends(require_roles(ROLE_INSURANCE, ROLE_ADMIN)),
):
    report = run_settled_reconciliation_report(limit=limit, department=dept.strip() if dept else None)
    record_audit_log(
        operator_id=operator["sub"],
        role_type=operator["role"],
        channel="API",
        action_type="SETTLED_RECONCILIATION",
        target_ref=f"LIMIT:{limit}",
        auth_result="PERMITTED",
    )
    return report

@app.get("/admin/audit-logs", response_class=HTMLResponse, summary="合规审计日志监控 Web 控制台")
async def audit_logs_admin_page():
    """纯本地静态页面，通过 Bearer Token 调用 /api/admin/audit-logs 查询脱敏日志。"""
    return HTMLResponse(content=AUDIT_LOGS_ADMIN_HTML)

@app.get("/api/admin/audit-logs", summary="检索脱敏合规审计日志（仅管理员）")
async def api_list_audit_logs(
    days: int = Query(7, ge=1, le=90, description="查询最近天数 (1-90)"),
    limit: int = Query(100, ge=1, le=200, description="最大返回条数 (1-200)"),
    auth_result: Optional[str] = Query(None, description="PERMITTED 或 DENIED"),
    action_type: Optional[str] = Query(None, description="动作类型精确筛选"),
    operator: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN)),
):
    from datetime import datetime, timedelta
    if auth_result and auth_result.strip().upper() not in ("PERMITTED", "DENIED"):
        raise HTTPException(status_code=400, detail="auth_result 仅支持 PERMITTED 或 DENIED")

    since_dt = datetime.now() - timedelta(days=days)
    cursor, conn = get_local_db_connection(as_dict=True)
    try:
        sql = """
        SELECT TOP (%s)
            log_id, operator_id, role_type, channel, action_type,
            target_ref, auth_result, rule_version, batch_id, created_at
        FROM dbo.agent_audit_log
        WHERE created_at >= %s
        """
        params: List[Any] = [limit, since_dt]
        if auth_result:
            sql += " AND auth_result = %s"
            params.append(auth_result.strip().upper())
        if action_type and action_type.strip():
            sql += " AND action_type = %s"
            params.append(action_type.strip()[:50])
        sql += " ORDER BY log_id DESC"
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        ts = r.get("created_at")
        items.append({
            "log_id": r.get("log_id"),
            "operator_id": r.get("operator_id"),
            "role_type": r.get("role_type"),
            "channel": r.get("channel"),
            "action_type": r.get("action_type"),
            "target_ref": r.get("target_ref"),
            "auth_result": r.get("auth_result"),
            "rule_version": r.get("rule_version"),
            "batch_id": r.get("batch_id"),
            "created_at": ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts or ""),
        })

    return {"days": days, "total": len(items), "items": items}

@app.get("/admin/operators", response_class=HTMLResponse, summary="操作员权限管理 Web 控制台")
async def admin_operators_page():
    """纯本地静态管理控制台，所有业务数据通过 Bearer Token 调用受保护 API 获取。"""
    return HTMLResponse(content=ADMIN_OPERATORS_HTML)

@app.get("/api/admin/operators", summary="列出全部操作员（脱敏）")
async def api_list_operators(operator: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    items = list_operators()
    record_audit_log(
        operator_id=operator["sub"], role_type=operator["role"], channel="API",
        action_type="ADMIN_LIST_OPERATORS", target_ref=f"COUNT:{len(items)}", auth_result="PERMITTED"
    )
    return {"total": len(items), "items": items}

@app.post("/api/admin/operators", summary="创建或重置操作员账号")
async def api_upsert_operator(
    req: OperatorUpsertRequest,
    operator: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN)),
):
    if req.role_type not in ALL_ROLES:
        raise HTTPException(status_code=400, detail=f"非法角色类型: {req.role_type}")
    try:
        create_operator(
            operator_id=req.operator_id.strip(),
            display_name=req.display_name.strip(),
            password=req.password,
            role_type=req.role_type,
            dept_scope=req.dept_scope.strip(),
            dingtalk_userid=req.dingtalk_userid.strip(),
            can_export_case=req.can_export_case,
            doctor_id=req.doctor_id.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    record_audit_log(
        operator_id=operator["sub"], role_type=operator["role"], channel="API",
        action_type="ADMIN_UPSERT_OPERATOR", target_ref=f"OP:{req.operator_id.strip()[:32]}", auth_result="PERMITTED"
    )
    return {"success": True, "operator_id": req.operator_id.strip(), "role_type": req.role_type}

@app.patch("/api/admin/operators/{operator_id}/status", summary="启用或停用操作员账号")
async def api_set_operator_status(
    operator_id: str,
    req: OperatorStatusRequest,
    operator: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN)),
):
    target_status = req.status.strip().upper()
    if target_status not in ("ACTIVE", "DISABLED"):
        raise HTTPException(status_code=400, detail="status 仅支持 ACTIVE 或 DISABLED")
    if operator_id.strip() == operator["sub"] and target_status == "DISABLED":
        raise HTTPException(status_code=400, detail="禁止停用当前正在使用的管理员账号")
    ok = set_operator_status(operator_id.strip(), target_status)
    if not ok:
        raise HTTPException(status_code=404, detail=f"操作员不存在: {operator_id}")
    record_audit_log(
        operator_id=operator["sub"], role_type=operator["role"], channel="API",
        action_type="ADMIN_SET_OPERATOR_STATUS", target_ref=f"OP:{operator_id.strip()[:32]}->{target_status}", auth_result="PERMITTED"
    )
    return {"success": True, "operator_id": operator_id.strip(), "status": target_status}

@app.post("/api/admin/dip-rules/import", summary="DIP 新版规则目录批量导入接口")
async def import_dip_rules_api(
    req: DipRuleImportRequest,
    operator: Dict[str, Any] = Depends(require_roles(ROLE_INSURANCE, ROLE_ADMIN)),
):
    """仅限医保办或管理员批量校验并导入新版 DIP 规则目录（支持 dry-run 预检与单事务覆盖）。"""
    rows = req.records
    if rows is None and req.csv_text:
        rows = parse_csv_bytes(req.csv_text.encode("utf-8"))
    if not rows:
        raise HTTPException(status_code=400, detail="请提供非空的 records 或 csv_text")

    res = import_dip_rules_records(
        rows,
        rule_version=req.rule_version,
        source_doc=req.source_doc,
        effective_date=req.effective_date,
        review_status="APPROVED" if req.approve else "PENDING",
        replace_version=req.replace_version,
        dry_run=req.dry_run,
    )
    if res.get("success") and not req.dry_run and req.approve:
        from tools.dip_calculator import get_loaded_rules
        get_loaded_rules(rule_version=req.rule_version, force_reload=True)

    record_audit_log(
        operator_id=operator["sub"],
        role_type=operator["role"],
        channel="API",
        action_type="DIP_RULE_IMPORT",
        target_ref=f"VER:{req.rule_version[:30]}",
        auth_result="PERMITTED" if res.get("success") else "DENIED",
        rule_version=req.rule_version,
    )
    return res

@app.post("/api/chat", summary="院内本地自然语言意图分类与规则问答接口")
async def chat_interaction(req: ChatRequest, authorization: Optional[str] = Header(None)):
    """
    接收自然语言提问，执行本地纯正则/规则意图分流，零云端大模型依赖，严格保护患者隐私。
    科室级问答保持开放；身份仅取自有效令牌，绝不信任自报工号请求头。
    """
    op = operator_from_optional_token(authorization)
    channel = req.channel or "web"
    result = parse_local_intent(req.text, channel=channel)

    # Function Calling 闭环：仅在未触发钉钉隐私盾牌与报销/参保追问拦截时，尝试调度本地业务工具
    if result.get("intent") not in ("privacy_shielded", "need_reimbursement_clarification"):
        tc = extract_tool_call(req.text, channel=channel)
        if tc is not None:
            result = dispatch_tool_call(tc, op)

    # CHAT-AUDIT-001：依据实际授权与调度结果记录 DENIED / PERMITTED
    intent_code = result.get("intent", "UNKNOWN")
    res_status = (result.get("data") or {}).get("status") if isinstance(result.get("data"), dict) else None
    is_denied = (intent_code == "tool_call_denied" or res_status in ("AUTH_REQUIRED", "FORBIDDEN"))
    record_audit_log(
        operator_id=op["sub"],
        role_type=op["role"],
        channel=req.channel or "web",
        action_type="CHAT_INTENT",
        target_ref=f"INTENT:{intent_code}",
        auth_result="DENIED" if is_denied else "PERMITTED"
    )
    return result

@app.get("/", summary="轻量级院内服务首页")
async def index():
    return {
        "system": "私立医院智能业务管理 Agent 系统",
        "version": "v1.2",
        "engine": "Database-Backed DIP Rule Engine (36 sample rules in his_agent_db)",
        "health_check": "/health",
        "api_docs": "/docs"
    }
