# -*- coding: utf-8 -*-
"""
tests/test_dip_importer.py — DIP 规则批量导入工具离线单元测试
"""
from decimal import Decimal
import io
import os
import sys
from unittest.mock import MagicMock, patch

import openpyxl

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.dip_importer import (
    import_dip_rules_records,
    normalize_rule_row,
    parse_csv_bytes,
    parse_xlsx_bytes,
)


def test_normalize_rule_row_calculates_decimal_benchmarks():
    raw = {
        "科室": "中医科",
        "病种分组": "M17.9 膝关节病保守治疗",
        "ICD编码": "m17.9",
        "分值": "620.20",
        "职工点值": "7.50",
        "居民点值": "7.00",
        "医院系数": "0.60",
    }
    norm = normalize_rule_row(raw, rule_version="2026_V2", effective_date="2026-12-01", source_doc="doc1")
    assert norm["icd_code"] == "M17.9"
    assert norm["employee_base"] == Decimal("2790.90")
    assert norm["resident_base"] == Decimal("2604.84")
    assert norm["employee_min"] == Decimal("2511.81")
    assert norm["employee_max"] == Decimal("3069.99")
    assert norm["review_status"] == "PENDING"


def test_parse_csv_and_xlsx_bytes():
    csv_data = "科室,病种分组,ICD编码,分值\n内科,高血压保守,I10,100.0\n".encode("utf-8-sig")
    csv_rows = parse_csv_bytes(csv_data)
    assert len(csv_rows) == 1
    assert csv_rows[0]["ICD编码"] == "I10"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["department", "dip_group_name", "icd_code", "dip_score"])
    ws.append(["骨伤科", "腰椎间盘突出", "M51.2", 500])
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_rows = parse_xlsx_bytes(buf.getvalue())
    assert len(xlsx_rows) == 1
    assert xlsx_rows[0]["icd_code"] == "M51.2"


def test_import_dry_run_and_validation_error():
    bad_rows = [{"department": "内科", "dip_group_name": "测试", "icd_code": "I10", "dip_score": "-5"}]
    res_bad = import_dip_rules_records(bad_rows, rule_version="2026_V2", source_doc="doc", dry_run=True)
    assert res_bad["success"] is False
    assert len(res_bad["errors"]) == 1

    good_rows = [{"department": "内科", "dip_group_name": "测试", "icd_code": "I10", "dip_score": "100"}]
    res_ok = import_dip_rules_records(good_rows, rule_version="2026_V2", source_doc="doc", dry_run=True)
    assert res_ok["success"] is True
    assert res_ok["valid_rows"] == 1
    assert res_ok["inserted_rows"] == 0


def test_import_db_transaction_commit_and_rollback():
    mock_cur = MagicMock()
    mock_conn = MagicMock()
    good_rows = [{"department": "内科", "dip_group_name": "测试", "icd_code": "I10", "dip_score": "100"}]

    with patch("tools.dip_importer.get_local_db_connection", return_value=(mock_cur, mock_conn)):
        res = import_dip_rules_records(
            good_rows,
            rule_version="2026_V2",
            source_doc="doc",
            review_status="APPROVED",
            replace_version=True,
        )
        assert res["success"] is True
        assert res["inserted_rows"] == 1
        assert mock_conn.commit.called
        assert mock_conn.close.called

    mock_cur2 = MagicMock()
    mock_cur2.execute.side_effect = RuntimeError("DB failure")
    mock_conn2 = MagicMock()
    with patch("tools.dip_importer.get_local_db_connection", return_value=(mock_cur2, mock_conn2)):
        try:
            import_dip_rules_records(good_rows, rule_version="2026_V2", source_doc="doc")
            assert False, "should raise"
        except RuntimeError:
            assert mock_conn2.rollback.called
            assert mock_conn2.close.called
