# -*- coding: utf-8 -*-
"""
tools/dip_importer.py — DIP 规则新版目录批量导入工具

支持 CSV 与 Excel (.xlsx) 批量解析、字段合规校验、高精度 Decimal 基准自动推算、
dry-run 预检以及单事务幂等入库。
"""
import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from db.local_db import get_local_db_connection

# 中英文字段别名映射，兼容院内常见导出表头
HEADER_ALIASES = {
    "科室": "department",
    "department": "department",
    "病种分组": "dip_group_name",
    "分组名称": "dip_group_name",
    "dip_group_name": "dip_group_name",
    "icd编码": "icd_code",
    "主要诊断编码": "icd_code",
    "icd_code": "icd_code",
    "icd名称": "icd_name",
    "诊断名称": "icd_name",
    "icd_name": "icd_name",
    "手术编码": "op_code",
    "op_code": "op_code",
    "手术名称": "op_name",
    "操作名称": "op_name",
    "op_name": "op_name",
    "目录类型": "catalog_type",
    "catalog_type": "catalog_type",
    "辅助目录": "aux_flag",
    "aux_flag": "aux_flag",
    "辅助说明": "aux_detail",
    "aux_detail": "aux_detail",
    "计费方式": "billing_unit",
    "billing_unit": "billing_unit",
    "分值": "dip_score",
    "dip分值": "dip_score",
    "dip_score": "dip_score",
    "居民点值": "resident_point",
    "resident_point": "resident_point",
    "职工点值": "employee_point",
    "employee_point": "employee_point",
    "医院系数": "hosp_coef",
    "hosp_coef": "hosp_coef",
    "低倍阈值": "low_threshold",
    "low_threshold": "low_threshold",
    "高倍阈值": "high_threshold",
    "high_threshold": "high_threshold",
    "region_code": "region_code",
}


def _to_dec(val: Any, default: Optional[Decimal] = None) -> Decimal:
    if val is None or str(val).strip() == "":
        if default is not None:
            return default
        raise ValueError("数值字段不能为空")
    try:
        dec = Decimal(str(val).strip())
    except InvalidOperation as exc:
        raise ValueError(f"非法数值: {val}") from exc
    if not dec.is_finite():
        raise ValueError(f"数值必须为有限实数: {val}")
    return dec


def _quant2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_rule_row(
    raw: Dict[str, Any],
    rule_version: str,
    effective_date: str,
    source_doc: str,
    review_status: str = "PENDING",
    region_code: str = "LOCAL_SAMPLE",
) -> Dict[str, Any]:
    """将单行原始字典标准化并校验所有 DIP 规则必需字段与 Decimal 基准。"""
    mapped: Dict[str, Any] = {}
    for k, v in raw.items():
        if k is None:
            continue
        norm_k = HEADER_ALIASES.get(str(k).strip().lower(), str(k).strip())
        mapped[norm_k] = v

    department = str(mapped.get("department") or "").strip()
    dip_group_name = str(mapped.get("dip_group_name") or "").strip()
    icd_code = str(mapped.get("icd_code") or "").strip().upper()
    if not department or not dip_group_name or not icd_code:
        raise ValueError("缺少必填字段: department / dip_group_name / icd_code")

    dip_score = _to_dec(mapped.get("dip_score"))
    if dip_score <= Decimal("0"):
        raise ValueError(f"dip_score 必须大于 0，当前为 {dip_score}")

    res_pt = _to_dec(mapped.get("resident_point"), Decimal("7.0000"))
    emp_pt = _to_dec(mapped.get("employee_point"), Decimal("7.5000"))
    coef = _to_dec(mapped.get("hosp_coef"), Decimal("0.6000"))
    low_th = _to_dec(mapped.get("low_threshold"), Decimal("0.9000"))
    high_th = _to_dec(mapped.get("high_threshold"), Decimal("1.1000"))

    if res_pt <= Decimal("0") or emp_pt <= Decimal("0"):
        raise ValueError(f"点值必须大于 0: resident_point={res_pt}, employee_point={emp_pt}")
    if coef <= Decimal("0") or coef > Decimal("10"):
        raise ValueError(f"医院系数范围非法 (0 < coef <= 10): hosp_coef={coef}")
    if low_th <= Decimal("0") or high_th <= low_th or high_th > Decimal("10"):
        raise ValueError(f"阈值范围非法: low={low_th}, high={high_th}")

    res_base = _quant2(dip_score * res_pt * coef)
    emp_base = _quant2(dip_score * emp_pt * coef)

    billing_unit = str(mapped.get("billing_unit") or "PER_CASE").strip().upper()
    if billing_unit in ("按床日", "床日"):
        billing_unit = "PER_BED_DAY"
    elif billing_unit in ("按病种", "按例"):
        billing_unit = "PER_CASE"
    if billing_unit not in ("PER_CASE", "PER_BED_DAY"):
        raise ValueError(f"不支持的 billing_unit: {billing_unit}")

    aux_raw = str(mapped.get("aux_flag") or "0").strip()
    aux_flag = 1 if aux_raw in ("1", "true", "True", "是") else 0

    return {
        "region_code": str(mapped.get("region_code") or region_code).strip(),
        "rule_version": rule_version.strip(),
        "effective_date": effective_date,
        "expiry_date": None,
        "source_doc": source_doc.strip(),
        "review_status": review_status,
        "department": department,
        "dip_group_name": dip_group_name,
        "icd_code": icd_code,
        "icd_name": str(mapped.get("icd_name") or dip_group_name).strip(),
        "op_code": str(mapped.get("op_code") or "").strip(),
        "op_name": str(mapped.get("op_name") or "").strip(),
        "catalog_type": str(mapped.get("catalog_type") or "CORE").strip(),
        "aux_flag": aux_flag,
        "aux_detail": str(mapped.get("aux_detail") or "").strip(),
        "billing_unit": billing_unit,
        "dip_score": dip_score,
        "resident_point": res_pt,
        "employee_point": emp_pt,
        "hosp_coef": coef,
        "low_threshold": low_th,
        "high_threshold": high_th,
        "rounding_mode": "HALF_UP_2DP",
        "resident_base": res_base,
        "resident_min": _quant2(res_base * low_th),
        "resident_max": _quant2(res_base * high_th),
        "employee_base": emp_base,
        "employee_min": _quant2(emp_base * low_th),
        "employee_max": _quant2(emp_base * high_th),
    }


def parse_csv_bytes(raw_bytes: bytes) -> List[Dict[str, Any]]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别 CSV 文件编码，请使用 UTF-8 或 GBK 编码")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def parse_xlsx_bytes(raw_bytes: bytes) -> List[Dict[str, Any]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = next(rows_iter, None)
    if not headers:
        return []
    clean_headers = [str(h).strip() if h is not None else "" for h in headers]
    out: List[Dict[str, Any]] = []
    for row in rows_iter:
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        out.append({clean_headers[i]: row[i] for i in range(min(len(clean_headers), len(row)))})
    wb.close()
    return out


def import_dip_rules_records(
    raw_records: List[Dict[str, Any]],
    rule_version: str,
    source_doc: str,
    effective_date: Optional[str] = None,
    review_status: str = "PENDING",
    replace_version: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    校验并批量导入 DIP 规则列表。
    任一行校验失败即中止且不写入数据库；数据库写入采用单事务，失败自动回滚。
    """
    if not rule_version or not source_doc:
        raise ValueError("rule_version 与 source_doc 不能为空")
    if not raw_records:
        return {
            "success": False,
            "dry_run": dry_run,
            "total_rows": 0,
            "valid_rows": 0,
            "inserted_rows": 0,
            "errors": [{"row": 0, "error": "RULE-IMPORT-001: 导入记录集不能为空，拒绝执行空批次导入或版本覆盖"}],
        }
    eff_date = effective_date or date.today().isoformat()
    if review_status not in ("PENDING", "APPROVED", "ARCHIVED"):
        raise ValueError(f"非法审核状态: {review_status}")

    normalized: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for idx, raw in enumerate(raw_records, start=1):
        try:
            normalized.append(
                normalize_rule_row(
                    raw,
                    rule_version=rule_version,
                    effective_date=eff_date,
                    source_doc=source_doc,
                    review_status=review_status,
                )
            )
        except Exception as exc:
            errors.append({"row": idx, "error": str(exc)})

    if errors:
        return {
            "success": False,
            "dry_run": dry_run,
            "total_rows": len(raw_records),
            "valid_rows": len(normalized),
            "inserted_rows": 0,
            "errors": errors,
        }

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "rule_version": rule_version,
            "total_rows": len(raw_records),
            "valid_rows": len(normalized),
            "inserted_rows": 0,
            "errors": [],
        }

    cursor, conn = get_local_db_connection(as_dict=False)
    try:
        if replace_version:
            cursor.execute("DELETE FROM dbo.dip_rules WHERE rule_version = %s", (rule_version,))

        insert_sql = """
        INSERT INTO dbo.dip_rules (
            region_code, rule_version, effective_date, expiry_date, source_doc, review_status,
            department, dip_group_name, icd_code, icd_name, op_code, op_name, catalog_type,
            aux_flag, aux_detail, billing_unit, dip_score, resident_point, employee_point,
            hosp_coef, low_threshold, high_threshold, rounding_mode,
            resident_base, resident_min, resident_max, employee_base, employee_min, employee_max
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
        """
        for item in normalized:
            cursor.execute(insert_sql, (
                item["region_code"], item["rule_version"], item["effective_date"], item["expiry_date"],
                item["source_doc"], item["review_status"], item["department"], item["dip_group_name"],
                item["icd_code"], item["icd_name"], item["op_code"], item["op_name"], item["catalog_type"],
                item["aux_flag"], item["aux_detail"], item["billing_unit"], item["dip_score"],
                item["resident_point"], item["employee_point"], item["hosp_coef"],
                item["low_threshold"], item["high_threshold"], item["rounding_mode"],
                item["resident_base"], item["resident_min"], item["resident_max"],
                item["employee_base"], item["employee_min"], item["employee_max"],
            ))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    return {
        "success": True,
        "dry_run": False,
        "rule_version": rule_version,
        "review_status": review_status,
        "total_rows": len(raw_records),
        "valid_rows": len(normalized),
        "inserted_rows": len(normalized),
        "errors": [],
    }


def main():
    parser = argparse.ArgumentParser(description="DIP 规则新版目录批量导入工具")
    parser.add_argument("--file", required=True, help="CSV 或 XLSX 文件路径")
    parser.add_argument("--version", required=True, help="目标规则版本号，如 2026_V2")
    parser.add_argument("--source-doc", required=True, help="来源文件名或发文号")
    parser.add_argument("--effective-date", default=None, help="生效日期 YYYY-MM-DD")
    parser.add_argument("--approve", action="store_true", help="直接置为 APPROVED（默认 PENDING）")
    parser.add_argument("--replace-version", action="store_true", help="覆盖同版本号旧规则")
    parser.add_argument("--dry-run", action="store_true", help="仅校验不入库")
    args = parser.parse_args()

    with open(args.file, "rb") as f:
        raw_bytes = f.read()

    if args.file.lower().endswith(".xlsx"):
        records = parse_xlsx_bytes(raw_bytes)
    else:
        records = parse_csv_bytes(raw_bytes)

    res = import_dip_rules_records(
        records,
        rule_version=args.version,
        source_doc=args.source_doc,
        effective_date=args.effective_date,
        review_status="APPROVED" if args.approve else "PENDING",
        replace_version=args.replace_version,
        dry_run=args.dry_run,
    )
    print(res)


if __name__ == "__main__":
    main()
