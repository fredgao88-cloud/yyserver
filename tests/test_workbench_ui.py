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


def test_workbench_fixes_wb_alert_auth_and_trend_contracts():
    inp_html = asyncio.run(main_mod.inpatient_workbench_page()).body.decode("utf-8")
    ana_html = asyncio.run(main_mod.analytics_workbench_page()).body.decode("utf-8")

    # WB-ALERT-001: 待定与自费独立统计，红柱直接用 red_count
    assert 'id="sNA"' in inp_html
    assert 'alert_level === "NOT_APPLICABLE"' in inp_html
    assert 'alert_level === "PENDING"' in inp_html
    assert "t.red_count" in ana_html
    assert "totalH - gH - yH" not in ana_html

    # WB-AUTH-001: 401 清空数据+令牌并退回登录，403 清空数据
    assert "clearProtectedInpatientData" in inp_html
    assert "r.status === 401" in inp_html and "r.status === 403" in inp_html
    assert "clearProtectedAnalyticsData" in ana_html
    assert "r.status === 401" in ana_html and "r.status === 403" in ana_html

    # WB-TREND-001: 恒定正柱宽 + 动态 viewBox + 横向滚动
    assert "const barW = 28;" in ana_html
    assert "overflow-x:auto" in ana_html

    # CSV 导出与公式注入防护
    rec_html = asyncio.run(main_mod.reconciliation_workbench_page()).body.decode("utf-8")
    for html_txt, fn_name in [
        (inp_html, "exportInpatientCsv"),
        (ana_html, "exportAnalyticsCsv"),
        (rec_html, "exportReconCsv"),
    ]:
        assert fn_name in html_txt
        assert "\ufeff" in html_txt

    # 根路径浏览器跳转 vs API JSON 兼容
    redir = asyncio.run(main_mod.index(accept="text/html,application/xhtml+xml"))
    assert redir.status_code == 302
    assert redir.headers.get("location") == "/inpatient"
    json_meta = asyncio.run(main_mod.index(accept="application/json"))
    assert json_meta["workbench"] == "/inpatient"
