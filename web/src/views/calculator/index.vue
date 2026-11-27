<script setup lang="ts">
import { reactive, ref } from 'vue';
import { checkPatientDip, queryDipRule, sendHospitalChat } from '@/service/api/hospital';

defineOptions({ name: 'DipCalculatorPage' });

const calcMode = ref<'rule' | 'patient'>('rule');
const calcLoading = ref(false);
const calcResult = ref<Record<string, any> | null>(null);

const form = reactive({
  disease: 'M17.9',
  ins_type: '职工',
  age: null as number | null,
  operation: '',
  bed_days: null as number | null,
  patient_id: '',
  current_cost: 2450,
  rule_version: '2026_SAMPLE_V1'
});

const insOptions = [
  { label: '城镇职工医保', value: '职工' },
  { label: '城乡居民医保', value: '居民' },
  { label: '自费', value: '自费' },
  { label: '留空（测试待定追问）', value: '' }
];

const chatInput = ref('');
const chatLoading = ref(false);
const chatHistory = ref<{ role: 'user' | 'assistant'; text: string; intent?: string }[]>([
  {
    role: 'assistant',
    text: '您好，我是院内本地 DIP 业务助手（零云端外传）。您可以输入病种、年龄、参保类型、治疗方式咨询分值与基准，或带住院号和费用执行判灯核查。'
  }
]);

const quickPrompts = [
  '68岁职工，诊断 M17.9，保守治疗，DIP 基准是多少？',
  '结肠息肉 K63.5 拟行切除术+胃镜检查，职工分值是多少？',
  '颈臂综合征 M53.1 职工按床日住院12天'
];

async function runCalculate() {
  if (!form.disease.trim()) {
    window.$message?.warning('请输入疾病诊断或 ICD 编码');
    return;
  }
  calcLoading.value = true;
  try {
    if (calcMode.value === 'rule') {
      calcResult.value = await queryDipRule({
        disease: form.disease.trim(),
        age: form.age,
        ins_type: form.ins_type || null,
        operation: form.operation.trim() || null,
        bed_days: form.bed_days,
        rule_version: form.rule_version.trim() || '2026_SAMPLE_V1'
      });
    } else {
      calcResult.value = await checkPatientDip({
        patient_id: form.patient_id.trim() || null,
        disease: form.disease.trim(),
        current_cost: Number(form.current_cost || 0),
        age: form.age,
        ins_type: form.ins_type || null,
        operation: form.operation.trim() || null,
        bed_days: form.bed_days,
        rule_version: form.rule_version.trim() || '2026_SAMPLE_V1'
      });
    }
  } catch (err: any) {
    if (err?.status !== 499 && err?.status !== 401) {
      window.$message?.error(err?.message || '测算请求失败');
    }
  } finally {
    calcLoading.value = false;
  }
}

async function sendChat(preset?: string) {
  const q = (preset ?? chatInput.value).trim();
  if (!q) return;
  if (!preset) chatInput.value = '';
  chatHistory.value.push({ role: 'user', text: q });
  chatLoading.value = true;
  try {
    const res = await sendHospitalChat(q);
    chatHistory.value.push({ role: 'assistant', text: res.reply, intent: res.intent });
  } catch (err: any) {
    chatHistory.value.push({ role: 'assistant', text: `请求失败：${err?.message || '未知错误'}` });
  } finally {
    chatLoading.value = false;
  }
}
</script>

<template>
  <NGrid cols="1 m:2" responsive="screen" :x-gap="16" :y-gap="16">
    <NGi>
      <NCard title="DIP 规则试算与费用判灯" :bordered="false" class="card-wrapper">
        <NTabs v-model:value="calcMode" type="segment" class="mb-14px">
          <NTab name="rule">病种规则与基准测算</NTab>
          <NTab name="patient">在院费用判灯核查</NTab>
        </NTabs>

        <NForm label-placement="left" label-width="110" size="small">
          <NFormItem label="诊断 / ICD *">
            <NInput v-model:value="form.disease" placeholder="如 M17.9、K63.5、膝关节病" />
          </NFormItem>
          <NFormItem label="参保类型">
            <NSelect v-model:value="form.ins_type" :options="insOptions" />
          </NFormItem>
          <NFormItem label="手术 / 操作">
            <NInput v-model:value="form.operation" placeholder="如 保守、胃镜检查、切除术" clearable />
          </NFormItem>
          <NGrid cols="2" :x-gap="12">
            <NGi>
              <NFormItem label="患者年龄(岁)">
                <NInputNumber v-model:value="form.age" :min="0" :max="120" clearable style="width: 100%" />
              </NFormItem>
            </NGi>
            <NGi>
              <NFormItem label="住院床日(天)">
                <NInputNumber v-model:value="form.bed_days" :min="1" :max="365" clearable style="width: 100%" />
              </NFormItem>
            </NGi>
          </NGrid>
          <template v-if="calcMode === 'patient'">
            <NGrid cols="2" :x-gap="12">
              <NGi>
                <NFormItem label="住院流水号">
                  <NInput v-model:value="form.patient_id" placeholder="医生/主任需填管床流水号" clearable />
                </NFormItem>
              </NGi>
              <NGi>
                <NFormItem label="当前累计费用">
                  <NInputNumber v-model:value="form.current_cost" :precision="2" style="width: 100%" />
                </NFormItem>
              </NGi>
            </NGrid>
          </template>
          <NFormItem label="规则版本号">
            <NInput v-model:value="form.rule_version" />
          </NFormItem>
          <NButton type="primary" block :loading="calcLoading" @click="runCalculate">开始测算</NButton>
        </NForm>

        <NCard v-if="calcResult" size="small" embedded class="mt-16px" title="测算结果">
          <NDescriptions v-if="calcResult.status === 'MATCHED'" :column="2" size="small" bordered>
            <NDescriptionsItem label="状态"><NTag type="success" size="small">匹配成功</NTag></NDescriptionsItem>
            <NDescriptionsItem label="适用分组">{{ calcResult.group_name }}</NDescriptionsItem>
            <NDescriptionsItem label="DIP 分值">{{ calcResult.score }}</NDescriptionsItem>
            <NDescriptionsItem label="基准金额">￥{{ Number(calcResult.benchmark_amount || 0).toFixed(2) }}</NDescriptionsItem>
            <NDescriptionsItem v-if="calcResult.evaluation" label="判灯结果">
              <NTag
                size="small"
                :type="calcResult.evaluation.light === '红灯' ? 'error' : calcResult.evaluation.light === '黄灯' ? 'warning' : 'success'"
              >
                {{ calcResult.evaluation.light }}（{{ calcResult.evaluation.ratio_percent }}%）
              </NTag>
            </NDescriptionsItem>
            <NDescriptionsItem v-if="calcResult.evaluation?.boundary_note" label="边界提示">
              {{ calcResult.evaluation.boundary_note }}
            </NDescriptionsItem>
          </NDescriptions>
          <NAlert v-else :type="calcResult.status === 'NOT_APPLICABLE' ? 'info' : 'warning'" :title="calcResult.status || '结果提示'">
            <div>{{ calcResult.message }}</div>
            <ul v-if="calcResult.candidates?.length" class="mt-8px pl-16px text-12px">
              <li v-for="c in calcResult.candidates" :key="c.rule_id">
                #{{ c.rule_id }} {{ c.dip_group_name }}（{{ c.op_name || c.billing_unit }}）
              </li>
            </ul>
          </NAlert>
        </NCard>
      </NCard>
    </NGi>

    <NGi>
      <NCard title="本地智能业务问答（零云端外传）" :bordered="false" class="card-wrapper">
        <NSpace wrap :size="6" class="mb-12px">
          <NButton v-for="q in quickPrompts" :key="q" size="tiny" secondary @click="sendChat(q)">
            {{ q }}
          </NButton>
        </NSpace>

        <div class="h-380px overflow-y-auto rounded-6px bg-#f8fafc p-12px dark:bg-#18181c">
          <div v-for="(msg, idx) in chatHistory" :key="idx" class="mb-12px flex" :class="msg.role === 'user' ? 'justify-end' : 'justify-start'">
            <div
              class="max-w-86% whitespace-pre-wrap rounded-8px px-12px py-8px text-13px leading-20px"
              :class="msg.role === 'user' ? 'bg-primary text-white' : 'bg-white text-#1e293b shadow-sm dark:bg-#26262c dark:text-#e2e8f0'"
            >
              {{ msg.text }}
            </div>
          </div>
        </div>

        <div class="mt-12px flex gap-8px">
          <NInput v-model:value="chatInput" placeholder="输入自然语言提问，回车发送..." @keyup.enter="sendChat()" />
          <NButton type="primary" :loading="chatLoading" @click="sendChat()">发送</NButton>
        </div>
      </NCard>
    </NGi>
  </NGrid>
</template>
