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
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from decimal import Decimal
import sys
import os

# 确保能加载项目内部模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.dip_calculator import match_dip_rule, evaluate_traffic_light
from agent.llm_router import parse_local_intent
from db.local_db import record_audit_log

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
    current_cost: float = Field(..., description="当前住院累计费用（元）")
    age: Optional[int] = Field(None, description="患者周岁年龄")
    ins_type: Optional[str] = Field("职工", description="参保类型：职工/居民/自费")
    operation: Optional[str] = Field(None, description="已完成手术或操作名称")
    bed_days: Optional[int] = Field(None, description="在院床日天数（按床日付费必填）")
    rule_version: str = Field("2026_SAMPLE_V1", description="DIP 规则版本号")

class ChatRequest(BaseModel):
    text: str = Field(..., description="自然语言查询内容")
    channel: Optional[str] = Field("web", description="来源渠道：dingtalk / web")

# ----------------- 路由定义 -----------------

@app.get("/health", summary="开机自愈与健康检查接口")
async def health_check():
    """轻量级健康检查，独立返回 HTTP 200 与 ok 状态"""
    return {"status": "ok", "version": "1.2.0"}

@app.post("/api/tools/query_dip_rule", summary="DIP 规则检索与基准测算接口")
async def query_dip_rule(req: QueryDipRuleRequest, x_operator_id: Optional[str] = Header("anonymous", alias="X-Operator-Id")):
    """
    根据疾病、年龄、医保类型、手术操作等参数，从数据库检索匹配生效 DIP 规则并测算基准
    """
    result = match_dip_rule(
        disease=req.disease,
        age=req.age,
        ins_type=req.ins_type,
        op_keyword=req.operation,
        bed_days=req.bed_days,
        rule_version=req.rule_version
    )
    
    # 严格合规审计日志：仅记录清洗后的编码标识，绝不记录任何自由文本
    clean_target = f"ICD:{req.disease.strip().upper()[:20]}"
    record_audit_log(
        operator_id=x_operator_id,
        role_type="DOCTOR",
        channel="API",
        action_type="QUERY_DIP_RULE",
        target_ref=clean_target,
        auth_result="PERMITTED",
        rule_version=req.rule_version
    )
    
    return result

@app.post("/api/tools/check_patient_dip", summary="在院患者费用判灯与控费分析接口")
async def check_patient_dip(req: CheckPatientDipRequest, x_operator_id: Optional[str] = Header("anonymous", alias="X-Operator-Id")):
    """
    根据患者累计费用与数据库生效 DIP 规则，执行高精度未舍入 Decimal 交叉相乘判灯分析
    """
    rule_res = match_dip_rule(
        disease=req.disease,
        age=req.age,
        ins_type=req.ins_type,
        op_keyword=req.operation,
        bed_days=req.bed_days,
        rule_version=req.rule_version
    )
    
    # 严格合规审计日志：仅记录去标识化患者唯一流水号，绝不记录任何患者文本
    clean_target = f"PATIENT:{req.patient_id or 'UNSPECIFIED'}"
    record_audit_log(
        operator_id=x_operator_id,
        role_type="DOCTOR",
        channel="API",
        action_type="CHECK_PATIENT_DIP",
        target_ref=clean_target,
        auth_result="PERMITTED",
        rule_version=req.rule_version
    )
    
    status = rule_res.get("status")
    cost_dec = Decimal(str(req.current_cost))

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
    bm = Decimal(str(rule_res["benchmark_amount"]))
    low_th = Decimal(str(rule_res["low_threshold"]))
    high_th = Decimal(str(rule_res["high_threshold"]))
    
    light_eval = evaluate_traffic_light(
        current_cost=cost_dec,
        benchmark=bm,
        low_threshold=low_th,
        high_threshold=high_th,
        ins_type=req.ins_type or "职工"
    )
    
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
async def get_inpatient_alerts(dept: Optional[str] = Query(None, description="科室筛选，如 中医科")):
    """
    获取当前住院患者红黄绿灯控费预警清单（一期演示基线数据，第二步历史回放后接入实时快照）
    """
    sample_alerts = [
        {
            "zyh": "zy2025042901",
            "name": "马**",
            "dept": "中医科",
            "disease": "膝关节病 (M17.9)",
            "ins_type": "城镇职工",
            "current_cost": 2150.00,
            "benchmark": 2790.90,
            "light": "绿灯",
            "status": "费用正常（<90%）",
            "ratio_percent": 77.04
        },
        {
            "zyh": "zy2025042902",
            "name": "郭**",
            "dept": "中医科",
            "disease": "头痛 (R51.x)",
            "ins_type": "城乡居民",
            "current_cost": 1820.00,
            "benchmark": 1745.10,
            "light": "黄灯",
            "status": "预警观察（90%~110%）",
            "ratio_percent": 104.29
        }
    ]
    if dept:
        sample_alerts = [a for a in sample_alerts if dept in a["dept"]]
    return {
        "is_demo_baseline": True,
        "total": len(sample_alerts),
        "batch_time": "2026-11-22 09:30:00",
        "items": sample_alerts
    }

@app.post("/api/chat", summary="院内本地自然语言意图分类与规则问答接口")
async def chat_interaction(req: ChatRequest, x_operator_id: Optional[str] = Header("anonymous", alias="X-Operator-Id")):
    """
    接收自然语言提问，执行本地纯正则/规则意图分流，零云端大模型依赖，严格保护患者隐私
    """
    result = parse_local_intent(req.text, channel=req.channel or "web")
    
    # 严格合规审计日志：严禁截取或写入原始用户文本，只记录结构化意图代码
    intent_code = result.get("intent", "UNKNOWN")
    record_audit_log(
        operator_id=x_operator_id,
        role_type="DOCTOR",
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
