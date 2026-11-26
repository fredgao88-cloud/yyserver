# -*- coding: utf-8 -*-
"""
tests/test_dingtalk_notifier.py — DingTalk Webhook 主动推送模块离线单元测试
"""
import json
import os
import sys
import urllib.parse
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.dingtalk_notifier import _sign_webhook_url, build_alert_markdown, send_dingtalk_alert


def test_sign_webhook_url():
    signed = _sign_webhook_url(
        "https://oapi.dingtalk.com/robot/send?access_token=abc123",
        "SECtestsecret",
        timestamp_ms=1700000000000,
    )
    parsed = urllib.parse.urlparse(signed)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["access_token"] == ["abc123"]
    assert qs["timestamp"] == ["1700000000000"]
    assert len(qs["sign"][0]) > 10


def test_build_alert_markdown_privacy_safe():
    summary = {
        "batch_id": "BATCH_20261126_080000",
        "data_cutoff_time": "2026-11-26 08:00:00",
        "rule_version": "2026_SAMPLE_V1",
        "total_scanned": 12,
        "RED": 2,
        "YELLOW": 3,
        "GREEN": 5,
        "PENDING": 1,
        "NOT_APPLICABLE": 1,
        "dept_alert_counts": {
            "中医科": {"RED": 2, "YELLOW": 1},
            "骨伤科": {"RED": 0, "YELLOW": 2},
        },
        "snapshots": [
            {"visit_id": "zy999999", "patient_name": "张三丰", "bed_no": "12床"},
        ],
    }
    title, text = build_alert_markdown(summary)
    assert "红灯 2 例" in title
    assert "ALERT-" in text
    assert "BATCH_20261126_080000" not in text
    assert "中医科(红2/黄1)" in text
    assert "骨伤科(红0/黄2)" in text
    # 隐私红线：严禁包含任何患者明细
    assert "zy999999" not in text
    assert "张三丰" not in text
    assert "12床" not in text


def test_send_dingtalk_alert_skips_when_no_alerts():
    summary = {"batch_id": "B1", "RED": 0, "YELLOW": 0, "GREEN": 5}
    res = send_dingtalk_alert(summary, webhook_url="https://oapi.dingtalk.com/robot/send?access_token=t")
    assert res["sent"] is False
    assert res["skipped"] is True
    assert res["reason"] == "NO_RED_OR_YELLOW_ALERTS"


def test_send_dingtalk_alert_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    res = send_dingtalk_alert({"batch_id": "B1", "RED": 1, "YELLOW": 0})
    assert res["sent"] is False
    assert res["skipped"] is True
    assert res["reason"] == "WEBHOOK_URL_NOT_CONFIGURED"


def test_send_dingtalk_alert_success_mock():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({"errcode": 0, "errmsg": "ok"}).encode("utf-8")
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_resp
    mock_cm.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=mock_cm) as mock_open:
        res = send_dingtalk_alert(
            {"batch_id": "B2", "RED": 1, "YELLOW": 2, "GREEN": 3, "total_scanned": 6},
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=mock",
            secret="SECmock",
        )
        assert res["sent"] is True
        assert res["skipped"] is False
        assert mock_open.called
        req = mock_open.call_args[0][0]
        assert "sign=" in req.full_url
        body = json.loads(req.data.decode("utf-8"))
        assert body["msgtype"] == "markdown"
        assert "红灯 1 例" in body["markdown"]["title"]


def test_send_dingtalk_alert_network_error_safe():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        res = send_dingtalk_alert(
            {"batch_id": "B3", "RED": 1, "YELLOW": 0},
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=mock",
        )
        assert res["sent"] is False
        assert res["skipped"] is False
        assert "NETWORK_ERROR" in res["reason"]
