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
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from decimal import Decimal
import sys
import os
import re

# 确保能加载项目内部模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.dip_calculator import match_dip_rule, evaluate_traffic_light
from agent.llm_router import parse_local_intent
from db.local_db import record_audit_log, get_local_db_connection
from db.his_reader import HISReader
from auth.rbac import (
    get_authorized_dept_ids,
    authenticate, issue_token, operator_from_optional_token, require_roles,
    ensure_auth_schema, TOKEN_TTL_MINUTES, PATIENT_QUERY_ROLES, ALERTS_ROLES,
    ROLE_DEPT_HEAD, get_operator
)

app = FastAPI(
    title="私立医院智能业务管理 Agent 系统 (FastAPI 规则引擎)",
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc"
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

# ----------------- 启动自检 -----------------

@app.on_event("startup")
async def _startup_auth_schema():
    """启动时确保鉴权账号表就绪（数据库暂不可用时仅告警，不阻断服务）"""
    try:
        ensure_auth_schema()
    except Exception as e:
        print(f"[WARN] 鉴权账号表自检失败: {e}")

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
            op_rec = get_operator(operator["sub"]) or {}
            valid_doc_ids = {
                operator["sub"].strip(),
                (op_rec.get("operator_id") or "").strip(),
                (op_rec.get("doctor_id") or "").strip(),
                (operator.get("doctor_id") or "").strip()
            }
            valid_doc_ids.discard("")
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
            cur_dept_name = str(adm.get("current_dept") or "").strip()
            auth_dept_ids = get_authorized_dept_ids(operator.get("dept") or "")
            is_authorized_dept = bool(
                (cur_dept_id and cur_dept_id in auth_dept_ids) or
                (cur_dept_name and cur_dept_name in auth_dept_ids)
            )
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
    from datetime import datetime, timedelta
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

        now_dt = datetime.now()
        is_stale = False
        stale_warning = None
        if synced_at and (now_dt - synced_at) > timedelta(hours=24):
            is_stale = True
            stale_warning = f"快照数据生成于 {synced_at.strftime('%Y-%m-%d %H:%M:%S')}，已超过 24 小时未更新，请及时运行定时巡检任务"

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
                raise HTTPException(status_code=403, detail="科室主任账号未配置科室范围 (dept_scope)，无法访问预警看板")
            placeholders = ', '.join(['%s'] * len(auth_dept_ids))
            sql += f" AND (dept_id IN ({placeholders}) OR dept_name IN ({placeholders}))"
            params.extend(list(auth_dept_ids))
        elif dept:
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
            ratio = float(r["fee_ratio"] * Decimal("100")) if r["fee_ratio"] is not None else None
            items.append({
                "zyh": r["visit_id"],
                "dept": r["dept_name"],
                "ward": r["ward_name"],
                "bed": r["bed_no"],
                "doctor_id": r["doctor_id"],
                "disease": r["primary_icd"] or "未明确",
                "ins_type": r["ins_type"],
                "current_cost": float(r["current_fee"]),
                "benchmark": float(r["target_base"]) if r["target_base"] is not None else None,
                "light": light_map.get(level, level),
                "alert_level": level,
                "status": status_desc_map.get(level, level),
                "pending_reason": r.get("pending_reason"),
                "ratio_percent": round(ratio, 2) if ratio is not None else None
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

@app.post("/api/chat", summary="院内本地自然语言意图分类与规则问答接口")
async def chat_interaction(req: ChatRequest, authorization: Optional[str] = Header(None)):
    """
    接收自然语言提问，执行本地纯正则/规则意图分流，零云端大模型依赖，严格保护患者隐私。
    科室级问答保持开放；身份仅取自有效令牌，绝不信任自报工号请求头。
    """
    op = operator_from_optional_token(authorization)
    result = parse_local_intent(req.text, channel=req.channel or "web")
    
    # 严格合规审计日志：严禁截取或写入原始用户文本，只记录结构化意图代码
    intent_code = result.get("intent", "UNKNOWN")
    record_audit_log(
        operator_id=op["sub"],
        role_type=op["role"],
        channel=req.channel or "web",
        action_type="CHAT_INTENT",
        target_ref=f"INTENT:{intent_code}",
        auth_result="PERMITTED"
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
