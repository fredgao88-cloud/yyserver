# -*- coding: utf-8 -*-
"""
tests/test_admin_operators_api.py — 操作员管理 Web 界面与 Admin REST API 离线单元测试
"""
import asyncio
import os
import sys
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.main as main_mod
from auth.rbac import ROLE_ADMIN, ROLE_DOCTOR, require_roles


def test_admin_operators_html_page_has_no_external_cdn():
    resp = asyncio.run(main_mod.admin_operators_page())
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "院内操作员权限管理控制台" in body
    # 内网零外网依赖检查
    assert "http://" not in body and "https://" not in body


def test_admin_operators_requires_admin_role():
    dep = require_roles(ROLE_ADMIN)

    # 未认证 -> 401
    with patch("auth.rbac.record_audit_log"):
        try:
            asyncio.run(dep(authorization=None))
            assert False, "expected 401"
        except HTTPException as exc:
            assert exc.status_code == 401

    # 医生角色越权 -> 403
    with patch("auth.rbac.verify_token", return_value={"sub": "DOC1", "role": ROLE_DOCTOR, "cv": 0}), \
         patch("auth.rbac.revalidate_operator", return_value=({"sub": "DOC1", "role": ROLE_DOCTOR, "dept": ""}, "")), \
         patch("auth.rbac.record_audit_log"):
        try:
            asyncio.run(dep(authorization="Bearer fake"))
            assert False, "expected 403"
        except HTTPException as exc:
            assert exc.status_code == 403


def test_admin_operators_crud_and_self_lockout_guard():
    admin_op = {"sub": "admin", "role": ROLE_ADMIN, "dept": ""}
    fake_ops = [
        {"operator_id": "admin", "display_name": "系统管理员", "role_type": "ADMIN", "dept_scope": "", "doctor_id": "", "status": "ACTIVE"}
    ]
    with patch("api.main.record_audit_log"), \
         patch("api.main.list_operators", return_value=fake_ops), \
         patch("api.main.create_operator") as mock_create, \
         patch("api.main.set_operator_status", return_value=True) as mock_status:

        res_list = asyncio.run(main_mod.api_list_operators(operator=admin_op))
        assert res_list["total"] == 1
        assert "password_hash" not in str(res_list)

        req_create = main_mod.OperatorUpsertRequest(
            operator_id="DOC_NEW",
            display_name="新医生",
            password="StrongPassword123",
            role_type="DOCTOR",
            dept_scope="101",
            doctor_id="HIS_D1",
        )
        res_create = asyncio.run(main_mod.api_upsert_operator(req=req_create, operator=admin_op))
        assert res_create["success"] is True
        assert mock_create.called

        # 防止管理员停用自己
        try:
            asyncio.run(
                main_mod.api_set_operator_status(
                    operator_id="admin",
                    req=main_mod.OperatorStatusRequest(status="DISABLED"),
                    operator=admin_op,
                )
            )
            assert False, "expected 400"
        except HTTPException as exc:
            assert exc.status_code == 400

        # 停用其他账号成功
        res_other = asyncio.run(
            main_mod.api_set_operator_status(
                operator_id="DOC_NEW",
                req=main_mod.OperatorStatusRequest(status="DISABLED"),
                operator=admin_op,
            )
        )
        assert res_other["success"] is True
        assert res_other["status"] == "DISABLED"
        assert mock_status.called


def test_auth_me_returns_sanitized_operator_profile():
    op = {"sub": "ins01", "role": "INSURANCE_OFFICER", "dept": "101", "doctor_id": ""}
    with patch("api.main.get_operator", return_value={"operator_id": "ins01", "display_name": "王主任", "can_export_case": 1, "password_hash": "SECRET"}):
        res = asyncio.run(main_mod.auth_me(operator=op))
        assert res["operator_id"] == "ins01"
        assert res["display_name"] == "王主任"
        assert res["role_type"] == "INSURANCE_OFFICER"
        assert res["can_export_case"] is True
        assert "password_hash" not in str(res)
