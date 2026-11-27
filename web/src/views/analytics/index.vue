<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import type { DataTableColumns } from 'naive-ui';
import { useAuthStore } from '@/store/modules/auth';
import { useEcharts } from '@/hooks/common/echarts';
import { fetchDoctorPerformance, type DoctorPerformanceItem, type DoctorPerformanceResponse } from '@/service/api/hospital';

defineOptions({ name: 'AnalyticsPage' });

const authStore = useAuthStore();
const loading = ref(false);
const days = ref(30);
const deptId = ref('');
const report = ref<DoctorPerformanceResponse | null>(null);

const canFilterDept = computed(() => authStore.userInfo.roles.some(r => ['ADMIN', 'INSURANCE_OFFICER'].includes(r)));

const daysOptions = [
  { label: '近 7 天', value: 7 },
  { label: '近 14 天', value: 14 },
  { label: '近 30 天', value: 30 },
  { label: '近 90 天', value: 90 }
];

const { domRef: chartRef, updateOptions } = useEcharts(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['绿灯', '黄灯', '红灯', '待定', '平均费用占比(%)'] },
  grid: { left: '4%', right: '5%', bottom: '10%', top: '16%', containLabel: true },
  xAxis: { type: 'category', data: [] as string[] },
  yAxis: [
    { type: 'value', name: '病例数' },
    { type: 'value', name: '占比(%)', min: 0 }
  ],
  series: [
    { name: '绿灯', type: 'bar', stack: 'total', color: '#16a34a', data: [] as number[] },
    { name: '黄灯', type: 'bar', stack: 'total', color: '#f59e0b', data: [] as number[] },
    { name: '红灯', type: 'bar', stack: 'total', color: '#dc2626', data: [] as number[] },
    { name: '待定', type: 'bar', stack: 'total', color: '#94a3b8', data: [] as number[] },
    { name: '平均费用占比(%)', type: 'line', yAxisIndex: 1, smooth: true, color: '#2563eb', data: [] as (number | null)[] }
  ]
}));

const columns: DataTableColumns<DoctorPerformanceItem> = [
  { title: '医生工号', key: 'doctor_id', width: 130 },
  { title: '科室', key: 'dept_name', width: 120 },
  { title: '病例数', key: 'total_cases', width: 85 },
  { title: '绿灯', key: 'green_count', width: 70 },
  { title: '黄灯', key: 'yellow_count', width: 70 },
  { title: '红灯', key: 'red_count', width: 70 },
  { title: '待定', key: 'pending_count', width: 70 },
  {
    title: '绿灯率',
    key: 'green_rate_percent',
    width: 90,
    render: row => (row.green_rate_percent !== null ? `${row.green_rate_percent}%` : '-')
  },
  {
    title: '平均占比',
    key: 'avg_fee_ratio_percent',
    width: 95,
    render: row => (row.avg_fee_ratio_percent !== null ? `${row.avg_fee_ratio_percent}%` : '-')
  },
  { title: '累计费用(元)', key: 'total_fee_str', width: 125 },
  { title: 'DIP 基准(元)', key: 'total_benchmark_str', width: 125 }
];

let anaReqSeq = 0;

async function loadData() {
  const mySeq = ++anaReqSeq;
  loading.value = true;
  try {
    const res = await fetchDoctorPerformance(days.value, canFilterDept.value ? deptId.value.trim() || undefined : undefined);
    if (mySeq !== anaReqSeq) return;
    report.value = res;
    const trends = res.trends || [];
    updateOptions(opts => {
      opts.xAxis.data = trends.map(t => t.cutoff_time.slice(5, 16) || t.batch_id);
      opts.series[0].data = trends.map(t => t.green_count);
      opts.series[1].data = trends.map(t => t.yellow_count);
      opts.series[2].data = trends.map(t => t.red_count);
      opts.series[3].data = trends.map(t => t.pending_count);
      opts.series[4].data = trends.map(t => t.avg_fee_ratio_percent);
      return opts;
    });
  } catch (err: any) {
    if (mySeq !== anaReqSeq) return;
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '加载绩效报表失败');
    }
  } finally {
    if (mySeq === anaReqSeq) {
      loading.value = false;
    }
  }
}

function exportCsv() {
  const docs = report.value?.doctors || [];
  const esc = (v: unknown) => {
    let s = v === null || v === undefined ? '' : String(v);
    if (/^[=+\-@\t\r]/.test(s) && !/^-?\d+(?:\.\d+)?%?$/.test(s)) s = `'${s}`;
    return `"${s.replace(/"/g, '""')}"`;
  };
  const headers = ['医生工号', '科室', '病例数', '绿灯', '黄灯', '红灯', '待定', '绿灯率(%)', '平均占比(%)', '累计费用', 'DIP基准'];
  const rows = docs.map(d =>
    [
      d.doctor_id,
      d.dept_name,
      d.total_cases,
      d.green_count,
      d.yellow_count,
      d.red_count,
      d.pending_count,
      d.green_rate_percent ?? '-',
      d.avg_fee_ratio_percent ?? '-',
      d.total_fee_str,
      d.total_benchmark_str
    ]
      .map(esc)
      .join(',')
  );
  const blob = new Blob([`\uFEFF${headers.map(esc).join(',')}\r\n${rows.join('\r\n')}`], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `doctor_performance_${days.value}d.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

onMounted(loadData);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace wrap align="center">
        <NSelect v-model:value="days" :options="daysOptions" style="width: 140px" />
        <NInput v-if="canFilterDept" v-model:value="deptId" placeholder="按科室 ID 筛选" clearable style="width: 180px" />
        <NButton type="primary" :loading="loading" @click="loadData">刷新报表</NButton>
        <NButton secondary @click="exportCsv">导出汇总 CSV</NButton>
        <span class="text-12px text-#64748b">样本快照数：{{ report?.total_snapshots ?? 0 }}</span>
      </NSpace>
    </NCard>

    <NCard title="历史批次控费趋势图" :bordered="false" class="card-wrapper">
      <div ref="chartRef" class="h-320px w-full"></div>
    </NCard>

    <NCard title="医生控费绩效排行" :bordered="false" class="card-wrapper">
      <NDataTable :loading="loading" :columns="columns" :data="report?.doctors || []" :pagination="{ pageSize: 15 }" :scroll-x="1050" size="small" />
    </NCard>
  </NSpace>
</template>
