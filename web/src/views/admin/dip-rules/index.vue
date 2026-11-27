<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue';
import type { DataTableColumns } from 'naive-ui';
import { NTag } from 'naive-ui';
import { fetchDipRules, importDipRules, reviewDipRules, type DipRuleItem } from '@/service/api/hospital';

defineOptions({ name: 'DipRulesAdminPage' });

const loading = ref(false);
const importing = ref(false);
const reviewing = ref(false);
const filterVersion = ref('2026_SAMPLE_V1');
const filterStatus = ref('');
const rules = ref<DipRuleItem[]>([]);

const importForm = reactive({
  rule_version: '2026_V2',
  source_doc: '医保局2026目录发文',
  effective_date: '',
  replace_version: false,
  approve: false,
  csv_text: ''
});

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: 'APPROVED（已审批）', value: 'APPROVED' },
  { label: 'PENDING（待审批）', value: 'PENDING' },
  { label: 'ARCHIVED（已归档）', value: 'ARCHIVED' }
];

const columns: DataTableColumns<DipRuleItem> = [
  { title: 'ID', key: 'rule_id', width: 70 },
  { title: '版本号', key: 'rule_version', width: 135 },
  {
    title: '状态',
    key: 'review_status',
    width: 105,
    render: row => {
      const t = row.review_status === 'APPROVED' ? 'success' : row.review_status === 'PENDING' ? 'warning' : 'default';
      return h(NTag, { type: t, size: 'small', bordered: false }, () => row.review_status);
    }
  },
  { title: '科室', key: 'department', width: 100 },
  { title: 'DIP 病种分组', key: 'dip_group_name', minWidth: 200 },
  { title: 'ICD 编码', key: 'icd_code', width: 95 },
  { title: '计费方式', key: 'billing_unit', width: 115 },
  { title: '分值', key: 'dip_score', width: 95 },
  { title: '职工基准(元)', key: 'employee_base', width: 115 },
  { title: '居民基准(元)', key: 'resident_base', width: 115 }
];

async function loadRules() {
  loading.value = true;
  try {
    const res = await fetchDipRules({
      rule_version: filterVersion.value.trim() || undefined,
      review_status: filterStatus.value || undefined,
      limit: 300
    });
    rules.value = res.items || [];
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '查询 DIP 规则失败');
    }
  } finally {
    loading.value = false;
  }
}

function handleCsvFileChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    importForm.csv_text = String(reader.result || '');
  };
  reader.readAsText(file, 'utf-8');
}

async function submitImport(dryRun: boolean) {
  if (!importForm.rule_version.trim() || !importForm.source_doc.trim() || !importForm.csv_text.trim()) {
    window.$message?.warning('请填写规则版本号、来源文件依据与 CSV 内容');
    return;
  }
  importing.value = true;
  try {
    const res = await importDipRules({
      rule_version: importForm.rule_version.trim(),
      source_doc: importForm.source_doc.trim(),
      effective_date: importForm.effective_date.trim() || null,
      approve: importForm.approve,
      replace_version: importForm.replace_version,
      dry_run: dryRun,
      csv_text: importForm.csv_text
    });
    if (res.success) {
      window.$message?.success(
        dryRun ? `预检通过：共 ${res.valid_rows} 条有效规则` : `导入成功：写入 ${res.inserted_rows} 条规则（${res.review_status}）`
      );
      if (!dryRun) {
        filterVersion.value = importForm.rule_version.trim();
        await loadRules();
      }
    } else {
      const firstErr = res.errors?.[0];
      window.$message?.error(`校验失败：第 ${firstErr?.row ?? '?'} 行 - ${firstErr?.error ?? '未知错误'}`);
    }
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '导入请求失败');
    }
  } finally {
    importing.value = false;
  }
}

async function handleReview(targetStatus: 'APPROVED' | 'ARCHIVED') {
  const ver = filterVersion.value.trim();
  if (!ver) {
    window.$message?.warning('请先填写目标版本号');
    return;
  }
  reviewing.value = true;
  try {
    const res = await reviewDipRules(ver, targetStatus);
    window.$message?.success(`版本 ${res.rule_version} 已整批置为 ${res.target_status}（影响 ${res.updated_rows} 条）`);
    await loadRules();
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '审批失败');
    }
  } finally {
    reviewing.value = false;
  }
}

onMounted(loadRules);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard title="批量导入新版 DIP 规则目录（CSV）" :bordered="false" class="card-wrapper">
      <NGrid cols="1 s:2 m:3" responsive="screen" :x-gap="12" :y-gap="12">
        <NGi>
          <NFormItem label="目标版本号 *" label-placement="top">
            <NInput v-model:value="importForm.rule_version" placeholder="如 2026_V2" />
          </NFormItem>
        </NGi>
        <NGi>
          <NFormItem label="来源发文/文件名 *" label-placement="top">
            <NInput v-model:value="importForm.source_doc" placeholder="如 医保办2026DIP目录.csv" />
          </NFormItem>
        </NGi>
        <NGi>
          <NFormItem label="生效日期 (可选)" label-placement="top">
            <NInput v-model:value="importForm.effective_date" placeholder="YYYY-MM-DD" />
          </NFormItem>
        </NGi>
      </NGrid>
      <div class="mb-10px flex flex-wrap items-center gap-16px">
        <input type="file" accept=".csv,.txt" @change="handleCsvFileChange" />
        <NCheckbox v-model:checked="importForm.replace_version">覆盖同版本号旧规则</NCheckbox>
        <NCheckbox v-model:checked="importForm.approve">导入后直接置为 APPROVED</NCheckbox>
      </div>
      <NInput
        v-model:value="importForm.csv_text"
        type="textarea"
        :rows="5"
        placeholder="粘贴 CSV 内容（表头需含 科室,病种分组,ICD编码,分值 等字段），或上方选择本地 CSV 文件"
      />
      <NSpace class="mt-12px">
        <NButton secondary :loading="importing" @click="submitImport(true)">预检 Dry-Run</NButton>
        <NButton type="primary" :loading="importing" @click="submitImport(false)">确认导入入库</NButton>
      </NSpace>
    </NCard>

    <NCard title="DIP 规则目录与版本审批" :bordered="false" class="card-wrapper">
      <NSpace wrap align="center" class="mb-14px">
        <NInput v-model:value="filterVersion" placeholder="版本号" clearable style="width: 180px" />
        <NSelect v-model:value="filterStatus" :options="statusOptions" style="width: 190px" />
        <NButton type="primary" :loading="loading" @click="loadRules">查询规则</NButton>
        <NButton type="success" secondary :loading="reviewing" @click="handleReview('APPROVED')">整批审批生效</NButton>
        <NButton type="warning" secondary :loading="reviewing" @click="handleReview('ARCHIVED')">整批归档</NButton>
      </NSpace>
      <NDataTable :loading="loading" :columns="columns" :data="rules" :pagination="{ pageSize: 15 }" :scroll-x="1150" size="small" />
    </NCard>
  </NSpace>
</template>
