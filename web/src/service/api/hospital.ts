import { localStg } from '@/utils/storage';
import { useAuthStore } from '@/store/modules/auth';

export class HospitalApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

let currentAuthEpoch = 0;

export function bumpHospitalAuthEpoch() {
  currentAuthEpoch += 1;
  return currentAuthEpoch;
}

export function getHospitalAuthEpoch() {
  return currentAuthEpoch;
}

async function parseErrorDetail(resp: Response, fallback: string): Promise<string> {
  try {
    const data = await resp.json();
    if (data && typeof data === 'object' && typeof data.detail === 'string' && data.detail) {
      return data.detail;
    }
  } catch {
    // ignore non-json error body
  }
  return fallback;
}

export async function hospitalRequest<T>(
  url: string,
  options: RequestInit = {},
  config: { requireAuth?: boolean; silent401Logout?: boolean } = {}
): Promise<T> {
  const { requireAuth = true, silent401Logout = false } = config;
  const myEpoch = currentAuthEpoch;
  const token = localStg.get('token');

  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const resp = await fetch(url, { ...options, headers });

  if (requireAuth && myEpoch !== currentAuthEpoch) {
    throw new HospitalApiError('会话已切换，忽略过期请求响应', 499);
  }

  if (resp.status === 401) {
    if (!silent401Logout && requireAuth) {
      const authStore = useAuthStore();
      await authStore.resetStore();
    }
    const detail = await parseErrorDetail(resp, '登录状态已失效，请重新登录');
    throw new HospitalApiError(detail, 401);
  }

  if (!resp.ok) {
    const detail = await parseErrorDetail(resp, `请求失败 (${resp.status})`);
    throw new HospitalApiError(detail, resp.status);
  }

  let data: unknown;
  try {
    data = await resp.json();
  } catch {
    throw new HospitalApiError('服务端返回非 JSON 响应内容', resp.status);
  }

  if (requireAuth && myEpoch !== currentAuthEpoch) {
    throw new HospitalApiError('会话已切换，忽略过期请求响应', 499);
  }

  return data as T;
}

export async function downloadHospitalCsv(url: string, filename: string): Promise<void> {
  const myEpoch = currentAuthEpoch;
  const token = localStg.get('token');
  const headers: Record<string, string> = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const resp = await fetch(url, { headers });
  if (myEpoch !== currentAuthEpoch) {
    throw new HospitalApiError('会话已切换，忽略过期导出响应', 499);
  }

  if (resp.status === 401) {
    const authStore = useAuthStore();
    await authStore.resetStore();
    const detail = await parseErrorDetail(resp, '登录状态已失效，请重新登录');
    throw new HospitalApiError(detail, 401);
  }

  if (!resp.ok) {
    const detail = await parseErrorDetail(resp, `导出失败 (${resp.status})`);
    throw new HospitalApiError(detail, resp.status);
  }

  const blob = await resp.blob();
  if (myEpoch !== currentAuthEpoch) {
    throw new HospitalApiError('会话已切换，忽略过期导出文件', 499);
  }

  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------------- Types & Endpoints ----------------

export interface InpatientAlertItem {
  zyh: string;
  dept_id: string | null;
  dept_name: string;
  dept: string;
  ward: string;
  bed: string;
  doctor_id: string;
  doctor_name?: string | null;
  disease: string;
  ins_type: string;
  matched_rule_id?: number | null;
  current_cost: number;
  current_cost_str: string;
  benchmark: number | null;
  benchmark_str: string | null;
  light: string;
  alert_level: string;
  status: string;
  pending_reason: string | null;
  ratio_percent: number | null;
}

export interface InpatientAlertsResponse {
  is_demo_baseline: boolean;
  scope?: 'active' | 'month';
  year_month?: string | null;
  total: number;
  total_scanned?: number | null;
  scan_limit?: number | null;
  is_truncated?: boolean | null;
  has_batch_meta?: boolean;
  authorized_scope?: 'DEPARTMENT' | 'HOSPITAL';
  batch_id: string | null;
  batch_time: string | null;
  is_stale: boolean;
  stale_warning: string | null;
  items: InpatientAlertItem[];
}

export function fetchInpatientAlerts(dept?: string, scope: 'active' | 'month' = 'month', yearMonth?: string) {
  const qs = new URLSearchParams();
  if (dept) qs.set('dept', dept);
  if (scope) qs.set('scope', scope);
  if (scope === 'month' && yearMonth) qs.set('year_month', yearMonth);
  const query = qs.toString() ? `?${qs.toString()}` : '';
  return hospitalRequest<InpatientAlertsResponse>(`/api/inpatient/alerts${query}`);
}

export function triggerManualScan(
  limit?: number,
  notifyDingtalk = false,
  scope: 'active' | 'month' = 'active',
  yearMonth?: string
) {
  const body: Record<string, any> = { notify_dingtalk: notifyDingtalk, scope };
  if (typeof limit === 'number' && limit > 0) body.limit = limit;
  if (scope === 'month' && yearMonth) body.year_month = yearMonth;
  return hospitalRequest<{
    success: boolean;
    batch_id: string;
    rule_version: string;
    data_cutoff_time: string;
    total_scanned: number;
    scan_limit?: number | null;
    is_truncated?: boolean;
    year_month?: string | null;
    GREEN: number;
    YELLOW: number;
    RED: number;
    PENDING: number;
    NOT_APPLICABLE: number;
  }>('/api/admin/trigger-scan', {
    method: 'POST',
    body: JSON.stringify(body)
  });
}

export interface DoctorPerformanceItem {
  doctor_id: string;
  doctor_name?: string | null;
  dept_id: string | null;
  dept_name: string;
  total_cases: number;
  green_count: number;
  yellow_count: number;
  red_count: number;
  pending_count: number;
  not_applicable_count: number;
  green_rate_percent: number | null;
  red_rate_percent: number | null;
  avg_fee_ratio_percent: number | null;
  total_fee: number;
  total_fee_str: string;
  total_benchmark: number;
  total_benchmark_str: string;
}

export interface TrendBatchItem {
  batch_id: string;
  cutoff_time: string;
  total: number;
  green_count: number;
  yellow_count: number;
  red_count: number;
  pending_count: number;
  not_applicable_count: number;
  avg_fee_ratio_percent: number | null;
}

export interface DoctorPerformanceResponse {
  days: number;
  total_snapshots: number;
  doctors: DoctorPerformanceItem[];
  trends: TrendBatchItem[];
}

export function fetchDoctorPerformance(days = 30, deptId?: string) {
  const params = new URLSearchParams({ days: String(days) });
  if (deptId) params.set('dept_id', deptId);
  return hospitalRequest<DoctorPerformanceResponse>(`/api/analytics/doctor-performance?${params.toString()}`);
}

export interface ReconciliationItem {
  visit_id: string;
  settlement_id: string;
  dept_name: string;
  settlement_time: string;
  reconciliation_scope: string;
  settlement_count: number;
  recon_status: 'MATCHED' | 'UNDISPENSED_ONLY' | 'MISMATCH' | string;
  total_amount: number;
  total_amount_str: string;
  category_sum: number;
  category_diff: number;
  category_diff_str: string;
  clinical_diff: number;
  clinical_diff_str: string;
  pharmacy_diff: number;
  pharmacy_diff_str: string;
  undispensed_count: number;
  undispensed_amount: number;
  undispensed_amount_str: string;
}

export interface ReconciliationReport {
  sampled_count: number;
  reconciled_count: number;
  unreconciled_count: number;
  unreconciled_reasons: { visit_id: string; reason: string }[];
  total: number;
  matched_count: number;
  undispensed_only_count: number;
  mismatch_count: number;
  error_count: number;
  items: ReconciliationItem[];
}

export async function fetchSettledReconciliation(limit = 50, dept?: string): Promise<ReconciliationReport> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (dept) params.set('dept', dept);
  const data = await hospitalRequest<unknown>(`/api/reconciliation/settled?${params.toString()}`);

  // RECON-COVER-001：严格校验覆盖计数之和、明细长度与原因数组及元素完整性，拒绝矛盾/残缺对象冒充有效空批次或引发模板 TypeError
  const isNonNegInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0;
  const isNonEmptyStr = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0;
  const isPlainObj = (v: unknown): v is Record<string, unknown> => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
  if (!isPlainObj(data)) {
    throw new HospitalApiError('对账结果读取失败：响应内容无法解析，请重新查询', 502);
  }
  const rep = data as Partial<ReconciliationReport>;
  if (
    !isNonNegInt(rep.sampled_count) ||
    !isNonNegInt(rep.reconciled_count) ||
    !isNonNegInt(rep.unreconciled_count) ||
    !Array.isArray(rep.items) ||
    !Array.isArray(rep.unreconciled_reasons) ||
    rep.items.length !== rep.reconciled_count ||
    rep.reconciled_count + rep.unreconciled_count !== rep.sampled_count ||
    rep.unreconciled_reasons.length !== rep.unreconciled_count ||
    !rep.items.every(item => isPlainObj(item)) ||
    !rep.unreconciled_reasons.every(item => isPlainObj(item) && isNonEmptyStr(item.visit_id) && isNonEmptyStr(item.reason))
  ) {
    throw new HospitalApiError('对账结果读取失败：核对计数与明细结构不一致，请重新查询', 502);
  }
  return rep as ReconciliationReport;
}

export function queryDipRule(payload: {
  disease: string;
  age?: number | null;
  ins_type?: string | null;
  operation?: string | null;
  bed_days?: number | null;
  rule_version?: string;
}) {
  return hospitalRequest<Record<string, any>>(
    '/api/tools/query_dip_rule',
    { method: 'POST', body: JSON.stringify(payload) },
    { requireAuth: false }
  );
}

export function checkPatientDip(payload: {
  patient_id?: string | null;
  disease: string;
  current_cost: number;
  age?: number | null;
  ins_type?: string | null;
  operation?: string | null;
  bed_days?: number | null;
  rule_version?: string;
}) {
  return hospitalRequest<Record<string, any>>('/api/tools/check_patient_dip', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function sendHospitalChat(text: string) {
  return hospitalRequest<{ intent: string; reply: string; data?: Record<string, any>; tool_executed?: boolean }>(
    '/api/chat',
    { method: 'POST', body: JSON.stringify({ text, channel: 'web' }) },
    { requireAuth: false }
  );
}

export interface DipRuleItem {
  rule_id: number;
  rule_version: string;
  review_status: 'PENDING' | 'APPROVED' | 'ARCHIVED';
  department: string;
  dip_group_name: string;
  icd_code: string;
  billing_unit: string;
  dip_score: number;
  employee_base: number;
  resident_base: number;
}

export function fetchDipRules(params: { rule_version?: string; review_status?: string; limit?: number }) {
  const qs = new URLSearchParams();
  if (params.rule_version) qs.set('rule_version', params.rule_version);
  if (params.review_status) qs.set('review_status', params.review_status);
  qs.set('limit', String(params.limit || 200));
  return hospitalRequest<{ total: number; items: DipRuleItem[] }>(`/api/admin/dip-rules?${qs.toString()}`);
}

export function importDipRules(payload: {
  rule_version: string;
  source_doc: string;
  effective_date?: string | null;
  approve: boolean;
  replace_version: boolean;
  dry_run: boolean;
  csv_text: string;
}) {
  return hospitalRequest<Record<string, any>>('/api/admin/dip-rules/import', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function reviewDipRules(rule_version: string, target_status: 'APPROVED' | 'ARCHIVED') {
  return hospitalRequest<{ success: boolean; rule_version: string; target_status: string; updated_rows: number }>(
    '/api/admin/dip-rules/review',
    { method: 'POST', body: JSON.stringify({ rule_version, target_status }) }
  );
}

export interface AuditLogItem {
  log_id: number;
  operator_id: string;
  role_type: string;
  channel: string;
  action_type: string;
  target_ref: string | null;
  auth_result: 'PERMITTED' | 'DENIED';
  rule_version: string | null;
  batch_id: string | null;
  created_at: string;
}

export function fetchAuditLogs(params: { days: number; limit: number; auth_result?: string; action_type?: string }) {
  const qs = new URLSearchParams({ days: String(params.days), limit: String(params.limit) });
  if (params.auth_result) qs.set('auth_result', params.auth_result);
  if (params.action_type) qs.set('action_type', params.action_type);
  return hospitalRequest<{ days: number; total: number; items: AuditLogItem[] }>(`/api/admin/audit-logs?${qs.toString()}`);
}

export interface OperatorItem {
  operator_id: string;
  display_name: string;
  role_type: string;
  dept_scope: string;
  dingtalk_userid: string | null;
  can_export_case: boolean;
  doctor_id: string | null;
  status: 'ACTIVE' | 'DISABLED';
  updated_at: string;
}

export function fetchOperators() {
  return hospitalRequest<{ total: number; items: OperatorItem[] }>('/api/admin/operators');
}

export interface HisDoctorItem {
  doctor_id: string;
  doctor_name: string;
  zh: string;
  ybysdm: string;
  gjgbm_ybysdm: string;
  dept_id: string;
  dept_name: string;
  is_doctor: boolean;
}

export function fetchHisDoctors(q?: string) {
  const qs = q?.trim() ? `?q=${encodeURIComponent(q.trim())}` : '';
  return hospitalRequest<{ total: number; items: HisDoctorItem[] }>(`/api/admin/his-doctors${qs}`);
}

export function upsertOperator(payload: {
  operator_id: string;
  display_name: string;
  password: string;
  role_type: string;
  dept_scope: string;
  doctor_id: string;
  dingtalk_userid: string;
  can_export_case: boolean;
}) {
  return hospitalRequest<{ success: boolean; operator_id: string; role_type: string }>('/api/admin/operators', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function updateOperatorStatus(operatorId: string, status: 'ACTIVE' | 'DISABLED') {
  return hospitalRequest<{ success: boolean; operator_id: string; status: string }>(
    `/api/admin/operators/${encodeURIComponent(operatorId)}/status`,
    { method: 'PATCH', body: JSON.stringify({ status }) }
  );
}
