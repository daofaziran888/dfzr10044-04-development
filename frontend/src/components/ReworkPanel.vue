<template>
  <section class="rw-panel" data-testid="rework-panel">
    <div class="rw-head">
      <h3>部分行回炉审批 · 回炉明细 / 复检 / 入湖回执</h3>
      <div class="rw-head-actions">
        <button class="btn ghost" type="button" data-testid="rw-reseed" @click="loadScenario(true)">
          重置题面场景
        </button>
        <button class="btn ghost" type="button" data-testid="rw-open-detail" @click="openBatchDetail">
          打开原批次详情
        </button>
      </div>
    </div>

    <div v-if="coords" class="rw-body">
      <p class="rw-scope" data-testid="rw-scope">
        湖表 <b>{{ coords.lake_table }}</b> ｜ 批次 <b>{{ coords.batch_id }}</b> ｜
        规则 <b>{{ coords.rule_id }}</b>（命中版本 <b>{{ coords.rule_version }}</b>，上限 100）
        <br />
        <span class="rw-warn">另一张湖表 <b>{{ coords.other_lake_table }}</b> 使用相同批号与规则号，用于越权校验。</span>
      </p>

      <table class="rw-rows" data-testid="rw-quarantine-rows">
        <thead>
          <tr><th>下标</th><th>行标识</th><th>user</th><th>amount</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in coords.rows" :key="item.row_id" :data-testid="`rw-row-${item.row_index}`">
            <td>{{ item.row_index }}</td>
            <td class="mono">{{ item.row_id }}</td>
            <td>{{ item.row?.user_id }}</td>
            <td :class="{ bad: Number(item.row?.amount) > 100, empty: item.row?.amount == null }">
              {{ item.row?.amount == null ? '（空，可补录）' : item.row.amount }}
            </td>
            <td>{{ item.row_index === 0 ? '本次只批准这一行' : '180 元超限，继续隔离' }}</td>
          </tr>
        </tbody>
      </table>

      <div class="rw-controls">
        <button class="btn primary" type="button" data-testid="rw-approve-first" @click="approveFirst">
          批准前一行回炉
        </button>
        <button class="btn" type="button" data-testid="rw-cross-table" @click="crossTableClaim">
          用另一张湖表领取（应拒绝·越权）
        </button>

        <template v-if="detailId">
          <label class="rw-amount">
            补录金额
            <input v-model="amount" data-testid="rw-amount-input" placeholder="例如 50" />
          </label>
          <button class="btn" type="button" data-testid="rw-reinspect" @click="reinspect">补录并复检</button>
          <button class="btn" type="button" data-testid="rw-claim" @click="claim">入湖领取</button>
          <button class="btn" type="button" data-testid="rw-withdraw" @click="withdraw">审批撤签（与领取冲突）</button>
          <button class="btn primary" type="button" data-testid="rw-submit" @click="submit">提交并确认入湖</button>
        </template>
      </div>

      <p v-if="statusLine" class="rw-status" data-testid="rw-status-line" :data-status="statusLine">
        当前回炉状态：<b>{{ statusLine }}</b>
        <span v-if="receiptId"> ｜ 入湖回执：<b class="mono" data-testid="rw-receipt-id">{{ receiptId }}</b></span>
      </p>
      <p v-if="messageLine" class="rw-message" data-testid="rw-message">{{ messageLine }}</p>

      <div v-if="batchDetail" class="rw-detail" data-testid="rw-batch-detail">
        <h4>原批次详情 · 回炉状态与入湖回执（按批准行关联）</h4>
        <table class="rw-rows">
          <thead>
            <tr><th>approved_row_id</th><th>detail_id</th><th>回炉状态</th><th>receipt_id</th></tr>
          </thead>
          <tbody>
            <tr v-for="a in batchDetail.associations" :key="a.detail_id"
                :data-testid="`rw-assoc-${a.approved_row_id}`">
              <td class="mono">{{ a.approved_row_id }}</td>
              <td class="mono">{{ a.detail_id }}</td>
              <td>{{ a.rework_status }}</td>
              <td class="mono">{{ a.receipt_id || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p class="rw-sub">隔离区剩余 {{ batchDetail.quarantine_count }} 行；回执总数 {{ batchDetail.receipts?.length ?? 0 }}。</p>
      </div>

      <details class="rw-json">
        <summary>最近一次请求返回（JSON）</summary>
        <pre data-testid="rw-json">{{ lastJson }}</pre>
      </details>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

type AnyRecord = Record<string, any>

const coords = ref<AnyRecord | null>(null)
const approvalId = ref('')
const detailId = ref('')
const approvedRowId = ref('')
const receiptId = ref('')
const amount = ref('50')
const statusLine = ref('')
const messageLine = ref('')
const lastJson = ref('')
const batchDetail = ref<AnyRecord | null>(null)

async function post(path: string, body: AnyRecord = {}): Promise<AnyRecord> {
  const resp = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return (await resp.json()) as AnyRecord
}

async function getJson(path: string): Promise<AnyRecord> {
  const resp = await fetch(`/api${path}`)
  return (await resp.json()) as AnyRecord
}

function show(result: AnyRecord): void {
  lastJson.value = JSON.stringify(result, null, 2)
  statusLine.value = String(result.rework_status ?? result.status ?? statusLine.value)
  messageLine.value = String(result.message ?? '')
  if (result.receipt_id) receiptId.value = String(result.receipt_id)
}

async function loadScenario(reseed = false): Promise<void> {
  const result = reseed
    ? await post('/quarantine_zone/scenario/seed', { force: true })
    : await getJson('/quarantine_zone/scenario')
  coords.value = result.scenario
  approvalId.value = ''
  detailId.value = ''
  approvedRowId.value = ''
  receiptId.value = ''
  statusLine.value = ''
  messageLine.value = ''
  batchDetail.value = null
}

async function approveFirst(): Promise<void> {
  if (!coords.value) return
  const emptyRowId = coords.value.empty_amount_row.row_id as string
  const result = await post('/quarantine_zone/approvals', {
    lake_table: coords.value.lake_table,
    batch_id: coords.value.batch_id,
    rule_id: coords.value.rule_id,
    row_refs: [emptyRowId],
    approver: 'approver_01',
    role: 'quality_approver',
  })
  show(result)
  if (result.ok && result.details?.[0]) {
    approvalId.value = String(result.approval_id)
    detailId.value = String(result.details[0].detail_id)
    approvedRowId.value = String(result.details[0].approved_row_id)
  }
}

async function reinspect(): Promise<void> {
  const result = await post('/quarantine_zone/reinspect', {
    detail_id: detailId.value,
    amount: amount.value,
  })
  show(result)
}

async function claim(): Promise<void> {
  const result = await post(`/quarantine_zone/details/${detailId.value}/claim`, {
    claimant: 'ingest-console',
  })
  show(result)
}

async function withdraw(): Promise<void> {
  const result = await post(`/quarantine_zone/approvals/${approvalId.value}/withdraw`, {})
  show(result)
}

async function submit(): Promise<void> {
  const result = await post(`/quarantine_zone/details/${detailId.value}/submit`, {
    amount: amount.value,
  })
  show(result)
  if (result.ok) await openBatchDetail()
}

async function crossTableClaim(): Promise<void> {
  if (!detailId.value || !coords.value) {
    messageLine.value = '请先批准前一行，再演示跨湖表越权领取。'
    return
  }
  const result = await post(`/quarantine_zone/details/${detailId.value}/claim`, {
    lake_table: coords.value.other_lake_table,
    batch_id: coords.value.batch_id,
    rule_id: coords.value.rule_id,
  })
  show(result)
}

async function openBatchDetail(): Promise<void> {
  if (!coords.value) return
  const q = new URLSearchParams({ lake_table: coords.value.lake_table })
  const result = await getJson(
    `/quarantine_zone/batches/${coords.value.batch_id}/detail?${q.toString()}`,
  )
  batchDetail.value = result
  lastJson.value = JSON.stringify(result, null, 2)
}

onMounted(() => {
  void loadScenario(false)
})
</script>

<style scoped>
.rw-panel {
  border: 1px solid #d8e2ee;
  border-radius: 12px;
  padding: 16px 18px;
  margin: 0 0 18px;
  background: linear-gradient(180deg, #f7faff 0%, #ffffff 60%);
}
.rw-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.rw-head h3 { margin: 0; font-size: 16px; color: #16324f; }
.rw-head-actions { display: flex; gap: 8px; }
.rw-scope { font-size: 13px; line-height: 1.7; color: #33475b; }
.rw-warn { color: #9a6a00; }
.rw-rows { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 8px 0 12px; }
.rw-rows th, .rw-rows td { border: 1px solid #e2e8f2; padding: 6px 8px; text-align: left; }
.rw-rows th { background: #eef4fc; color: #274b6d; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
td.bad { color: #c0392b; font-weight: 700; }
td.empty { color: #b7791f; font-weight: 600; }
.rw-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 6px 0 10px; }
.rw-amount { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.rw-amount input { width: 90px; padding: 6px 8px; border: 1px solid #c6d2e0; border-radius: 8px; }
.rw-status { font-size: 13.5px; color: #1f4e79; margin: 4px 0; }
.rw-message { font-size: 13px; color: #5a6b7d; margin: 2px 0 8px; }
.rw-detail h4 { margin: 10px 0 6px; font-size: 14px; color: #16324f; }
.rw-sub { font-size: 12.5px; color: #5a6b7d; }
.rw-json summary { cursor: pointer; font-size: 12.5px; color: #4a6076; }
.rw-json pre {
  max-height: 260px; overflow: auto; background: #0f2233; color: #cfe6ff;
  padding: 10px; border-radius: 8px; font-size: 12px;
}
</style>
