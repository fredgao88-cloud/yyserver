# -*- coding: utf-8 -*-
"""
api/workbench_ui.py — 院内业务管理 Web 工作台页面（零外部 CDN 依赖，全本地原生实现）

包含三个静态页面壳（所有业务数据均需登录后通过 Bearer Token 请求受保护 API 获取）：
1. /inpatient       — 在院患者 DIP 控费预警看板
2. /analytics       — 医生绩效分析与历史批次趋势看板
3. /admin/dip-rules — DIP 规则新版目录批量导入与预检控制台
"""

_COMMON_STYLE = """
  :root {
    --bg:#f4f6f9; --card:#ffffff; --primary:#1f5fa8; --primary-dark:#16467d;
    --danger:#c62828; --warn:#d97706; --ok:#2e7d32; --border:#dce3ec; --text:#1e293b;
  }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); }
  header { background:var(--primary); color:#fff; padding:12px 24px; display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px; }
  header h1 { margin:0; font-size:17px; font-weight:600; }
  nav { display:flex; gap:8px; flex-wrap:wrap; }
  nav a { color:#dbeafe; text-decoration:none; font-size:13px; padding:6px 10px; border-radius:4px; }
  nav a:hover, nav a.active { background:var(--primary-dark); color:#fff; font-weight:600; }
  .wrap { max-width:1200px; margin:18px auto; padding:0 16px; display:grid; gap:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:18px; box-shadow:0 1px 3px rgba(0,0,0,0.04); }
  .card h2 { margin:0 0 12px 0; font-size:16px; border-bottom:1px solid var(--border); padding-bottom:8px; }
  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; }
  .field { display:flex; flex-direction:column; gap:4px; min-width:140px; flex:1; }
  label { font-size:12px; color:#475569; font-weight:600; }
  input, select, textarea { padding:7px 10px; border:1px solid var(--border); border-radius:5px; font-size:13px; font-family:inherit; }
  button { padding:8px 14px; border:none; border-radius:5px; cursor:pointer; font-size:13px; font-weight:600; background:var(--primary); color:#fff; }
  button.secondary { background:#64748b; }
  button.danger { background:var(--danger); }
  button.ok { background:var(--ok); }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }
  .stat-box { background:#f8fafc; border:1px solid var(--border); border-radius:6px; padding:12px; text-align:center; }
  .stat-box .num { font-size:22px; font-weight:700; margin-top:4px; }
  table { width:100%; border-collapse:collapse; font-size:13px; margin-top:10px; }
  th, td { border-bottom:1px solid var(--border); padding:9px 8px; text-align:left; }
  th { background:#f8fafc; color:#475569; font-weight:600; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:12px; font-weight:600; }
  .badge-RED { background:#fee2e2; color:#991b1b; }
  .badge-YELLOW { background:#fef3c7; color:#92400e; }
  .badge-GREEN { background:#dcfce7; color:#166534; }
  .badge-PENDING { background:#e2e8f0; color:#334155; }
  .banner-warn { background:#fef3c7; border:1px solid #f59e0b; color:#92400e; padding:10px 14px; border-radius:6px; font-size:13px; display:none; }
  #msg { font-size:13px; padding:8px 12px; border-radius:5px; display:none; }
  #msg.ok { display:block; background:#dcfce7; color:#166534; }
  #msg.err { display:block; background:#fee2e2; color:#991b1b; }
  .hidden { display:none; }
"""

def _nav_html(active: str) -> str:
    items = [
        ("/inpatient", "在院预警看板", "inpatient"),
        ("/analytics", "医生绩效趋势", "analytics"),
        ("/admin/dip-rules", "DIP 规则导入", "dip-rules"),
        ("/admin/audit-logs", "审计日志", "audit-logs"),
        ("/admin/operators", "操作员管理", "operators"),
    ]
    links = []
    for href, label, key in items:
        cls = ' class="active"' if key == active else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    return "".join(links)


INPATIENT_ALERTS_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>在院患者 DIP 控费预警看板</title>
<style>{_COMMON_STYLE}</style>
</head>
<body>
<header>
  <h1>🏥 院内 DIP 控费工作台 · 在院预警看板</h1>
  <nav>{_nav_html("inpatient")}</nav>
</header>
<div class="wrap">
  <div id="msg"></div>
  <div id="staleBanner" class="banner-warn"></div>

  <div class="card" id="loginCard">
    <h2>身份认证</h2>
    <div class="row">
      <div class="field"><label>工号</label><input id="loginId" placeholder="请输入工号"></div>
      <div class="field"><label>口令</label><input id="loginPwd" type="password" placeholder="请输入口令"></div>
      <button onclick="doLogin()">登录</button>
    </div>
  </div>

  <div id="mainArea" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center;">
        <div class="row" style="flex:1;">
          <div class="field" style="max-width:180px;">
            <label>灯号筛选</label>
            <select id="filterLevel" onchange="renderRows()">
              <option value="">全部</option>
              <option value="RED">红灯 (&gt;110%)</option>
              <option value="YELLOW">黄灯 (90%~110%)</option>
              <option value="GREEN">绿灯 (&lt;90%)</option>
              <option value="PENDING">待定核查</option>
            </select>
          </div>
          <div class="field" style="max-width:200px;">
            <label>科室筛选 (管理员/医保办)</label>
            <input id="filterDept" placeholder="科室 ID 或名称">
          </div>
          <button onclick="loadAlerts()">查询预警</button>
        </div>
        <button class="danger" onclick="doLogout()">退出登录</button>
      </div>
      <div class="stats" style="margin-top:14px;">
        <div class="stat-box"><div>总在院</div><div class="num" id="sTotal">0</div></div>
        <div class="stat-box"><div>红灯超标</div><div class="num" style="color:var(--danger)" id="sRed">0</div></div>
        <div class="stat-box"><div>黄灯观察</div><div class="num" style="color:var(--warn)" id="sYellow">0</div></div>
        <div class="stat-box"><div>绿灯正常</div><div class="num" style="color:var(--ok)" id="sGreen">0</div></div>
        <div class="stat-box"><div>待定核查</div><div class="num" id="sPending">0</div></div>
        <div class="stat-box"><div>自费不适用</div><div class="num" id="sNA">0</div></div>
      </div>
    </div>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h2 style="border:none;margin:0;padding:0;">在院预警明细</h2>
        <span id="batchMeta" style="font-size:12px;color:#64748b;"></span>
      </div>
      <table>
        <thead>
          <tr><th>住院号</th><th>科室</th><th>床号</th><th>医生工号</th><th>主诊断</th><th>参保</th><th>当前费用(元)</th><th>DIP 基准(元)</th><th>占比</th><th>状态</th></tr>
        </thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const TOKEN_KEY = "hospital_admin_token";
let cachedItems = [];
function showMsg(text, isErr) {{
  const el = document.getElementById("msg");
  el.textContent = text;
  el.className = isErr ? "err" : "ok";
}}
function getToken() {{ return sessionStorage.getItem(TOKEN_KEY) || ""; }}
function authHeaders() {{ return {{ "Authorization": "Bearer " + getToken() }}; }}
async function doLogin() {{
  const operator_id = document.getElementById("loginId").value.trim();
  const password = document.getElementById("loginPwd").value;
  const r = await fetch("/api/auth/login", {{
    method: "POST", headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ operator_id, password }})
  }});
  const data = await r.json();
  if (!r.ok) {{ showMsg(data.detail || "登录失败", true); return; }}
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  showMsg("登录成功 (" + data.role_type + ")", false);
  loadAlerts();
}}
function doLogout() {{ sessionStorage.removeItem(TOKEN_KEY); location.reload(); }}
function clearProtectedInpatientData() {{
  cachedItems = [];
  document.getElementById("tbody").innerHTML = "";
  document.getElementById("batchMeta").textContent = "";
  const banner = document.getElementById("staleBanner");
  banner.textContent = "";
  banner.style.display = "none";
  document.getElementById("sTotal").textContent = "0";
  document.getElementById("sRed").textContent = "0";
  document.getElementById("sYellow").textContent = "0";
  document.getElementById("sGreen").textContent = "0";
  document.getElementById("sPending").textContent = "0";
  document.getElementById("sNA").textContent = "0";
}}
async function loadAlerts() {{
  const dept = document.getElementById("filterDept").value.trim();
  const url = dept ? "/api/inpatient/alerts?dept=" + encodeURIComponent(dept) : "/api/inpatient/alerts";
  const r = await fetch(url, {{ headers: authHeaders() }});
  const data = await r.json();
  if (r.status === 401) {{
    clearProtectedInpatientData();
    sessionStorage.removeItem(TOKEN_KEY);
    document.getElementById("mainArea").classList.add("hidden");
    document.getElementById("loginCard").classList.remove("hidden");
    showMsg(data.detail || "登录已失效，请重新登录", true);
    return;
  }}
  if (r.status === 403 || !r.ok) {{
    clearProtectedInpatientData();
    showMsg(data.detail || "无权访问或加载预警失败", true);
    return;
  }}
  cachedItems = data.items || [];
  const banner = document.getElementById("staleBanner");
  if (data.is_stale && data.stale_warning) {{
    banner.textContent = "⚠️ " + data.stale_warning;
    banner.style.display = "block";
  }} else {{
    banner.style.display = "none";
  }}
  document.getElementById("batchMeta").textContent = "批次: " + (data.batch_id || "-") + " | 截止: " + (data.batch_time || "-");
  updateStats();
  renderRows();
}}
function updateStats() {{
  let r = 0, y = 0, g = 0, p = 0, na = 0;
  for (const it of cachedItems) {{
    if (it.alert_level === "RED") r++;
    else if (it.alert_level === "YELLOW") y++;
    else if (it.alert_level === "GREEN") g++;
    else if (it.alert_level === "NOT_APPLICABLE") na++;
    else if (it.alert_level === "PENDING") p++;
  }}
  document.getElementById("sTotal").textContent = String(cachedItems.length);
  document.getElementById("sRed").textContent = String(r);
  document.getElementById("sYellow").textContent = String(y);
  document.getElementById("sGreen").textContent = String(g);
  document.getElementById("sPending").textContent = String(p);
  document.getElementById("sNA").textContent = String(na);
}}
function renderRows() {{
  const level = document.getElementById("filterLevel").value;
  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  const list = level ? cachedItems.filter(x => x.alert_level === level) : cachedItems;
  for (const it of list) {{
    const tr = document.createElement("tr");
    const vals = [
      it.zyh || "-", it.dept_name || "-", it.bed || "-", it.doctor_id || "-",
      it.disease || "-", it.ins_type || "-",
      it.current_cost_str || String(it.current_cost ?? "-"),
      it.benchmark_str || (it.benchmark != null ? String(it.benchmark) : "-"),
      it.ratio_percent != null ? it.ratio_percent + "%" : "-"
    ];
    for (const v of vals) {{
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    }}
    const tdSt = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge badge-" + (it.alert_level || "PENDING");
    badge.textContent = it.status || it.alert_level || "-";
    tdSt.appendChild(badge);
    tr.appendChild(tdSt);
    tbody.appendChild(tr);
  }}
}}
if (getToken()) {{
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  loadAlerts();
}}
</script>
</body>
</html>
"""


ANALYTICS_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>医生 DIP 控费绩效与历史趋势看板</title>
<style>{_COMMON_STYLE}</style>
</head>
<body>
<header>
  <h1>📊 院内 DIP 控费工作台 · 医生绩效与历史趋势</h1>
  <nav>{_nav_html("analytics")}</nav>
</header>
<div class="wrap">
  <div id="msg"></div>
  <div class="card" id="loginCard">
    <h2>身份认证</h2>
    <div class="row">
      <div class="field"><label>工号</label><input id="loginId" placeholder="请输入工号"></div>
      <div class="field"><label>口令</label><input id="loginPwd" type="password" placeholder="请输入口令"></div>
      <button onclick="doLogin()">登录</button>
    </div>
  </div>

  <div id="mainArea" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center;">
        <div class="row" style="flex:1;">
          <div class="field" style="max-width:160px;">
            <label>统计周期</label>
            <select id="daysSel" onchange="loadReport()">
              <option value="7">近 7 天</option>
              <option value="30" selected>近 30 天</option>
              <option value="90">近 90 天</option>
            </select>
          </div>
          <div class="field" style="max-width:180px;">
            <label>科室 ID (可选)</label>
            <input id="deptId" placeholder="如 101">
          </div>
          <button onclick="loadReport()">刷新报表</button>
        </div>
        <button class="danger" onclick="doLogout()">退出登录</button>
      </div>
    </div>

    <div class="card">
      <h2>历史批次红/黄/绿趋势分布</h2>
      <div id="trendSummary" style="font-size:13px;color:#475569;margin-bottom:8px;"></div>
      <div style="overflow-x:auto;border:1px solid var(--border);border-radius:6px;background:#f8fafc;">
        <svg id="trendSvg" height="200" viewBox="0 0 800 200" style="display:block;min-width:800px;"></svg>
      </div>
    </div>

    <div class="card">
      <h2>医生控费绩效聚合</h2>
      <table>
        <thead>
          <tr><th>医生工号</th><th>科室</th><th>病例数</th><th>红灯</th><th>黄灯</th><th>绿灯</th><th>绿灯达标率</th><th>平均费用比</th><th>总费用(元)</th><th>总基准(元)</th></tr>
        </thead>
        <tbody id="docTbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const TOKEN_KEY = "hospital_admin_token";
function showMsg(text, isErr) {{
  const el = document.getElementById("msg");
  el.textContent = text;
  el.className = isErr ? "err" : "ok";
}}
function getToken() {{ return sessionStorage.getItem(TOKEN_KEY) || ""; }}
function authHeaders() {{ return {{ "Authorization": "Bearer " + getToken() }}; }}
async function doLogin() {{
  const operator_id = document.getElementById("loginId").value.trim();
  const password = document.getElementById("loginPwd").value;
  const r = await fetch("/api/auth/login", {{
    method: "POST", headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ operator_id, password }})
  }});
  const data = await r.json();
  if (!r.ok) {{ showMsg(data.detail || "登录失败", true); return; }}
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  loadReport();
}}
function doLogout() {{ sessionStorage.removeItem(TOKEN_KEY); location.reload(); }}
function clearProtectedAnalyticsData() {{
  document.getElementById("docTbody").innerHTML = "";
  document.getElementById("trendSummary").textContent = "";
  const svg = document.getElementById("trendSvg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
}}
async function loadReport() {{
  const days = document.getElementById("daysSel").value;
  const dept = document.getElementById("deptId").value.trim();
  let url = "/api/analytics/doctor-performance?days=" + encodeURIComponent(days);
  if (dept) url += "&dept_id=" + encodeURIComponent(dept);
  const r = await fetch(url, {{ headers: authHeaders() }});
  const data = await r.json();
  if (r.status === 401) {{
    clearProtectedAnalyticsData();
    sessionStorage.removeItem(TOKEN_KEY);
    document.getElementById("mainArea").classList.add("hidden");
    document.getElementById("loginCard").classList.remove("hidden");
    showMsg(data.detail || "登录已失效，请重新登录", true);
    return;
  }}
  if (r.status === 403 || !r.ok) {{
    clearProtectedAnalyticsData();
    showMsg(data.detail || "无权访问或加载报表失败", true);
    return;
  }}
  renderDoctors(data.doctors || []);
  renderTrends(data.trends || []);
  showMsg("已加载近 " + data.days + " 天共 " + data.total_snapshots + " 条快照记录", false);
}}
function renderDoctors(docs) {{
  const tbody = document.getElementById("docTbody");
  tbody.innerHTML = "";
  for (const d of docs) {{
    const tr = document.createElement("tr");
    const vals = [
      d.doctor_id, d.dept_name || "-", String(d.total_cases),
      String(d.red_count), String(d.yellow_count), String(d.green_count),
      d.green_rate_percent != null ? d.green_rate_percent + "%" : "-",
      d.avg_fee_ratio_percent != null ? d.avg_fee_ratio_percent + "%" : "-",
      d.total_fee_str || String(d.total_fee),
      d.total_benchmark_str || String(d.total_benchmark)
    ];
    for (const v of vals) {{
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    }}
    tbody.appendChild(tr);
  }}
}}
function renderTrends(trends) {{
  const svg = document.getElementById("trendSvg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  document.getElementById("trendSummary").textContent = "共 " + trends.length + " 个历史扫描批次";
  if (!trends.length) return;
  const ns = "http://www.w3.org/2000/svg";
  const maxTotal = Math.max(...trends.map(t => (t.green_count || 0) + (t.yellow_count || 0) + (t.red_count || 0)), 1);
  const barW = 28;
  const gap = 10;
  const svgW = Math.max(800, 60 + trends.length * (barW + gap));
  svg.setAttribute("viewBox", "0 0 " + svgW + " 200");
  svg.style.minWidth = svgW + "px";
  trends.forEach((t, i) => {{
    const x = 30 + i * (barW + gap);
    const gH = Math.round(((t.green_count || 0) / maxTotal) * 140);
    const yH = Math.round(((t.yellow_count || 0) / maxTotal) * 140);
    const rH = Math.round(((t.red_count || 0) / maxTotal) * 140);
    const addRect = (y, h, fill) => {{
      if (h <= 0) return;
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", String(x));
      rect.setAttribute("y", String(y));
      rect.setAttribute("width", String(barW));
      rect.setAttribute("height", String(h));
      rect.setAttribute("fill", fill);
      svg.appendChild(rect);
    }};
    addRect(165 - gH, gH, "#2e7d32");
    addRect(165 - gH - yH, yH, "#d97706");
    addRect(165 - gH - yH - rH, rH, "#c62828");
    const txt = document.createElementNS(ns, "text");
    txt.setAttribute("x", String(x));
    txt.setAttribute("y", "185");
    txt.setAttribute("font-size", "10");
    txt.setAttribute("fill", "#475569");
    txt.textContent = (t.cutoff_time || t.batch_id || "").slice(5, 16);
    svg.appendChild(txt);
  }});
}}
if (getToken()) {{
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  loadReport();
}}
</script>
</body>
</html>
"""


DIP_RULES_ADMIN_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DIP 新版规则目录批量导入控制台</title>
<style>{_COMMON_STYLE}</style>
</head>
<body>
<header>
  <h1>📥 院内 DIP 控费工作台 · 规则批量导入</h1>
  <nav>{_nav_html("dip-rules")}</nav>
</header>
<div class="wrap">
  <div id="msg"></div>
  <div class="card" id="loginCard">
    <h2>管理员 / 医保办认证</h2>
    <div class="row">
      <div class="field"><label>工号</label><input id="loginId" placeholder="请输入工号"></div>
      <div class="field"><label>口令</label><input id="loginPwd" type="password" placeholder="请输入口令"></div>
      <button onclick="doLogin()">登录</button>
    </div>
  </div>

  <div id="mainArea" class="hidden">
    <div class="card">
      <h2>上传或粘贴 CSV 规则目录</h2>
      <div class="row">
        <div class="field"><label>规则版本号 *</label><input id="rVer" placeholder="如 2026_V2"></div>
        <div class="field"><label>来源发文编号 *</label><input id="rDoc" placeholder="如 包医保发〔2026〕12号"></div>
        <div class="field"><label>生效日期</label><input id="rDate" type="date"></div>
      </div>
      <div class="row" style="margin-top:10px;">
        <div class="field"><label>选择本地 CSV 文件 (可选)</label><input id="csvFile" type="file" accept=".csv" onchange="loadCsvFile()"></div>
        <label style="display:flex;align-items:center;gap:4px;"><input id="chkDry" type="checkbox" checked> 仅预检 (dry-run)</label>
        <label style="display:flex;align-items:center;gap:4px;"><input id="chkApprove" type="checkbox"> 直接置为 APPROVED</label>
        <label style="display:flex;align-items:center;gap:4px;"><input id="chkReplace" type="checkbox"> 覆盖同版本号旧规则</label>
      </div>
      <div class="field" style="margin-top:10px;">
        <label>CSV 文本内容（表头支持：科室,病种分组,ICD编码,分值,职工点值,居民点值,医院系数）</label>
        <textarea id="csvText" rows="8" placeholder="科室,病种分组,ICD编码,分值&#10;中医科,膝关节病保守治疗,M17.9,620.20"></textarea>
      </div>
      <div class="row" style="margin-top:12px;justify-content:space-between;">
        <button onclick="submitImport()">执行校验 / 导入</button>
        <button class="danger" onclick="doLogout()">退出登录</button>
      </div>
    </div>

    <div class="card">
      <h2>执行结果</h2>
      <pre id="resultBox" style="background:#f8fafc;padding:12px;border-radius:6px;font-size:12px;overflow:auto;">尚未执行</pre>
    </div>
  </div>
</div>

<script>
const TOKEN_KEY = "hospital_admin_token";
function showMsg(text, isErr) {{
  const el = document.getElementById("msg");
  el.textContent = text;
  el.className = isErr ? "err" : "ok";
}}
function getToken() {{ return sessionStorage.getItem(TOKEN_KEY) || ""; }}
function authHeaders() {{ return {{ "Content-Type": "application/json", "Authorization": "Bearer " + getToken() }}; }}
async function doLogin() {{
  const operator_id = document.getElementById("loginId").value.trim();
  const password = document.getElementById("loginPwd").value;
  const r = await fetch("/api/auth/login", {{
    method: "POST", headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ operator_id, password }})
  }});
  const data = await r.json();
  if (!r.ok) {{ showMsg(data.detail || "登录失败", true); return; }}
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  showMsg("登录成功", false);
}}
function doLogout() {{ sessionStorage.removeItem(TOKEN_KEY); location.reload(); }}
function loadCsvFile() {{
  const file = document.getElementById("csvFile").files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {{ document.getElementById("csvText").value = e.target.result || ""; }};
  reader.readAsText(file, "UTF-8");
}}
async function submitImport() {{
  const payload = {{
    rule_version: document.getElementById("rVer").value.trim(),
    source_doc: document.getElementById("rDoc").value.trim(),
    effective_date: document.getElementById("rDate").value || null,
    dry_run: document.getElementById("chkDry").checked,
    approve: document.getElementById("chkApprove").checked,
    replace_version: document.getElementById("chkReplace").checked,
    csv_text: document.getElementById("csvText").value
  }};
  const r = await fetch("/api/admin/dip-rules/import", {{
    method: "POST", headers: authHeaders(), body: JSON.stringify(payload)
  }});
  const data = await r.json();
  document.getElementById("resultBox").textContent = JSON.stringify(data, null, 2);
  if (!r.ok || !data.success) {{
    showMsg(data.detail || "校验/导入存在错误，请查看下方结果", true);
  }} else {{
    showMsg((payload.dry_run ? "预检通过" : "导入成功") + "：有效行数 " + data.valid_rows, false);
  }}
}}
if (getToken()) {{
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
}}
</script>
</body>
</html>
"""


AUDIT_LOGS_ADMIN_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>合规审计日志监控台</title>
<style>{_COMMON_STYLE}</style>
</head>
<body>
<header>
  <h1>🛡️ 院内 DIP 控费工作台 · 合规审计日志</h1>
  <nav>{_nav_html("audit-logs")}</nav>
</header>
<div class="wrap">
  <div id="msg"></div>
  <div class="card" id="loginCard">
    <h2>管理员身份认证</h2>
    <div class="row">
      <div class="field"><label>工号</label><input id="loginId" placeholder="请输入管理员工号"></div>
      <div class="field"><label>口令</label><input id="loginPwd" type="password" placeholder="请输入口令"></div>
      <button onclick="doLogin()">登录</button>
    </div>
  </div>

  <div id="mainArea" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center;">
        <div class="row" style="flex:1;">
          <div class="field" style="max-width:150px;">
            <label>时间范围</label>
            <select id="daysSel">
              <option value="1">最近 1 天</option>
              <option value="7" selected>最近 7 天</option>
              <option value="30">最近 30 天</option>
            </select>
          </div>
          <div class="field" style="max-width:150px;">
            <label>鉴权结果</label>
            <select id="authResSel">
              <option value="">全部</option>
              <option value="PERMITTED">PERMITTED (允许)</option>
              <option value="DENIED">DENIED (拒绝/拦截)</option>
            </select>
          </div>
          <div class="field" style="max-width:180px;">
            <label>操作类型 (可选)</label>
            <input id="actionFilter" placeholder="如 LOGIN / CHAT_INTENT">
          </div>
          <button onclick="loadLogs()">查询日志</button>
        </div>
        <button class="danger" onclick="doLogout()">退出登录</button>
      </div>
    </div>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h2 style="border:none;margin:0;padding:0;">审计记录列表（已脱敏，不含原始文本）</h2>
        <span id="logCount" style="font-size:12px;color:#64748b;"></span>
      </div>
      <table>
        <thead>
          <tr><th>ID</th><th>时间</th><th>操作员</th><th>角色</th><th>渠道</th><th>动作类型</th><th>目标引用</th><th>结果</th><th>版本</th></tr>
        </thead>
        <tbody id="logTbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const TOKEN_KEY = "hospital_admin_token";
function showMsg(text, isErr) {{
  const el = document.getElementById("msg");
  el.textContent = text;
  el.className = isErr ? "err" : "ok";
}}
function getToken() {{ return sessionStorage.getItem(TOKEN_KEY) || ""; }}
function authHeaders() {{ return {{ "Authorization": "Bearer " + getToken() }}; }}
function clearProtectedAuditData() {{
  document.getElementById("logTbody").innerHTML = "";
  document.getElementById("logCount").textContent = "";
}}
async function doLogin() {{
  const operator_id = document.getElementById("loginId").value.trim();
  const password = document.getElementById("loginPwd").value;
  const r = await fetch("/api/auth/login", {{
    method: "POST", headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ operator_id, password }})
  }});
  const data = await r.json();
  if (!r.ok) {{ showMsg(data.detail || "登录失败", true); return; }}
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  loadLogs();
}}
function doLogout() {{ sessionStorage.removeItem(TOKEN_KEY); location.reload(); }}
async function loadLogs() {{
  const days = document.getElementById("daysSel").value;
  const authRes = document.getElementById("authResSel").value;
  const action = document.getElementById("actionFilter").value.trim();
  let url = "/api/admin/audit-logs?days=" + encodeURIComponent(days);
  if (authRes) url += "&auth_result=" + encodeURIComponent(authRes);
  if (action) url += "&action_type=" + encodeURIComponent(action);
  const r = await fetch(url, {{ headers: authHeaders() }});
  const data = await r.json();
  if (r.status === 401) {{
    clearProtectedAuditData();
    sessionStorage.removeItem(TOKEN_KEY);
    document.getElementById("mainArea").classList.add("hidden");
    document.getElementById("loginCard").classList.remove("hidden");
    showMsg(data.detail || "登录已失效，请重新登录", true);
    return;
  }}
  if (r.status === 403 || !r.ok) {{
    clearProtectedAuditData();
    showMsg(data.detail || "无权查看审计日志（仅限管理员）", true);
    return;
  }}
  const items = data.items || [];
  document.getElementById("logCount").textContent = "共返回 " + items.length + " 条";
  const tbody = document.getElementById("logTbody");
  tbody.innerHTML = "";
  for (const row of items) {{
    const tr = document.createElement("tr");
    const vals = [
      String(row.log_id || "-"),
      row.created_at || "-",
      row.operator_id || "-",
      row.role_type || "-",
      row.channel || "-",
      row.action_type || "-",
      row.target_ref || "-",
    ];
    for (const v of vals) {{
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    }}
    const tdRes = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + (row.auth_result === "DENIED" ? "badge-RED" : "badge-GREEN");
    badge.textContent = row.auth_result || "-";
    tdRes.appendChild(badge);
    tr.appendChild(tdRes);

    const tdVer = document.createElement("td");
    tdVer.textContent = row.rule_version || "-";
    tr.appendChild(tdVer);
    tbody.appendChild(tr);
  }}
  showMsg("审计日志已刷新", false);
}}
if (getToken()) {{
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  loadLogs();
}}
</script>
</body>
</html>
"""
