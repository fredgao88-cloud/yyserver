# -*- coding: utf-8 -*-
"""
tools/reconciliation_reporter.py — 出院决算账实自动对账引擎

复用 HISReader.get_settled_cases 与 HISReader.get_case_fee_detail（SERIALIZABLE 事务隔离级），
执行高精度 Decimal 核对，并明确区分抽样数、成功核对数与未核对数（RECON-COVER-001）。
输出严格脱敏（不含患者姓名等明文身份信息）。
"""
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any, Dict, List, Optional

from db.his_reader import HISReader

logger = logging.getLogger("hospital_agent.reconciliation")

_TOLERANCE = Decimal("0.01")


def _dec2(val: Any) -> Decimal:
    return Decimal(str(val if val is not None else 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def evaluate_case_reconciliation(
    detail: Dict[str, Any],
    case_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """基于 get_case_fee_detail 返回结构执行单一住院流水的 Decimal 账实核对。"""
    meta = case_meta or {}
    zyjz = detail.get("zyjz") or {}
    cz_stat = detail.get("cz_stat") or {}
    fy_stat = detail.get("fy_stat") or {}

    amoney = _dec2(zyjz.get("amoney"))
    cz_expected = _dec2(zyjz.get("czmoney")) + _dec2(zyjz.get("yjmoney"))
    yf_expected = _dec2(zyjz.get("yfmoney"))

    category_sum = _dec2(detail.get("category_sum"))
    cz_actual = _dec2(cz_stat.get("total"))
    fy_actual = _dec2(fy_stat.get("total"))
    unfy_amount = _dec2(fy_stat.get("undispensed"))
    unfy_count = int(fy_stat.get("undispensed_count") or 0)

    cat_diff = (amoney - category_sum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cz_diff = (cz_expected - cz_actual).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    yf_diff = (yf_expected - (fy_actual + unfy_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    has_diff = (abs(cat_diff) > _TOLERANCE) or (abs(cz_diff) > _TOLERANCE) or (abs(yf_diff) > _TOLERANCE)
    if has_diff:
        recon_status = "MISMATCH"
    elif unfy_count > 0:
        recon_status = "UNDISPENSED_ONLY"
    else:
        recon_status = "MATCHED"

    st_time = meta.get("settlement_time") or zyjz.get("jzrq")
    st_time_str = st_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(st_time, "strftime") else str(st_time or "-")

    return {
        "visit_id": str(zyjz.get("lsh") or meta.get("lsh") or ""),
        "settlement_id": str(zyjz.get("id") or meta.get("settlement_id") or ""),
        "dept_name": str(meta.get("discharge_dept") or meta.get("admission_dept") or "未知科室"),
        "settlement_time": st_time_str,
        "reconciliation_scope": detail.get("reconciliation_scope", "SINGLE_SETTLEMENT"),
        "settlement_count": int(detail.get("settlement_count") or 1),
        "recon_status": recon_status,
        "total_amount": float(amoney),
        "total_amount_str": f"{amoney:.2f}",
        "category_sum": float(category_sum),
        "category_diff": float(cat_diff),
        "category_diff_str": f"{cat_diff:.2f}",
        "clinical_diff": float(cz_diff),
        "clinical_diff_str": f"{cz_diff:.2f}",
        "pharmacy_diff": float(yf_diff),
        "pharmacy_diff_str": f"{yf_diff:.2f}",
        "undispensed_count": unfy_count,
        "undispensed_amount": float(unfy_amount),
        "undispensed_amount_str": f"{unfy_amount:.2f}",
    }


def run_settled_reconciliation_report(
    limit: int = 30,
    department: Optional[str] = None,
    reader: Optional[HISReader] = None,
) -> Dict[str, Any]:
    """批量对近期已结算病例执行对账并汇总差异统计（含未核对覆盖率统计）。"""
    his = reader or HISReader()
    limit_clamped = max(1, min(int(limit), 100))
    cases = his.get_settled_cases(limit=limit_clamped, department=department)

    seen_lsh = set()
    items: List[Dict[str, Any]] = []
    unreconciled_reasons: List[Dict[str, str]] = []
    counts = {"MATCHED": 0, "UNDISPENSED_ONLY": 0, "MISMATCH": 0, "ERROR": 0}
    sampled_count = 0

    for c in cases:
        lsh = str(c.get("lsh") or "").strip()
        if not lsh or lsh in seen_lsh:
            continue
        seen_lsh.add(lsh)
        sampled_count += 1
        try:
            detail = his.get_case_fee_detail(lsh, settlement_id=str(c.get("settlement_id") or ""))
            if not detail:
                unreconciled_reasons.append({"visit_id": lsh, "reason": "EMPTY_DETAIL"})
                continue
            row = evaluate_case_reconciliation(detail, case_meta=c)
            items.append(row)
            counts[row["recon_status"]] += 1
        except Exception as exc:
            err_code = f"READ_ERROR:{type(exc).__name__}"
            logger.warning("EVENT=RECON_CASE_READ_FAILED err_type=%s", type(exc).__name__)
            counts["ERROR"] += 1
            unreconciled_reasons.append({"visit_id": lsh, "reason": err_code})

    return {
        "sampled_count": sampled_count,
        "reconciled_count": len(items),
        "unreconciled_count": len(unreconciled_reasons),
        "unreconciled_reasons": unreconciled_reasons,
        "total": len(items),
        "matched_count": counts["MATCHED"],
        "undispensed_only_count": counts["UNDISPENSED_ONLY"],
        "mismatch_count": counts["MISMATCH"],
        "error_count": counts["ERROR"],
        "items": items,
    }
