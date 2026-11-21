<template>
  <section class="page" data-module="drill-gap-detail">
    <header class="page-head">
      <div>
        <h2>下钻详情 · {{ detail?.gap.parent_value ?? gapId }}</h2>
        <p class="page-desc">
          缺层数据按父级标识关联门店层立方分区，读出已得到的事实与仍需回补的部分。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/drill_path">返回缺层清单</RouterLink>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="loading" class="page-desc">加载中…</p>

    <template v-if="detail">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">空层原因</span>
          <strong class="stat-value reason-code">{{ detail.gap.reason_code }}</strong>
          <p class="card-note">{{ detail.gap.empty_layer_reason }}</p>
        </article>
        <article class="stat-card">
          <span class="stat-label">父行已核算收入</span>
          <strong class="stat-value">{{ primaryMeasure(detail.gap.parent_measures) }}</strong>
          <p class="card-note">{{ formatMeasures(detail.gap.parent_measures) }}</p>
        </article>
        <article class="stat-card">
          <span class="stat-label">已归类金额</span>
          <strong class="stat-value trend-up">{{ primaryMeasure(detail.gap.classified_measures) }}</strong>
          <p class="card-note">{{ formatMeasures(detail.gap.classified_measures) }}</p>
        </article>
        <article class="stat-card">
          <span class="stat-label">待归类收入</span>
          <strong class="stat-value trend-down">{{ primaryMeasure(detail.gap.pending_measures) }}</strong>
          <p class="card-note">{{ formatMeasures(detail.gap.pending_measures) }}</p>
        </article>
      </div>

      <div class="detail-grid">
        <article class="detail-panel">
          <h3>立方分区（按父级标识 {{ JSON.stringify(detail.gap.parent_key) }} 关联）</h3>
          <dl v-if="detail.partition" class="kv-list">
            <dt>分区编号</dt><dd>{{ detail.partition.partition_id }}</dd>
            <dt>所属立方</dt><dd>{{ detail.partition.cube_name }}</dd>
            <dt>下钻层维度</dt><dd>{{ detail.partition.level_dimension }}</dd>
            <dt>父级范围</dt><dd>{{ JSON.stringify(detail.partition.scope) }}</dd>
            <dt>分区状态</dt>
            <dd><span class="status-chip" :class="partitionClass(detail.partition.status)">{{ detail.partition.status }}</span></dd>
            <dt>构建次数</dt><dd>{{ detail.partition.attempt }}</dd>
            <dt>已核算度量</dt><dd>{{ formatMeasures(detail.partition.classified_measures) || '—' }}</dd>
            <dt v-if="detail.partition.last_error">最近错误</dt>
            <dd v-if="detail.partition.last_error" class="trend-down">{{ detail.partition.last_error }}</dd>
          </dl>
          <p v-else class="empty-state">暂无关联分区</p>
        </article>

        <article class="detail-panel">
          <h3>对账与回补</h3>
          <dl class="kv-list">
            <dt>缺层状态</dt>
            <dd><span class="status-chip" :class="gapStatusClass(detail.gap.status)">{{ detail.gap.status }}</span></dd>
            <dt>对账恒等式</dt>
            <dd v-for="name in detail.gap.measure_names" :key="name">
              {{ name }}：{{ detail.gap.classified_measures[name] ?? 0 }}
              + {{ detail.gap.pending_measures[name] ?? 0 }}
              = {{ detail.reconciliation[name] }}
              <span :class="detail.balanced ? 'trend-up' : 'trend-down'">
                （{{ detail.balanced ? '与父行一致' : '与父行不一致' }}）
              </span>
            </dd>
          </dl>

          <div class="action-box">
            <template v-if="detail.gap.reason_code === 'MISSING_DIMENSION_ATTR'">
              <p class="page-desc">事实已到达，补齐门店维值后待归类收入即可进入门店层。</p>
              <button
                class="btn primary"
                type="button"
                data-testid="backfill-attribute"
                :disabled="busy || detail.gap.status === '已补齐'"
                @click="onBackfillAttribute"
              >
                补齐门店维值并归位
              </button>
            </template>
            <template v-else>
              <p class="page-desc">门店层分区尚未构建；重试只接收当前父级事实，相邻父级分区会被拒收。</p>
              <button
                class="btn"
                type="button"
                data-testid="retry-fail"
                :disabled="busy"
                @click="onRetryFail"
              >
                模拟构建失败
              </button>
              <button
                class="btn primary"
                type="button"
                data-testid="retry-success"
                :disabled="busy || detail.gap.status === '已补齐'"
                @click="onRetrySuccess"
              >
                重试构建并回补
              </button>
            </template>
          </div>
        </article>
      </div>

      <article class="detail-panel">
        <h3>当前父级事实行（仅 {{ detail.gap.parent_value }}，相邻父级不进入本层）</h3>
        <table class="data-table" data-testid="fact-table">
          <thead>
            <tr>
              <th v-for="column in factColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in detail.fact_rows" :key="index" :data-testid="`fact-row-${index}`">
              <td v-for="column in factColumns" :key="column">{{ row[column] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </article>

      <article class="detail-panel">
        <h3>处理轨迹</h3>
        <ul class="history-list" data-testid="history-list">
          <li v-for="(event, index) in detail.gap.history" :key="index">
            <span class="history-event">{{ event.event }}</span>
            <code>{{ JSON.stringify(omitEvent(event)) }}</code>
          </li>
        </ul>
      </article>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  backfillGapAttribute,
  getDrillGapDetail,
  retryGapPartition,
  type DrillGapDetail,
} from '@/api/backend-service'

const props = defineProps<{ gapId: string }>()
const gapId = computed(() => props.gapId)

const detail = ref<DrillGapDetail | null>(null)
const loading = ref(true)
const busy = ref(false)
const errorMessage = ref('')

const factColumns = computed(() => {
  const columns = new Set<string>()
  for (const row of detail.value?.fact_rows ?? []) {
    Object.keys(row).forEach((key) => columns.add(key))
  }
  return Array.from(columns)
})

function primaryMeasure(measures: Record<string, number>): number {
  const first = detail.value?.gap.measure_names[0] ?? 'revenue'
  return measures[first] ?? 0
}

function formatMeasures(measures: Record<string, number>): string {
  return Object.entries(measures)
    .map(([name, value]) => `${name}=${value}`)
    .join('，')
}

function partitionClass(status: string): string {
  if (status === '已上线') return 'chip-ok'
  if (status === '构建失败') return 'chip-fail'
  return 'chip-pending'
}

function gapStatusClass(status: string): string {
  if (status === '已补齐') return 'chip-ok'
  if (status === '构建失败') return 'chip-fail'
  return 'chip-pending'
}

function omitEvent(event: Record<string, unknown>): Record<string, unknown> {
  const { event: _event, ...rest } = event
  return rest
}

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    detail.value = await getDrillGapDetail(gapId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下钻详情读取失败'
  } finally {
    loading.value = false
  }
}

async function runAction(action: () => Promise<{ ok: boolean; detail: DrillGapDetail; message?: string }>) {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await action()
    if (!result.ok) throw new Error(result.message ?? '操作失败')
    detail.value = result.detail
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function onBackfillAttribute() {
  return runAction(() => backfillGapAttribute(gapId.value))
}

function onRetryFail() {
  return runAction(() => retryGapPartition(gapId.value, { fail: true, failure_reason: '手动模拟构建失败' }))
}

function onRetrySuccess() {
  return runAction(() => retryGapPartition(gapId.value, { fail: false }))
}

onMounted(load)
</script>

<style scoped>
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.detail-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
.detail-panel h3 { margin: 0 0 10px; font-size: 14px; }
.kv-list { display: grid; grid-template-columns: 120px 1fr; gap: 6px 10px; margin: 0; }
.kv-list dt { color: var(--muted); font-size: 12px; }
.kv-list dd { margin: 0; font-size: 13px; }
.card-note { margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.reason-code { font-size: 14px; font-family: monospace; }
.trend-up { color: #166534; }
.trend-down { color: #991b1b; }
.status-chip { border-radius: 10px; padding: 2px 8px; font-size: 12px; }
.chip-ok { background: #dcfce7; color: #166534; }
.chip-fail { background: #fee2e2; color: #991b1b; }
.chip-pending { background: #fef9c3; color: #854d0e; }
.action-box { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.history-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.history-list li { display: flex; gap: 8px; align-items: baseline; font-size: 12px; }
.history-event { background: #eef2ff; color: #3730a3; border-radius: 4px; padding: 2px 6px; white-space: nowrap; }
.history-list code { color: var(--muted); }
@media (max-width: 900px) { .detail-grid { grid-template-columns: 1fr; } }
</style>
