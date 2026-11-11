import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  jobStreamUrl,
  type JobState,
  type ToolParam,
  type ToolSpec,
} from './api'
import Artifacts from './Artifacts'
import CloudUsage from './CloudUsage'
import R2CleanupPreview from './R2CleanupPreview'

type ParamValue = string | number | boolean
type ParamValues = Record<string, ParamValue>

const STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '运行中',
  cancelling: '取消中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])

// 参数间的联动约束：与 registry.py 的 argv_builder 语义保持一致
const DEPENDENCIES: Record<string, Record<string, string>> = {
  'r2-download': { delete_missing: 'delete_after' },
}
// 会产生落盘文件的工具，展示「产物文件」面板
const ARTIFACT_TOOLS = new Set([
  'db-backup', 'export-links', 'pdf-dedup', 'clean-missing', 'clean-noise',
  'record-duplicates', 'fanhao', 'check-dates',
])

function defaultValues(tool: ToolSpec | null): ParamValues {
  const values: ParamValues = {}
  for (const spec of tool?.params ?? []) values[spec.name] = spec.default
  return values
}

function ParamField({
  spec, value, onChange, disabled,
}: {
  spec: ToolParam
  value: ParamValue
  onChange: (next: ParamValue) => void
  disabled: boolean
}) {
  if (spec.type === 'bool') {
    return (
      <label className={`param-field param-check ${disabled ? 'disabled' : ''}`}>
        <input
          type="checkbox"
          checked={Boolean(value)}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span title={spec.help}>{spec.label}</span>
      </label>
    )
  }
  if (spec.type === 'select') {
    return (
      <label className="param-field">
        <span title={spec.help}>{spec.label}</span>
        <select
          value={String(value)}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        >
          {spec.options.map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
      </label>
    )
  }
  if (spec.type === 'int') {
    return (
      <label className="param-field">
        <span title={spec.help}>{spec.label}</span>
        <input
          type="number"
          min={spec.min ?? undefined}
          max={spec.max ?? undefined}
          value={Number(value)}
          disabled={disabled}
          onChange={(event) => onChange(Number(event.target.value))}
        />
      </label>
    )
  }
  return (
    <label className="param-field">
      <span title={spec.help}>{spec.label}</span>
      <input
        type="text"
        value={String(value)}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  )
}

export default function JobsPanel({ group }: { group: 'maintenance' | 'sync' }) {
  const toolsQuery = useQuery({
    queryKey: ['tools'],
    queryFn: api.tools,
    staleTime: 5 * 60_000,
  })
  const tools = (toolsQuery.data?.tools ?? []).filter((tool) => tool.group === group)
  const grouped = useMemo(() => {
    const sections = new Map<string, ToolSpec[]>()
    for (const tool of tools) {
      const list = sections.get(tool.section) ?? []
      list.push(tool)
      sections.set(tool.section, list)
    }
    return [...sections.entries()].sort(
      ([, left], [, right]) => (left[0]?.section_order ?? 99) - (right[0]?.section_order ?? 99),
    )
  }, [tools])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = tools.find((tool) => tool.id === selectedId) ?? tools[0] ?? null
  const [values, setValues] = useState<ParamValues>({})
  const [ack, setAck] = useState(false)
  const [jobId, setJobId] = useState<string | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [status, setStatus] = useState<JobState | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  const requestSeq = useRef(0)

  const jobsQuery = useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs,
    refetchInterval: 4000,
  })
  const recentJobs = (jobsQuery.data?.jobs ?? []).slice(0, 12)

  const queryClient = useQueryClient()
  const statsQuery = useQuery({ queryKey: ['stats'], queryFn: api.stats })
  const cacheQuery = useQuery({
    queryKey: ['cache-overview'],
    queryFn: api.cacheOverview,
    enabled: selected?.id === 'warmup-cache',
  })

  useEffect(() => {
    if (!status || !TERMINAL.has(status.status)) return
    queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    queryClient.invalidateQueries({ queryKey: ['cache-overview'] })
    queryClient.invalidateQueries({ queryKey: ['stats'] })
    queryClient.invalidateQueries({ queryKey: ['analytics'] })
    queryClient.invalidateQueries({ queryKey: ['records'] })
  }, [status?.status, queryClient])

  const currentToolId = selected?.id ?? ''
  useEffect(() => {
    setValues(defaultValues(selected))
    setAck(false)
    setError('')
  }, [currentToolId])

  useEffect(() => {
    if (!jobId) return
    const source = new EventSource(jobStreamUrl(jobId))
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as { type?: string; line?: string }
        if (payload.type === 'log' && typeof payload.line === 'string') {
          const line = payload.line
          setLogs((previous) =>
            previous.length >= 2000 ? [...previous.slice(-1500), line] : [...previous, line],
          )
        }
      } catch {
        // 忽略心跳与非 JSON 数据
      }
    }
    const readState = (event: Event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data) as Partial<JobState> & { reason?: string }
        // done 帧在超时/任务被淘汰时只带 reason，不能覆盖已有状态
        if (!payload || typeof payload.status !== 'string') return
        setStatus(payload as JobState)
      } catch {
        // 忽略格式异常的状态帧
      }
    }
    source.addEventListener('status', readState)
    source.addEventListener('done', (event) => {
      readState(event)
      source.close()
    })
    source.onerror = () => {
      setError('日志连接中断，已切换到任务列表轮询')
      source.close()
      void jobsQuery.refetch()
    }
    return () => source.close()
  }, [jobId])

  useEffect(() => {
    const element = logRef.current
    if (element) element.scrollTop = element.scrollHeight
  }, [logs])

  const hasDryRun = selected?.params.some(
    (spec) => spec.name === 'run' && spec.default === false,
  ) ?? false
  const presets = selected?.presets

  const setParam = (name: string, next: ParamValue) => {
    setValues((previous) => {
      const updated = { ...previous, [name]: next }
      if (selected?.id === 'replace-domain' && name === 'preset' && presets) {
        const pair = presets[String(next)]
        if (pair) {
          updated.old = pair.old
          updated.new = pair.new
        }
      }
      return updated
    })
  }

  const isDisabled = (spec: ToolParam): boolean => {
    const dependency = DEPENDENCIES[selected?.id ?? '']?.[spec.name]
    if (dependency && !Boolean(values[dependency])) return true
    if (
      selected?.id === 'replace-domain' && presets &&
      (spec.name === 'old' || spec.name === 'new') &&
      presets[String(values.preset ?? '自定义')]
    ) return true
    return false
  }

  const submit = async () => {
    if (!selected) return
    if (selected.danger && !ack) {
      setError('该工具可能改动数据，请先勾选风险确认')
      return
    }
    setSubmitting(true)
    setError('')
    setLogs([])
    setStatus(null)
    setJobId(null)
    const seq = ++requestSeq.current
    try {
      const payload: Record<string, unknown> = { ...values, _ack: ack }
      const job = await api.createJob(selected.id, payload)
      if (seq !== requestSeq.current) return
      setLogs(job.logs ?? [])
      setStatus(job)
      setJobId(job.id)
    } catch (cause) {
      if (seq === requestSeq.current) setError(String(cause))
    } finally {
      if (seq === requestSeq.current) setSubmitting(false)
    }
  }

  const cancel = async () => {
    if (!jobId) return
    try {
      await api.cancelJob(jobId)
    } catch (cause) {
      setError(String(cause))
    }
  }

  const openJob = (id: string) => {
    requestSeq.current += 1
    setLogs([])
    setStatus(null)
    setError('')
    setJobId(id)
  }

  return (
    <div className="jobs-layout">
      <aside className="tool-list">
        <div className="panel-title">
          {group === 'maintenance' ? '批量管理工具' : '云端同步工具'} · {tools.length}
        </div>
        {toolsQuery.isError && <div className="panel-error">{String(toolsQuery.error)}</div>}
        {grouped.map(([section, list]) => (
          <div className="tool-group" key={section}>
            <div className="tool-group-title">{section} · {list.length}</div>
            {list.map((tool) => (
              <button
                key={tool.id}
                className={`tool-item ${selected?.id === tool.id ? 'active' : ''}`}
                onClick={() => setSelectedId(tool.id)}
              >
                <span className="tool-head">
                  <span className="tool-name">{tool.title}</span>
                  {tool.danger && <span className="tool-danger">需确认</span>}
                </span>
                <span className="tool-desc">{tool.description}</span>
              </button>
            ))}
          </div>
        ))}
      </aside>

      <section className="job-main">
        {group === 'sync' && <CloudUsage />}
        {!selected ? (
          <div className="empty">工具目录加载中或为空</div>
        ) : (
          <>
            <div className="job-form">
              <h2>{selected.title}</h2>
              <p>{selected.description}</p>
              {(selected.id === 'db-backup' || selected.id === 'db-vacuum') && (
                <p className="hint" title={statsQuery.data?.db_path}>
                  当前数据库：{statsQuery.data?.db_path ?? '…'} · {statsQuery.data?.db_size_mb ?? 0} MB
                </p>
              )}
              <div className="param-grid">
                {selected.params.map((spec) => (
                  <ParamField
                    key={spec.name}
                    spec={spec}
                    value={values[spec.name] ?? spec.default}
                    disabled={isDisabled(spec)}
                    onChange={(next) => setParam(spec.name, next)}
                  />
                ))}
              </div>
              {selected.danger && (
                <label className="ack">
                  <input
                    type="checkbox"
                    checked={ack}
                    onChange={(event) => setAck(event.target.checked)}
                  />
                  {hasDryRun
                    ? '我已知晓该工具可能改动数据；默认仅以 Dry Run 预览，需再勾选「正式执行」参数才会写入'
                    : '我已知晓该工具没有预览模式，确认后会立即写入数据'}
                </label>
              )}
              <div className="form-actions">
                <button className="primary" onClick={submit} disabled={submitting}>
                  {submitting ? '提交中…' : '执行任务'}
                </button>
                <span className="hint">
                  同时最多 {toolsQuery.data?.max_concurrent ?? 2} 个任务并行
                </span>
              </div>
              {error && <div className="panel-error">{error}</div>}
            </div>

            {status?.metrics && status.metrics.length > 0 && (
              <div className="metrics-row">
                {status.metrics.map((metric) => (
                  <div className="metric-card" key={metric.label}>
                    <span>{metric.label}</span>
                    <strong>{metric.value}</strong>
                  </div>
                ))}
              </div>
            )}

            {ARTIFACT_TOOLS.has(selected.id) && <Artifacts />}

            {selected.id === 'r2-clean' && (
              <R2CleanupPreview
                prefix={String(values.prefix ?? 'pdf/')}
                localDir={String(values.local_dir ?? '')}
              />
            )}

            {selected.id === 'warmup-cache' && (
              <div className="aux-panel">
                <div className="panel-title">缩略图缓存覆盖率</div>
                {cacheQuery.isError && (
                  <div className="panel-error">{String(cacheQuery.error)}</div>
                )}
                {cacheQuery.data && (
                  <>
                    <div className="aux-stats">
                      <div>
                        <span>含 PDF 记录</span>
                        <strong>{cacheQuery.data.pdf_records.toLocaleString('zh-CN')}</strong>
                      </div>
                      <div>
                        <span>已预热文档</span>
                        <strong>{cacheQuery.data.warm_units.toLocaleString('zh-CN')}</strong>
                      </div>
                      <div>
                        <span>页图缓存</span>
                        <strong>{cacheQuery.data.page_images.toLocaleString('zh-CN')}</strong>
                      </div>
                      <div>
                        <span>覆盖率</span>
                        <strong>{cacheQuery.data.coverage}%</strong>
                      </div>
                    </div>
                    <div className="coverage-track">
                      <i style={{ width: `${Math.min(cacheQuery.data.coverage, 100)}%` }} />
                    </div>
                    <span className="hint">缓存目录: {cacheQuery.data.cache_dir}</span>
                  </>
                )}
              </div>
            )}

            <div className="job-console">
              <div className="console-head">
                <span className={`job-status ${status?.status ?? 'idle'}`}>
                  {status ? STATUS_LABEL[status.status] ?? status.status : '等待任务'}
                </span>
                <span className="console-title">{status?.title ?? '实时日志'}</span>
                <span className="console-meta">
                  {logs.length} 行 · 退出码 {status?.exit_code ?? '-'}
                </span>
                <button
                  onClick={cancel}
                  disabled={!status || TERMINAL.has(status.status)}
                >
                  取消任务
                </button>
              </div>
              <div ref={logRef} className="console-log">
                {logs.length === 0 ? (
                  <div className="log-placeholder">执行任务后，这里会实时显示子进程输出</div>
                ) : (
                  logs.map((line, index) => (
                    <div key={index} className="log-line">{line}</div>
                  ))
                )}
              </div>
            </div>

            <div className="recent-jobs">
              <div className="panel-title">最近任务</div>
              {recentJobs.length === 0 ? (
                <span className="hint">暂无任务记录</span>
              ) : (
                recentJobs.map((job) => (
                  <button key={job.id} className="recent-item" onClick={() => openJob(job.id)}>
                    <span className={`dot ${job.status}`} />
                    <span>{job.title}</span>
                    <span className="recent-status">
                      {STATUS_LABEL[job.status] ?? job.status}
                    </span>
                    <span className="recent-time">
                      {new Date(job.created_at * 1000).toLocaleTimeString('zh-CN')}
                    </span>
                  </button>
                ))
              )}
            </div>
          </>
        )}
      </section>
    </div>
  )
}
