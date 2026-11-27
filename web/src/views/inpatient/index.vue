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
const filterDept = ref('');
const filterLevel = ref<string | null>(null);
const notifyDingtalk = ref(false);
const report = ref<InpatientAlertsResponse | null>(null);

const canScan = computed(() => authStore.userInfo.roles.some(r => ['ADMIN', 'INSURANCE_OFFICER'].includes(r)));
const canExport = computed(() => Boolean(authStore.userInfo.canExportCase));

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
  { title: '医生工号', key: 'doctor_id', width: 120 },
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
    const res = await fetchInpatientAlerts(filterDept.value.trim() || undefined);
    if (mySeq !== inpReqSeq) return;
    report.value = res;
  } catch (err: any) {
    if (mySeq !== inpReqSeq) return;
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '加载在院预警失败');
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
    const res = await triggerManualScan(100, notifyDingtalk.value);
    window.$message?.success(
      `巡检完成（${res.batch_id}）：共 ${res.total_scanned} 例，红灯 ${res.RED} / 黄灯 ${res.YELLOW} / 绿灯 ${res.GREEN} / 待定 ${res.PENDING}`
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
  if (filterDept.value.trim()) qs.set('dept', filterDept.value.trim());
  if (filterLevel.value) qs.set('level', filterLevel.value);
  const queryStr = qs.toString() ? `?${qs.toString()}` : '';
  try {
    await downloadHospitalCsv(`/api/inpatient/alerts/export${queryStr}`, 'inpatient_alerts.csv');
    window.$message?.success('已导出在院预警 CSV');
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
          <NInput v-model:value="filterDept" placeholder="科室名称或科室 ID" clearable style="width: 180px" @keyup.enter="loadData" />
          <NSelect v-model:value="filterLevel" :options="levelOptions" placeholder="灯号筛选" clearable style="width: 160px" />
          <NButton type="primary" :loading="loading" @click="loadData">查询刷新</NButton>
          <NButton v-if="canExport" secondary @click="handleExport">导出 CSV</NButton>
        </NSpace>
        <NSpace v-if="canScan" align="center">
          <NCheckbox v-model:checked="notifyDingtalk">同步推送钉钉</NCheckbox>
          <NButton type="warning" :loading="scanning" @click="handleTriggerScan">立即触发全院巡检</NButton>
        </NSpace>
      </div>
      <div class="mt-10px text-12px text-#64748b">
        当前批次：{{ report?.batch_id || '-' }} | 数据截止时间：{{ report?.batch_time || '-' }}
      </div>
    </NCard>

    <NAlert v-if="report?.is_stale && report?.stale_warning" type="warning" title="快照数据过期提醒">
      {{ report.stale_warning }}
    </NAlert>

    <NGrid cols="2 s:3 m:6" responsive="screen" :x-gap="12" :y-gap="12">
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="在院总人数" :value="counts.total" /></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="红灯超标"><span class="text-#dc2626 font-bold">{{ counts.red }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="黄灯观察"><span class="text-#d97706 font-bold">{{ counts.yellow }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="绿灯正常"><span class="text-#16a34a font-bold">{{ counts.green }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="待定核查"><span class="text-#64748b font-bold">{{ counts.pending }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="自费不适用"><span class="text-#0284c7 font-bold">{{ counts.na }}</span></NStatistic></NCard></NGi>
    </NGrid>

    <NCard title="在院患者控费预警明细（已脱敏，不含患者姓名）" :bordered="false" class="card-wrapper">
      <NDataTable :loading="loading" :columns="columns" :data="filteredItems" :pagination="{ pageSize: 20 }" :scroll-x="1100" size="small" />
    </NCard>
  </NSpace>
</template>
