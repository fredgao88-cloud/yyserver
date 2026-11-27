# -*- coding: utf-8 -*-
"""
tests/test_reconciliation_reporter.py — 出院决算对账引擎、接口与看板离线单元测试
"""
import asyncio
from datetime import datetime
from decimal import Decimal
import os
import sys
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.main as main_mod
from auth.rbac import ROLE_DOCTOR, ROLE_INSURANCE, require_roles
from tools.reconciliation_reporter import (
    evaluate_case_reconciliation,
    run_settled_reconciliation_report,
)


def test_evaluate_case_reconciliation_matched_undispensed_and_mismatch():
    # 1) 账实相符
    d_ok = {
        "reconciliation_scope": "SINGLE_SETTLEMENT",
        "settlement_count": 1,
        "zyjz": {"lsh": "ZY001", "id": "S1", "amoney": Decimal("100.00"), "czmoney": Decimal("40.00"), "yjmoney": Decimal("20.00"), "yfmoney": Decimal("40.00")},
        "category_sum": Decimal("100.00"),
        "cz_stat": {"total": Decimal("60.00")},
        "fy_stat": {"total": Decimal("40.00"), "undispensed_count": 0, "undispensed": Decimal("0.00")},
    }
    r_ok = evaluate_case_reconciliation(d_ok, {"patient_name": "严禁泄露姓名", "discharge_dept": "中医科"})
    assert r_ok["recon_status"] == "MATCHED"
    assert "严禁泄露姓名" not in str(r_ok)

    # 2) 仅存在已计费未发药
    d_unfy = {
        "reconciliation_scope": "SINGLE_SETTLEMENT",
        "settlement_count": 1,
        "zyjz": {"lsh": "ZY002", "id": "S2", "amoney": Decimal("100.00"), "czmoney": Decimal("60.00"), "yjmoney": Decimal("0.00"), "yfmoney": Decimal("40.00")},
        "category_sum": Decimal("100.00"),
        "cz_stat": {"total": Decimal("60.00")},
        "fy_stat": {"total": Decimal("30.00"), "undispensed_count": 1, "undispensed": Decimal("10.00")},
    }
    r_unfy = evaluate_case_reconciliation(d_unfy)
    assert r_unfy["recon_status"] == "UNDISPENSED_ONLY"
    assert r_unfy["pharmacy_diff_str"] == "0.00"
    assert r_unfy["undispensed_amount_str"] == "10.00"

    # 3) 账实差异
    d_bad = {
        "reconciliation_scope": "ADMISSION_LEVEL",
        "settlement_count": 2,
        "zyjz": {"lsh": "ZY003", "id": "S3,S4", "amoney": Decimal("200.00"), "czmoney": Decimal("100.00"), "yjmoney": Decimal("0.00"), "yfmoney": Decimal("100.00")},
        "category_sum": Decimal("200.00"),
        "cz_stat": {"total": Decimal("90.00")},
        "fy_stat": {"total": Decimal("100.00"), "undispensed_count": 0, "undispensed": Decimal("0.00")},
    }
    r_bad = evaluate_case_reconciliation(d_bad)
    assert r_bad["recon_status"] == "MISMATCH"
    assert r_bad["clinical_diff_str"] == "10.00"


def test_run_settled_reconciliation_report_dedup_and_error_isolation():
    fake_reader = MagicMock()
    fake_reader.get_settled_cases.return_value = [
        {"lsh": "ZY1", "settlement_id": "1", "discharge_dept": "中医科", "settlement_time": datetime(2026, 11, 26, 9, 0)},
        {"lsh": "ZY1", "settlement_id": "2", "discharge_dept": "中医科", "settlement_time": datetime(2026, 11, 26, 10, 0)},
        {"lsh": "ZY2", "settlement_id": "3", "discharge_dept": "内科", "settlement_time": datetime(2026, 11, 26, 11, 0)},
    ]
    fake_reader.get_case_fee_detail.side_effect = [
        {
            "reconciliation_scope": "ADMISSION_LEVEL",
            "settlement_count": 2,
            "zyjz": {"lsh": "ZY1", "id": "1,2", "amoney": Decimal("100.00"), "czmoney": Decimal("50.00"), "yjmoney": Decimal("0.00"), "yfmoney": Decimal("50.00")},
            "category_sum": Decimal("100.00"),
            "cz_stat": {"total": Decimal("50.00")},
            "fy_stat": {"total": Decimal("50.00"), "undispensed_count": 0, "undispensed": Decimal("0.00")},
        },
        RuntimeError("transient error"),
    ]
    res = run_settled_reconciliation_report(limit=10, reader=fake_reader)
    assert res["sampled_count"] == 2
    assert res["reconciled_count"] == 1
    assert res["unreconciled_count"] == 1
    assert res["total"] == 1
    assert res["matched_count"] == 1
    assert res["error_count"] == 1

    # RECON-COVER-001: 零样本、全空详情、正常+空详情
    r_zero = MagicMock()
    r_zero.get_settled_cases.return_value = []
    res0 = run_settled_reconciliation_report(limit=10, reader=r_zero)
    assert res0["sampled_count"] == 0 and res0["reconciled_count"] == 0 and res0["unreconciled_count"] == 0

    r_empty = MagicMock()
    r_empty.get_settled_cases.return_value = [{"lsh": "ZY_E1", "settlement_id": "1"}]
    r_empty.get_case_fee_detail.return_value = {}
    res_empty = run_settled_reconciliation_report(limit=10, reader=r_empty)
    assert res_empty["sampled_count"] == 1 and res_empty["reconciled_count"] == 0 and res_empty["unreconciled_count"] == 1
    assert res_empty["unreconciled_reasons"][0]["reason"] == "EMPTY_DETAIL"

    r_mix = MagicMock()
    r_mix.get_settled_cases.return_value = [{"lsh": "ZY_OK", "settlement_id": "1"}, {"lsh": "ZY_E2", "settlement_id": "2"}]
    r_mix.get_case_fee_detail.side_effect = [
        {
            "reconciliation_scope": "SINGLE_SETTLEMENT", "settlement_count": 1,
            "zyjz": {"lsh": "ZY_OK", "id": "1", "amoney": Decimal("10"), "czmoney": Decimal("10"), "yjmoney": Decimal("0"), "yfmoney": Decimal("0")},
            "category_sum": Decimal("10"), "cz_stat": {"total": Decimal("10")},
            "fy_stat": {"total": Decimal("0"), "undispensed_count": 0, "undispensed": Decimal("0")},
        },
        {},
    ]
    res_mix = run_settled_reconciliation_report(limit=10, reader=r_mix)
    assert res_mix["sampled_count"] == 2 and res_mix["reconciled_count"] == 1 and res_mix["unreconciled_count"] == 1


def test_reconciliation_ui_and_rbac_protection():
    resp = asyncio.run(main_mod.reconciliation_workbench_page())
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "出院决算账实对账" in body
    assert "clearProtectedReconData" in body
    assert "reconReqSeq" in body and "reconAuthEpoch" in body and "safeReconJson" in body
    assert "parseReconReportJson" in body and "对账结果读取失败" in body
    assert "data.reconciled_count + data.unreconciled_count !== data.sampled_count" in body
    assert "data.unreconciled_reasons.length !== data.unreconciled_count" in body
    load_section = body[body.index("async function loadRecon()"):body.index("function renderReconRows(")]
    export_section = body[body.index("async function exportReconCsv()"):body.index("if (getToken())")]
    assert load_section.index("if (myEpoch !== reconAuthEpoch) return;") < load_section.index("if (r.status === 401)")
    assert load_section.index("if (r.status === 401)") < load_section.index("await safeReconJson(r)")
    assert load_section.index("await parseReconReportJson(r)") < load_section.index("已完成 SERIALIZABLE")
    assert export_section.index("if (myEpoch !== reconAuthEpoch) return;") < export_section.index("if (r.status === 401)")
    assert export_section.index("if (r.status === 401)") < export_section.index("await safeReconJson(r)")
    assert 'id="rSampled"' in body and 'id="rUnrecon"' in body and "unreconBanner" in body
    assert "部分完成" in body
    assert "http://" not in body and "https://" not in body

    dep = require_roles(ROLE_INSURANCE, main_mod.ROLE_ADMIN)
    with patch("auth.rbac.verify_token", return_value={"sub": "DOC1", "role": ROLE_DOCTOR, "cv": 0}), \
         patch("auth.rbac.revalidate_operator", return_value=({"sub": "DOC1", "role": ROLE_DOCTOR, "dept": ""}, "")), \
         patch("auth.rbac.record_audit_log"):
        try:
            asyncio.run(dep(authorization="Bearer fake"))
            assert False, "expected 403"
        except HTTPException as exc:
            assert exc.status_code == 403
