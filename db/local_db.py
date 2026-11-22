# -*- coding: utf-8 -*-
"""
本地 SQL Server 2022 (his_agent_db) 统一数据库操作封装模块
严格按照《开发设计文档 v1.2》架构规范实现，从 .env 读取连接参数，统一管理连接与事务
支持容器内与宿主机环境无缝切换，提供规则库检索、在院快照保存与合规审计日志记录
"""
import os
import pymssql
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from dotenv import load_dotenv

# 加载 .env 环境变量
dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

LOCAL_DB_HOST = os.getenv("LOCAL_DB_HOST", "sqlserver")
LOCAL_DB_PORT = int(os.getenv("LOCAL_DB_PORT", "1433"))
LOCAL_DB_USER = os.getenv("LOCAL_DB_USER", "sa")
LOCAL_DB_PASS = os.getenv("LOCAL_DB_PASS")
if not LOCAL_DB_PASS:
    raise ValueError("LOCAL_DB_PASS environment variable must be explicitly configured and non-empty.")
LOCAL_DB_NAME = os.getenv("LOCAL_DB_NAME", "his_agent_db")

def get_local_db_connection(as_dict: bool = True) -> Tuple[Any, pymssql.Connection]:
    """
    获取 his_agent_db 数据库连接。
    具备自适应宿主机与容器环境能力：优先尝试环境变量配置的 HOST，若连接失败则自动尝试回退地址
    """
    candidates = [LOCAL_DB_HOST]
    for fallback in ["127.0.0.1", "localhost", "sqlserver"]:
        if fallback not in candidates:
            candidates.append(fallback)
            
    last_err = None
    for h in candidates:
        try:
            conn = pymssql.connect(
                server=h,
                port=LOCAL_DB_PORT,
                user=LOCAL_DB_USER,
                password=LOCAL_DB_PASS,
                database=LOCAL_DB_NAME,
                charset="utf8",
                timeout=5,
                login_timeout=5
            )
            return conn.cursor(as_dict=as_dict), conn
        except Exception as e:
            last_err = e
            continue
            
    raise ConnectionError(f"无法连接到本地数据库 his_agent_db (尝试地址: {candidates}): {last_err}")

def query_approved_dip_rules(
    rule_version: str = "2026_SAMPLE_V1",
    icd_code: Optional[str] = None,
    department: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    从 dbo.dip_rules 查询已审核 (APPROVED) 的生效规则。
    不使用任何写死参数，完全读取库中字段与动态配置。
    """
    cursor, conn = get_local_db_connection(as_dict=True)
    try:
        sql = """
            SELECT 
                rule_id, region_code, rule_version, effective_date, expiry_date,
                source_doc, review_status, department, dip_group_name,
                icd_code, icd_name, op_code, op_name, catalog_type,
                aux_flag, aux_detail, billing_unit, dip_score,
                resident_point, employee_point, hosp_coef,
                low_threshold, high_threshold, rounding_mode,
                resident_base, resident_min, resident_max,
                employee_base, employee_min, employee_max
            FROM dbo.dip_rules
            WHERE review_status = 'APPROVED'
              AND rule_version = %s
        """
        params = [rule_version]
        
        if icd_code:
            sql += " AND (icd_code = %s OR icd_code LIKE %s OR dip_group_name LIKE %s)"
            params.extend([icd_code, f"{icd_code}%", f"%{icd_code}%"])
            
        if department:
            sql += " AND department = %s"
            params.append(department)
            
        sql += " ORDER BY rule_id ASC"
        
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        return rows
    finally:
        conn.close()

def record_audit_log(
    operator_id: str,
    role_type: str,
    channel: str,
    action_type: str,
    target_ref: Optional[str] = None,
    auth_result: str = "PERMITTED",
    rule_version: Optional[str] = None,
    batch_id: Optional[str] = None
):
    """记录 Agent 审计日志到 dbo.agent_audit_log（绝不保存患者敏感明细与自由文本）"""
    cursor, conn = get_local_db_connection(as_dict=False)
    try:
        sql = """
            INSERT INTO dbo.agent_audit_log (
                operator_id, role_type, channel, action_type,
                target_ref, auth_result, rule_version, batch_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(sql, (
            operator_id, role_type, channel, action_type,
            target_ref, auth_result, rule_version, batch_id
        ))
        conn.commit()
    except Exception as e:
        print(f"[WARN] 记录审计日志失败: {e}")
    finally:
        conn.close()
