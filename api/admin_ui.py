# -*- coding: utf-8 -*-
"""
api/admin_ui.py — 院内操作员管理 Web 控制台（纯本地静态页面，零外部 CDN 依赖）
"""

ADMIN_OPERATORS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>院内操作员权限管理控制台 - DIP 智能业务管理 Agent</title>
<style>
  :root { --bg:#f4f6f9; --card:#ffffff; --primary:#1f5fa8; --danger:#c62828; --ok:#2e7d32; --border:#dce3ec; --text:#1e293b; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); }
  header { background:var(--primary); color:#fff; padding:14px 24px; display:flex; justify-content:space-between; align-items:center; }
  header h1 { margin:0; font-size:18px; font-weight:600; }
  .wrap { max-width:1120px; margin:20px auto; padding:0 16px; display:grid; gap:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:18px; box-shadow:0 1px 3px rgba(0,0,0,0.04); }
  .card h2 { margin:0 0 12px 0; font-size:16px; border-bottom:1px solid var(--border); padding-bottom:8px; }
  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; }
  .field { display:flex; flex-direction:column; gap:4px; min-width:150px; flex:1; }
  label { font-size:12px; color:#475569; font-weight:600; }
  input, select { padding:7px 10px; border:1px solid var(--border); border-radius:5px; font-size:13px; }
  button { padding:8px 14px; border:none; border-radius:5px; cursor:pointer; font-size:13px; font-weight:600; background:var(--primary); color:#fff; }
  button.secondary { background:#64748b; }
  button.danger { background:var(--danger); }
  button.ok { background:var(--ok); }
  table { width:100%; border-collapse:collapse; font-size:13px; margin-top:10px; }
  th, td { border-bottom:1px solid var(--border); padding:9px 8px; text-align:left; }
  th { background:#f8fafc; color:#475569; font-weight:600; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:12px; font-weight:600; }
  .badge-active { background:#dcfce7; color:#166534; }
  .badge-disabled { background:#fee2e2; color:#991b1b; }
  #msg { font-size:13px; padding:8px 12px; border-radius:5px; display:none; }
  #msg.ok { display:block; background:#dcfce7; color:#166534; }
  #msg.err { display:block; background:#fee2e2; color:#991b1b; }
  .hidden { display:none; }
</style>
</head>
<body>
<header>
  <h1>🏥 院内操作员权限管理控制台</h1>
  <div id="userInfo" style="font-size:13px;">未登录</div>
</header>
<div class="wrap">
  <div id="msg"></div>

  <div class="card" id="loginCard">
    <h2>管理员身份认证</h2>
    <div class="row">
      <div class="field"><label>工号 (operator_id)</label><input id="loginId" placeholder="如 admin"></div>
      <div class="field"><label>口令 (password)</label><input id="loginPwd" type="password" placeholder="请输入口令"></div>
      <button onclick="doLogin()">登录控制台</button>
    </div>
  </div>

  <div id="mainArea" class="hidden">
    <div class="card">
      <h2>新增 / 重置操作员账号</h2>
      <div class="row">
        <div class="field"><label>工号 *</label><input id="fId" placeholder="例如 DOC_001"></div>
        <div class="field"><label>姓名 *</label><input id="fName" placeholder="例如 张医生"></div>
        <div class="field"><label>角色 *</label>
          <select id="fRole">
            <option value="DOCTOR">DOCTOR (管床医生)</option>
            <option value="DEPT_HEAD">DEPT_HEAD (科室主任)</option>
            <option value="INSURANCE_OFFICER">INSURANCE_OFFICER (医保办)</option>
            <option value="ADMIN">ADMIN (系统管理员)</option>
          </select>
        </div>
        <div class="field"><label>科室范围 (dept_scope / ID)</label><input id="fDept" placeholder="如 101 或 101,102"></div>
        <div class="field"><label>绑定 HIS 医生工号</label><input id="fDocId" placeholder="如 13（HIS ryid）"></div>
        <div class="field">
          <label>从 HIS 选择医生（自动带出工号/姓名/科室）</label>
          <div style="display:flex;gap:6px;">
            <select id="hisDocSelect" onchange="applyHisDoctor()" style="flex:1;min-width:160px;">
              <option value="">-- 点击右侧同步 HIS 医生 --</option>
            </select>
            <button type="button" class="secondary" onclick="loadHisDoctors()">同步 HIS</button>
          </div>
        </div>
        <div class="field"><label>初始/重置口令 (至少8位) *</label><input id="fPwd" type="password" placeholder="至少 8 位字符"></div>
        <button onclick="saveOperator()">保存账号</button>
      </div>
    </div>

    <div class="card" style="margin-top:16px;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h2 style="border:none;margin:0;padding:0;">操作员列表</h2>
        <div>
          <button class="secondary" onclick="loadOperators()">刷新列表</button>
          <button class="danger" onclick="doLogout()" style="margin-left:6px;">退出</button>
        </div>
      </div>
      <table>
        <thead>
          <tr><th>工号</th><th>姓名</th><th>角色</th><th>科室范围</th><th>医生工号</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody id="opTbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const TOKEN_KEY = "hospital_admin_token";
function showMsg(text, isErr) {
  const el = document.getElementById("msg");
  el.textContent = text;
  el.className = isErr ? "err" : "ok";
}
function getToken() { return sessionStorage.getItem(TOKEN_KEY) || ""; }
function authHeaders() {
  return { "Content-Type": "application/json", "Authorization": "Bearer " + getToken() };
}
async function doLogin() {
  const operator_id = document.getElementById("loginId").value.trim();
  const password = document.getElementById("loginPwd").value;
  const r = await fetch("/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operator_id, password })
  });
  const data = await r.json();
  if (!r.ok) { showMsg(data.detail || "登录失败", true); return; }
  if (data.role_type !== "ADMIN") { showMsg("仅 ADMIN 角色可进入操作员管理控制台", true); return; }
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  document.getElementById("userInfo").textContent = data.display_name + " (" + data.operator_id + ")";
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  showMsg("登录成功", false);
  loadOperators();
}
function doLogout() {
  sessionStorage.removeItem(TOKEN_KEY);
  location.reload();
}
let cachedHisDocs = [];
async function loadHisDoctors() {
  const sel = document.getElementById("hisDocSelect");
  const r = await fetch("/api/admin/his-doctors", { headers: authHeaders() });
  const data = await r.json();
  if (!r.ok) { showMsg(data.detail || "同步 HIS 医生列表失败", true); return; }
  cachedHisDocs = data.items || [];
  sel.innerHTML = "";
  const opt0 = document.createElement("option");
  opt0.value = "";
  opt0.textContent = "-- 请选择 HIS 医生 (" + cachedHisDocs.length + " 人) --";
  sel.appendChild(opt0);
  for (const d of cachedHisDocs) {
    const opt = document.createElement("option");
    opt.value = d.doctor_id || "";
    opt.textContent = (d.doctor_name || "未命名") + " (工号:" + (d.doctor_id || "-") + (d.dept_name ? " / " + d.dept_name : "") + ")";
    sel.appendChild(opt);
  }
  showMsg("已同步 HIS 医生字典共 " + cachedHisDocs.length + " 人", false);
}
function applyHisDoctor() {
  const val = document.getElementById("hisDocSelect").value;
  if (!val) return;
  const doc = cachedHisDocs.find(x => x.doctor_id === val);
  if (!doc) return;
  document.getElementById("fDocId").value = doc.doctor_id || "";
  if (!document.getElementById("fName").value.trim() && doc.doctor_name) {
    document.getElementById("fName").value = doc.doctor_name;
  }
  if (!document.getElementById("fDept").value.trim() && doc.dept_id) {
    document.getElementById("fDept").value = doc.dept_id;
  }
  if (!document.getElementById("fId").value.trim() && doc.doctor_id) {
    document.getElementById("fId").value = "DOC_" + doc.doctor_id;
  }
}
async function loadOperators() {
  const r = await fetch("/api/admin/operators", { headers: authHeaders() });
  const data = await r.json();
  if (!r.ok) { showMsg(data.detail || "加载失败", true); return; }
  const tbody = document.getElementById("opTbody");
  tbody.innerHTML = "";
  for (const op of (data.items || [])) {
    const tr = document.createElement("tr");
    const cells = [op.operator_id, op.display_name, op.role_type, op.dept_scope || "-", op.doctor_id || "-"];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    const tdSt = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + (op.status === "ACTIVE" ? "badge-active" : "badge-disabled");
    badge.textContent = op.status;
    tdSt.appendChild(badge);
    tr.appendChild(tdSt);

    const tdAct = document.createElement("td");
    const btn = document.createElement("button");
    const nextStatus = op.status === "ACTIVE" ? "DISABLED" : "ACTIVE";
    btn.className = op.status === "ACTIVE" ? "danger" : "ok";
    btn.textContent = op.status === "ACTIVE" ? "停用" : "启用";
    btn.onclick = () => toggleStatus(op.operator_id, nextStatus);
    tdAct.appendChild(btn);
    tr.appendChild(tdAct);
    tbody.appendChild(tr);
  }
}
async function saveOperator() {
  const payload = {
    operator_id: document.getElementById("fId").value.trim(),
    display_name: document.getElementById("fName").value.trim(),
    role_type: document.getElementById("fRole").value,
    dept_scope: document.getElementById("fDept").value.trim(),
    doctor_id: document.getElementById("fDocId").value.trim(),
    password: document.getElementById("fPwd").value
  };
  const r = await fetch("/api/admin/operators", {
    method: "POST", headers: authHeaders(), body: JSON.stringify(payload)
  });
  const data = await r.json();
  if (!r.ok) { showMsg(data.detail || "保存失败", true); return; }
  showMsg("操作员 " + payload.operator_id + " 已保存", false);
  document.getElementById("fPwd").value = "";
  loadOperators();
}
async function toggleStatus(opId, status) {
  const r = await fetch("/api/admin/operators/" + encodeURIComponent(opId) + "/status", {
    method: "PATCH", headers: authHeaders(), body: JSON.stringify({ status })
  });
  const data = await r.json();
  if (!r.ok) { showMsg(data.detail || "状态更新失败", true); return; }
  showMsg("已更新 " + opId + " 状态为 " + status, false);
  loadOperators();
}
if (getToken()) {
  document.getElementById("loginCard").classList.add("hidden");
  document.getElementById("mainArea").classList.remove("hidden");
  loadOperators();
}
</script>
</body>
</html>
"""
