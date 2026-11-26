# -*- coding: utf-8 -*-
"""
tests/test_phase3_features.py — 医生试算问答台、规则审核流与手动触发扫描离线单元测试
"""
import asyncio
from decimal import Decimal
import os
import sys
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.main as main_mod
from auth.rbac import ROLE_ADMIN, ROLE_DOCTOR, ROLE_INSURANCE, require_roles


def test_calculator_ui_safe_and_no_cdn():
    resp = asyncio.run(main_mod.calculator_workbench_page())
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "医生试算与智能问答" in body
    assert "clearProtectedCalcData" in body
    assert "http://" not in body and "https://" not in body


def test_dip_rules_list_and_review_updates_and_reloads_cache():
    class _FakeCur:
        rowcount = 5

        def execute(self, sql, params):
            pass

        def fetchall(self):
            return [
                {
                    "rule_id": 1,
                    "rule_version": "2026_V2",
                    "review_status": "PENDING",
                    "department": "中医科",
                    "dip_group_name": "膝关节病",
                    "icd_code": "M17.9",
                    "billing_unit": "PER_CASE",
                    "dip_score": Decimal("620.20"),
                    "employee_base": Decimal("2790.90"),
                    "resident_base": Decimal("2604.84"),
                }
            ]

    class _FakeConn:
        committed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            pass

    conn = _FakeConn()
    ins_op = {"sub": "ins01", "role": ROLE_INSURANCE}
    with patch.object(main_mod, "get_local_db_connection", return_value=(_FakeCur(), conn)), \
         patch.object(main_mod, "record_audit_log"), \
         patch("tools.dip_calculator.get_loaded_rules") as mock_reload:

        res_list = asyncio.run(main_mod.api_list_dip_rules(rule_version="2026_V2", review_status="PENDING", limit=50, operator=ins_op))
        assert res_list["total"] == 1
        assert res_list["items"][0]["review_status"] == "PENDING"

        res_rev = asyncio.run(
            main_mod.api_review_dip_rules(
                main_mod.DipRuleReviewRequest(rule_version="2026_V2", target_status="APPROVED"),
                operator=ins_op,
            )
        )
        assert res_rev["success"] is True
        assert res_rev["updated_rows"] == 5
        assert conn.committed is True
        assert mock_reload.called


def test_manual_trigger_scan_rbac_reentrancy_and_privacy():
    # 非管理员/医保办角色 -> 403
    dep = require_roles(ROLE_INSURANCE, ROLE_ADMIN)
    with patch("auth.rbac.verify_token", return_value={"sub": "DOC1", "role": ROLE_DOCTOR, "cv": 0}), \
         patch("auth.rbac.revalidate_operator", return_value=({"sub": "DOC1", "role": ROLE_DOCTOR, "dept": ""}, "")), \
         patch("auth.rbac.record_audit_log"):
        try:
            asyncio.run(dep(authorization="Bearer fake"))
            assert False, "expected 403"
        except HTTPException as exc:
            assert exc.status_code == 403

    # 防重入锁命中 -> 409
    admin_op = {"sub": "admin", "role": ROLE_ADMIN}
    assert main_mod._manual_scan_lock.acquire(blocking=False)
    try:
        try:
            asyncio.run(main_mod.api_trigger_manual_scan(main_mod.ManualScanRequest(), operator=admin_op))
            assert False, "expected 409"
        except HTTPException as exc:
            assert exc.status_code == 409
    finally:
        main_mod._manual_scan_lock.release()

    # 正常执行 -> 返回汇总，绝不包含 snapshots 患者明细
    fake_scanner = MagicMock()
    fake_scanner.scan_and_snapshot.return_value = {
        "batch_id": "BATCH_20261126_210000",
        "rule_version": "2026_SAMPLE_V1",
        "data_cutoff_time": "2026-11-26 21:00:00",
        "total_scanned": 3,
        "GREEN": 2,
        "YELLOW": 1,
        "RED": 0,
        "PENDING": 0,
        "NOT_APPLICABLE": 0,
        "snapshots": [{"visit_id": "SECRET_ZY", "patient_name": "保密姓名"}],
    }
    with patch("tools.inpatient_scanner.InpatientScanner", return_value=fake_scanner), \
         patch.object(main_mod, "record_audit_log"):
        res = asyncio.run(main_mod.api_trigger_manual_scan(main_mod.ManualScanRequest(), operator=admin_op))
        assert res["success"] is True
        assert res["total_scanned"] == 3
        assert "snapshots" not in res
        assert "SECRET_ZY" not in str(res)
        assert "保密姓名" not in str(res)
