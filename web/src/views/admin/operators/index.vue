<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue';
import type { DataTableColumns } from 'naive-ui';
import { NButton, NTag } from 'naive-ui';
import { fetchOperators, updateOperatorStatus, upsertOperator, type OperatorItem } from '@/service/api/hospital';

defineOptions({ name: 'OperatorsAdminPage' });

const loading = ref(false);
const saving = ref(false);
const modalOpen = ref(false);
const operators = ref<OperatorItem[]>([]);

const form = reactive({
  operator_id: '',
  display_name: '',
  role_type: 'DOCTOR',
  dept_scope: '',
  doctor_id: '',
  dingtalk_userid: '',
  can_export_case: false,
  password: ''
});

const roleOptions = [
  { label: 'DOCTOR（管床医生）', value: 'DOCTOR' },
  { label: 'DEPT_HEAD（科室主任）', value: 'DEPT_HEAD' },
  { label: 'INSURANCE_OFFICER（医保办）', value: 'INSURANCE_OFFICER' },
  { label: 'ADMIN（系统管理员）', value: 'ADMIN' }
];

const columns: DataTableColumns<OperatorItem> = [
  { title: '工号', key: 'operator_id', width: 130 },
  { title: '姓名', key: 'display_name', width: 110 },
  { title: '角色', key: 'role_type', width: 165 },
  { title: '科室范围', key: 'dept_scope', width: 130, render: row => row.dept_scope || '-' },
  { title: 'HIS 医生工号', key: 'doctor_id', width: 135, render: row => row.doctor_id || '-' },
  {
    title: '导出权限',
    key: 'can_export_case',
    width: 95,
    render: row =>
      h(NTag, { type: row.can_export_case ? 'info' : 'default', size: 'small', bordered: false }, () =>
        row.can_export_case ? '允许' : '禁止'
      )
  },
  {
    title: '状态',
    key: 'status',
    width: 95,
    render: row =>
      h(NTag, { type: row.status === 'ACTIVE' ? 'success' : 'error', size: 'small', bordered: false }, () => row.status)
  },
  {
    title: '操作',
    key: 'actions',
    width: 170,
    render: row =>
      h('div', { class: 'flex gap-8px' }, [
        h(
          NButton,
          { size: 'tiny', secondary: true, onClick: () => openEdit(row) },
          () => '重置/编辑'
        ),
        h(
          NButton,
          {
            size: 'tiny',
            type: row.status === 'ACTIVE' ? 'error' : 'success',
            onClick: () => handleToggleStatus(row)
          },
          () => (row.status === 'ACTIVE' ? '停用' : '启用')
        )
      ])
  }
];

async function loadData() {
  loading.value = true;
  try {
    const res = await fetchOperators();
    operators.value = res.items || [];
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '加载操作员列表失败');
    }
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  form.operator_id = '';
  form.display_name = '';
  form.role_type = 'DOCTOR';
  form.dept_scope = '';
  form.doctor_id = '';
  form.dingtalk_userid = '';
  form.can_export_case = false;
  form.password = '';
  modalOpen.value = true;
}

function openEdit(row: OperatorItem) {
  form.operator_id = row.operator_id;
  form.display_name = row.display_name;
  form.role_type = row.role_type;
  form.dept_scope = row.dept_scope || '';
  form.doctor_id = row.doctor_id || '';
  form.dingtalk_userid = row.dingtalk_userid || '';
  form.can_export_case = Boolean(row.can_export_case);
  form.password = '';
  modalOpen.value = true;
}

async function handleSave() {
  if (!form.operator_id.trim() || !form.display_name.trim() || form.password.length < 8) {
    window.$message?.warning('请填写工号、姓名，并输入至少 8 位口令');
    return;
  }
  saving.value = true;
  try {
    await upsertOperator({
      operator_id: form.operator_id.trim(),
      display_name: form.display_name.trim(),
      password: form.password,
      role_type: form.role_type,
      dept_scope: form.dept_scope.trim(),
      doctor_id: form.doctor_id.trim(),
      dingtalk_userid: form.dingtalk_userid.trim(),
      can_export_case: form.can_export_case
    });
    window.$message?.success(`操作员 ${form.operator_id.trim()} 已保存`);
    modalOpen.value = false;
    await loadData();
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '保存失败');
    }
  } finally {
    saving.value = false;
  }
}

async function handleToggleStatus(row: OperatorItem) {
  const nextStatus = row.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE';
  try {
    await updateOperatorStatus(row.operator_id, nextStatus);
    window.$message?.success(`已更新 ${row.operator_id} 状态为 ${nextStatus}`);
    await loadData();
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '状态更新失败');
    }
  }
}

onMounted(loadData);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <div class="flex items-center justify-between">
        <span class="text-13px text-#64748b">管理医生、科主任、医保办与管理员的访问范围、医生工号绑定及病例导出权限。</span>
        <NSpace>
          <NButton secondary :loading="loading" @click="loadData">刷新列表</NButton>
          <NButton type="primary" @click="openCreate">新增操作员</NButton>
        </NSpace>
      </div>
    </NCard>

    <NCard title="操作员列表" :bordered="false" class="card-wrapper">
      <NDataTable :loading="loading" :columns="columns" :data="operators" :pagination="{ pageSize: 20 }" :scroll-x="1050" size="small" />
    </NCard>

    <NModal v-model:show="modalOpen" preset="card" title="新增 / 重置操作员账号" style="width: 520px; max-width: 94vw">
      <NForm label-placement="left" label-width="115" size="small">
        <NFormItem label="工号 *"><NInput v-model:value="form.operator_id" placeholder="如 DOC_001" /></NFormItem>
        <NFormItem label="姓名 *"><NInput v-model:value="form.display_name" placeholder="如 张医生" /></NFormItem>
        <NFormItem label="角色 *"><NSelect v-model:value="form.role_type" :options="roleOptions" /></NFormItem>
        <NFormItem label="科室范围 (ID)"><NInput v-model:value="form.dept_scope" placeholder="科主任建议填科室 ID，如 101" /></NFormItem>
        <NFormItem label="HIS 医生工号"><NInput v-model:value="form.doctor_id" placeholder="医生角色绑定，如 D1502..." /></NFormItem>
        <NFormItem label="钉钉 userid"><NInput v-model:value="form.dingtalk_userid" placeholder="可选" /></NFormItem>
        <NFormItem label="病例导出权限"><NSwitch v-model:value="form.can_export_case" /></NFormItem>
        <NFormItem label="登录口令(>=8位)*"><NInput v-model:value="form.password" type="password" show-password-on="click" /></NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="modalOpen = false">取消</NButton>
          <NButton type="primary" :loading="saving" @click="handleSave">保存账号</NButton>
        </NSpace>
      </template>
    </NModal>
  </NSpace>
</template>
