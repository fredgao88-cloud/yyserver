<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useAuthStore } from '@/store/modules/auth';
import { useRouterPush } from '@/hooks/common/router';
import {
  fetchDoctorPerformance,
  fetchInpatientAlerts,
  type DoctorPerformanceResponse,
  type InpatientAlertsResponse
} from '@/service/api/hospital';

defineOptions({ name: 'HospitalHome' });

const authStore = useAuthStore();
const { routerPushByKey } = useRouterPush();

const loading = ref(false);
const alertsData = ref<InpatientAlertsResponse | null>(null);
const perfData = ref<DoctorPerformanceResponse | null>(null);

const roleLabelMap: Record<string, string> = {
  ADMIN: '系统管理员',
  INSURANCE_OFFICER: '医保办专员',
  DEPT_HEAD: '科室主任',
  DOCTOR: '管床医生'
};

const roleLabel = computed(() => roleLabelMap[authStore.userInfo.roles[0] || ''] || authStore.userInfo.roles[0] || '-');
const canViewAlerts = computed(() =>
  authStore.userInfo.roles.some(r => ['ADMIN', 'INSURANCE_OFFICER', 'DEPT_HEAD'].includes(r))
);
const canViewAdmin = computed(() =>
  authStore.userInfo.roles.some(r => ['ADMIN', 'INSURANCE_OFFICER'].includes(r))
);

const stats = computed(() => {
  const items = alertsData.value?.items || [];
  let red = 0;
  let yellow = 0;
  let green = 0;
  let pending = 0;
  for (const it of items) {
    if (it.alert_level === 'RED') red += 1;
    else if (it.alert_level === 'YELLOW') yellow += 1;
    else if (it.alert_level === 'GREEN') green += 1;
    else if (it.alert_level === 'PENDING') pending += 1;
  }
  return { total: items.length, red, yellow, green, pending };
});

async function loadDashboard() {
  loading.value = true;
  try {
    const tasks: Promise<void>[] = [
      fetchDoctorPerformance(30)
        .then(res => {
          perfData.value = res;
        })
        .catch(() => {})
    ];
    if (canViewAlerts.value) {
      tasks.push(
        fetchInpatientAlerts()
          .then(res => {
            alertsData.value = res;
          })
          .catch(() => {})
      );
    }
    await Promise.all(tasks);
  } finally {
    loading.value = false;
  }
}

onMounted(loadDashboard);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <div class="flex flex-wrap items-center justify-between gap-16px">
        <div>
          <h2 class="text-20px font-bold">
            欢迎回来，{{ authStore.userInfo.userName }}（{{ roleLabel }}）
          </h2>
          <p class="mt-6px text-13px text-#64748b">
            工号：{{ authStore.userInfo.userId }}
            <span v-if="authStore.userInfo.deptScope"> | 科室范围：{{ authStore.userInfo.deptScope }}</span>
            <span v-if="authStore.userInfo.doctorId"> | HIS 医生工号：{{ authStore.userInfo.doctorId }}</span>
            <span> | 病例导出权限：{{ authStore.userInfo.canExportCase ? '已开通' : '未开通' }}</span>
          </p>
        </div>
        <NSpace>
          <NButton type="primary" @click="routerPushByKey('calculator')">DIP 规则试算 / 问答</NButton>
          <NButton v-if="canViewAlerts" @click="routerPushByKey('inpatient')">进入在院预警看板</NButton>
          <NButton v-if="canViewAdmin" @click="routerPushByKey('reconciliation')">出院决算对账</NButton>
        </NSpace>
      </div>
    </NCard>

    <NSpin :show="loading">
      <NGrid v-if="canViewAlerts" cols="1 s:2 m:5" responsive="screen" :x-gap="14" :y-gap="14">
        <NGi>
          <NCard :bordered="false" class="card-wrapper">
            <NStatistic label="最新批次在院" :value="stats.total" />
          </NCard>
        </NGi>
        <NGi>
          <NCard :bordered="false" class="card-wrapper">
            <NStatistic label="红灯超标 (>110%)">
              <span class="text-#dc2626 font-bold">{{ stats.red }}</span>
            </NStatistic>
          </NCard>
        </NGi>
        <NGi>
          <NCard :bordered="false" class="card-wrapper">
            <NStatistic label="黄灯观察 (90%~110%)">
              <span class="text-#d97706 font-bold">{{ stats.yellow }}</span>
            </NStatistic>
          </NCard>
        </NGi>
        <NGi>
          <NCard :bordered="false" class="card-wrapper">
            <NStatistic label="绿灯正常 (<90%)">
              <span class="text-#16a34a font-bold">{{ stats.green }}</span>
            </NStatistic>
          </NCard>
        </NGi>
        <NGi>
          <NCard :bordered="false" class="card-wrapper">
            <NStatistic label="待定核查 (PENDING)">
              <span class="text-#64748b font-bold">{{ stats.pending }}</span>
            </NStatistic>
          </NCard>
        </NGi>
      </NGrid>
    </NSpin>

    <NAlert v-if="alertsData?.is_stale && alertsData?.stale_warning" type="warning" title="快照更新提醒">
      {{ alertsData.stale_warning }}
    </NAlert>

    <NGrid cols="1 m:2" responsive="screen" :x-gap="16" :y-gap="16">
      <NGi>
        <NCard title="近期扫描快照概览 (近 30 天)" :bordered="false" class="card-wrapper">
          <NDescriptions :column="2" bordered size="small">
            <NDescriptionsItem label="累计快照条数">{{ perfData?.total_snapshots ?? 0 }}</NDescriptionsItem>
            <NDescriptionsItem label="统计医生数">{{ perfData?.doctors?.length ?? 0 }}</NDescriptionsItem>
            <NDescriptionsItem label="历史扫描批次">{{ perfData?.trends?.length ?? 0 }}</NDescriptionsItem>
            <NDescriptionsItem label="最新批次时间">{{ alertsData?.batch_time || '-' }}</NDescriptionsItem>
          </NDescriptions>
          <div class="mt-14px">
            <NButton quaternary type="primary" @click="routerPushByKey('analytics')">查看医生控费绩效与趋势图表 →</NButton>
          </div>
        </NCard>
      </NGi>
      <NGi>
        <NCard title="常用业务入口" :bordered="false" class="card-wrapper">
          <NSpace wrap :size="10">
            <NButton secondary type="primary" @click="routerPushByKey('calculator')">🩺 DIP 规则试算与问答</NButton>
            <NButton secondary @click="routerPushByKey('analytics')">📊 医生绩效与历史趋势</NButton>
            <NButton v-if="canViewAlerts" secondary type="warning" @click="routerPushByKey('inpatient')">🚨 在院患者控费预警</NButton>
            <NButton v-if="canViewAdmin" secondary type="info" @click="routerPushByKey('reconciliation')">🧾 出院决算账实对账</NButton>
            <NButton v-if="canViewAdmin" secondary @click="routerPushByKey('admin_dip-rules')">📥 DIP 规则批量导入</NButton>
            <NButton v-if="authStore.userInfo.roles.includes('ADMIN')" secondary @click="routerPushByKey('admin_audit-logs')">🛡️ 合规审计日志</NButton>
            <NButton v-if="authStore.userInfo.roles.includes('ADMIN')" secondary @click="routerPushByKey('admin_operators')">👥 操作员权限管理</NButton>
          </NSpace>
        </NCard>
      </NGi>
    </NGrid>
  </NSpace>
</template>
