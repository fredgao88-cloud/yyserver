# -*- coding: utf-8 -*-
"""
tests/test_analytics_reporter.py — 医生绩效分析与历史趋势聚合离线单元测试
"""
from datetime import datetime
from decimal import Decimal
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.analytics_reporter import (
    aggregate_doctor_performance,
    aggregate_historical_trends,
)


def test_aggregate_doctor_performance_deduplicates_visits():
    rows = [
        # 同一患者 V1 在两个批次，应只取最新批次 (11-26) 的费用 3100.00 (RED)
        {
            "batch_id": "B1",
            "visit_id": "V1",
            "dept_id": "101",
            "dept_name": "中医科",
            "doctor_id": "D001",
            "current_fee": Decimal("2000.00"),
            "target_base": Decimal("2790.90"),
            "fee_ratio": Decimal("0.7166"),
            "alert_level": "GREEN",
            "data_cutoff_time": datetime(2026, 11, 25, 8, 0, 0),
        },
        {
            "batch_id": "B2",
            "visit_id": "V1",
            "dept_id": "101",
            "dept_name": "中医科",
            "doctor_id": "D001",
            "current_fee": Decimal("3100.00"),
            "target_base": Decimal("2790.90"),
            "fee_ratio": Decimal("1.1108"),
            "alert_level": "RED",
            "data_cutoff_time": datetime(2026, 11, 26, 8, 0, 0),
        },
        {
            "batch_id": "B2",
            "visit_id": "V2",
            "dept_id": "101",
            "dept_name": "中医科",
            "doctor_id": "D001",
            "current_fee": Decimal("1500.00"),
            "target_base": Decimal("2790.90"),
            "fee_ratio": Decimal("0.5375"),
            "alert_level": "GREEN",
            "data_cutoff_time": datetime(2026, 11, 26, 8, 0, 0),
        },
    ]

    res = aggregate_doctor_performance(rows)
    assert len(res) == 1
    d1 = res[0]
    assert d1["doctor_id"] == "D001"
    assert d1["total_cases"] == 2
    assert d1["red_count"] == 1
    assert d1["green_count"] == 1
    assert d1["green_rate_percent"] == 50.0
    assert d1["red_rate_percent"] == 50.0
    assert d1["total_fee_str"] == "4600.00"


def test_aggregate_historical_trends_sorted_and_accurate():
    rows = [
        {
            "batch_id": "B2",
            "visit_id": "V1",
            "alert_level": "RED",
            "fee_ratio": Decimal("1.20"),
            "data_cutoff_time": datetime(2026, 11, 26, 8, 0, 0),
        },
        {
            "batch_id": "B1",
            "visit_id": "V1",
            "alert_level": "GREEN",
            "fee_ratio": Decimal("0.80"),
            "data_cutoff_time": datetime(2026, 11, 25, 8, 0, 0),
        },
        {
            "batch_id": "B1",
            "visit_id": "V2",
            "alert_level": "YELLOW",
            "fee_ratio": Decimal("1.00"),
            "data_cutoff_time": datetime(2026, 11, 25, 8, 0, 0),
        },
    ]

    trends = aggregate_historical_trends(rows)
    assert len(trends) == 2
    assert trends[0]["batch_id"] == "B1"
    assert trends[0]["total"] == 2
    assert trends[0]["green_count"] == 1
    assert trends[0]["yellow_count"] == 1
    assert trends[0]["avg_fee_ratio_percent"] == 90.0
    assert trends[1]["batch_id"] == "B2"
    assert trends[1]["red_count"] == 1
    assert trends[1]["avg_fee_ratio_percent"] == 120.0
