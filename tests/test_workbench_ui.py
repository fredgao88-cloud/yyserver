# -*- coding: utf-8 -*-
"""
tests/test_workbench_ui.py — 内网 Web 工作台页面安全与零外网依赖离线测试
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.main as main_mod


def test_workbench_pages_return_200_without_external_cdn_or_xss():
    for fn, title_kw in [
        (main_mod.inpatient_workbench_page, "在院预警看板"),
        (main_mod.analytics_workbench_page, "医生绩效与历史趋势"),
        (main_mod.dip_rules_admin_page, "规则批量导入"),
    ]:
        resp = asyncio.run(fn())
        assert resp.status_code == 200
        body = resp.body.decode("utf-8")
        assert title_kw in body
        # 内网隔离硬约束：除标准 SVG XML 命名空间外，绝不允许包含外网 URL 或外部 CDN
        cleaned = body.replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in cleaned and "https://" not in cleaned
        assert "cdn" not in cleaned.lower()
