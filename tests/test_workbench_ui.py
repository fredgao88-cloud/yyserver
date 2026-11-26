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

    # WB-AUTH-001: 401 清空数据+令牌并退回登录，403 清空数据；先状态判定后 safeJson 解析；reqSeq/authEpoch 防并发回流
    assert "clearProtectedInpatientData" in inp_html
    assert "inpReqSeq" in inp_html and "inpAuthEpoch" in inp_html and "safeJson" in inp_html
    assert inp_html.index("if (r.status === 401)") < inp_html.index("await safeJson(r)")
    assert "clearProtectedAnalyticsData" in ana_html
    assert "anaReqSeq" in ana_html and "anaAuthEpoch" in ana_html and "safeJson" in ana_html
    assert ana_html.index("if (r.status === 401)") < ana_html.index("await safeJson(r)")

    # WB-TREND-001: 恒定正柱宽 + 动态 viewBox + 横向滚动
    assert "const barW = 28;" in ana_html
    assert "overflow-x:auto" in ana_html

    # WB-JS-001: 检查全部页面 <script> 不含原始控制字符 (\r / \t)，并在有 node 时执行 node --check
    import re
    import shutil
    import subprocess
    all_pages = [
        main_mod.inpatient_workbench_page,
        main_mod.analytics_workbench_page,
        main_mod.dip_rules_admin_page,
        main_mod.audit_logs_admin_page,
        main_mod.reconciliation_workbench_page,
        main_mod.calculator_workbench_page,
        main_mod.admin_operators_page,
    ]
    node_bin = shutil.which("node")
    for fn in all_pages:
        html_src = asyncio.run(fn()).body.decode("utf-8")
        m_js = re.search(r"<script>(.*?)</script>", html_src, re.S)
        assert m_js is not None
        js_code = m_js.group(1)
        assert "\r" not in js_code and "\t" not in js_code
        if node_bin:
            subprocess.run([node_bin, "--check", "-"], input=js_code, text=True, encoding="utf-8", check=True)

    # WB-EXPORT-001: 在院与对账导出改为调用后端受保护接口，前端不直接生成病例 CSV
    rec_html = asyncio.run(main_mod.reconciliation_workbench_page()).body.decode("utf-8")
    assert "/api/inpatient/alerts/export" in inp_html
    assert "/api/reconciliation/settled/export" in rec_html
    assert "exportAnalyticsCsv" in ana_html and ("\\uFEFF" in ana_html or "\ufeff" in ana_html)

    # 校验后端导出权限 can_export_case=0 -> 403，can_export_case=1 -> 200 CSV
    from unittest.mock import patch
    with patch.object(main_mod, "get_operator", return_value={"operator_id": "ins01", "can_export_case": False}), \
         patch.object(main_mod, "record_audit_log"):
        try:
            asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, operator={"sub": "ins01", "role": main_mod.ROLE_INSURANCE}))
            assert False, "expected 403 when can_export_case is False"
        except main_mod.HTTPException as exc:
            assert exc.status_code == 403

        try:
            asyncio.run(main_mod.export_settled_reconciliation_csv(limit=10, dept=None, status=None, operator={"sub": "ins01", "role": main_mod.ROLE_INSURANCE}))
            assert False, "expected 403 when can_export_case is False"
        except main_mod.HTTPException as exc:
            assert exc.status_code == 403

    with patch.object(main_mod, "get_operator", return_value={"operator_id": "ins01", "can_export_case": True}), \
         patch.object(main_mod, "record_audit_log"), \
         patch.object(main_mod, "get_inpatient_alerts", return_value={"batch_id": "B1", "items": [{"zyh": "ZY1", "dept_name": "中医科", "bed": "1", "doctor_id": "D1", "disease": "M17.9", "ins_type": "职工", "current_cost_str": "100.00", "benchmark_str": "200.00", "ratio_percent": 50.0, "alert_level": "GREEN", "status": "正常"}]}):
        csv_resp = asyncio.run(main_mod.export_inpatient_alerts_csv(dept=None, level=None, operator={"sub": "ins01", "role": main_mod.ROLE_INSURANCE}))
        assert csv_resp.status_code == 200
        assert "text/csv" in csv_resp.media_type
        assert "ZY1" in csv_resp.body.decode("utf-8")

    # 根路径浏览器跳转 vs API JSON 兼容
    redir = asyncio.run(main_mod.index(accept="text/html,application/xhtml+xml"))
    assert redir.status_code == 302
    assert redir.headers.get("location") == "/inpatient"
    json_meta = asyncio.run(main_mod.index(accept="application/json"))
    assert json_meta["workbench"] == "/inpatient"
