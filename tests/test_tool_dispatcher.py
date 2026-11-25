# -*- coding: utf-8 -*-
"""
tests/test_tool_dispatcher.py — 本地自然语言 Function Calling 闭环单元测试
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tool_dispatcher import dispatch_tool_call, extract_tool_call


def test_extract_tool_call_does_not_interfere_with_dip_rule_query():
    # 纯规则问答不应被拦截为患者工具调用
    q = "68岁，职工医保，诊断编码 M17.9，保守治疗。请查询医院 DIP 预估支付额。"
    assert extract_tool_call(q) is None


def test_extract_tool_call_check_patient_dip():
    q = "帮我查一下住院号 zy2025042901 的控费判灯情况，诊断 M17.9，目前费用 2550 元，职工医保，保守治疗"
    tc = extract_tool_call(q)
    assert tc is not None
    assert tc["name"] == "check_patient_dip"
    assert tc["arguments"]["patient_id"] == "zy2025042901"
    assert tc["arguments"]["disease"] == "M17.9"
    assert tc["arguments"]["current_cost"] == "2550"
    assert tc["arguments"]["ins_type"] == "职工"
    assert tc["arguments"]["operation"] == "保守"


def test_extract_tool_call_alerts_and_performance():
    tc_alerts = extract_tool_call("请查看中医科的在院预警看板")
    assert tc_alerts is not None
    assert tc_alerts["name"] == "get_inpatient_alerts"
    assert tc_alerts["arguments"]["dept"] == "中医科"

    tc_perf = extract_tool_call("查看最近15天的医生控费绩效报表")
    assert tc_perf is not None
    assert tc_perf["name"] == "get_doctor_performance"
    assert tc_perf["arguments"]["days"] == 15


def test_dispatch_requires_authentication():
    tc = {"name": "get_inpatient_alerts", "arguments": {}}
    res = dispatch_tool_call(tc, {"sub": "anonymous", "role": "DOCTOR", "authenticated": False})
    assert res["tool_executed"] is False
    assert res["data"]["status"] == "AUTH_REQUIRED"


def test_dispatch_doctor_cannot_access_alerts():
    tc = {"name": "get_inpatient_alerts", "arguments": {}}
    res = dispatch_tool_call(tc, {"sub": "D001", "role": "DOCTOR", "authenticated": True})
    assert res["tool_executed"] is False
    assert res["data"]["status"] == "FORBIDDEN"


def test_dispatch_doctor_performance_executes_for_admin():
    tc = {"name": "get_doctor_performance", "arguments": {"days": 7}}
    fake_report = {"days": 7, "total_snapshots": 4, "doctors": [{"doctor_id": "D1"}], "trends": []}
    with patch("agent.tool_dispatcher.get_doctor_performance_report", return_value=fake_report):
        res = dispatch_tool_call(tc, {"sub": "admin", "role": "ADMIN", "authenticated": True})
        assert res["tool_executed"] is True
        assert res["intent"] == "tool_call_result"
        assert res["data"]["total_snapshots"] == 4
