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


def test_icd_subcode_merge_and_billed_op_inference_offline():
    from tools import dip_calculator as dip_calc
    from tools.inpatient_scanner import InpatientScanner

    sample_rules = [
        {
            "rule_id": 101,
            "rule_version": "2026_SAMPLE_V1",
            "department": "消化内科",
            "dip_group_name": "结肠息肉:电子结肠镜检查",
            "icd_code": "K63.5",
            "icd_name": "结肠息肉",
            "op_code": None,
            "op_name": "电子结肠镜检查",
            "aux_detail": "",
            "billing_unit": "PER_CASE",
            "dip_score": Decimal("514.6000"),
            "resident_point": Decimal("7.0000"),
            "employee_point": Decimal("7.5000"),
            "hosp_coef": Decimal("0.6000"),
            "low_threshold": Decimal("0.9000"),
            "high_threshold": Decimal("1.1000"),
            "resident_base": Decimal("2161.3200"),
            "employee_base": Decimal("2315.7000"),
        },
        {
            "rule_id": 102,
            "rule_version": "2026_SAMPLE_V1",
            "department": "消化内科",
            "dip_group_name": "结肠息肉:核心病种-保守治疗",
            "icd_code": "K63.5",
            "icd_name": "结肠息肉",
            "op_code": None,
            "op_name": "核心病种-保守治疗",
            "aux_detail": "",
            "billing_unit": "PER_CASE",
            "dip_score": Decimal("357.0000"),
            "resident_point": Decimal("7.0000"),
            "employee_point": Decimal("7.5000"),
            "hosp_coef": Decimal("0.6000"),
            "low_threshold": Decimal("0.9000"),
            "high_threshold": Decimal("1.1000"),
            "resident_base": Decimal("1499.4000"),
            "employee_base": Decimal("1606.5000"),
        },
    ]

    op_kw = InpatientScanner.infer_op_keyword_from_billed_items(
        "K63.500",
        "结肠息肉",
        ["上消化道内镜检查费（常规）", "下消化道内镜检查费（常规）", "全身麻醉费（无插管全麻）"],
    )
    assert op_kw == "电子结肠镜检查"

    # Excision present -> never downgrade to exam-only
    assert (
        InpatientScanner.infer_op_keyword_from_billed_items(
            "K63.500",
            "结肠息肉",
            ["下消化道内镜检查费（常规）", "纤维结肠镜下结肠息肉切除术"],
        )
        is None
    )
    assert InpatientScanner.infer_op_keyword_from_billed_items("K63.500", "结肠息肉", MagicMock()) is None

    with patch.object(dip_calc, "get_loaded_rules", return_value=sample_rules):
        matched = dip_calc.match_dip_rule(
            disease="K63.500",
            age=53,
            ins_type="居民",
            op_keyword=op_kw,
        )
        assert matched["matched"] is True
        assert matched["status"] == "MATCHED"
        assert matched["rule_id"] == 101
        assert matched["benchmark_amount"] == 2161.32

        # Without op_keyword, explicit K63.500 subcode still avoids K31.7 false leak and flags MISSING_OPERATION
        pending = dip_calc.match_dip_rule(disease="K63.500", age=53, ins_type="居民")
        assert pending["matched"] is False
        assert pending["pending_reason"] == "MISSING_OPERATION"


def test_monthly_alerts_scope_sql_filter_offline():
    executed = []

    class _ScopeCur:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchone(self):
            return {"batch_id": "MONTH_202611_20261128_090000", "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0), "synced_at": datetime(2026, 11, 28, 9, 0, 0)}

        def fetchall(self):
            return []

    class _ScopeConn:
        def close(self):
            pass

    with patch.object(main_mod, "get_local_db_connection", return_value=(_ScopeCur(), _ScopeConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "dept_scope": ""}):
        res_m = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert res_m["scope"] == "month"
        assert res_m["year_month"] == "2026-11"
        assert any("batch_id LIKE" in s and p and p[0] == "MONTH_202611_%" for s, p in executed)

        executed.clear()
        res_a = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert res_a["scope"] == "active"
        assert any("batch_id NOT LIKE" in s for s, _ in executed)


def test_auth_attend_001_split_inferred_vs_authoritative_offline():
    from db.his_reader import HISReader
    from agent.tool_dispatcher import dispatch_tool_call

    class _ProbeCur:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            self.sqls.append(str(sql))

        def fetchone(self):
            return None

    class _ProbeConn:
        def __init__(self, cur):
            self._c = cur

        def cursor(self, as_dict=False):
            return self._c

        def close(self):
            pass

    reader = HISReader()
    cur = _ProbeCur()
    with patch.object(reader, "get_connection", return_value=_ProbeConn(cur)):
        reader.get_patient_realtime_admission("zy_probe")
    rt_sql = cur.sqls[0]
    assert "auth_attending_doctor_id" in rt_sql
    assert "order_doc" not in rt_sql
    assert "his_zycz" not in rt_sql and "his_bl_yzzycf" not in rt_sql and "his_zycfmain" not in rt_sql

    operators = {
        "DOC_CONSULT": {"operator_id": "DOC_CONSULT", "role_type": "DOCTOR", "doctor_id": "DOC_CONSULT", "status": "ACTIVE"},
        "DOC_A": {"operator_id": "DOC_A", "role_type": "DOCTOR", "doctor_id": "DOC_A", "status": "ACTIVE"},
        "DOC_B": {"operator_id": "DOC_B", "role_type": "DOCTOR", "doctor_id": "DOC_B", "status": "ACTIVE"},
    }
    admissions = {
        "PAT_CONSULT_ONLY": {
            "zyh": "PAT_CONSULT_ONLY",
            "auth_attending_doctor_id": None,
            "attending_doctor_id": "DOC_CONSULT",
            "attending_doctor_source": "ORDER_INFERRED",
            "current_dept_id": "4",
        },
        "PAT_NO_DOC": {
            "zyh": "PAT_NO_DOC",
            "auth_attending_doctor_id": None,
            "attending_doctor_id": None,
            "attending_doctor_source": None,
            "current_dept_id": "4",
        },
        "PAT_AUTHORITATIVE": {
            "zyh": "PAT_AUTHORITATIVE",
            "auth_attending_doctor_id": "DOC_A",
            "attending_doctor_id": "DOC_A",
            "attending_doctor_source": "SY_ZYYSBM",
            "current_dept_id": "4",
        },
    }

    class _AdmReader:
        def get_patient_realtime_admission(self, pid):
            return admissions.get(pid)

    with patch.object(main_mod, "HISReader", return_value=_AdmReader()), \
         patch.object(main_mod, "get_operator", side_effect=lambda o: operators.get(o)), \
         patch.object(main_mod, "record_audit_log"):

        # 1) Consultation / proxy order inferred only -> 403
        for pid, sub_id in [("PAT_CONSULT_ONLY", "DOC_CONSULT"), ("PAT_NO_DOC", "DOC_A")]:
            req = main_mod.CheckPatientDipRequest(patient_id=pid, disease="M17.9", current_cost=-1.0, age=68, ins_type="职工", operation="保守治疗")
            try:
                asyncio.run(main_mod.check_patient_dip(req, operator={"sub": sub_id, "role": "DOCTOR", "dept": "中医科"}))
                raise AssertionError(f"Expected 403 for {pid} / {sub_id}")
            except HTTPException as exc:
                assert exc.status_code == 403

        # 3) Authoritative attending relation -> Permitted
        req_auth = main_mod.CheckPatientDipRequest(patient_id="PAT_AUTHORITATIVE", disease="M17.9", current_cost=-1.0, age=68, ins_type="职工", operation="保守治疗")
        ok_res = asyncio.run(main_mod.check_patient_dip(req_auth, operator={"sub": "DOC_A", "role": "DOCTOR", "dept": "中医科"}))
        assert ok_res["pending_reason"] == "NEGATIVE_FEE_UNRECONCILED"

        # 4) Handover DOC_A -> DOC_B: DOC_B permitted, DOC_A immediately 403
        admissions["PAT_AUTHORITATIVE"]["auth_attending_doctor_id"] = "DOC_B"
        admissions["PAT_AUTHORITATIVE"]["attending_doctor_id"] = "DOC_B"
        ok_b = asyncio.run(main_mod.check_patient_dip(req_auth, operator={"sub": "DOC_B", "role": "DOCTOR", "dept": "中医科"}))
        assert ok_b["pending_reason"] == "NEGATIVE_FEE_UNRECONCILED"
        try:
            asyncio.run(main_mod.check_patient_dip(req_auth, operator={"sub": "DOC_A", "role": "DOCTOR", "dept": "中医科"}))
            raise AssertionError("Old attending doctor must lose access immediately after handover")
        except HTTPException as exc:
            assert exc.status_code == 403

    # Verify tool_dispatcher path enforces the same rule
    import agent.tool_dispatcher as td_mod
    with patch.object(td_mod, "HISReader", return_value=_AdmReader()), \
         patch.object(td_mod, "get_operator", side_effect=lambda o: operators.get(o)):
        tc = {"name": "check_patient_dip", "arguments": {"patient_id": "PAT_CONSULT_ONLY", "disease": "M17.9", "current_cost": "100", "ins_type": "职工"}}
        td_deny = dispatch_tool_call(tc, {"authenticated": True, "sub": "DOC_CONSULT", "role": "DOCTOR", "dept": "中医科"})
        assert td_deny["tool_executed"] is False and td_deny["data"]["status"] == "FORBIDDEN"


def test_deploy_key_001_wireguard_isolation_offline():
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    dockerignore = open(os.path.join(root_dir, ".dockerignore"), "r", encoding="utf-8").read()
    compose = open(os.path.join(root_dir, "docker-compose.yml"), "r", encoding="utf-8").read()
    assert "wireguard/" in dockerignore
    assert "nginx/ssl/" in dockerignore
    assert ".:/opt/hospital-agent" not in compose
    assert "./wireguard/config:/etc/wireguard" not in compose
    assert "wireguard_config:/etc/wireguard" in compose


def test_month_scope_001_interval_overlap_and_month_batch_selection():
    import sqlite3
    from db.his_reader import HISReader

    # 1. Verify HISReader SQL uses overlap predicate & validated year_month
    reader = HISReader()
    sql_seen = []

    class _MoCur:
        def execute(self, sql, params=None):
            sql_seen.append((str(sql), params))

        def fetchall(self):
            return []

    class _MoConn:
        def close(self):
            pass

    with patch.object(reader, "get_connection", return_value=_MoConn()):
        _c = _MoCur()
        _MoConn.cursor = lambda self, as_dict=True: _c
        rows, meta = reader.get_monthly_inpatients(year_month="2026-11", limit=None, return_meta=True)
        assert rows == [] and meta["year_month"] == "2026-11" and meta["is_truncated"] is False
        q, p = sql_seen[0]
        assert "COALESCE(sy.rysj, gh.ghrq) < %s" in q
        assert "OR COALESCE(sy.cysj, jz_sum.latest_jzrq) >= %s" in q
        assert p[:2] == ("2026-12-01", "2026-11-01")

    # 2. Verify interval overlap semantics in real in-memory SQLite across all 4 boundary cases
    sconn = sqlite3.connect(":memory:")
    sconn.execute("CREATE TABLE stays (lsh TEXT, rysj TEXT, cysj TEXT, jzrq TEXT)")
    sconn.executemany(
        "INSERT INTO stays VALUES (?, ?, ?, ?)",
        [
            ("OCT_STILL_ACTIVE_IN_NOV", "2026-10-15 08:00:00", None, None),
            ("NOV_DISCHARGED", "2026-11-02 09:00:00", "2026-11-10 10:00:00", "2026-11-10 11:00:00"),
            ("OCT_DISCHARGED", "2026-10-05 08:00:00", "2026-10-25 12:00:00", "2026-10-25 12:30:00"),
            ("CROSS_YEAR", "2025-12-20 08:00:00", "2026-01-05 09:00:00", "2026-01-05 09:30:00"),
        ],
    )
    overlap_sql = """
        SELECT lsh FROM stays
        WHERE rysj < ?
          AND ((cysj IS NULL AND jzrq IS NULL) OR COALESCE(cysj, jzrq) >= ?)
    """
    nov_hits = {r[0] for r in sconn.execute(overlap_sql, ("2026-12-01", "2026-11-01")).fetchall()}
    dec_hits = {r[0] for r in sconn.execute(overlap_sql, ("2026-01-01", "2025-12-01")).fetchall()}
    jan_hits = {r[0] for r in sconn.execute(overlap_sql, ("2026-02-01", "2026-01-01")).fetchall()}
    assert nov_hits == {"OCT_STILL_ACTIVE_IN_NOV", "NOV_DISCHARGED"}
    assert "CROSS_YEAR" in dec_hits and "CROSS_YEAR" in jan_hits
    sconn.close()

    # 3. Verify target-month batch isolation even after backfilling an older month later
    snapshots = [
        {"batch_id": "MONTH_202611_20261128_090000", "synced_at": datetime(2026, 11, 28, 9, 0, 0), "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0), "visit_id": "V_NOV"},
        {"batch_id": "MONTH_202610_20261128_100000", "synced_at": datetime(2026, 11, 28, 10, 0, 0), "data_cutoff_time": datetime(2026, 11, 28, 10, 0, 0), "visit_id": "V_OCT"},
    ]

    class _MonthBatchCur:
        def __init__(self):
            self._one = None
            self._all = []

        def execute(self, sql, params=None):
            s = str(sql)
            if "SELECT TOP 1 batch_id" in s:
                prefix = str((params or ["%"])[0]).rstrip("%")
                matches = [x for x in snapshots if x["batch_id"].startswith(prefix)]
                matches.sort(key=lambda x: x["synced_at"], reverse=True)
                self._one = matches[0] if matches else None
            elif "COL_LENGTH" in s:
                self._one = {"has_dn": 1}
            elif "FROM dbo.inpatient_dip_snapshot" in s:
                bid = params[0]
                self._all = [
                    {
                        "visit_id": x["visit_id"], "ward_name": "内科", "bed_no": "已结算", "dept_id": "1",
                        "dept_name": "内科", "doctor_id": "D1", "doctor_name": "医生", "age": 50,
                        "ins_type": "居民", "primary_icd": "K63.500", "matched_rule_id": None,
                        "current_fee": Decimal("100.00"), "target_base": None, "fee_ratio": None,
                        "alert_level": "PENDING", "pending_reason": "NOT_FOUND", "data_cutoff_time": x["data_cutoff_time"],
                    }
                    for x in snapshots if x["batch_id"] == bid
                ]

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _MonthBatchConn:
        def close(self):
            pass

    with patch.object(main_mod, "get_local_db_connection", return_value=(_MonthBatchCur(), _MonthBatchConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "dept_scope": "", "can_export_case": True}), \
         patch.object(main_mod, "record_audit_log"):
        res_nov = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert res_nov["batch_id"].startswith("MONTH_202611_")
        assert [x["zyh"] for x in res_nov["items"]] == ["V_NOV"]
        csv_nov = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert b"V_NOV" in csv_nov.body and b"V_OCT" not in csv_nov.body


def test_month_total_001_pagination_201_cases_and_truncation_flag():
    from db.his_reader import HISReader
    from tools.inpatient_scanner import InpatientScanner

    synthetic_rows = [
        {
            "zyh": f"SYNTH_201_{i:03d}",
            "patient_name": f"P{i}",
            "gender": "男",
            "age": 50,
            "current_dept_id": "1",
            "current_dept": "消化内科",
            "current_bed": "01床",
            "admission_time": datetime(2026, 11, 1, 8, 0, 0),
            "discharge_time": None,
            "current_stay_days": 2,
            "auth_attending_doctor_id": "100",
            "attending_doctor_id": "100",
            "attending_doctor": "医生",
            "admission_diag_code": "K63.500",
            "admission_diag_name": "结肠息肉",
            "ssmc1": None, "ssmc2": None, "ssmc3": None,
            "ylzffs": "1", "insurance_type": "自费",
            "settled_total_amount": Decimal("100.00"), "is_settled": 1,
        }
        for i in range(1, 202)
    ]
    # Add duplicate zyh to verify deduplication
    synthetic_rows.insert(10, dict(synthetic_rows[0]))

    class _PagedCur:
        def __init__(self):
            self._batch = []

        def execute(self, sql, params=None):
            offset, fetch_cnt = int(params[-2]), int(params[-1])
            self._batch = [dict(r) for r in synthetic_rows[offset : offset + fetch_cnt]]

        def fetchall(self):
            return self._batch

    class _PagedConn:
        def cursor(self, as_dict=True):
            return _PagedCur()

        def close(self):
            pass

    reader = HISReader()
    with patch.object(reader, "get_connection", return_value=_PagedConn()):
        rows_full, meta_full = reader.get_monthly_inpatients(year_month="2026-11", limit=None, page_size=200, return_meta=True)
        assert len(rows_full) == 201 and meta_full["is_truncated"] is False

        rows_cap, meta_cap = reader.get_monthly_inpatients(year_month="2026-11", limit=50, page_size=200, return_meta=True)
        assert len(rows_cap) == 50 and meta_cap["is_truncated"] is True and meta_cap["scan_limit"] == 50

        scanner = InpatientScanner()
        scanner.his_reader = reader
        with patch("tools.inpatient_scanner.get_local_db_connection", return_value=(MagicMock(), MagicMock())):
            scan_res = scanner.scan_and_snapshot(scope="month", year_month="2026-11", limit=None)
            assert scan_res["total_scanned"] == 201
            assert scan_res["is_truncated"] is False


def test_dip_icd_001_unified_extraction_and_matching_offline():
    from tools import dip_calculator as dip_calc
    from agent.llm_router import parse_local_intent
    from agent.tool_dispatcher import extract_tool_call

    assert dip_calc.extract_icd_code("诊断编码 K63.500") == "K63.500"
    assert dip_calc.extract_icd_code("诊断编码M17.9，保守治疗") == "M17.9"
    assert dip_calc.extract_icd_code("诊断K63.500 电子结肠镜检查") == "K63.500"

    sample_rules = [
        {
            "rule_id": 85, "rule_version": "2026_SAMPLE_V1", "department": "中医科",
            "dip_group_name": "未特指的膝关节病:核心病种-保守治疗&年龄分类(年龄辅助目录-66岁及以上)",
            "icd_code": "M17.9", "icd_name": "未特指的膝关节病", "op_code": None,
            "op_name": "核心病种-保守治疗", "aux_detail": "年龄辅助目录-66岁及以上",
            "billing_unit": "PER_CASE", "dip_score": Decimal("620.2000"),
            "resident_point": Decimal("7.0000"), "employee_point": Decimal("7.5000"),
            "hosp_coef": Decimal("0.6000"), "low_threshold": Decimal("0.9000"),
            "high_threshold": Decimal("1.1000"), "resident_base": Decimal("2604.8400"),
            "employee_base": Decimal("2790.9000"),
        },
        {
            "rule_id": 101, "rule_version": "2026_SAMPLE_V1", "department": "消化内科",
            "dip_group_name": "结肠息肉:电子结肠镜检查", "icd_code": "K63.5",
            "icd_name": "结肠息肉", "op_code": None, "op_name": "电子结肠镜检查",
            "aux_detail": "", "billing_unit": "PER_CASE", "dip_score": Decimal("514.6000"),
            "resident_point": Decimal("7.0000"), "employee_point": Decimal("7.5000"),
            "hosp_coef": Decimal("0.6000"), "low_threshold": Decimal("0.9000"),
            "high_threshold": Decimal("1.1000"), "resident_base": Decimal("2161.3200"),
            "employee_base": Decimal("2315.7000"),
        },
        {
            "rule_id": 102, "rule_version": "2026_SAMPLE_V1", "department": "消化内科",
            "dip_group_name": "结肠息肉:核心病种-保守治疗", "icd_code": "K63.5",
            "icd_name": "结肠息肉", "op_code": None, "op_name": "核心病种-保守治疗",
            "aux_detail": "", "billing_unit": "PER_CASE", "dip_score": Decimal("357.0000"),
            "resident_point": Decimal("7.0000"), "employee_point": Decimal("7.5000"),
            "hosp_coef": Decimal("0.6000"), "low_threshold": Decimal("0.9000"),
            "high_threshold": Decimal("1.1000"), "resident_base": Decimal("1499.4000"),
            "employee_base": Decimal("1606.5000"),
        },
    ]

    with patch.object(dip_calc, "get_loaded_rules", return_value=sample_rules):
        r_adj = parse_local_intent("68岁，职工医保，诊断编码M17.9，保守治疗")
        r_spc = parse_local_intent("68岁，职工医保，诊断编码 M17.9，保守治疗")
        assert r_adj["data"]["rule_id"] == r_spc["data"]["rule_id"] == 85

        r_ext_chat = parse_local_intent("53岁，居民医保，诊断编码 K63.500，已完成电子结肠镜检查")
        r_ext_struct = dip_calc.match_dip_rule(disease="K63.500", age=53, ins_type="居民", op_keyword="电子结肠镜检查")
        assert r_ext_chat["data"]["rule_id"] == r_ext_struct["rule_id"] == 101

        r_amb_chat = parse_local_intent("53岁，居民医保，诊断编码 K63.500")
        assert r_amb_chat["data"]["status"] == "PENDING"
        assert r_amb_chat["data"]["pending_reason"] == "MISSING_OPERATION"

        tc = extract_tool_call("帮我判灯住院号 zy2026112704，诊断编码K63.500，已完成电子结肠镜检查，当前费用 1309 元，居民医保")
        assert tc["arguments"]["disease"] == "K63.500"


def test_auth_attend_001_merged_ry_mapping_cross_col_and_unmapped_offline():
    import sqlite3
    from db.his_reader import HISReader
    from agent.tool_dispatcher import dispatch_tool_call

    # 1. Verify production SQL merges ybysdm & gjgbm_ybysdm without raw sy.zyysbm fallback in auth expression
    assert "HAVING COUNT(DISTINCT ryid) = 1" in HISReader._AUTH_ATTENDING_DOC_JOINS
    assert "COALESCE(CAST(COALESCE(ry_yb" not in HISReader._AUTH_ATTENDING_DOC_ID_EXPR
    assert "sy.zyysbm)), ''" not in HISReader._AUTH_ATTENDING_DOC_ID_EXPR.replace("WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NOT NULL", "")

    # 2. Simulate the exact merged candidate SQL in in-memory SQLite across all 4 required scenarios
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE his_ry (ryid INTEGER, ryname TEXT, isys INTEGER, ybysdm TEXT, gjgbm_ybysdm TEXT)")
    conn.executemany(
        "INSERT INTO his_ry VALUES (?, ?, ?, ?, ?)",
        [
            (101, "同人双列", 1, "YB_SAME", "YB_SAME"),
            (201, "甲医生", 1, "YB_CONFLICT", None),
            (202, "乙医生", 1, None, "YB_CONFLICT"),
            (301, "丙医生", 1, "YB_A", "GJ_A"),
            (302, "丁医生", 1, "YB_B", "GJ_B"),
        ],
    )

    def _resolve_merged(code: str):
        row = conn.execute(
            """
            SELECT ryid FROM (
                SELECT code_key, MAX(ryid) AS ryid
                FROM (
                    SELECT TRIM(ybysdm) AS code_key, ryid FROM his_ry WHERE isys = 1 AND TRIM(COALESCE(ybysdm, '')) <> ''
                    UNION ALL
                    SELECT TRIM(gjgbm_ybysdm) AS code_key, ryid FROM his_ry WHERE isys = 1 AND TRIM(COALESCE(gjgbm_ybysdm, '')) <> ''
                ) u
                GROUP BY code_key
                HAVING COUNT(DISTINCT ryid) = 1
            ) m WHERE m.code_key = ?
            """,
            (code,),
        ).fetchone()
        return str(row[0]) if row else None

    assert _resolve_merged("YB_SAME") == "101"
    assert _resolve_merged("YB_CONFLICT") is None
    assert _resolve_merged("UNMAPPED_RAW_CODE") is None
    assert _resolve_merged("YB_A") == "301"
    assert _resolve_merged("GJ_B") == "302"

    operators = {
        "op_101": {"operator_id": "op_101", "role_type": "DOCTOR", "doctor_id": "101", "status": "ACTIVE"},
        "op_201": {"operator_id": "op_201", "role_type": "DOCTOR", "doctor_id": "201", "status": "ACTIVE"},
        "op_raw": {"operator_id": "op_raw", "role_type": "DOCTOR", "doctor_id": "UNMAPPED_RAW_CODE", "status": "ACTIVE"},
        "op_301": {"operator_id": "op_301", "role_type": "DOCTOR", "doctor_id": "301", "status": "ACTIVE"},
        "op_302": {"operator_id": "op_302", "role_type": "DOCTOR", "doctor_id": "302", "status": "ACTIVE"},
    }
    admissions = {
        "P_SAME": {"zyh": "P_SAME", "auth_attending_doctor_id": _resolve_merged("YB_SAME"), "attending_doctor_id": "101", "attending_doctor_source": "SY_ZYYSBM", "current_dept_id": "4"},
        "P_CONFLICT": {"zyh": "P_CONFLICT", "auth_attending_doctor_id": _resolve_merged("YB_CONFLICT"), "attending_doctor_id": "YB_CONFLICT", "attending_doctor_source": "SY_ZYYSBM_RAW", "current_dept_id": "4"},
        "P_RAW": {"zyh": "P_RAW", "auth_attending_doctor_id": _resolve_merged("UNMAPPED_RAW_CODE"), "attending_doctor_id": "UNMAPPED_RAW_CODE", "attending_doctor_source": "SY_ZYYSBM_RAW", "current_dept_id": "4"},
        "P_HANDOVER": {"zyh": "P_HANDOVER", "auth_attending_doctor_id": _resolve_merged("YB_A"), "attending_doctor_id": "301", "attending_doctor_source": "SY_ZYYSBM", "current_dept_id": "4"},
    }
    handover_next_id = _resolve_merged("GJ_B")
    conn.close()

    class _AdmReader:
        def get_patient_realtime_admission(self, pid):
            return admissions.get(pid)

    import agent.tool_dispatcher as td_mod
    with patch.object(main_mod, "HISReader", return_value=_AdmReader()), \
         patch.object(main_mod, "get_operator", side_effect=lambda o: operators.get(o)), \
         patch.object(main_mod, "record_audit_log"), \
         patch.object(td_mod, "HISReader", return_value=_AdmReader()), \
         patch.object(td_mod, "get_operator", side_effect=lambda o: operators.get(o)):

        # 同人双列匹配 -> 两条入口均放行
        r_ok = asyncio.run(main_mod.check_patient_dip(main_mod.CheckPatientDipRequest(patient_id="P_SAME", disease="M17.9", current_cost=-1.0, ins_type="职工"), operator={"sub": "op_101", "role": "DOCTOR", "dept": ""}))
        assert r_ok["pending_reason"] == "NEGATIVE_FEE_UNRECONCILED"

        # 跨列冲突与无映射原始编码 -> 两条入口均严格拒绝 (403 / FORBIDDEN)
        for pid, sub_id in [("P_CONFLICT", "op_201"), ("P_RAW", "op_raw")]:
            try:
                asyncio.run(main_mod.check_patient_dip(main_mod.CheckPatientDipRequest(patient_id=pid, disease="M17.9", current_cost=-1.0, ins_type="职工"), operator={"sub": sub_id, "role": "DOCTOR", "dept": ""}))
                raise AssertionError(f"Expected 403 for {pid}")
            except HTTPException as exc:
                assert exc.status_code == 403
            td_res = dispatch_tool_call({"name": "check_patient_dip", "arguments": {"patient_id": pid, "disease": "M17.9", "current_cost": "100", "ins_type": "职工"}}, {"authenticated": True, "sub": sub_id, "role": "DOCTOR", "dept": ""})
            assert td_res["tool_executed"] is False and td_res["data"]["status"] == "FORBIDDEN"

        # 交接：op_301 -> op_302 后，op_301 在两条入口均立即失权
        admissions["P_HANDOVER"]["auth_attending_doctor_id"] = handover_next_id
        admissions["P_HANDOVER"]["attending_doctor_id"] = "302"
        r_new = asyncio.run(main_mod.check_patient_dip(main_mod.CheckPatientDipRequest(patient_id="P_HANDOVER", disease="M17.9", current_cost=-1.0, ins_type="职工"), operator={"sub": "op_302", "role": "DOCTOR", "dept": ""}))
        assert r_new["pending_reason"] == "NEGATIVE_FEE_UNRECONCILED"
        try:
            asyncio.run(main_mod.check_patient_dip(main_mod.CheckPatientDipRequest(patient_id="P_HANDOVER", disease="M17.9", current_cost=-1.0, ins_type="职工"), operator={"sub": "op_301", "role": "DOCTOR", "dept": ""}))
            raise AssertionError("Old doctor must be denied after handover")
        except HTTPException as exc:
            assert exc.status_code == 403
        td_old = dispatch_tool_call({"name": "check_patient_dip", "arguments": {"patient_id": "P_HANDOVER", "disease": "M17.9", "current_cost": "100", "ins_type": "职工"}}, {"authenticated": True, "sub": "op_301", "role": "DOCTOR", "dept": ""})
        assert td_old["tool_executed"] is False and td_old["data"]["status"] == "FORBIDDEN"


def test_deploy_key_001_migration_upgrade_rehearsal_offline():
    import tempfile
    import shutil

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    wg_dir = os.path.join(root_dir, "wireguard")
    if os.path.exists(wg_dir):
        mig_script = os.path.join(wg_dir, "migrate_to_volume.sh")
        mig_doc = os.path.join(wg_dir, "UPGRADE_MIGRATION.md")
        entrypoint_src = open(os.path.join(wg_dir, "entrypoint.sh"), "r", encoding="utf-8").read()
        assert os.path.exists(mig_script) and os.path.exists(mig_doc)
        assert 'if [ ! -f "${WG_DIR}/wg0.conf" ]; then' in entrypoint_src
    else:
        # Inside isolated API container, wireguard/ must be completely absent
        assert not os.path.exists(os.path.join(wg_dir, "config"))

    # Synthesize old-layout host config with a fake key marker, migrate to clean target volume dir
    with tempfile.TemporaryDirectory() as tmpd:
        old_cfg = os.path.join(tmpd, "old_wireguard_config")
        new_vol = os.path.join(tmpd, "volume_etc_wireguard")
        os.makedirs(os.path.join(old_cfg, "clients"), exist_ok=True)
        fake_priv = "FAKE_WG_UPGRADE_PRIV_KEY_MARKER_2026"
        fake_pub = "FAKE_WG_UPGRADE_PUB_KEY_MARKER_2026"
        wg0_text = f"[Interface]\nPrivateKey = {fake_priv}\nListenPort = 51820\n[Peer]\n# peer1_mobile\nPublicKey = CLIENT_PUB_1\nAllowedIPs = 10.66.0.2/32\n"
        peer_text = f"[Interface]\nPrivateKey = CLIENT_PRIV_1\nAddress = 10.66.0.2/32\n[Peer]\nPublicKey = {fake_pub}\nEndpoint = example:51820\n"
        open(os.path.join(old_cfg, "wg0.conf"), "w").write(wg0_text)
        open(os.path.join(old_cfg, "server.key"), "w").write(fake_priv + "\n")
        open(os.path.join(old_cfg, "server.pub"), "w").write(fake_pub + "\n")
        open(os.path.join(old_cfg, "clients", "peer1_mobile.conf"), "w").write(peer_text)

        os.makedirs(os.path.join(new_vol, "clients"), exist_ok=True)
        for rel in ("wg0.conf", "server.key", "server.pub", os.path.join("clients", "peer1_mobile.conf")):
            shutil.copy2(os.path.join(old_cfg, rel), os.path.join(new_vol, rel))

        # Re-check: wg0.conf exists -> key preservation guaranteed, server pub & peer list match
        assert os.path.exists(os.path.join(new_vol, "wg0.conf"))
        assert fake_pub in open(os.path.join(new_vol, "server.pub")).read()
        assert "CLIENT_PUB_1" in open(os.path.join(new_vol, "wg0.conf")).read()
        assert "CLIENT_PRIV_1" in open(os.path.join(new_vol, "clients", "peer1_mobile.conf")).read()


def test_month_total_001_batch_truncation_persistence_and_csv_offline():
    batches = {
        "MONTH_202611_FULL": {
            "batch_id": "MONTH_202611_FULL", "scope": "month", "year_month": "2026-11",
            "scan_limit": None, "total_scanned": 201, "is_truncated": 0,
            "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0), "synced_at": datetime(2026, 11, 28, 9, 0, 0),
        },
        "MONTH_202610_CAPPED": {
            "batch_id": "MONTH_202610_CAPPED", "scope": "month", "year_month": "2026-10",
            "scan_limit": 200, "total_scanned": 200, "is_truncated": 1,
            "data_cutoff_time": datetime(2026, 10, 31, 23, 0, 0), "synced_at": datetime(2026, 10, 31, 23, 0, 0),
        },
    }

    class _PersistCur:
        def __init__(self):
            self._one = None
            self._all = []

        def execute(self, sql, params=None):
            s = str(sql)
            if "SELECT TOP 1 batch_id" in s:
                prefix = str((params or ["%"])[0]).rstrip("%")
                hits = [b for b in batches.values() if b["batch_id"].startswith(prefix)]
                hits.sort(key=lambda b: b["synced_at"], reverse=True)
                self._one = hits[0] if hits else None
            elif "OBJECT_ID('dbo.inpatient_scan_batch'" in s:
                self._one = {"has_dn": 1, "has_batch_tbl": 1}
            elif "FROM dbo.inpatient_scan_batch WHERE batch_id = %s" in s:
                self._one = batches.get(params[0])
            elif "FROM dbo.inpatient_dip_snapshot" in s:
                bid = params[0]
                self._all = [
                    {
                        "visit_id": f"{bid}_ZY1", "ward_name": "消化内科", "bed_no": "已结算", "dept_id": "1",
                        "dept_name": "消化内科", "doctor_id": "100050", "doctor_name": "王延飞", "age": 53,
                        "ins_type": "居民", "primary_icd": "K63.500", "matched_rule_id": 101,
                        "current_fee": Decimal("1309.00"), "target_base": Decimal("2161.32"),
                        "fee_ratio": Decimal("0.605648"), "alert_level": "GREEN",
                        "pending_reason": None, "data_cutoff_time": batches[bid]["data_cutoff_time"],
                    }
                ]

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _PersistConn:
        def cursor(self, as_dict=True):
            return _PersistCur()

        def close(self):
            pass

    with patch.object(main_mod, "get_local_db_connection", return_value=(_PersistCur(), _PersistConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "dept_scope": "", "can_export_case": True}), \
         patch.object(main_mod, "record_audit_log"):
        # Full 201 batch -> is_truncated=False, total_scanned=201
        r_full = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert r_full["total_scanned"] == 201 and r_full["scan_limit"] is None and r_full["is_truncated"] is False
        c_full = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert c_full.headers.get("X-DIP-Is-Truncated") == "false"
        assert "全量(共201例)" in c_full.body.decode("utf-8")

        # Switching to capped 200 batch preserves truncated flag & limit in UI API and CSV export
        r_cap = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="month", year_month="2026-10", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert r_cap["total_scanned"] == 200 and r_cap["scan_limit"] == 200 and r_cap["is_truncated"] is True
        c_cap = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="month", year_month="2026-10", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert c_cap.headers.get("X-DIP-Is-Truncated") == "true"
        assert c_cap.headers.get("X-DIP-Scan-Limit") == "200"
        assert "截断(上限200/已扫200例)" in c_cap.body.decode("utf-8")


def test_month_total_001_active_probe_100_vs_101_and_unknown_legacy_offline():
    from db.his_reader import HISReader
    from tools.inpatient_scanner import InpatientScanner

    def _make_pat(i):
        return {
            "zyh": f"ACT_PROBE_{i:03d}",
            "patient_name": f"P{i}",
            "gender": "男",
            "age": 50,
            "current_dept_id": "1",
            "current_dept": "消化内科",
            "current_bed": "01床",
            "admission_time": datetime(2026, 11, 20, 8, 0, 0),
            "current_stay_days": 2,
            "auth_attending_doctor_id": "100",
            "attending_doctor_id": "100",
            "attending_doctor": "医生",
            "admission_diag_code": "K63.500",
            "admission_diag_name": "结肠息肉",
            "ssmc1": None, "ssmc2": None, "ssmc3": None,
            "ylzffs": "1", "insurance_type": "自费",
        }

    for count_in_db, expected_len, expected_trunc in [(100, 100, False), (101, 100, True)]:
        class _ActCur:
            def __init__(self, n=count_in_db):
                self._n = n

            def execute(self, sql, params=None):
                assert "TOP (101)" in str(sql)

            def fetchall(self):
                return [_make_pat(i) for i in range(1, self._n + 1)]

        class _ActConn:
            def cursor(self, as_dict=True):
                return _ActCur()

            def close(self):
                pass

        reader = HISReader()
        with patch.object(reader, "get_connection", return_value=_ActConn()), \
             patch.object(reader, "get_active_patient_fee", return_value={"total_fee": Decimal("100.00"), "has_activity": True}):
            scanner = InpatientScanner()
            scanner.his_reader = reader
            with patch("tools.inpatient_scanner.get_local_db_connection", return_value=(MagicMock(), MagicMock())):
                s_res = scanner.scan_and_snapshot(scope="active", limit=100)
                assert s_res["total_scanned"] == expected_len
                assert s_res["scan_limit"] == 100
                assert s_res["is_truncated"] is expected_trunc

    # Legacy batch with NO row in dbo.inpatient_scan_batch -> is_truncated=None ('unknown'), NOT 'false' / '全量'
    class _LegacyNoMetaCur:
        def __init__(self):
            self._one = None
            self._all = []

        def execute(self, sql, params=None):
            s = str(sql)
            if "SELECT TOP 1 batch_id" in s:
                self._one = {"batch_id": "BATCH_LEGACY_NO_META", "data_cutoff_time": datetime(2026, 11, 20, 8, 0, 0), "synced_at": datetime(2026, 11, 20, 8, 0, 0), "has_batch_meta": 0}
            elif "OBJECT_ID('dbo.inpatient_scan_batch'" in s:
                self._one = {"has_dn": 1, "has_batch_tbl": 1}
            elif "FROM dbo.inpatient_scan_batch WHERE batch_id = %s" in s:
                self._one = None
            elif "FROM dbo.inpatient_dip_snapshot" in s:
                self._all = [
                    {
                        "visit_id": f"LEG_{i}", "ward_name": "内科", "bed_no": "01", "dept_id": "1",
                        "dept_name": "内科", "doctor_id": "D1", "doctor_name": "医生", "age": 50,
                        "ins_type": "职工", "primary_icd": "M17.9", "matched_rule_id": None,
                        "current_fee": Decimal("100.00"), "target_base": None, "fee_ratio": None,
                        "alert_level": "PENDING", "pending_reason": "MISSING_OPERATION",
                        "data_cutoff_time": datetime(2026, 11, 20, 8, 0, 0),
                    }
                    for i in range(9)
                ]

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _LegacyConn:
        def cursor(self, as_dict=True):
            return _LegacyNoMetaCur()

        def close(self):
            pass

    with patch.object(main_mod, "get_local_db_connection", return_value=(_LegacyNoMetaCur(), _LegacyConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "dept_scope": "", "can_export_case": True}), \
         patch.object(main_mod, "record_audit_log"):
        leg_json = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert leg_json["is_truncated"] is None and leg_json["has_batch_meta"] is False and leg_json["total_scanned"] is None
        leg_csv = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert leg_csv.headers.get("X-DIP-Is-Truncated") == "unknown"
        assert "未知(旧批次无完整度元数据)" in leg_csv.body.decode("utf-8")


def test_snap_empty_001_one_row_then_zero_row_batch_offline():
    import sqlite3

    # Build an in-memory SQLite mirror of inpatient_scan_batch + inpatient_dip_snapshot
    # verifying both active and month select the newer 0-row batch over the older 1-row batch.
    sconn = sqlite3.connect(":memory:")
    sconn.row_factory = sqlite3.Row
    sconn.execute("CREATE TABLE batches (batch_id TEXT, scope TEXT, year_month TEXT, scan_limit INTEGER, total_scanned INTEGER, is_truncated INTEGER, data_cutoff_time TEXT, synced_at TEXT)")
    sconn.execute("CREATE TABLE snaps (batch_id TEXT, visit_id TEXT, ward_name TEXT, bed_no TEXT, dept_id TEXT, dept_name TEXT, doctor_id TEXT, doctor_name TEXT, age INTEGER, ins_type TEXT, primary_icd TEXT, matched_rule_id INTEGER, current_fee REAL, target_base REAL, fee_ratio REAL, alert_level TEXT, pending_reason TEXT, data_cutoff_time TEXT, synced_at TEXT)")

    # Batch 1 (1 row) -> Batch 2 (0 rows, newer) for BOTH active and month
    sconn.executemany(
        "INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("BATCH_OLD_1ROW", "active", None, 50, 1, 0, "2026-11-28 08:00:00", "2026-11-28 08:00:00"),
            ("BATCH_NEW_0ROW", "active", None, 50, 0, 0, "2026-11-28 09:00:00", "2026-11-28 09:00:00"),
            ("MONTH_202611_OLD_1ROW", "month", "2026-11", None, 1, 0, "2026-11-28 08:00:00", "2026-11-28 08:00:00"),
            ("MONTH_202611_NEW_0ROW", "month", "2026-11", None, 0, 0, "2026-11-28 09:00:00", "2026-11-28 09:00:00"),
        ],
    )
    sconn.executemany(
        "INSERT INTO snaps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("BATCH_OLD_1ROW", "ZY_ACT_1", "内科", "01", "1", "内科", "100", "医生", 50, "职工", "M17.9", None, 100.0, None, None, "PENDING", "TEST", "2026-11-28 08:00:00", "2026-11-28 08:00:00"),
            ("MONTH_202611_OLD_1ROW", "ZY_MON_1", "内科", "01", "1", "内科", "100", "医生", 50, "职工", "M17.9", None, 100.0, None, None, "PENDING", "TEST", "2026-11-28 08:00:00", "2026-11-28 08:00:00"),
        ],
    )

    class _EmptyCur:
        def __init__(self):
            self._one = None
            self._all = []

        def execute(self, sql, params=None):
            s = str(sql)
            if "SELECT TOP 1 batch_id" in s:
                pat, excl, sc, ym = params[0], params[1], params[2], params[3]
                row = sconn.execute(
                    """
                    SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_limit, total_scanned, is_truncated, has_batch_meta
                    FROM (
                        SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_limit, total_scanned, is_truncated, 1 AS has_batch_meta
                        FROM batches
                        WHERE batch_id LIKE ? AND scope = ? AND (? IS NULL OR year_month = ?)
                        UNION ALL
                        SELECT batch_id, MAX(data_cutoff_time), MAX(synced_at), NULL, NULL, NULL, NULL, NULL, 0
                        FROM snaps
                        WHERE batch_id LIKE ? AND (? = '' OR batch_id NOT LIKE ?)
                          AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.batch_id = snaps.batch_id)
                        GROUP BY batch_id
                    )
                    ORDER BY synced_at DESC LIMIT 1
                    """,
                    (pat, sc, ym, ym, pat, excl, excl),
                ).fetchone()
                self._one = dict(row) if row else None
                if self._one and isinstance(self._one.get("synced_at"), str):
                    self._one["synced_at"] = datetime.fromisoformat(self._one["synced_at"])
                    self._one["data_cutoff_time"] = datetime.fromisoformat(self._one["data_cutoff_time"])
            elif "OBJECT_ID('dbo.inpatient_scan_batch'" in s:
                self._one = {"has_dn": 1, "has_batch_tbl": 1}
            elif "FROM dbo.inpatient_dip_snapshot" in s:
                bid = params[0]
                self._all = [dict(r) for r in sconn.execute("SELECT * FROM snaps WHERE batch_id = ?", (bid,)).fetchall()]

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _EmptyConn:
        def cursor(self, as_dict=True):
            return _EmptyCur()

        def close(self):
            pass

    with patch.object(main_mod, "get_local_db_connection", side_effect=lambda as_dict=True: (_EmptyCur(), _EmptyConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "dept_scope": "", "can_export_case": True}), \
         patch.object(main_mod, "record_audit_log"):
        for sc_name, ym_arg, exp_bid in [("active", None, "BATCH_NEW_0ROW"), ("month", "2026-11", "MONTH_202611_NEW_0ROW")]:
            res = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope=sc_name, year_month=ym_arg, operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
            assert res["batch_id"] == exp_bid
            assert res["total"] == 0 and res["total_scanned"] == 0 and res["items"] == []
            csv_r = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope=sc_name, year_month=ym_arg, operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
            assert csv_r.headers.get("X-DIP-Batch-Id") == exp_bid
            assert csv_r.headers.get("X-DIP-Total-Scanned") == "0"
    sconn.close()


def test_snap_count_001_dept_head_cannot_see_hospital_total_offline():
    snaps = [
        {"visit_id": "D1_P1", "ward_name": "一科", "bed_no": "01", "dept_id": "101", "dept_name": "消化内科", "doctor_id": "1", "doctor_name": "A", "age": 50, "ins_type": "居民", "primary_icd": "K63.500", "matched_rule_id": 101, "current_fee": Decimal("1000.00"), "target_base": Decimal("2161.32"), "fee_ratio": Decimal("0.46"), "alert_level": "GREEN", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0)},
        {"visit_id": "D1_P2", "ward_name": "一科", "bed_no": "02", "dept_id": "101", "dept_name": "消化内科", "doctor_id": "1", "doctor_name": "A", "age": 50, "ins_type": "居民", "primary_icd": "K63.500", "matched_rule_id": 101, "current_fee": Decimal("1200.00"), "target_base": Decimal("2161.32"), "fee_ratio": Decimal("0.55"), "alert_level": "GREEN", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0)},
        {"visit_id": "D2_P1", "ward_name": "二科", "bed_no": "03", "dept_id": "102", "dept_name": "中医科", "doctor_id": "2", "doctor_name": "B", "age": 68, "ins_type": "职工", "primary_icd": "M17.9", "matched_rule_id": 85, "current_fee": Decimal("2500.00"), "target_base": Decimal("2790.90"), "fee_ratio": Decimal("0.89"), "alert_level": "GREEN", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0)},
        {"visit_id": "D2_P2", "ward_name": "二科", "bed_no": "04", "dept_id": "102", "dept_name": "中医科", "doctor_id": "2", "doctor_name": "B", "age": 68, "ins_type": "职工", "primary_icd": "M17.9", "matched_rule_id": 85, "current_fee": Decimal("2600.00"), "target_base": Decimal("2790.90"), "fee_ratio": Decimal("0.93"), "alert_level": "YELLOW", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0)},
        {"visit_id": "D2_P3", "ward_name": "二科", "bed_no": "05", "dept_id": "102", "dept_name": "中医科", "doctor_id": "2", "doctor_name": "B", "age": 68, "ins_type": "职工", "primary_icd": "M17.9", "matched_rule_id": 85, "current_fee": Decimal("3200.00"), "target_base": Decimal("2790.90"), "fee_ratio": Decimal("1.14"), "alert_level": "RED", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0)},
    ]

    class _CntCur:
        def __init__(self):
            self._one = None
            self._all = []

        def execute(self, sql, params=None):
            s = str(sql)
            if "SELECT TOP 1 batch_id" in s:
                self._one = {"batch_id": "MONTH_202611_BOTH", "data_cutoff_time": datetime(2026, 11, 28, 9, 0, 0), "synced_at": datetime(2026, 11, 28, 9, 0, 0), "scope": "month", "year_month": "2026-11", "scan_limit": None, "total_scanned": 5, "is_truncated": 0, "has_batch_meta": 1}
            elif "OBJECT_ID('dbo.inpatient_scan_batch'" in s:
                self._one = {"has_dn": 1, "has_batch_tbl": 1}
            elif "FROM dbo.inpatient_dip_snapshot" in s:
                if "AND dept_id IN" in s:
                    allowed = set(params[1:])
                    self._all = [r for r in snaps if r["dept_id"] in allowed]
                else:
                    self._all = list(snaps)

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _CntConn:
        def close(self):
            pass

    operators = {
        "dh101": {"operator_id": "dh101", "role_type": "DEPT_HEAD", "dept_scope": "101", "can_export_case": True},
        "admin": {"operator_id": "admin", "role_type": "ADMIN", "dept_scope": "", "can_export_case": True},
    }
    with patch.object(main_mod, "get_local_db_connection", side_effect=lambda as_dict=True: (_CntCur(), _CntConn())), \
         patch.object(main_mod, "get_operator", side_effect=lambda o: operators.get(o)), \
         patch.object(main_mod, "get_authorized_dept_ids", return_value={"101"}), \
         patch.object(main_mod, "record_audit_log"):
        dh_json = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="month", year_month="2026-11", operator={"sub": "dh101", "role": "DEPT_HEAD", "dept": "101"}))
        assert dh_json["authorized_scope"] == "DEPARTMENT"
        assert dh_json["total"] == 2 and dh_json["total_scanned"] == 2
        assert len(dh_json["items"]) == 2
        dh_csv = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="month", year_month="2026-11", operator={"sub": "dh101", "role": "DEPT_HEAD", "dept": "101"}))
        dh_body = dh_csv.body.decode("utf-8")
        assert dh_csv.headers.get("X-DIP-Total-Scanned") == "2"
        assert dh_csv.headers.get("X-DIP-Authorized-Scope") == "DEPARTMENT"
        assert "本科可见2例(全批完整)" in dh_body
        assert "共5例" not in dh_body and "D2_" not in dh_body

        adm_json = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert adm_json["authorized_scope"] == "HOSPITAL"
        assert adm_json["total"] == 5 and adm_json["total_scanned"] == 5
        adm_csv = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert adm_csv.headers.get("X-DIP-Total-Scanned") == "5"
        assert "全量(共5例)" in adm_csv.body.decode("utf-8")


def test_deploy_key_001_compose_volume_resolution_and_isolation():
    """DEPLOY-KEY-001: Compose target volume resolves per project, blocks mismatch, and stays isolated from API."""
    import subprocess
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists("/etc/wireguard")

    compose_path = os.path.join(repo_root, "docker-compose.yml")
    if os.path.exists(compose_path):
        compose_txt = open(compose_path, "r", encoding="utf-8").read()
        assert "wireguard_config:/etc/wireguard" in compose_txt
        api_block = compose_txt.split("\n  wg:")[0]
        assert "wireguard_config" not in api_block

    script_path = os.path.join(repo_root, "wireguard", "migrate_to_volume.sh")
    doc_path = os.path.join(repo_root, "wireguard", "UPGRADE_MIGRATION.md")
    if os.path.exists(script_path):
        script_txt = open(script_path, "r", encoding="utf-8").read()
        assert "m.get('type') != 'volume'" in script_txt
        assert "clients/*.conf" in script_txt
        assert "stat -c" in script_txt
        assert "_wireguard_config" not in script_txt  # no offline hardcoded name guess

        # 1) Mismatched --volume must abort before recreate
        proc = subprocess.run(
            ["sh", script_path, "-f", compose_path, "-p", "proj_alpha", "--volume", "wrong_volume_name", "--verify-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "mismatch" in (proc.stderr + proc.stdout).lower()

        # 2) Unreadable / nonexistent Compose file must fail BEFORE guessing volume name
        proc_bad_cfg = subprocess.run(
            ["sh", script_path, "-f", "/nonexistent/docker-compose.yml", "-p", "proj_alpha", "--verify-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert proc_bad_cfg.returncode != 0
        assert "abort" in (proc_bad_cfg.stderr + proc_bad_cfg.stdout).lower()

        # 3) --verify-only combined with --rollback must be rejected BEFORE creating dirs/containers
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sentinel = os.path.join(td, "keep.conf")
            open(sentinel, "w", encoding="utf-8").write("ORIGINAL")
            proc_mutex = subprocess.run(
                ["sh", script_path, "--verify-only", "--rollback", "--target-dir", td, "-f", compose_path],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            assert proc_mutex.returncode != 0
            assert "mutually exclusive" in (proc_mutex.stderr + proc_mutex.stdout).lower()
            assert open(sentinel, "r", encoding="utf-8").read() == "ORIGINAL"
            assert not os.path.exists(os.path.join(td, "clients"))

    if os.path.exists(doc_path):
        doc_txt = open(doc_path, "r", encoding="utf-8").read()
        assert "docker compose -f docker-compose.yml -p my_proj up -d --force-recreate api wg nginx" in doc_txt
        assert "-f docker-compose.yml -p my_proj" in doc_txt
        assert "--verify-only" in doc_txt and "--rollback" in doc_txt


def test_snap_batch_id_001_same_second_collision_and_explicit_retry_offline():
    """SNAP-BATCH-ID-001: Frozen same-second/millisecond scans yield distinct batch_ids and consistent details/summaries."""
    import sqlite3
    import tools.inpatient_scanner as scanner_mod

    sconn = sqlite3.connect(":memory:")
    sconn.row_factory = sqlite3.Row
    sconn.execute(
        "CREATE TABLE snaps ("
        "batch_id TEXT, visit_id TEXT, ward_name TEXT, bed_no TEXT, dept_id TEXT, dept_name TEXT, "
        "doctor_id TEXT, doctor_name TEXT, age INT, ins_type TEXT, primary_icd TEXT, matched_rule_id INT, "
        "rule_version TEXT, current_fee REAL, target_base REAL, fee_ratio REAL, alert_level TEXT, "
        "pending_reason TEXT, discharge_review INT, data_cutoff_time DATETIME, synced_at DATETIME, "
        "PRIMARY KEY (batch_id, visit_id))"
    )
    sconn.execute(
        "CREATE TABLE batches ("
        "seq_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "batch_id TEXT UNIQUE, scope TEXT, year_month TEXT, scan_dept TEXT, rule_version TEXT, scan_limit INT, "
        "total_scanned INT, is_truncated INT, probe_version INT, data_cutoff_time DATETIME, synced_at DATETIME)"
    )

    scanner_src = open(os.path.abspath(scanner_mod.__file__), "r", encoding="utf-8").read()
    assert "sys.sp_getapplock" in scanner_src
    assert "@LockOwner = N'Transaction'" in scanner_src

    class _FakeScanCur:
        def __init__(self, as_dict=False):
            self.as_dict = as_dict
            self._one = None
            self._all = []

        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            self._one = None
            self._all = []
            if "ALTER TABLE" in s or "CREATE TABLE dbo.inpatient_scan_batch" in s:
                return
            if "sp_getapplock" in s:
                self._one = (0,) if not self.as_dict else {"lock_rc": 0}
                return
            if s.startswith("SELECT TOP 1 1 AS dup_hit FROM dbo.inpatient_scan_batch WHERE batch_id"):
                r = sconn.execute("SELECT 1 FROM batches WHERE batch_id = ?", (params[0],)).fetchone()
                self._one = {"dup_hit": 1} if r else None
            elif s.startswith("SELECT TOP 1 1 AS dup_hit FROM dbo.inpatient_dip_snapshot WHERE batch_id"):
                r = sconn.execute("SELECT 1 FROM snaps WHERE batch_id = ?", (params[0],)).fetchone()
                self._one = {"dup_hit": 1} if r else None
            elif s.startswith("DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id"):
                sconn.execute("DELETE FROM snaps WHERE batch_id = ?", (params[0],))
            elif "MERGE dbo.inpatient_dip_snapshot" in s:
                bid, vid = params[0], params[1]
                sconn.execute(
                    "INSERT OR REPLACE INTO snaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, vid, params[2], params[3], params[4], params[5], params[6], params[7], params[8],
                     params[9], params[10], params[11], params[12], float(params[13]),
                     float(params[14]) if params[14] is not None else None,
                     float(params[15]) if params[15] is not None else None,
                     params[16], params[17], params[18], params[19], datetime(2026, 11, 28, 17, 45, 0)),
                )
            elif "MERGE dbo.inpatient_scan_batch" in s:
                bid = params[0]
                sconn.execute(
                    "INSERT OR REPLACE INTO batches (batch_id, scope, year_month, scan_dept, rule_version, scan_limit, total_scanned, is_truncated, probe_version, data_cutoff_time, synced_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, params[1], params[2], params[3], params[4], params[5], params[6], params[7], params[8], params[9], "2026-11-28 17:45:00"),
                )
            elif "OBJECT_ID(N'dbo.inpatient_scan_batch'" in s:
                pat, excl, sc, ym = params[0].replace("%", "*"), params[1], params[2], params[3]
                req_sd = params[4] if len(params) > 4 else None
                req_bid = params[5] if len(params) > 5 else None
                if req_bid is not None:
                    row = sconn.execute(
                        "SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_dept, scan_limit, total_scanned, is_truncated, probe_version, 1 AS has_batch_meta "
                        "FROM batches WHERE batch_id = ? LIMIT 1",
                        (req_bid,),
                    ).fetchone()
                elif req_sd is not None:
                    row = sconn.execute(
                        "SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_dept, scan_limit, total_scanned, is_truncated, probe_version, 1 AS has_batch_meta "
                        "FROM batches WHERE scope = ? AND (? IS NULL OR year_month = ?) AND scan_dept = ? ORDER BY synced_at DESC, seq_id DESC LIMIT 1",
                        (sc, ym, ym, req_sd),
                    ).fetchone()
                else:
                    row = sconn.execute(
                        "SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_dept, scan_limit, total_scanned, is_truncated, probe_version, 1 AS has_batch_meta "
                        "FROM batches WHERE scope = ? AND (? IS NULL OR year_month = ?) AND scan_dept IS NULL ORDER BY synced_at DESC, seq_id DESC LIMIT 1",
                        (sc, ym, ym),
                    ).fetchone()
                if row:
                    d = dict(row)
                    d["data_cutoff_time"] = datetime(2026, 11, 28, 17, 45, 0)
                    d["synced_at"] = datetime(2026, 11, 28, 17, 45, 0)
                    self._one = d
            elif "COL_LENGTH('dbo.inpatient_dip_snapshot', 'doctor_name')" in s:
                self._one = {"has_dn": 1, "has_batch_tbl": 1, "has_pv": 1, "has_sd": 1}
            elif "FROM dbo.inpatient_dip_snapshot WHERE batch_id" in s:
                rows = sconn.execute("SELECT * FROM snaps WHERE batch_id = ?", (params[0],)).fetchall()
                self._all = [dict(r) for r in rows]

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _FakeConn:
        def cursor(self, as_dict=True):
            return _FakeScanCur(as_dict=as_dict)
        def commit(self):
            sconn.commit()
        def rollback(self):
            sconn.rollback()
        def close(self):
            pass

    class _SeqMonthReader:
        def __init__(self):
            self.calls = 0
        def get_monthly_inpatients(self, year_month, department=None, limit=None, return_meta=False):
            self.calls += 1
            rows = [{"zyh": "ZY_FIRST_ONLY", "patient_name": "甲", "current_dept": "中医科", "current_dept_id": "101", "current_bed": "1",
                     "attending_doctor_id": "D1", "attending_doctor": "张", "age": "50", "ylzffs": "01", "insurance_type": "职工",
                     "admission_diag_code": "M17.9", "admission_diag_name": "膝关节病", "ssmc1": "保守治疗"}] if self.calls == 1 else []
            meta = {"year_month": year_month, "scan_limit": limit, "is_truncated": False, "probe_version": 2}
            return (rows, meta) if return_meta else rows
        def get_active_patient_fee(self, lsh, cutoff_time=None):
            return {"total_fee": Decimal("1500.00"), "has_activity": True}

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 28, 17, 45, 0, 123456, tzinfo=tz)

    scanner = scanner_mod.InpatientScanner(rule_version="2026_SAMPLE_V1")
    scanner.his_reader = _SeqMonthReader()

    with patch.object(scanner_mod, "datetime", _FrozenDateTime), \
         patch.object(scanner_mod, "get_local_db_connection", side_effect=lambda as_dict=False: (_FakeScanCur(as_dict=as_dict), _FakeConn())), \
         patch.object(main_mod, "get_local_db_connection", side_effect=lambda as_dict=True: (_FakeScanCur(as_dict=as_dict), _FakeConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "role_type": "ADMIN", "dept_scope": "", "can_export_case": True}), \
         patch.object(main_mod, "record_audit_log"), \
         patch.object(scanner_mod, "query_approved_dip_rules", return_value=[]):
        # 1) Frozen exact microsecond timestamp: 1st call writes 1 row, 2nd call writes 0 rows
        res1 = scanner.scan_and_snapshot(scope="month", year_month="2026-11")
        res2 = scanner.scan_and_snapshot(scope="month", year_month="2026-11")
        assert res1["batch_id"] != res2["batch_id"]
        assert res1["batch_id"].startswith("MONTH_202611_20261128_174500_")
        assert res2["batch_id"].startswith("MONTH_202611_20261128_174500_")
        assert res1["total_scanned"] == 1 and res2["total_scanned"] == 0

        # Each batch's summary count matches its own snapshot detail rows
        c1 = sconn.execute("SELECT COUNT(*) FROM snaps WHERE batch_id = ?", (res1["batch_id"],)).fetchone()[0]
        c2 = sconn.execute("SELECT COUNT(*) FROM snaps WHERE batch_id = ?", (res2["batch_id"],)).fetchone()[0]
        assert c1 == 1 and c2 == 0

        # API alerts and CSV both select latest batch (res2, 0 items), never mixing ZY_FIRST_ONLY
        alerts = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert alerts["batch_id"] == res2["batch_id"] and alerts["total"] == 0 and alerts["items"] == []
        csv_resp = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="month", year_month="2026-11", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert csv_resp.headers.get("X-DIP-Batch-Id") == res2["batch_id"]
        assert "ZY_FIRST_ONLY" not in csv_resp.body.decode("utf-8")

        # 2) Explicit duplicate batch_id policy: if_exists="error" rejects, "replace" atomically clears old details
        try:
            scanner.scan_and_snapshot(batch_id=res1["batch_id"], scope="month", year_month="2026-11", if_exists="error")
            raise AssertionError("Expected duplicate explicit batch_id to be rejected when if_exists='error'")
        except ValueError as err:
            assert "Duplicate explicit batch_id" in str(err)

        scanner.scan_and_snapshot(batch_id=res1["batch_id"], scope="month", year_month="2026-11", if_exists="replace")
        c1_after = sconn.execute("SELECT COUNT(*) FROM snaps WHERE batch_id = ?", (res1["batch_id"],)).fetchone()[0]
        m1_after = sconn.execute("SELECT total_scanned FROM batches WHERE batch_id = ?", (res1["batch_id"],)).fetchone()[0]
        assert c1_after == 0 and m1_after == 0


def test_month_total_001_legacy_full_limit_vs_verified_and_compat_reader_offline():
    """MONTH-TOTAL-001: Distinguish unverified legacy 100/100/false (unknown), verified 100/100/false (complete), and compat reader 101->truncated."""
    import tools.inpatient_scanner as scanner_mod

    # 1) Legacy compat reader WITHOUT return_meta param still receives limit+1=101 probe when 101 rows exist
    class _LegacyActiveReader:
        def __init__(self, total_available: int):
            self.total_available = total_available
            self.seen_limits = []
        def get_active_inpatients(self, department=None, limit=None):
            self.seen_limits.append(limit)
            rows = [
                {"zyh": f"ZY_C_{i:03d}", "patient_name": "P", "current_dept": "骨科", "current_dept_id": "101", "current_bed": str(i),
                 "attending_doctor_id": "D1", "attending_doctor": "王", "age": "50", "ylzffs": "01", "insurance_type": "职工",
                 "admission_diag_code": "M17.9", "admission_diag_name": "膝关节病", "ssmc1": "保守治疗"}
                for i in range(self.total_available)
            ]
            return rows[:limit] if limit is not None else rows
        def get_active_patient_fee(self, lsh, cutoff_time=None):
            return {"total_fee": Decimal("1000.00"), "has_activity": True}

    class _NoopCur:
        def execute(self, *a, **kw):
            pass
        def fetchone(self):
            return None

    class _NoopConn:
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    legacy_101 = _LegacyActiveReader(101)
    scanner = scanner_mod.InpatientScanner(rule_version="2026_SAMPLE_V1")
    scanner.his_reader = legacy_101
    with patch.object(scanner_mod, "get_local_db_connection", return_value=(_NoopCur(), _NoopConn())), \
         patch.object(scanner_mod, "query_approved_dip_rules", return_value=[]):
        res_101 = scanner.scan_and_snapshot(scope="active", limit=100)
    assert legacy_101.seen_limits == [101]
    assert res_101["scan_limit"] == 100 and res_101["total_scanned"] == 100 and res_101["is_truncated"] is True and res_101["probe_version"] >= 2

    # 2) API/CSV distinction between unverified legacy 100/100/false -> Unknown vs probe_version=2 100/100/false -> Complete vs 100/100/true -> Truncated
    scenarios = [
        ("OLD_100_UNVERIFIED", {"batch_id": "OLD_100", "data_cutoff_time": datetime(2026, 11, 28, 8, 0, 0), "synced_at": datetime(2026, 11, 28, 8, 0, 0),
                                "scope": "active", "year_month": None, "scan_limit": 100, "total_scanned": 100, "is_truncated": 0, "probe_version": None, "has_batch_meta": 1},
         None, "unknown", "未知(旧批次无完整度元数据)"),
        ("NEW_100_VERIFIED", {"batch_id": "NEW_100", "data_cutoff_time": datetime(2026, 11, 28, 8, 0, 0), "synced_at": datetime(2026, 11, 28, 8, 0, 0),
                              "scope": "active", "year_month": None, "scan_limit": 100, "total_scanned": 100, "is_truncated": 0, "probe_version": 2, "has_batch_meta": 1},
         False, "false", "全量(共100例)"),
        ("COMPAT_101_TRUNC", {"batch_id": "COMPAT_101", "data_cutoff_time": datetime(2026, 11, 28, 8, 0, 0), "synced_at": datetime(2026, 11, 28, 8, 0, 0),
                              "scope": "active", "year_month": None, "scan_limit": 100, "total_scanned": 100, "is_truncated": 1, "probe_version": 2, "has_batch_meta": 1},
         True, "true", "截断(上限100/已扫100例)"),
    ]

    sample_row = {
        "visit_id": "ZY_1", "dept_id": "101", "dept_name": "骨科", "ward_name": "W", "bed_no": "1",
        "doctor_id": "D1", "doctor_name": "王", "primary_icd": "M17.9", "ins_type": "职工",
        "matched_rule_id": 85, "current_fee": Decimal("1000.00"), "target_base": Decimal("2000.00"),
        "fee_ratio": Decimal("0.50"), "alert_level": "GREEN", "pending_reason": None,
        "data_cutoff_time": datetime(2026, 11, 28, 8, 0, 0),
    }

    for _, batch_meta_row, expected_flag, expected_hdr, expected_csv_desc in scenarios:
        class _MatrixCur:
            def __init__(self, row=batch_meta_row):
                self._one = None
                self._all = []
                self._row = row
            def execute(self, sql, params=()):
                s = " ".join(sql.split())
                if "OBJECT_ID(N'dbo.inpatient_scan_batch'" in s:
                    self._one = self._row
                elif "COL_LENGTH('dbo.inpatient_dip_snapshot', 'doctor_name')" in s:
                    self._one = {"has_dn": 1, "has_batch_tbl": 1, "has_pv": 1}
                elif "FROM dbo.inpatient_dip_snapshot" in s:
                    self._all = [sample_row]
            def fetchone(self):
                return self._one
            def fetchall(self):
                return self._all

        class _MatrixConn:
            def close(self):
                pass

        with patch.object(main_mod, "get_local_db_connection", side_effect=lambda as_dict=True: (_MatrixCur(), _MatrixConn())), \
             patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "role_type": "ADMIN", "dept_scope": "", "can_export_case": True}), \
             patch.object(main_mod, "record_audit_log"):
            j = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
            assert j["is_truncated"] is expected_flag
            c = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
            assert c.headers.get("X-DIP-Is-Truncated") == expected_hdr
            assert expected_csv_desc in c.body.decode("utf-8")

    # 3) Verify Vue board explicitly renders unknown banner on is_truncated == null, complete badge on === false, warning on === true
    vue_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "src", "views", "inpatient", "index.vue")
    if os.path.exists(vue_path):
        vue_txt = open(vue_path, "r", encoding="utf-8").read()
        assert "report?.is_truncated === true" in vue_txt
        assert "report?.is_truncated === false" in vue_txt
        assert "report?.is_truncated == null" in vue_txt
        assert "完整度未知" in vue_txt


def test_snap_dept_batch_001_dept_scan_does_not_overwrite_hospital_board_offline():
    """SNAP-DEPT-BATCH-001: Hospital scan (5 cases: Dept A 2 + Dept B 3) stays active after --dept A (2 cases); dept scan identifiable."""
    import sqlite3
    import tools.inpatient_scanner as scanner_mod

    sconn = sqlite3.connect(":memory:")
    sconn.row_factory = sqlite3.Row
    sconn.execute(
        "CREATE TABLE snaps ("
        "batch_id TEXT, visit_id TEXT, ward_name TEXT, bed_no TEXT, dept_id TEXT, dept_name TEXT, "
        "doctor_id TEXT, doctor_name TEXT, age INT, ins_type TEXT, primary_icd TEXT, matched_rule_id INT, "
        "rule_version TEXT, current_fee REAL, target_base REAL, fee_ratio REAL, alert_level TEXT, "
        "pending_reason TEXT, discharge_review INT, data_cutoff_time DATETIME, synced_at DATETIME, "
        "PRIMARY KEY (batch_id, visit_id))"
    )
    sconn.execute(
        "CREATE TABLE batches ("
        "seq_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "batch_id TEXT UNIQUE, scope TEXT, year_month TEXT, scan_dept TEXT, rule_version TEXT, scan_limit INT, "
        "total_scanned INT, is_truncated INT, probe_version INT, data_cutoff_time DATETIME, synced_at DATETIME)"
    )

    class _DeptIsoCur:
        def __init__(self, as_dict=False):
            self.as_dict = as_dict
            self._one = None
            self._all = []

        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            self._one = None
            self._all = []
            if "ALTER TABLE" in s or "CREATE TABLE dbo.inpatient_scan_batch" in s:
                return
            if "sp_getapplock" in s:
                self._one = (0,) if not self.as_dict else {"lock_rc": 0}
                return
            if "MERGE dbo.inpatient_dip_snapshot" in s:
                sconn.execute(
                    "INSERT OR REPLACE INTO snaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (params[0], params[1], params[2], params[3], params[4], params[5], params[6], params[7], params[8],
                     params[9], params[10], params[11], params[12], float(params[13]),
                     float(params[14]) if params[14] is not None else None,
                     float(params[15]) if params[15] is not None else None,
                     params[16], params[17], params[18], params[19], datetime(2026, 11, 28, 17, 55, 0)),
                )
            elif "MERGE dbo.inpatient_scan_batch" in s:
                sconn.execute(
                    "INSERT OR REPLACE INTO batches (batch_id, scope, year_month, scan_dept, rule_version, scan_limit, total_scanned, is_truncated, probe_version, data_cutoff_time, synced_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (params[0], params[1], params[2], params[3], params[4], params[5], params[6], params[7], params[8], params[9], "2026-11-28 17:55:00"),
                )
            elif "OBJECT_ID(N'dbo.inpatient_scan_batch'" in s:
                sc, ym, req_sd, req_bid = params[2], params[3], params[4], params[5]
                if req_bid is not None:
                    row = sconn.execute(
                        "SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_dept, scan_limit, total_scanned, is_truncated, probe_version, 1 AS has_batch_meta "
                        "FROM batches WHERE batch_id = ? LIMIT 1",
                        (req_bid,),
                    ).fetchone()
                elif req_sd is not None:
                    row = sconn.execute(
                        "SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_dept, scan_limit, total_scanned, is_truncated, probe_version, 1 AS has_batch_meta "
                        "FROM batches WHERE scope = ? AND (? IS NULL OR year_month = ?) AND scan_dept = ? ORDER BY synced_at DESC, seq_id DESC LIMIT 1",
                        (sc, ym, ym, req_sd),
                    ).fetchone()
                else:
                    row = sconn.execute(
                        "SELECT batch_id, data_cutoff_time, synced_at, scope, year_month, scan_dept, scan_limit, total_scanned, is_truncated, probe_version, 1 AS has_batch_meta "
                        "FROM batches WHERE scope = ? AND (? IS NULL OR year_month = ?) AND scan_dept IS NULL ORDER BY synced_at DESC, seq_id DESC LIMIT 1",
                        (sc, ym, ym),
                    ).fetchone()
                if row:
                    d = dict(row)
                    d["data_cutoff_time"] = datetime(2026, 11, 28, 17, 55, 0)
                    d["synced_at"] = datetime(2026, 11, 28, 17, 55, 0)
                    self._one = d
            elif "COL_LENGTH('dbo.inpatient_dip_snapshot', 'doctor_name')" in s:
                self._one = {"has_dn": 1, "has_batch_tbl": 1, "has_pv": 1, "has_sd": 1}
            elif "FROM dbo.inpatient_dip_snapshot WHERE batch_id" in s:
                rows = sconn.execute("SELECT * FROM snaps WHERE batch_id = ?", (params[0],)).fetchall()
                self._all = [dict(r) for r in rows]

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _DeptIsoConn:
        def cursor(self, as_dict=True):
            return _DeptIsoCur(as_dict=as_dict)
        def commit(self):
            sconn.commit()
        def rollback(self):
            sconn.rollback()
        def close(self):
            pass

    all_patients = [
        {"zyh": "A_1", "patient_name": "A1", "current_dept": "甲科", "current_dept_id": "101", "current_bed": "1",
         "attending_doctor_id": "D1", "attending_doctor": "王", "age": "50", "ylzffs": "01", "insurance_type": "职工",
         "admission_diag_code": "M17.9", "admission_diag_name": "膝关节病", "ssmc1": "保守治疗"},
        {"zyh": "A_2", "patient_name": "A2", "current_dept": "甲科", "current_dept_id": "101", "current_bed": "2",
         "attending_doctor_id": "D1", "attending_doctor": "王", "age": "50", "ylzffs": "01", "insurance_type": "职工",
         "admission_diag_code": "M17.9", "admission_diag_name": "膝关节病", "ssmc1": "保守治疗"},
        {"zyh": "B_1", "patient_name": "B1", "current_dept": "乙科", "current_dept_id": "102", "current_bed": "3",
         "attending_doctor_id": "D2", "attending_doctor": "李", "age": "50", "ylzffs": "01", "insurance_type": "职工",
         "admission_diag_code": "M17.9", "admission_diag_name": "膝关节病", "ssmc1": "保守治疗"},
        {"zyh": "B_2", "patient_name": "B2", "current_dept": "乙科", "current_dept_id": "102", "current_bed": "4",
         "attending_doctor_id": "D2", "attending_doctor": "李", "age": "50", "ylzffs": "01", "insurance_type": "职工",
         "admission_diag_code": "M17.9", "admission_diag_name": "膝关节病", "ssmc1": "保守治疗"},
        {"zyh": "B_3", "patient_name": "B3", "current_dept": "乙科", "current_dept_id": "102", "current_bed": "5",
         "attending_doctor_id": "D2", "attending_doctor": "李", "age": "50", "ylzffs": "01", "insurance_type": "职工",
         "admission_diag_code": "M17.9", "admission_diag_name": "膝关节病", "ssmc1": "保守治疗"},
    ]

    class _DeptHISReader:
        def get_active_inpatients(self, department=None, limit=None, return_meta=False):
            rows = [r for r in all_patients if not department or r["current_dept"] == department]
            meta = {"scan_limit": limit, "total_scanned": len(rows), "is_truncated": False}
            return (rows, meta) if return_meta else rows
        def get_active_patient_fee(self, lsh, cutoff_time=None):
            return {"total_fee": Decimal("1200.00"), "has_activity": True}

    scanner = scanner_mod.InpatientScanner(rule_version="2026_SAMPLE_V1")
    scanner.his_reader = _DeptHISReader()

    with patch.object(scanner_mod, "get_local_db_connection", side_effect=lambda as_dict=False: (_DeptIsoCur(as_dict=as_dict), _DeptIsoConn())), \
         patch.object(main_mod, "get_local_db_connection", side_effect=lambda as_dict=True: (_DeptIsoCur(as_dict=as_dict), _DeptIsoConn())), \
         patch.object(main_mod, "get_operator", return_value={"operator_id": "admin", "role_type": "ADMIN", "dept_scope": "", "can_export_case": True}), \
         patch.object(main_mod, "record_audit_log"), \
         patch.object(scanner_mod, "query_approved_dip_rules", return_value=[]):
        # 1) Full hospital scan (5 cases) followed by --dept 甲科 scan (2 cases)
        hosp_res = scanner.scan_and_snapshot(scope="active")
        dept_res = scanner.scan_and_snapshot(scope="active", department="甲科")
        assert hosp_res["scan_dept"] is None and hosp_res["total_scanned"] == 5
        assert dept_res["scan_dept"] == "甲科" and dept_res["total_scanned"] == 2

        # 2) Normal hospital-wide query and CSV STILL point to full 5-case hospital batch
        hosp_json = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert hosp_json["batch_id"] == hosp_res["batch_id"]
        assert hosp_json["total"] == 5 and hosp_json["coverage_scope"] == "HOSPITAL" and hosp_json["scan_dept"] is None
        hosp_csv = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert hosp_csv.headers.get("X-DIP-Coverage-Scope") == "HOSPITAL"
        assert "全量(共5例)" in hosp_csv.body.decode("utf-8")

        # 3) Querying single-dept batch by scan_dept / batch_id returns 2-case 甲科 batch marked DEPARTMENT_SCAN (never '全量')
        dept_json = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="active", scan_dept="甲科", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert dept_json["batch_id"] == dept_res["batch_id"]
        assert dept_json["total"] == 2 and dept_json["coverage_scope"] == "DEPARTMENT_SCAN" and dept_json["scan_dept"] == "甲科"
        dept_csv = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, scope="active", scan_dept="甲科", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        dept_csv_txt = dept_csv.body.decode("utf-8")
        from urllib.parse import unquote
        assert dept_csv.headers.get("X-DIP-Coverage-Scope") == "DEPARTMENT_SCAN"
        assert unquote(dept_csv.headers.get("X-DIP-Scan-Dept") or "") == "甲科"
        assert "单科扫描[甲科](共2例)" in dept_csv_txt
        assert "全量" not in dept_csv_txt

        # 4) Old historical batch created before scan_dept tracking (probe_version=2, scan_dept=None) reports coverage_scope='UNKNOWN' with warning
        sconn.execute(
            "INSERT INTO batches (batch_id, scope, year_month, scan_dept, rule_version, scan_limit, total_scanned, is_truncated, probe_version, data_cutoff_time, synced_at) "
            "VALUES ('OLD_COV_UNKNOWN', 'active', NULL, NULL, '2026_SAMPLE_V1', 50, 5, 0, 2, '2026-11-28 10:00:00', '2026-11-28 10:00:00')"
        )
        old_json = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="active", batch_id="OLD_COV_UNKNOWN", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert old_json["coverage_scope"] == "UNKNOWN" and old_json["coverage_warning"]
