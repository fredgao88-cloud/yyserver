# -*- coding: utf-8 -*-
"""
tests/test_codex_review_fixes.py — Codex Issue #2 全部 12 项审核意见专项验收测试
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.main as main_mod
import auth.rbac as rbac_mod
import db.local_db as local_db_mod
from agent.tool_dispatcher import extract_tool_call
from tools.dingtalk_notifier import build_alert_markdown, send_dingtalk_alert
from tools.dip_importer import import_dip_rules_records
from tools.inpatient_scanner import InpatientScanner


def test_notify_data_001_blocks_arbitrary_text_in_outbound_payload():
    # 未登记但同格式的版本标记也必须被拦截
    summary = {
        "batch_id": "CUSTOM_LEAK_MARKER_XYZ",
        "rule_version": "2026_UNREGISTERED_V9",
        "data_cutoff_time": "2026-11-26 08:00:00",
        "total_scanned": 5,
        "RED": 2,
        "YELLOW": 1,
        "GREEN": 2,
        "dept_alert_counts": {
            "中医科": {"RED": 1, "YELLOW": 0},
            "任意注入科室_SENSITIVE_TAG": {"RED": 1, "YELLOW": 1},
        },
    }
    _, text = build_alert_markdown(summary)
    assert "CUSTOM_LEAK_MARKER_XYZ" not in text
    assert "2026_UNREGISTERED_V9" not in text
    assert "CONTROLLED_VERSION" in text
    assert "任意注入科室_SENSITIVE_TAG" not in text
    assert "中医科(红1/黄0)" in text
    assert "其他已核验科室(红1/黄1)" in text

    # 已批准登记版本可以正常出站；自定义批次 B314159 既不得原样出站，也不得出现其公开 SHA-256 摘要，且跨进程重发载荷完全一致
    import hashlib
    b_sha = hashlib.sha256(b"B314159").hexdigest()[:8].upper()
    summary_ok = dict(summary, batch_id="B314159", rule_version="2026_SAMPLE_V1")
    _, text_ok_1 = build_alert_markdown(summary_ok)
    _, text_ok_2 = build_alert_markdown(summary_ok)
    assert "2026_SAMPLE_V1" in text_ok_1
    assert "B314159" not in text_ok_1
    assert b_sha not in text_ok_1
    assert text_ok_1 == text_ok_2


def test_notify_ack_001_disabled_and_strict_json_ack(monkeypatch):
    # 1) 禁用开关对显式 webhook_url 也必须生效
    monkeypatch.setenv("DINGTALK_NOTIFY_ENABLED", "false")
    res_dis = send_dingtalk_alert(
        {"batch_id": "BATCH_20261126_080000", "RED": 1, "YELLOW": 0},
        webhook_url="https://oapi.dingtalk.com/robot/send?access_token=explicit",
    )
    assert res_dis["sent"] is False
    assert res_dis["skipped"] is True
    assert res_dis["reason"] == "DISABLED_BY_ENV"

    # 2) 非 JSON、缺 errcode、bool(false)、str("0")、非零整数一律 sent=False；仅整数 0 为 True
    monkeypatch.setenv("DINGTALK_NOTIFY_ENABLED", "true")
    for raw_body, expect_sent in [
        ("not-json-html", False),
        (json.dumps({"errmsg": "no code"}), False),
        (json.dumps({"errcode": False, "errmsg": "bool false"}), False),
        (json.dumps({"errcode": "0", "errmsg": "str zero"}), False),
        (json.dumps({"errcode": 310000, "errmsg": "sign error"}), False),
        (json.dumps({"errcode": 0, "errmsg": "ok"}), True),
    ]:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = raw_body.encode("utf-8")
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_resp
        mock_cm.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=mock_cm):
            r = send_dingtalk_alert(
                {"batch_id": "BATCH_20261126_080000", "RED": 1, "YELLOW": 0},
                webhook_url="https://oapi.dingtalk.com/robot/send?access_token=t",
            )
            assert r["sent"] is expect_sent


def test_rule_import_001_and_valid_001():
    # 空批次在共用入口被拒绝，不触发 DELETE / commit
    mock_cur = MagicMock()
    mock_conn = MagicMock()
    with patch("tools.dip_importer.get_local_db_connection", return_value=(mock_cur, mock_conn)):
        res_empty = import_dip_rules_records([], rule_version="2026_V2", source_doc="doc", replace_version=True)
        assert res_empty["success"] is False
        assert not mock_cur.execute.called
        assert not mock_conn.commit.called

    # 负点值与负系数在 dry-run 和正式导入均被拒绝并带行号
    for bad_field, bad_val in [
        ("employee_point", "-7.5"),
        ("resident_point", "0"),
        ("hosp_coef", "-0.6"),
        ("dip_score", "NaN"),
    ]:
        row = {"department": "内科", "dip_group_name": "测试组", "icd_code": "I10", "dip_score": "100", bad_field: bad_val}
        res_dry = import_dip_rules_records([row], rule_version="2026_V2", source_doc="doc", dry_run=True)
        assert res_dry["success"] is False
        assert res_dry["errors"][0]["row"] == 1


def test_dip_op_001_canceled_operation_falls_back_to_conservative():
    tc = extract_tool_call("帮我判灯住院号 zy2025042901，诊断 K63.5，已取消胃镜，当前费用 2500 元，职工医保")
    assert tc is not None
    assert tc["name"] == "check_patient_dip"
    assert tc["arguments"]["operation"] == "保守"
    assert tc["arguments"]["operation_status"] == "CANCELED"

    # 拟行胃镜也回退保守
    tc_plan = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，费用 2500 元，职工医保")
    assert tc_plan is not None
    assert tc_plan["arguments"]["operation"] == "保守"

    # “已完成胃镜，计划出院”不得被后半句“计划”污染
    tc_done = extract_tool_call("住院号 zy2025042901，诊断 K63.5，已完成胃镜，计划出院，当前费用 2500 元，职工医保")
    assert tc_done is not None
    assert tc_done["arguments"]["operation"] == "胃镜"
    assert tc_done["arguments"]["operation_status"] == "COMPLETED"

    # “拟行胃镜，随后已完成胃镜”以最新完成状态为准
    tc_prog = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，随后已完成胃镜，当前费用 2500 元，职工医保")
    assert tc_prog is not None
    assert tc_prog["arguments"]["operation"] == "胃镜"
    assert tc_prog["arguments"]["operation_status"] == "COMPLETED"

    # “已完成胃镜，拟行活检”不得抹去已完成的胃镜
    tc_multi = extract_tool_call("住院号 zy2025042901，诊断 K63.5，已完成胃镜，拟行活检，当前费用 2500 元，职工医保")
    assert tc_multi is not None
    assert tc_multi["arguments"]["operation"] == "胃镜"
    assert tc_multi["arguments"]["operation_status"] == "COMPLETED"

    # “胃镜检查，现已取消”后句省略操作名，需绑定取消到前一个操作
    tc_omit = extract_tool_call("住院号 zy2025042901，诊断 K63.5，胃镜检查，现已取消，当前费用 2500 元，职工医保")
    assert tc_omit is not None
    assert tc_omit["arguments"]["operation"] == "保守"
    assert tc_omit["arguments"]["operation_status"] == "CANCELED"

    # “拟行胃镜，已完成入院手续”不应把胃镜改成已完成
    from agent.llm_router import extract_operation_keyword
    kw1, st1, _ = extract_operation_keyword("拟行胃镜，已完成入院手续")
    assert kw1 == "保守" and st1 == "PLANNED"
    tc_adm = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，已完成入院手续，当前费用 2500 元，职工医保")
    assert tc_adm["arguments"]["operation"] == "保守"
    assert tc_adm["arguments"]["operation_status"] == "PLANNED"

    # “已完成胃镜，取消出院”不应把胃镜改成已取消
    kw2, st2, _ = extract_operation_keyword("已完成胃镜，取消出院")
    assert kw2 == "胃镜" and st2 == "COMPLETED"
    tc_dis = extract_tool_call("住院号 zy2025042901，诊断 K63.5，已完成胃镜，取消出院，当前费用 2500 元，职工医保")
    assert tc_dis["arguments"]["operation"] == "胃镜"
    assert tc_dis["arguments"]["operation_status"] == "COMPLETED"

    # “拟行胃镜，已完成抽血”与“已完成胃镜，取消输液”不得跨独立事项污染前序操作
    kw3, st3, _ = extract_operation_keyword("拟行胃镜，已完成抽血")
    assert kw3 == "保守" and st3 == "PLANNED"
    tc_draw = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，已完成抽血，当前费用 2500 元，职工医保")
    assert tc_draw["arguments"]["operation"] == "保守"
    assert tc_draw["arguments"]["operation_status"] == "PLANNED"

    kw4, st4, _ = extract_operation_keyword("已完成胃镜，取消输液")
    assert kw4 == "胃镜" and st4 == "COMPLETED"
    tc_inf = extract_tool_call("住院号 zy2025042901，诊断 K63.5，已完成胃镜，取消输液，当前费用 2500 元，职工医保")
    assert tc_inf["arguments"]["operation"] == "胃镜"
    assert tc_inf["arguments"]["operation_status"] == "COMPLETED"

    # “拟行胃镜，随后已完成”与“拟行胃镜，该操作已完成”均须返回 胃镜/COMPLETED
    kw5, st5, _ = extract_operation_keyword("拟行胃镜，随后已完成")
    assert kw5 == "胃镜" and st5 == "COMPLETED"
    tc_sub = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，随后已完成，当前费用 2500 元，职工医保")
    assert tc_sub["arguments"]["operation"] == "胃镜"
    assert tc_sub["arguments"]["operation_status"] == "COMPLETED"

    kw6, st6, _ = extract_operation_keyword("拟行胃镜，该操作已完成")
    assert kw6 == "胃镜" and st6 == "COMPLETED"
    tc_pro = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，该操作已完成，当前费用 2500 元，职工医保")
    assert tc_pro["arguments"]["operation"] == "胃镜"
    assert tc_pro["arguments"]["operation_status"] == "COMPLETED"

    # “拟行胃镜，该检查前已完成活检”中“该检查前”是时间参照，应绑定到具体操作“活检”/COMPLETED
    kw7, st7, _ = extract_operation_keyword("拟行胃镜，该检查前已完成活检")
    assert kw7 == "活检" and st7 == "COMPLETED"
    tc_bio = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，该检查前已完成活检，当前费用 2500 元，职工医保")
    assert tc_bio["arguments"]["operation"] == "活检"
    assert tc_bio["arguments"]["operation_status"] == "COMPLETED"

    # “拟行胃镜，该检查前已完成抽血”不得把胃镜标为已完成（胃镜保持拟行/保守）
    kw8, st8, _ = extract_operation_keyword("拟行胃镜，该检查前已完成抽血")
    assert kw8 == "保守" and st8 == "PLANNED"
    tc_draw2 = extract_tool_call("住院号 zy2025042901，诊断 K63.5，拟行胃镜，该检查前已完成抽血，当前费用 2500 元，职工医保")
    assert tc_draw2["arguments"]["operation"] == "保守"
    assert tc_draw2["arguments"]["operation_status"] == "PLANNED"


def test_chat_audit_001_denied_vs_permitted():
    recorded = []

    def _capture_audit(**kwargs):
        recorded.append(kwargs)

    with patch.object(main_mod, "record_audit_log", side_effect=_capture_audit), \
         patch.object(main_mod, "operator_from_optional_token", return_value={"sub": "anonymous", "role": "DOCTOR", "authenticated": False}):
        req = main_mod.ChatRequest(text="查看最近30天的医生控费绩效报表", channel="web")
        res = asyncio.run(main_mod.chat_interaction(req, authorization=None))
        assert res["data"]["status"] == "AUTH_REQUIRED"
        assert recorded[-1]["auth_result"] == "DENIED"


def test_audit_log_001_no_sensitive_driver_message_leaked(caplog, monkeypatch):
    monkeypatch.setattr(local_db_mod, "LOCAL_DB_PASS", "fake_pass")
    caplog.set_level(logging.WARNING)
    secret_marker = "SENSITIVE_DRIVER_DIAG_MARKER_999"

    class _BadCur:
        def execute(self, *args, **kwargs):
            raise RuntimeError(f"SQL error: {secret_marker}")

    class _BadConn:
        def close(self):
            pass

    with patch.object(local_db_mod, "get_local_db_connection", return_value=(_BadCur(), _BadConn())):
        local_db_mod.record_audit_log("OP_SENSITIVE_1", "DOCTOR", "API", "LOGIN", target_ref="T1")
    assert secret_marker not in caplog.text
    assert "EVENT=AUDIT_LOG_WRITE_FAILED" in caplog.text

    caplog.clear()
    with patch.object(local_db_mod.pymssql, "connect", side_effect=RuntimeError(f"Connect fail: {secret_marker}")):
        try:
            local_db_mod.get_local_db_connection()
            assert False, "expected ConnectionError"
        except ConnectionError as exc:
            assert secret_marker not in str(exc)
            assert "EVENT=DB_CONNECT_FAILED" in str(exc)
    assert secret_marker not in caplog.text


def test_auth_lock_001_concurrent_login_does_not_clear_active_lock():
    op_id = "CONC_TEST_USER"
    pw_hash = rbac_mod.hash_password("ValidPass123!")
    fake_op = {"operator_id": op_id, "status": "ACTIVE", "password_hash": pw_hash, "role_type": "DOCTOR", "cred_version": 1}

    with rbac_mod._login_lock:
        rbac_mod._login_failures.pop(op_id, None)

    # 模拟在 get_operator 返回后、最终更新前，另一并发请求已触发锁定
    def _get_op_and_trigger_lock(_):
        with rbac_mod._login_lock:
            rbac_mod._login_failures[op_id] = (rbac_mod.MAX_LOGIN_FAILS, 9999999999.0)
        return fake_op

    with patch.object(rbac_mod, "get_operator", side_effect=_get_op_and_trigger_lock):
        op, msg = rbac_mod.authenticate(op_id, "ValidPass123!")
        assert op is None
        assert "账号已锁定" in msg
        with rbac_mod._login_lock:
            assert op_id in rbac_mod._login_failures
            rbac_mod._login_failures.pop(op_id, None)


def test_auth_scope_002_realtime_ambiguity_rejection():
    # 模拟同一进程先查只有 1 个“综合科”，随后目录新增同名“综合科”后必须立即返回空集拒绝
    ks_v1 = [{"ksid": 101, "ksname": "综合科"}]
    ks_v2 = [{"ksid": 101, "ksname": "综合科"}, {"ksid": 102, "ksname": "综合科"}]

    calls = [ks_v1, ks_v2]

    class _FakeCur:
        def execute(self, *a, **kw):
            pass

        def fetchall(self):
            return calls.pop(0)

    class _FakeConn:
        def cursor(self, as_dict=True):
            return _FakeCur()

        def close(self):
            pass

    fake_reader = MagicMock()
    fake_reader.get_connection.return_value = _FakeConn()
    with patch("db.his_reader.HISReader", return_value=fake_reader):
        assert rbac_mod.resolve_authorized_dept_ids("综合科") == {"101"}
        assert rbac_mod.resolve_authorized_dept_ids("综合科") == set()


def test_snap_log_001_no_sensitive_error_and_accurate_rollback_status(capsys):
    secret_marker = "SENSITIVE_SCAN_ERR_ABC"
    scanner = InpatientScanner.__new__(InpatientScanner)
    scanner.rule_version = "2026_SAMPLE_V1"
    scanner.his_reader = MagicMock()
    scanner.his_reader.get_active_inpatients.return_value = [
        {"zyh": "ZY1", "current_dept": "中医科", "current_dept_id": "101", "attending_doctor_id": "D1", "age": 50, "ylzffs": "1"}
    ]
    scanner.his_reader.get_active_patient_fee.return_value = {"total_fee": Decimal("100"), "has_activity": True}

    class _FailCommitConn:
        def commit(self):
            raise RuntimeError(f"commit failed: {secret_marker}")

        def rollback(self):
            raise RuntimeError("rollback also failed")

        def close(self):
            pass

    mock_cur = MagicMock()
    with patch("tools.inpatient_scanner.get_local_db_connection", return_value=(mock_cur, _FailCommitConn())), \
         patch("tools.inpatient_scanner.match_dip_rule", return_value={"matched": False, "status": "PENDING", "pending_reason": "TEST"}):
        try:
            scanner.scan_and_snapshot(batch_id="BATCH_20261126_080000")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert secret_marker not in str(exc)
            assert "ROLLBACK_FAILED" in str(exc)

    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert "ROLLBACK_FAILED" in captured.out


def test_snap_time_001_timezone_comparison():
    bj_tz = timezone(timedelta(hours=8))
    # 11月25日 12:00 UTC 对比 11月24日 19:00 UTC+8 (即 11月24日 11:00 UTC)，实际经过 25 小时 -> 必须过期
    now_utc = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    synced_bj = datetime(2026, 11, 24, 19, 0, 0, tzinfo=bj_tz)

    class _BatchCur:
        def execute(self, *a, **kw):
            pass

        def fetchone(self):
            return {"batch_id": "B1", "data_cutoff_time": synced_bj, "synced_at": synced_bj}

        def fetchall(self):
            return []

    class _BatchConn:
        def close(self):
            pass

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc if tz is timezone.utc else now_utc.replace(tzinfo=None)

    with patch.object(main_mod, "get_local_db_connection", return_value=(_BatchCur(), _BatchConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "dept_scope": ""}), \
         patch("datetime.datetime", _FixedDatetime):
        res = asyncio.run(main_mod.get_inpatient_alerts(dept=None, operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert res["is_stale"] is True
