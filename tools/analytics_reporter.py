# -*- coding: utf-8 -*-
"""
tools/analytics_reporter.py — 医生 DIP 控费绩效分析与历史趋势聚合报表模块

严格遵循架构规范：
1. 数据来源：本地 SQL Server `dbo.inpatient_dip_snapshot`（历史扫描批次快照）；
2. 精度规范：全部费用与基准汇总采用 Decimal 运算，最后 quantize 保留 2 位小数；
3. 权限隔离：由调用层传入 `authorized_dept_ids`（科主任）或 `enforced_doctor_id`（医生本人），
   严格在 SQL 查询与内存聚合前执行范围过滤，绝不越权返回其他科室或医生数据。
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Set

from db.local_db import get_local_db_connection


def _to_decimal(val: Any) -> Decimal:
    if val is None:
        return Decimal("0")
    return Decimal(str(val))


def aggregate_doctor_performance(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    按医生维度聚合控费绩效指标。
    对同一 (doctor_id, visit_id) 优先取最新一条快照（按 data_cutoff_time / synced_at 降序），
    防止同一患者跨多批次被重复累计费用。
    """
    latest_by_doc_visit: Dict[tuple, Dict[str, Any]] = {}
    known_doc_names: Dict[str, str] = {}
    for r in rows:
        doc_id = (r.get("doctor_id") or "UNKNOWN").strip()
        cand_name = (r.get("doctor_name") or "").strip()
        if cand_name:
            known_doc_names[doc_id] = cand_name
        visit_id = (r.get("visit_id") or "").strip()
        if not visit_id:
            continue
        key = (doc_id, visit_id)
        ts = r.get("data_cutoff_time") or r.get("synced_at") or datetime.min
        prev = latest_by_doc_visit.get(key)
        if prev is None:
            latest_by_doc_visit[key] = r
        else:
            prev_ts = prev.get("data_cutoff_time") or prev.get("synced_at") or datetime.min
            if ts >= prev_ts:
                latest_by_doc_visit[key] = r

    buckets: Dict[str, Dict[str, Any]] = {}
    for (_, _), r in latest_by_doc_visit.items():
        doc_id = (r.get("doctor_id") or "UNKNOWN").strip()
        doc_name = (r.get("doctor_name") or "").strip() or known_doc_names.get(doc_id, "")
        b = buckets.get(doc_id)
        if b is None:
            b = {
                "doctor_id": doc_id,
                "doctor_name": doc_name,
                "dept_id": r.get("dept_id"),
                "dept_name": r.get("dept_name") or "未知科室",
                "total_cases": 0,
                "GREEN": 0,
                "YELLOW": 0,
                "RED": 0,
                "PENDING": 0,
                "NOT_APPLICABLE": 0,
                "total_fee": Decimal("0"),
                "total_benchmark": Decimal("0"),
                "ratio_sum": Decimal("0"),
                "ratio_count": 0,
            }
            buckets[doc_id] = b
        elif not b["doctor_name"] and doc_name:
            b["doctor_name"] = doc_name

        b["total_cases"] += 1
        lvl = (r.get("alert_level") or "PENDING").upper()
        if lvl in ("GREEN", "YELLOW", "RED", "PENDING", "NOT_APPLICABLE"):
            b[lvl] += 1
        else:
            b["PENDING"] += 1

        b["total_fee"] += _to_decimal(r.get("current_fee"))
        if r.get("target_base") is not None:
            b["total_benchmark"] += _to_decimal(r.get("target_base"))
        if r.get("fee_ratio") is not None:
            b["ratio_sum"] += _to_decimal(r.get("fee_ratio"))
            b["ratio_count"] += 1

    results: List[Dict[str, Any]] = []
    for doc_id, b in buckets.items():
        evaluable = b["GREEN"] + b["YELLOW"] + b["RED"]
        green_rate = (
            (Decimal(b["GREEN"]) * Decimal("100") / Decimal(evaluable)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if evaluable > 0
            else None
        )
        red_rate = (
            (Decimal(b["RED"]) * Decimal("100") / Decimal(evaluable)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if evaluable > 0
            else None
        )
        avg_ratio_pct = (
            (b["ratio_sum"] * Decimal("100") / Decimal(b["ratio_count"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if b["ratio_count"] > 0
            else None
        )
        total_fee_q = b["total_fee"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_bm_q = b["total_benchmark"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        results.append({
            "doctor_id": doc_id,
            "doctor_name": b.get("doctor_name") or None,
            "dept_id": b["dept_id"],
            "dept_name": b["dept_name"],
            "total_cases": b["total_cases"],
            "green_count": b["GREEN"],
            "yellow_count": b["YELLOW"],
            "red_count": b["RED"],
            "pending_count": b["PENDING"],
            "not_applicable_count": b["NOT_APPLICABLE"],
            "green_rate_percent": float(green_rate) if green_rate is not None else None,
            "red_rate_percent": float(red_rate) if red_rate is not None else None,
            "avg_fee_ratio_percent": float(avg_ratio_pct) if avg_ratio_pct is not None else None,
            "total_fee": float(total_fee_q),
            "total_fee_str": f"{total_fee_q:.2f}",
            "total_benchmark": float(total_bm_q),
            "total_benchmark_str": f"{total_bm_q:.2f}",
        })

    results.sort(key=lambda x: (-x["red_count"], -x["total_cases"], x["doctor_id"]))
    return results


def aggregate_historical_trends(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按扫描批次 (batch_id) 聚合历史红黄绿灯趋势与平均费用占比。"""
    batches: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        bid = (r.get("batch_id") or "").strip()
        if not bid:
            continue
        b = batches.get(bid)
        if b is None:
            cutoff = r.get("data_cutoff_time") or r.get("synced_at")
            b = {
                "batch_id": bid,
                "cutoff_time": cutoff,
                "total": 0,
                "GREEN": 0,
                "YELLOW": 0,
                "RED": 0,
                "PENDING": 0,
                "NOT_APPLICABLE": 0,
                "ratio_sum": Decimal("0"),
                "ratio_count": 0,
            }
            batches[bid] = b

        b["total"] += 1
        lvl = (r.get("alert_level") or "PENDING").upper()
        if lvl in ("GREEN", "YELLOW", "RED", "PENDING", "NOT_APPLICABLE"):
            b[lvl] += 1
        else:
            b["PENDING"] += 1
        if r.get("fee_ratio") is not None:
            b["ratio_sum"] += _to_decimal(r.get("fee_ratio"))
            b["ratio_count"] += 1

    trends: List[Dict[str, Any]] = []
    for bid, b in batches.items():
        cutoff = b["cutoff_time"]
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S") if hasattr(cutoff, "strftime") else str(cutoff or "")
        avg_ratio = (
            (b["ratio_sum"] * Decimal("100") / Decimal(b["ratio_count"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if b["ratio_count"] > 0
            else None
        )
        trends.append({
            "batch_id": bid,
            "cutoff_time": cutoff_str,
            "total": b["total"],
            "green_count": b["GREEN"],
            "yellow_count": b["YELLOW"],
            "red_count": b["RED"],
            "pending_count": b["PENDING"],
            "not_applicable_count": b["NOT_APPLICABLE"],
            "avg_fee_ratio_percent": float(avg_ratio) if avg_ratio is not None else None,
        })

    trends.sort(key=lambda x: (x["cutoff_time"], x["batch_id"]))
    return trends


def get_doctor_performance_report(
    days: int = 30,
    dept_id: Optional[str] = None,
    doctor_id: Optional[str] = None,
    authorized_dept_ids: Optional[Set[str]] = None,
    enforced_doctor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """查询指定时间窗口的快照记录并生成医生绩效与历史趋势聚合报表。"""
    days_clamped = max(1, min(int(days), 365))
    since_dt = datetime.now() - timedelta(days=days_clamped)

    cursor, conn = get_local_db_connection(as_dict=True)
    try:
        cursor.execute("SELECT COL_LENGTH('dbo.inpatient_dip_snapshot', 'doctor_name') AS has_dn")
        dn_row = cursor.fetchone() or {}
        doc_name_col = ", doctor_name" if dn_row.get("has_dn") is not None else ""
        sql = f"""
        SELECT batch_id, visit_id, dept_id, dept_name, doctor_id{doc_name_col},
               current_fee, target_base, fee_ratio, alert_level,
               data_cutoff_time, synced_at
        FROM dbo.inpatient_dip_snapshot
        WHERE data_cutoff_time >= %s
        """
        params: List[Any] = [since_dt]

        if authorized_dept_ids is not None:
            dept_list = sorted(authorized_dept_ids)
            if not dept_list:
                return {"days": days_clamped, "total_snapshots": 0, "doctors": [], "trends": []}
            placeholders = ", ".join(["%s"] * len(dept_list))
            sql += f" AND dept_id IN ({placeholders})"
            params.extend(dept_list)
        elif dept_id:
            sql += " AND dept_id = %s"
            params.append(dept_id.strip())

        target_doc = enforced_doctor_id or doctor_id
        if target_doc:
            sql += " AND doctor_id = %s"
            params.append(target_doc.strip())

        sql += " ORDER BY data_cutoff_time ASC"
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
    finally:
        conn.close()

    return {
        "days": days_clamped,
        "total_snapshots": len(rows),
        "doctors": aggregate_doctor_performance(rows),
        "trends": aggregate_historical_trends(rows),
    }
