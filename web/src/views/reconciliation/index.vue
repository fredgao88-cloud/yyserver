<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue';
import type { DataTableColumns } from 'naive-ui';
import { NTag } from 'naive-ui';
import { useAuthStore } from '@/store/modules/auth';
import {
  downloadHospitalCsv,
  fetchSettledReconciliation,
  type ReconciliationItem,
  type ReconciliationReport
} from '@/service/api/hospital';

defineOptions({ name: 'ReconciliationPage' });

const authStore = useAuthStore();
const loading = ref(false);
const limit = ref(50);
const dept = ref('');
const statusFilter = ref('');
const report = ref<ReconciliationReport | null>(null);
const loadError = ref('');

const canExport = computed(() => Boolean(authStore.userInfo.canExportCase));

const limitOptions = [
  { label: '最近 20 例', value: 20 },
  { label: '最近 50 例', value: 50 },
  { label: '最近 100 例', value: 100 }
];

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: 'MATCHED（账实相符）', value: 'MATCHED' },
  { label: 'UNDISPENSED_ONLY（仅未发药）', value: 'UNDISPENSED_ONLY' },
  { label: 'MISMATCH（存在差异）', value: 'MISMATCH' }
];

const filteredItems = computed(() => {
  const list = report.value?.items || [];
  if (!statusFilter.value) return list;
  return list.filter(x => x.recon_status === statusFilter.value);
});

const columns: DataTableColumns<ReconciliationItem> = [
  { title: '住院流水号', key: 'visit_id', width: 135 },
  { title: '科室', key: 'dept_name', width: 110 },
  { title: '结算时间', key: 'settlement_time', width: 165 },
  { title: '对账口径', key: 'reconciliation_scope', width: 145 },
  { title: '主单总额(元)', key: 'total_amount_str', width: 115 },
  { title: '类目差异', key: 'category_diff_str', width: 90 },
  { title: '临床差异', key: 'clinical_diff_str', width: 90 },
  { title: '药房差异', key: 'pharmacy_diff_str', width: 90 },
  {
    title: '未发药(笔/元)',
    key: 'undispensed_count',
    width: 120,
    render: row => `${row.undispensed_count} / ${row.undispensed_amount_str}`
  },
  {
    title: '状态',
    key: 'recon_status',
    width: 145,
    render: row => {
      const tagMap: Record<string, 'success' | 'warning' | 'error'> = {
        MATCHED: 'success',
        UNDISPENSED_ONLY: 'warning',
        MISMATCH: 'error'
      };
      return h(NTag, { type: tagMap[row.recon_status] || 'default', size: 'small', bordered: false }, () => row.recon_status);
    }
  }
];

let reconReqSeq = 0;

async function loadData() {
  const mySeq = ++reconReqSeq;
  loading.value = true;
  loadError.value = '';
  try {
    const res = await fetchSettledReconciliation(limit.value, dept.value.trim() || undefined);
    if (mySeq !== reconReqSeq) return;
    report.value = res;
    if (res.unreconciled_count > 0) {
      window.$message?.warning(`部分完成：存在 ${res.unreconciled_count} 例未完成核对`);
    } else {
      window.$message?.success('已完成 SERIALIZABLE 事务隔离级出院账实核对');
    }
  } catch (err: any) {
    if (mySeq !== reconReqSeq) return;
    if (err?.status !== 499 && err?.status !== 401) {
      report.value = null;
      loadError.value = err?.message || '对账结果读取失败';
      window.$message?.error(loadError.value);
    }
  } finally {
    if (mySeq === reconReqSeq) {
      loading.value = false;
    }
  }
}

async function handleExport() {
  const qs = new URLSearchParams({ limit: String(limit.value) });
  if (dept.value.trim()) qs.set('dept', dept.value.trim());
  if (statusFilter.value) qs.set('status', statusFilter.value);
  try {
    await downloadHospitalCsv(`/api/reconciliation/settled/export?${qs.toString()}`, 'settled_reconciliation.csv');
    window.$message?.success('已导出出院对账 CSV');
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
      <NSpace wrap align="center">
        <NSelect v-model:value="limit" :options="limitOptions" style="width: 140px" />
        <NInput v-model:value="dept" placeholder="科室名称筛选" clearable style="width: 160px" @keyup.enter="loadData" />
        <NSelect v-model:value="statusFilter" :options="statusOptions" style="width: 210px" />
        <NButton type="primary" :loading="loading" @click="loadData">执行 SERIALIZABLE 对账</NButton>
        <NButton v-if="canExport" secondary @click="handleExport">导出 CSV</NButton>
      </NSpace>
    </NCard>

    <NAlert v-if="loadError" type="error" title="对账读取失败">{{ loadError }}</NAlert>

    <NAlert v-if="report && report.unreconciled_count > 0" type="warning" title="部分病例未核对提醒">
      共抽样 {{ report.sampled_count }} 例，成功核对 {{ report.reconciled_count }} 例，未核对 {{ report.unreconciled_count }} 例
      （{{ report.unreconciled_reasons.map(x => `${x.visit_id}:${x.reason}`).join('；') }}）
    </NAlert>

    <NGrid cols="2 s:3 m:6" responsive="screen" :x-gap="12" :y-gap="12">
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="抽样病例" :value="report?.sampled_count ?? 0" /></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="已核对病例" :value="report?.reconciled_count ?? 0" /></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="未核对病例"><span class="text-#d97706 font-bold">{{ report?.unreconciled_count ?? 0 }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="账实相符"><span class="text-#16a34a font-bold">{{ report?.matched_count ?? 0 }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="未发药异常"><span class="text-#d97706 font-bold">{{ report?.undispensed_only_count ?? 0 }}</span></NStatistic></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><NStatistic label="账实差异"><span class="text-#dc2626 font-bold">{{ report?.mismatch_count ?? 0 }}</span></NStatistic></NCard></NGi>
    </NGrid>

    <NCard title="出院决算账实对账明细（已脱敏，不含患者姓名）" :bordered="false" class="card-wrapper">
      <NDataTable :loading="loading" :columns="columns" :data="filteredItems" :pagination="{ pageSize: 20 }" :scroll-x="1200" size="small" />
    </NCard>
  </NSpace>
</template>
