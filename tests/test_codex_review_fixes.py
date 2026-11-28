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
        assert any("batch_id LIKE %s" in s and p == ("MONTH_202611_%",) for s, p in executed)

        executed.clear()
        res_a = asyncio.run(main_mod.get_inpatient_alerts(dept=None, scope="active", operator={"sub": "admin", "role": "ADMIN", "dept": ""}))
        assert res_a["scope"] == "active"
        assert any("NOT LIKE 'MONTH_%'" in s for s, _ in executed)


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
