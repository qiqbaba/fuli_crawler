export interface RecordItem {
  id: number | string
  title: string
  category: string
  source: string
  size: string
  format: string
  url: string
  resource_link: string
  pikpak_link: string
  pdf_path: string
  resolved_path: string
  publish_time: string
  has_pdf: boolean
  pdf_status: string
  dup_cnt: number
}

export interface Stats {
  total: number
  has_pdf_record: number
  sources: string[]
  categories: string[]
  db_path: string
  db_size_mb: number
  pdf_dir: string
  orphan_count: number
}

export interface FilterOptions {
  sources: string[]
  categories: string[]
  pdf_filters: string[]
  fix_filters: string[]
  sorts: string[]
  page_sizes: number[]
  orphan_count: number
}

export interface FilterOptionsParams {
  keyword: string
  source: string
  category: string
  pdf_filter: string
  fix_filter: string
}

export interface RecordsParams {
  keyword: string
  source: string
  category: string
  pdf_filter: string
  fix_filter: string
  sort_by: string
  page: number
  page_size: number
}

export interface RecordsResponse {
  total: number
  page: number
  page_size: number
  total_pages: number
  items: RecordItem[]
}

export interface DeleteResult {
  ok: boolean
  record_id: string
  db_deleted: boolean
  pdf_deleted: boolean
  other_refs: number
  message: string
}

export interface BackupItem {
  name: string
  size_mb: number
  mtime: number
  path: string
}

export interface ArtifactItem {
  name: string
  path: string
  dir: string
  kind: string
  size_mb: number
  mtime: number
}

export interface CacheOverview {
  pdf_records: number
  warm_units: number
  page_images: number
  coverage: number
  cache_dir: string
}

export interface AnalyticsMatrixRow {
  source: string
  total: number
  pdf_count: number
  rate: number
}

export interface AnalyticsData {
  total_records: number
  has_pdf_records: number
  pdf_rate: number
  db_size_mb: number
  sources_count: number
  categories_count: number
  source_data: Array<{ source: string; count: number }>
  cat_data: Array<{ category: string; count: number }>
  timeline_data: Array<{ month: string; count: number }>
  matrix_data: AnalyticsMatrixRow[]
}

export interface R2CleanupPreviewData {
  ok: boolean
  reason?: string
  prefix: string
  local_dir: string
  total: number
  have_count: number
  missing_count: number
  missing_size_human: string
  missing_preview: Array<{ key: string; size_human: string }>
  elapsed_ms: number
}

export interface CloudUsageParams {
  supabase: boolean
  r2: boolean
  dynamodb: boolean
  year: string
}

export interface CloudUsageData {
  generated_at: string
  elapsed_ms: number
  errors: string[]
  supabase: {
    ok: boolean
    reason?: string
    record_count?: number
    pdf_count?: number
    size_human?: string
    size_rpc?: string
    size_estimated?: boolean
    sources: Array<{ source: string; count: number }>
    quota_mb?: number
  }
  r2: {
    ok: boolean
    reason?: string
    prefix?: string
    file_count?: number
    size_human?: string
    years: Array<{ year: string; count: number; size_human: string }>
    quota_gb?: number
  }
  dynamodb: {
    ok: boolean
    reason?: string
    item_count?: number
    size_human?: string
    status?: string
    quota_gb?: number
  }
}

export interface ToolParam {
  name: string
  label: string
  type: 'bool' | 'int' | 'text' | 'select'
  default: string | number | boolean
  options: string[]
  min: number | null
  max: number | null
  help: string
}

export interface ToolSpec {
  id: string
  group: 'maintenance' | 'sync' | string
  title: string
  description: string
  danger: boolean
  section: string
  section_order: number
  params: ToolParam[]
  presets?: Record<string, { old: string; new: string }>
}

export interface ToolCatalog {
  tools: ToolSpec[]
  max_concurrent: number
}

export interface JobMetric {
  label: string
  value: string
}

export interface JobState {
  id: string
  tool_id: string
  title: string
  params: Record<string, unknown>
  status: string
  exit_code: number | null
  error: string
  created_at: number
  started_at: number | null
  finished_at: number | null
  seq: number
  log_count: number
  logs?: string[]
  metrics?: JobMetric[]
}

async function fetchJson<T>(path: string, params?: object): Promise<T> {
  const search = new URLSearchParams()
  if (params) {
    Object.entries(params as Record<string, unknown>).forEach(([key, value]) =>
      search.set(key, String(value)),
    )
  }
  const query = search.toString()
  const response = await fetch(query ? `${path}?${query}` : path)
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`请求失败 (${response.status}) ${detail.slice(0, 300)}`)
  }
  return (await response.json()) as T
}

async function postJson<T>(path: string, body: object): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`请求失败 (${response.status}) ${detail.slice(0, 300)}`)
  }
  return (await response.json()) as T
}

async function deleteJson<T>(path: string, params?: object): Promise<T> {
  const search = new URLSearchParams()
  if (params) {
    Object.entries(params as Record<string, unknown>).forEach(([key, value]) =>
      search.set(key, String(value)),
    )
  }
  const query = search.toString()
  const response = await fetch(query ? `${path}?${query}` : path, { method: 'DELETE' })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`请求失败 (${response.status}) ${detail.slice(0, 300)}`)
  }
  return (await response.json()) as T
}

export const jobStreamUrl = (jobId: string) =>
  `/api/jobs/${encodeURIComponent(jobId)}/stream`

export const pdfFileUrl = (path: string) =>
  `/api/pdf-file?${new URLSearchParams({ path }).toString()}`

export const artifactDownloadUrl = (path: string) =>
  `/api/artifacts/download?${new URLSearchParams({ path }).toString()}`

export const api = {
  stats: () => fetchJson<Stats>('/api/stats'),
  analytics: () => fetchJson<AnalyticsData>('/api/analytics'),
  filterOptions: (params: FilterOptionsParams) =>
    fetchJson<FilterOptions>('/api/filter-options', params),
  records: (params: RecordsParams) => fetchJson<RecordsResponse>('/api/records', params),
  deleteRecord: (recordId: number | string, pdfPath = '') =>
    deleteJson<DeleteResult>(`/api/records/${encodeURIComponent(String(recordId))}`, {
      pdf_path: pdfPath,
    }),
  pdfRefs: (recordId: number | string, pdfPath = '') =>
    fetchJson<{ record_id: string; other_refs: number }>(
      `/api/records/${encodeURIComponent(String(recordId))}/pdf-refs`,
      { pdf_path: pdfPath },
    ),
  reveal: (path: string) => postJson<{ ok: boolean; path: string }>('/api/reveal', { path }),
  backups: () => fetchJson<{ backups: BackupItem[] }>('/api/backups'),
  artifacts: () =>
    fetchJson<{ artifacts: ArtifactItem[]; roots: string[] }>('/api/artifacts'),
  cacheOverview: () => fetchJson<CacheOverview>('/api/cache-overview'),
  cloudUsage: (params: CloudUsageParams) =>
    fetchJson<CloudUsageData>('/api/cloud-usage', params),
  r2CleanupPreview: (prefix: string, localDir: string) =>
    fetchJson<R2CleanupPreviewData>('/api/r2-cleanup-preview', {
      prefix,
      local_dir: localDir,
    }),
  tools: () => fetchJson<ToolCatalog>('/api/tools'),
  createJob: (toolId: string, params: Record<string, unknown>) =>
    postJson<JobState>('/api/jobs', { tool_id: toolId, params }),
  jobs: () => fetchJson<{ jobs: JobState[] }>('/api/jobs'),
  job: (jobId: string) => fetchJson<JobState>(`/api/jobs/${encodeURIComponent(jobId)}`),
  cancelJob: (jobId: string) =>
    postJson<JobState>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {}),
}
