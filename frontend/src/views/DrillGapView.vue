<template>
  <section class="page" data-module="drill-gap">
    <header class="page-head">
      <div>
        <h2>城市下钻空层解释</h2>
        <p class="page-desc">
          城市向门店层下钻出现空层时，登记对应父级事实的缺层数据：保留父行已核算收入与空层原因，
          已归类金额与待归类收入全程可对账。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="reset-gaps" @click="onReset">重置演示数据</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">缺层记录</span>
        <strong class="stat-value">{{ gaps.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待回补</span>
        <strong class="stat-value">{{ pendingCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已补齐</span>
        <strong class="stat-value">{{ classifiedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">对账状态</span>
        <strong class="stat-value" :class="reconciliation?.all_balanced ? 'trend-up' : 'trend-down'">
          {{ reconciliation?.all_balanced ? '全部平衡' : '存在差异' }}
        </strong>
      </article>
    </div>

    <h3 class="section-title">各类空层解释</h3>
    <div class="reason-grid">
      <article v-for="reason in reasons" :key="reason.reason_code" class="reason-card" :data-testid="`reason-${reason.reason_code}`">
        <header>
          <span class="reason-code">{{ reason.reason_code }}</span>
          <span class="reason-count">待回补 {{ reason.pending_count }} / {{ reason.gap_count }}</span>
        </header>
        <p class="reason-label">{{ reason.reason_label }}</p>
        <p class="reason-parents">涉及城市：{{ reason.affected_parents.join('、') || '—' }}</p>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>空层原因</span>
        <select v-model="filters.reason_code" data-testid="filter-reason">
          <option value="">全部</option>
          <option v-for="reason in reasons" :key="reason.reason_code" :value="reason.reason_code">
            {{ reason.reason_label }}
          </option>
        </select>
      </label>
      <label class="filter-item">
        <span>回补状态</span>
        <select v-model="filters.status" data-testid="filter-status">
          <option value="">全部</option>
          <option value="待回补">待回补</option>
          <option value="构建失败">构建失败</option>
          <option value="已补齐">已补齐</option>
        </select>
      </label>
      <button class="btn" type="button" data-testid="apply-filter" @click="reload">查询</button>
    </form>

    <table class="data-table" data-testid="gap-table">
      <thead>
        <tr>
          <th>缺层编号</th>
          <th>城市（父级）</th>
          <th>下钻层</th>
          <th>空层原因</th>
          <th>父行已核算收入</th>
          <th>已归类金额</th>
          <th>待归类收入</th>
          <th>关联分区</th>
          <th>状态</th>
          <th>对账</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="gap in gaps" :key="gap.gap_id" :data-testid="`gap-row-${gap.gap_id}`" :data-status="gap.status">
          <td>{{ gap.gap_id }}</td>
          <td>{{ gap.parent_value }}</td>
          <td>{{ gap.level_dimension }}</td>
          <td>
            <span class="reason-chip" :data-reason="gap.reason_code">{{ gap.reason_label }}</span>
          </td>
          <td>{{ formatMeasures(gap.parent_measures) }}</td>
          <td>{{ formatMeasures(gap.classified_measures) }}</td>
          <td>{{ formatMeasures(gap.pending_measures) }}</td>
          <td>{{ gap.partition_id || '—' }}</td>
          <td>
            <span class="status-chip" :class="statusClass(gap.status)">{{ gap.status }}</span>
          </td>
          <td :class="gap.balanced ? 'trend-up' : 'trend-down'">{{ gap.balanced ? '平衡' : '差异' }}</td>
          <td class="row-actions">
            <RouterLink class="link" :data-testid="`detail-${gap.gap_id}`" :to="`/drill_path/gaps/${gap.gap_id}`">
              下钻详情
            </RouterLink>
          </td>
        </tr>
        <tr v-if="!gaps.length">
          <td colspan="11" class="empty-state">当前筛选条件下没有缺层记录</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  getGapReconciliation,
  listDrillGaps,
  listGapReasons,
  resetDrillGaps,
  type DrillGap,
  type GapReason,
  type ReconciliationReport,
} from '@/api/backend-service'

const gaps = ref<DrillGap[]>([])
const reasons = ref<GapReason[]>([])
const reconciliation = ref<ReconciliationReport | null>(null)
const errorMessage = ref('')
const filters = reactive<{ reason_code: string; status: string }>({ reason_code: '', status: '' })

const pendingCount = computed(() => gaps.value.filter((gap) => gap.status !== '已补齐').length)
const classifiedCount = computed(() => gaps.value.filter((gap) => gap.status === '已补齐').length)

function formatMeasures(measures: Record<string, number>): string {
  return Object.entries(measures)
    .map(([name, value]) => `${name}=${value}`)
    .join('，')
}

function statusClass(status: string): string {
  if (status === '已补齐') return 'chip-ok'
  if (status === '构建失败') return 'chip-fail'
  return 'chip-pending'
}

async function reload() {
  errorMessage.value = ''
  try {
    const [gapList, reasonList, report] = await Promise.all([
      listDrillGaps({ reason_code: filters.reason_code, status: filters.status }),
      listGapReasons(),
      getGapReconciliation(),
    ])
    gaps.value = gapList.items
    reasons.value = reasonList.items
    reconciliation.value = report
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '缺层数据读取失败'
  }
}

async function onReset() {
  errorMessage.value = ''
  try {
    await resetDrillGaps()
    filters.reason_code = ''
    filters.status = ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重置失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title { font-size: 15px; margin: 16px 0 8px; }
.reason-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; margin-bottom: 12px; }
.reason-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.reason-card header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.reason-code { font-family: monospace; font-size: 12px; background: #eef2ff; color: #3730a3; border-radius: 4px; padding: 2px 6px; }
.reason-count { font-size: 12px; color: var(--muted); }
.reason-label { margin: 4px 0; font-size: 13px; }
.reason-parents { margin: 0; font-size: 12px; color: var(--muted); }
.reason-chip { font-size: 12px; }
.status-chip { border-radius: 10px; padding: 2px 8px; font-size: 12px; }
.chip-ok { background: #dcfce7; color: #166534; }
.chip-fail { background: #fee2e2; color: #991b1b; }
.chip-pending { background: #fef9c3; color: #854d0e; }
.trend-up { color: #166534; }
.trend-down { color: #991b1b; }
.filter-item select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
</style>
