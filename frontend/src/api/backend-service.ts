import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 城市下钻空层（缺层解释 / 立方分区回补）----

export type GapReason = {
  reason_code: string
  reason_label: string
  affected_parents: string[]
  gap_count: number
  pending_count: number
}

export type CubePartitionInfo = {
  partition_id: string
  cube_name: string
  level_dimension: string
  scope: Record<string, string>
  status: string
  classified_measures: Record<string, number>
  attempt: number
  last_error: string
}

export type DrillGap = {
  gap_id: string
  cube_name: string
  parent_dimension: string
  parent_value: string
  parent_key: Record<string, string>
  level_dimension: string
  reason_code: string
  reason_label: string
  empty_layer_reason: string
  measure_names: string[]
  parent_measures: Record<string, number>
  parent_revenue: number
  classified_measures: Record<string, number>
  pending_measures: Record<string, number>
  status: string
  partition_id: string
  reconciliation: Record<string, number>
  balanced: boolean
  history: { event: string; [key: string]: unknown }[]
}

export type DrillGapList = {
  ok: boolean
  total: number
  items: DrillGap[]
  explanations: GapReason[]
}

export type DrillGapDetail = {
  ok: boolean
  gap: DrillGap
  partition: CubePartitionInfo | null
  obtained_facts: {
    parent_measures: Record<string, number>
    classified_measures: Record<string, number>
  }
  pending_backfill: {
    measures: Record<string, number>
    reason_code: string
    reason_label: string
  }
  reconciliation: Record<string, number>
  balanced: boolean
  fact_rows: Record<string, string | number | null>[]
}

export type ReconciliationReport = {
  ok: boolean
  all_balanced: boolean
  items: {
    gap_id: string
    parent_value: string
    reason_code: string
    status: string
    parent_measures: Record<string, number>
    classified_measures: Record<string, number>
    pending_measures: Record<string, number>
    reconciliation: Record<string, number>
    balanced: boolean
  }[]
}

async function readJson<T>(response: Response): Promise<T> {
  return (await response.json()) as T
}

export async function listDrillGaps(filters: { reason_code?: string; status?: string } = {}): Promise<DrillGapList> {
  const query = new URLSearchParams(
    Object.entries(filters).filter(([, value]) => Boolean(value)) as [string, string][],
  ).toString()
  const response = await request(`/api/analytics/drill-gaps?${query}`)
  if (!response.ok) throw new Error('下钻缺层清单读取失败')
  return readJson<DrillGapList>(response)
}

export async function listGapReasons(): Promise<{ ok: boolean; items: GapReason[] }> {
  const response = await request('/api/analytics/drill-gaps/reasons')
  if (!response.ok) throw new Error('空层解释读取失败')
  return readJson(response)
}

export async function getDrillGapDetail(gapId: string): Promise<DrillGapDetail> {
  const response = await request(`/api/analytics/drill-gaps/${encodeURIComponent(gapId)}`)
  if (!response.ok) throw new Error('下钻详情读取失败')
  return readJson<DrillGapDetail>(response)
}

export async function backfillGapAttribute(gapId: string): Promise<{ ok: boolean; detail: DrillGapDetail }> {
  const response = await request(`/api/analytics/drill-gaps/${encodeURIComponent(gapId)}/backfill-attribute`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('维值回补失败')
  return readJson(response)
}

export async function retryGapPartition(
  gapId: string,
  payload: { fail?: boolean; failure_reason?: string; incoming_rows?: Record<string, unknown>[] } = {},
): Promise<{ ok: boolean; message?: string; detail: DrillGapDetail }> {
  const response = await request(`/api/analytics/drill-gaps/${encodeURIComponent(gapId)}/retry-partition`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('分区构建重试失败')
  return readJson(response)
}

export async function getGapReconciliation(): Promise<ReconciliationReport> {
  const response = await request('/api/analytics/drill-gaps-reconciliation')
  if (!response.ok) throw new Error('对账数据读取失败')
  return readJson<ReconciliationReport>(response)
}

export async function listCubePartitions(): Promise<{ ok: boolean; items: CubePartitionInfo[] }> {
  const response = await request('/api/analytics/cube-partitions')
  if (!response.ok) throw new Error('立方分区读取失败')
  return readJson(response)
}

export async function resetDrillGaps(): Promise<{ ok: boolean; message: string }> {
  const response = await request('/api/analytics/drill-gaps/reset', { method: 'POST' })
  if (!response.ok) throw new Error('缺层数据重置失败')
  return readJson(response)
}
