<script setup lang="ts">
import { h, onMounted, ref } from 'vue';
import type { DataTableColumns } from 'naive-ui';
import { NTag } from 'naive-ui';
import { fetchAuditLogs, type AuditLogItem } from '@/service/api/hospital';

defineOptions({ name: 'AuditLogsAdminPage' });

const loading = ref(false);
const days = ref(7);
const limit = ref(100);
const authResult = ref('');
const actionType = ref('');
const logs = ref<AuditLogItem[]>([]);

const daysOptions = [
  { label: '近 1 天', value: 1 },
  { label: '近 7 天', value: 7 },
  { label: '近 30 天', value: 30 },
  { label: '近 90 天', value: 90 }
];

const authOptions = [
  { label: '全部鉴权结果', value: '' },
  { label: 'PERMITTED（放行）', value: 'PERMITTED' },
  { label: 'DENIED（拦截）', value: 'DENIED' }
];

const columns: DataTableColumns<AuditLogItem> = [
  { title: 'ID', key: 'log_id', width: 75 },
  { title: '时间', key: 'created_at', width: 165 },
  { title: '操作员工号', key: 'operator_id', width: 120 },
  { title: '角色', key: 'role_type', width: 140 },
  { title: '渠道', key: 'channel', width: 80 },
  { title: '动作类型', key: 'action_type', width: 195 },
  {
    title: '目标标识',
    key: 'target_ref',
    minWidth: 160,
    render: row => row.target_ref || '-'
  },
  {
    title: '鉴权结果',
    key: 'auth_result',
    width: 115,
    render: row =>
      h(
        NTag,
        { type: row.auth_result === 'PERMITTED' ? 'success' : 'error', size: 'small', bordered: false },
        () => row.auth_result
      )
  },
  {
    title: '版本/批次',
    key: 'batch_id',
    width: 170,
    render: row => row.batch_id || row.rule_version || '-'
  }
];

async function loadData() {
  loading.value = true;
  try {
    const res = await fetchAuditLogs({
      days: days.value,
      limit: limit.value,
      auth_result: authResult.value || undefined,
      action_type: actionType.value.trim() || undefined
    });
    logs.value = res.items || [];
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '加载审计日志失败');
    }
  } finally {
    loading.value = false;
  }
}

onMounted(loadData);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace wrap align="center">
        <NSelect v-model:value="days" :options="daysOptions" style="width: 130px" />
        <NSelect v-model:value="authResult" :options="authOptions" style="width: 185px" />
        <NInput v-model:value="actionType" placeholder="动作类型（如 LOGIN）" clearable style="width: 210px" @keyup.enter="loadData" />
        <NButton type="primary" :loading="loading" @click="loadData">检索审计日志</NButton>
        <span class="text-12px text-#64748b">共 {{ logs.length }} 条（已脱敏，不含口令与患者自由文本）</span>
      </NSpace>
    </NCard>

    <NCard title="合规审计日志流" :bordered="false" class="card-wrapper">
      <NDataTable :loading="loading" :columns="columns" :data="logs" :pagination="{ pageSize: 20 }" :scroll-x="1200" size="small" />
    </NCard>
  </NSpace>
</template>
