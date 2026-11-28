<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue';
import type { DataTableColumns } from 'naive-ui';
import { NTag } from 'naive-ui';
import { useAuthStore } from '@/store/modules/auth';
import {
  downloadHospitalCsv,
  fetchInpatientAlerts,
  triggerManualScan,
  type InpatientAlertItem,
  type InpatientAlertsResponse
} from '@/service/api/hospital';

defineOptions({ name: 'InpatientAlertsPage' });

const authStore = useAuthStore();
const loading = ref(false);
const scanning = ref(false);
const filterScope = ref<'month' | 'active'>('month');
const filterYearMonth = ref<string>(
  `${new Date().getFullYear()}-${String(new Date().getMonth() + 1).padStart(2, '0')}`
);
const filterDept = ref('');
const filterLevel = ref<string | null>(null);
const notifyDingtalk = ref(false);
const report = ref<InpatientAlertsResponse | null>(null);

const canScan = computed(() => authStore.userInfo.roles.some(r => ['ADMIN', 'INSURANCE_OFFICER'].includes(r)));
const canExport = computed(() => Boolean(authStore.userInfo.canExportCase));
const isDeptOnlyScope = computed(() => report.value?.authorized_scope === 'DEPARTMENT');

const scopeOptions = [
  { label: '本月全部患者（含出院结算）', value: 'month' },
  { label: '仅当前在院患者', value: 'active' }
];

const levelOptions = [
  { label: '全部灯号', value: '' },
  { label: '红灯 (>110%)', value: 'RED' },
  { label: '黄灯 (90%~110%)', value: 'YELLOW' },
  { label: '绿灯 (<90%)', value: 'GREEN' },
  { label: '待定核查', value: 'PENDING' },
  { label: '自费不适用', value: 'NOT_APPLICABLE' }
];

const filteredItems = computed(() => {
  const list = report.value?.items || [];
  if (!filterLevel.value) return list;
  return list.filter(x => x.alert_level === filterLevel.value);
});

const counts = computed(() => {
  const list = report.value?.items || [];
  let red = 0;
  let yellow = 0;
  let green = 0;
  let pending = 0;
  let na = 0;
  for (const it of list) {
    if (it.alert_level === 'RED') red += 1;
    else if (it.alert_level === 'YELLOW') yellow += 1;
    else if (it.alert_level === 'GREEN') green += 1;
    else if (it.alert_level === 'PENDING') pending += 1;
    else if (it.alert_level === 'NOT_APPLICABLE') na += 1;
  }
  return { total: list.length, red, yellow, green, pending, na };
});

const columns: DataTableColumns<InpatientAlertItem> = [
  { title: '住院号', key: 'zyh', width: 130 },
  { title: '科室', key: 'dept_name', width: 110 },
  { title: '床号', key: 'bed', width: 80 },
  { title: '医生工号', key: 'doctor_id', width: 110 },
  { title: '医生姓名', key: 'doctor_name', width: 105, render: row => row.doctor_name || '-' },
  { title: '主诊断', key: 'disease', minWidth: 130 },
  { title: '参保类型', key: 'ins_type', width: 95 },
  { title: '当前费用(元)', key: 'current_cost_str', width: 120 },
  {
    title: 'DIP 基准(元)',
    key: 'benchmark_str',
    width: 120,
    render: row => row.benchmark_str || '-'
  },
  {
    title: '费用占比',
    key: 'ratio_percent',
    width: 95,
    render: row => (row.ratio_percent !== null ? `${row.ratio_percent}%` : '-')
  },
  {
    title: '预警状态',
    key: 'alert_level',
    minWidth: 165,
    render: row => {
      const tagMap: Record<string, 'error' | 'warning' | 'success' | 'default' | 'info'> = {
        RED: 'error',
        YELLOW: 'warning',
        GREEN: 'success',
        PENDING: 'default',
        NOT_APPLICABLE: 'info'
      };
      return h(NTag, { type: tagMap[row.alert_level] || 'default', size: 'small', bordered: false }, () => row.status);
    }
  }
];

let inpReqSeq = 0;

async function loadData() {
  const mySeq = ++inpReqSeq;
  loading.value = true;
  try {
    const ym = filterScope.value === 'month' ? filterYearMonth.value.trim() || undefined : undefined;
    const res = await fetchInpatientAlerts(filterDept.value.trim() || undefined, filterScope.value, ym);
    if (mySeq !== inpReqSeq) return;
    report.value = res;
  } catch (err: any) {
    if (mySeq !== inpReqSeq) return;
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '加载控费预警失败');
    }
  } finally {
    if (mySeq === inpReqSeq) {
      loading.value = false;
    }
  }
}

async function handleTriggerScan() {
  scanning.value = true;
  try {
    const ym = filterScope.value === 'month' ? filterYearMonth.value.trim() || undefined : undefined;
    // 月度全量扫描不设硬截断上限；在院实时扫描使用默认全量或 100
    const scanLimit = filterScope.value === 'month' ? undefined : 100;
    const res = await triggerManualScan(scanLimit, notifyDingtalk.value, filterScope.value, ym);
    window.$message?.success(
      `巡检完成（${res.batch_id}）：共 ${res.total_scanned} 例${res.is_truncated ? `（受上限 ${res.scan_limit} 截断）` : ''}，红灯 ${res.RED} / 黄灯 ${res.YELLOW} / 绿灯 ${res.GREEN} / 待定 ${res.PENDING}`
    );
    await loadData();
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '触发扫描失败');
    }
  } finally {
    scanning.value = false;
  }
}

async function handleExport() {
  const qs = new URLSearchParams();
  qs.set('scope', filterScope.value);
  if (filterScope.value === 'month' && filterYearMonth.value.trim()) {
    qs.set('year_month', filterYearMonth.value.trim());
  }
  if (filterDept.value.trim()) qs.set('dept', filterDept.value.trim());
  if (filterLevel.value) qs.set('level', filterLevel.value);
  const queryStr = qs.toString() ? `?${qs.toString()}` : '';
  try {
    await downloadHospitalCsv(`/api/inpatient/alerts/export${queryStr}`, 'inpatient_alerts.csv');
    window.$message?.success('已导出控费预警 CSV');
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '导出失败');
    }
  }
}

onMounted(loadData);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <div class="flex flex-wrap items-center justify-between gap-12px">
        <NSpace wrap align="center">
          <NSelect
            v-model:value="filterScope"
            :options="scopeOptions"
            style="width: 230px"
            @update:value="loadData"
          />
          <NInput
            v-if="filterScope === 'month'"
            v-model:value="filterYearMonth"
            placeholder="YYYY-MM"
            style="width: 115px"
            @keyup.enter="loadData"
          />
          <NInput v-model:value="filterDept" placeholder="科室名称或科室 ID" clearable style="width: 180px" @keyup.enter="loadData" />
          <NSelect v-model:value="filterLevel" :options="levelOptions" placeholder="灯号筛选" clearable style="width: 160px" />
          <NButton type="primary" :loading="loading" @click="loadData">查询刷新</NButton>
          <NButton v-if="canExport" secondary @click="handleExport">导出 CSV</NButton>
        </NSpace>
        <NSpace v-if="canScan" align="center">
          <NCheckbox v-model:checked="notifyDingtalk">同步推送钉钉</NCheckbox>
          <NButton type="warning" :loading="scanning" @click="handleTriggerScan">
            {{ filterScope === 'month' ? '刷新本月全量巡检' : '立即触发在院巡检' }}
          </NButton>
        </NSpace>
      </div>
      <div class="mt-10px text-12px text-#64748b">
        当前批次：{{ report?.batch_id || '-' }} | 数据截止时间：{{ report?.batch_time || '-' }}
        <span v-if="report?.scope === 'month' && report?.year_month"> | 统计月份：{{ report.year_month }}</span>
        <span v-if="report?.coverage_scope === 'DEPARTMENT_SCAN' && report?.scan_dept" class="text-#d97706">
          | 单科扫描：{{ report.scan_dept }}（非全院）
        </span>
        <span v-else-if="report?.coverage_scope === 'UNKNOWN' && report?.batch_id" class="text-#64748b">
          | 历史批次覆盖范围未知
        </span>
        <span v-if="report?.is_truncated === true" class="text-#d97706">
          | 受上限 {{ report.scan_limit }} 截断（{{ isDeptOnlyScope ? `本科可见 ${counts.total}` : `已扫 ${report.total_scanned}` }} 例，非全量）
        </span>
        <span v-else-if="report?.is_truncated === false">
          | {{ isDeptOnlyScope ? `本科可见 ${counts.total} 例（全批完整未截断）` : `全量已扫 ${report?.total_scanned ?? counts.total} 例（未截断）` }}
        </span>
        <span v-else-if="report?.batch_id" class="text-#64748b">
          | 完整度未知（历史批次无完整度元数据，当前展示 {{ counts.total }} 例）
        </span>
      </div>
    </NCard>

    <NAlert v-if="report?.is_truncated === true" type="warning" title="扫描批次受上限截断提醒">
      当前批次受扫描上限 {{ report.scan_limit }} 截断（{{ isDeptOnlyScope ? `本科当前可见 ${counts.total} 例` : `已扫 ${report.total_scanned} 例` }}，可能存在未纳入的病例）。
    </NAlert>

    <NAlert v-else-if="report?.batch_id && report?.is_truncated == null" type="info" title="批次完整度未知说明">
      当前展示的批次缺少有效完整度探针元数据（历史旧批或旧版满额未校验批次），无法确认是否全量覆盖或受上限截断；建议由医保办/管理员重新触发一次扫描。
    </NAlert>

    <NAlert v-if="report?.coverage_warning" type="info" title="扫描覆盖范围提示">
      {{ report.coverage_warning }}
    </NAlert>

    <NAlert v-if="report?.is_stale && report?.stale_warning" type="warning" title="快照数据过期提醒">
      {{ report.stale_warning }}
    </NAlert>

    <NGrid cols="2 s:3 m:6" responsive="screen" :x-gap="12" :y-gap="12">
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic :label="isDeptOnlyScope ? '本科人数' : (filterScope === 'month' ? '本月总人数' : '在院总人数')" :value="counts.total" /></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="红灯超标"><span class="text-#dc2626 font-bold">{{ counts.red }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="黄灯观察"><span class="text-#d97706 font-bold">{{ counts.yellow }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="绿灯正常"><span class="text-#16a34a font-bold">{{ counts.green }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="待定核查"><span class="text-#64748b font-bold">{{ counts.pending }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="自费不适用"><span class="text-#0284c7 font-bold">{{ counts.na }}</span></NStatistic></NCard></NGi>
    </NGrid>

    <NCard :title="(filterScope === 'month' ? '本月全部患者控费预警明细' : '在院患者控费预警明细') + '（已脱敏，不含患者姓名）'" :bordered="false" class="card-wrapper">
      <NDataTable :loading="loading" :columns="columns" :data="filteredItems" :pagination="{ pageSize: 20 }" :scroll-x="1100" size="small" />
    </NCard>
  </NSpace>
</template>
