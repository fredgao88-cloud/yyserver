# -*- coding: utf-8 -*-
"""
agent/tool_dispatcher.py — 本地自然语言 Function Calling 工具调度闭环模块

实现零云端依赖的自然语言 -> 结构化工具调用 -> RBAC 鉴权校验 -> 业务执行 -> 摘要回复闭环：
支持工具：
1. check_patient_dip       — 在院患者费用判灯（需登录 + 管床/科室穿透鉴权）
2. get_inpatient_alerts    — 在院科室预警看板（需登录 + ALERTS_ROLES + 科室 ID 隔离）
3. get_doctor_performance  — 医生绩效分析与趋势报表（需登录 + 行级隔离）
"""
from decimal import Decimal
import re
from typing import Any, Dict, Optional

from agent.llm_router import extract_operation_keyword
from auth.rbac import (
    ALERTS_ROLES,
    PATIENT_QUERY_ROLES,
    ROLE_DEPT_HEAD,
    ROLE_DOCTOR,
    get_authorized_dept_ids,
    get_operator,
    role_permitted,
)
from db.his_reader import HISReader
from db.local_db import get_local_db_connection
from tools.analytics_reporter import get_doctor_performance_report
from tools.dip_calculator import (
    evaluate_traffic_light,
    match_dip_rule,
    normalize_insurance_type,
)


def extract_tool_call(text: str, channel: str = "web") -> Optional[Dict[str, Any]]:
    """
    从自然语言文本中提取可执行业务工具及参数（不含患者查询隐私拦截前的原始判断，
    调用方应先保证钉钉隐私盾牌已前置拦截）。
    未命中明确业务工具信号时返回 None，交由原有 parse_local_intent 处理规则问答。
    """
    text_clean = (text or "").strip()
    if not text_clean:
        return None

    # 1. 医生绩效分析 / 历史趋势报表
    if re.search(r'(绩效|控费趋势|达标率|绿灯率|红灯率|医生.*报表|历史趋势)', text_clean):
        days_m = re.search(r'(?:最近|近)\s*(\d+)\s*天', text_clean)
        days = int(days_m.group(1)) if days_m else 30
        doc_m = re.search(r'(?:工号|医生)\s*[:：]?\s*([A-Za-z0-9_-]{2,20})', text_clean)
        dept_m = re.search(r'科室\s*[:：]?\s*([A-Za-z0-9\u4e00-\u9fa5_-]{2,20})', text_clean)
        return {
            "name": "get_doctor_performance",
            "arguments": {
                "days": max(1, min(days, 365)),
                "doctor_id": doc_m.group(1) if doc_m else None,
                "dept_id": dept_m.group(1) if dept_m else None,
            },
        }

    # 2. 在院预警看板查询
    if re.search(r'(预警看板|预警列表|在院预警|红灯.*列表|红黄灯.*清单|超标.*患者列表)', text_clean):
        dept_m = re.search(r'(?:查看|查询|获取|看下|看一下|请|在|的|^)\s*([\u4e00-\u9fa5]{2,8}科)(?:的|在院|预警|$)', text_clean)
        dept_val = dept_m.group(1) if dept_m else None
        if dept_val:
            dept_val = re.sub(r'^(?:请|帮我|查看|查询|获取|看下|看一下)+', '', dept_val)
        return {
            "name": "get_inpatient_alerts",
            "arguments": {
                "dept": dept_val or None,
            },
        }

    # 3. 患者费用判灯核查（必须同时包含金额信号 + 住院号/判灯动词，避免干扰纯规则问答）
    cost_m = re.search(r'(?:费用|花销|花费|累计|金额)\s*[为是：:]?\s*(-?\d+(?:\.\d+)?)\s*元?', text_clean)
    if not cost_m:
        cost_m = re.search(r'(-?\d+(?:\.\d+)?)\s*元', text_clean)
    pid_m = re.search(r'\b(zy\d{4,20}|VISIT_[A-Za-z0-9_-]+)\b', text_clean, re.I)
    has_light_intent = bool(re.search(r'(判灯|控费分析|是否超标|超标了吗|红黄绿灯)', text_clean))

    if cost_m and (pid_m or has_light_intent):
        icd_m = re.search(r'\b([A-Z]\d{2}(?:\.\d{1,2})?)\b', text_clean, re.I)
        age_m = re.search(r'(\d+)\s*岁', text_clean)
        bed_m = re.search(r'(\d+)\s*(?:天|床日)', text_clean)
        norm_ins = normalize_insurance_type(text_clean)
        ins_type = norm_ins.get("category") if norm_ins.get("valid") else None

        op_kw, op_status, _ = extract_operation_keyword(text_clean)

        return {
            "name": "check_patient_dip",
            "arguments": {
                "patient_id": pid_m.group(1) if pid_m else None,
                "disease": icd_m.group(1).upper() if icd_m else text_clean,
                "current_cost": cost_m.group(1),
                "age": int(age_m.group(1)) if age_m else None,
                "ins_type": ins_type,
                "operation": op_kw,
                "operation_status": op_status,
                "bed_days": int(bed_m.group(1)) if bed_m else None,
            },
        }

    return None


def dispatch_tool_call(tool_call: Dict[str, Any], operator: Dict[str, Any]) -> Dict[str, Any]:
    """
    在服务端严格鉴权前提下执行工具调用，并返回标准 Function Calling 闭环响应。
    """
    tool_name = tool_call.get("name")
    args = tool_call.get("arguments") or {}

    if not operator.get("authenticated"):
        return {
            "intent": "tool_call_denied",
            "tool_executed": False,
            "tool_call": tool_call,
            "reply": "【需要登录认证】该操作涉及院内受保护业务数据，请先通过 /api/auth/login 登录并在请求头携带有效的 Bearer 令牌后再执行。",
            "data": {"status": "AUTH_REQUIRED"},
        }

    role = operator.get("role", "")
    sub = operator.get("sub", "")

    if tool_name == "get_doctor_performance":
        if not role_permitted(role, PATIENT_QUERY_ROLES):
            return {
                "intent": "tool_call_denied",
                "tool_executed": False,
                "tool_call": tool_call,
                "reply": "【权限不足】当前角色无权访问医生绩效报表。",
                "data": {"status": "FORBIDDEN"},
            }
        authorized_dept_ids = None
        enforced_doctor_id = None
        req_doc = args.get("doctor_id")
        req_dept = args.get("dept_id")

        if role == ROLE_DOCTOR:
            op_rec = get_operator(sub)
            if not op_rec or op_rec.get("status") != "ACTIVE":
                return {
                    "intent": "tool_call_denied",
                    "tool_executed": False,
                    "tool_call": tool_call,
                    "reply": "【越权访问】操作员账号状态异常。",
                    "data": {"status": "FORBIDDEN"},
                }
            enforced_doctor_id = (op_rec.get("doctor_id") or op_rec.get("operator_id") or "").strip()
            if req_doc and req_doc != enforced_doctor_id:
                return {
                    "intent": "tool_call_denied",
                    "tool_executed": False,
                    "tool_call": tool_call,
                    "reply": "【越权访问】医生仅可查看本人名下绩效报表。",
                    "data": {"status": "FORBIDDEN"},
                }
        elif role == ROLE_DEPT_HEAD:
            authorized_dept_ids = get_authorized_dept_ids(operator.get("dept") or "")
            if not authorized_dept_ids:
                return {
                    "intent": "tool_call_denied",
                    "tool_executed": False,
                    "tool_call": tool_call,
                    "reply": "【越权访问】科室主任账号未配置有效科室 ID 或存在同名歧义。",
                    "data": {"status": "FORBIDDEN"},
                }

        report = get_doctor_performance_report(
            days=int(args.get("days") or 30),
            dept_id=req_dept,
            doctor_id=req_doc,
            authorized_dept_ids=authorized_dept_ids,
            enforced_doctor_id=enforced_doctor_id,
        )
        doc_count = len(report.get("doctors") or [])
        reply = f"【医生绩效与趋势报表】近 {report['days']} 天共检索到 {report['total_snapshots']} 条快照记录，涵盖 {doc_count} 名医生。"
        return {
            "intent": "tool_call_result",
            "tool_executed": True,
            "tool_call": tool_call,
            "reply": reply,
            "data": report,
        }

    if tool_name == "get_inpatient_alerts":
        if not role_permitted(role, ALERTS_ROLES):
            return {
                "intent": "tool_call_denied",
                "tool_executed": False,
                "tool_call": tool_call,
                "reply": "【权限不足】在院预警看板仅限科室主任、医保办及管理员访问。",
                "data": {"status": "FORBIDDEN"},
            }
        cursor, conn = get_local_db_connection(as_dict=True)
        try:
            cursor.execute("SELECT TOP 1 batch_id, data_cutoff_time FROM dbo.inpatient_dip_snapshot ORDER BY synced_at DESC")
            latest = cursor.fetchone()
            if not latest:
                return {
                    "intent": "tool_call_result",
                    "tool_executed": True,
                    "tool_call": tool_call,
                    "reply": "【在院预警看板】当前暂无已生成的在院快照批次。",
                    "data": {"total": 0, "items": []},
                }
            sql = "SELECT visit_id, dept_id, dept_name, bed_no, alert_level, current_fee, target_base FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s"
            params = [latest["batch_id"]]
            if role == ROLE_DEPT_HEAD:
                auth_dept_ids = get_authorized_dept_ids(operator.get("dept") or "")
                if not auth_dept_ids:
                    return {
                        "intent": "tool_call_denied",
                        "tool_executed": False,
                        "tool_call": tool_call,
                        "reply": "【越权访问】科室主任账号未配置有效科室 ID。",
                        "data": {"status": "FORBIDDEN"},
                    }
                dept_list = sorted(auth_dept_ids)
                placeholders = ", ".join(["%s"] * len(dept_list))
                sql += f" AND dept_id IN ({placeholders})"
                params.extend(dept_list)
            elif args.get("dept"):
                sql += " AND (dept_id = %s OR dept_name = %s)"
                params.extend([args["dept"], args["dept"]])
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
        finally:
            conn.close()

        red_c = sum(1 for r in rows if r.get("alert_level") == "RED")
        yellow_c = sum(1 for r in rows if r.get("alert_level") == "YELLOW")
        return {
            "intent": "tool_call_result",
            "tool_executed": True,
            "tool_call": tool_call,
            "reply": f"【在院预警看板】批次 `{latest['batch_id']}` 共 {len(rows)} 例记录，其中红灯 {red_c} 例，黄灯 {yellow_c} 例。",
            "data": {"batch_id": latest["batch_id"], "total": len(rows), "red_count": red_c, "yellow_count": yellow_c},
        }

    if tool_name == "check_patient_dip":
        if not role_permitted(role, PATIENT_QUERY_ROLES):
            return {
                "intent": "tool_call_denied",
                "tool_executed": False,
                "tool_call": tool_call,
                "reply": "【权限不足】当前角色无权执行患者判灯核查。",
                "data": {"status": "FORBIDDEN"},
            }
        pid = args.get("patient_id")
        if role in (ROLE_DOCTOR, ROLE_DEPT_HEAD) and pid:
            reader = HISReader()
            adm = reader.get_patient_realtime_admission(pid)
            if not adm:
                return {
                    "intent": "tool_call_denied",
                    "tool_executed": False,
                    "tool_call": tool_call,
                    "reply": "【越权访问】管床鉴权未通过。",
                    "data": {"status": "FORBIDDEN"},
                }
            if role == ROLE_DOCTOR:
                op_rec = get_operator(sub)
                mapped = (op_rec.get("doctor_id") or "").strip() if op_rec and op_rec.get("status") == "ACTIVE" else ""
                valid_ids = {mapped} if mapped else ({(op_rec.get("operator_id") or "").strip()} if op_rec and op_rec.get("status") == "ACTIVE" else set())
                if not ((adm.get("attending_doctor_id") or "").strip() in valid_ids):
                    return {
                        "intent": "tool_call_denied",
                        "tool_executed": False,
                        "tool_call": tool_call,
                        "reply": "【越权访问】管床鉴权未通过。",
                        "data": {"status": "FORBIDDEN"},
                    }
            elif role == ROLE_DEPT_HEAD:
                auth_ids = get_authorized_dept_ids(operator.get("dept") or "")
                if not (str(adm.get("current_dept_id") or "").strip() in auth_ids):
                    return {
                        "intent": "tool_call_denied",
                        "tool_executed": False,
                        "tool_call": tool_call,
                        "reply": "【越权访问】管床鉴权未通过。",
                        "data": {"status": "FORBIDDEN"},
                    }

        cost_dec = Decimal(str(args.get("current_cost") or "0"))
        if cost_dec < Decimal("0"):
            return {
                "intent": "tool_call_result",
                "tool_executed": True,
                "tool_call": tool_call,
                "reply": "【费用异常待核查】当前费用为负数，暂停判灯并进入待定核查（NEGATIVE_FEE_UNRECONCILED）。",
                "data": {"status": "PENDING", "pending_reason": "NEGATIVE_FEE_UNRECONCILED"},
            }
        rule_res = match_dip_rule(
            disease=args.get("disease") or "",
            age=args.get("age"),
            ins_type=args.get("ins_type"),
            op_keyword=args.get("operation"),
            bed_days=args.get("bed_days"),
        )
        if not rule_res.get("matched"):
            return {
                "intent": "tool_call_result",
                "tool_executed": True,
                "tool_call": tool_call,
                "reply": f"【判灯待定】{rule_res.get('message', '未能唯一定位 DIP 规则')}",
                "data": rule_res,
            }
        bm = Decimal(str(rule_res["benchmark_amount"]))
        light_eval = evaluate_traffic_light(
            current_cost=cost_dec,
            benchmark=bm,
            low_threshold=Decimal(str(rule_res["low_threshold"])),
            high_threshold=Decimal(str(rule_res["high_threshold"])),
            ins_type=rule_res.get("ins_type") or "职工",
        )
        return {
            "intent": "tool_call_result",
            "tool_executed": True,
            "tool_call": tool_call,
            "reply": f"【患者判灯完成】分组：{rule_res['group_name']} | 当前费用：￥{cost_dec:.2f} | 基准：￥{bm:.2f} | 灯号：{light_eval.get('light')}（{light_eval.get('ratio_percent')}%）",
            "data": {"rule": rule_res, "evaluation": light_eval},
        }

    return {
        "intent": "unknown",
        "tool_executed": False,
        "reply": f"不支持的工具调用: {tool_name}",
    }
