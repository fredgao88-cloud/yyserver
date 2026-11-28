# -*- coding: utf-8 -*-
"""
tests/test_audit_logs_api.py — 审计日志监控接口与控制台页面离线单元测试
"""
import asyncio
from datetime import datetime
import os
import sys
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.main as main_mod
from auth.rbac import ROLE_ADMIN, ROLE_DOCTOR, require_roles


def test_audit_logs_html_page_safe_and_handles_401_403():
    resp = asyncio.run(main_mod.audit_logs_admin_page())
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "合规审计日志" in body
    assert "clearProtectedAuditData" in body
    assert "r.status === 401" in body and "r.status === 403" in body
    assert "http://" not in body and "https://" not in body


def test_audit_logs_api_requires_admin():
    dep = require_roles(ROLE_ADMIN)
    with patch("auth.rbac.verify_token", return_value={"sub": "DOC1", "role": ROLE_DOCTOR, "cv": 0}), \
         patch("auth.rbac.revalidate_operator", return_value=({"sub": "DOC1", "role": ROLE_DOCTOR, "dept": ""}, "")), \
         patch("auth.rbac.record_audit_log"):
        try:
            asyncio.run(dep(authorization="Bearer fake"))
            assert False, "expected 403"
        except HTTPException as exc:
            assert exc.status_code == 403


def test_audit_logs_api_returns_filtered_items():
    class _FakeCur:
        def execute(self, sql, params):
            assert "auth_result = %s" in sql
            assert "DENIED" in params

        def fetchall(self):
            return [
                {
                    "log_id": 101,
                    "operator_id": "anonymous",
                    "role_type": "UNKNOWN",
                    "channel": "API",
                    "action_type": "LOGIN",
                    "target_ref": "LOGIN",
                    "auth_result": "DENIED",
                    "rule_version": None,
                    "batch_id": None,
                    "created_at": datetime(2026, 11, 26, 20, 0, 0),
                }
            ]

    class _FakeConn:
        def close(self):
            pass

    with patch.object(main_mod, "get_local_db_connection", return_value=(_FakeCur(), _FakeConn())):
        res = asyncio.run(
            main_mod.api_list_audit_logs(
                days=7,
                limit=50,
                auth_result="DENIED",
                action_type=None,
                operator={"sub": "admin", "role": ROLE_ADMIN},
            )
        )
        assert res["total"] == 1
        assert res["items"][0]["auth_result"] == "DENIED"
        assert res["items"][0]["created_at"] == "2026-11-26 20:00:00"
