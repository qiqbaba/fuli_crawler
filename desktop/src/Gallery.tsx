import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useVirtualizer } from '@tanstack/react-virtual'
import {
  api,
  pdfFileUrl,
  type FilterOptionsParams,
  type RecordItem,
  type RecordsParams,
} from './api'

type Layout = 'double' | 'single' | 'triple' | 'table'
type PathMode = 'name' | 'relative' | 'absolute'

const LAYOUT_OPTIONS: Array<{ value: Layout; label: string }> = [
  { value: 'double', label: '双列画廊' },
  { value: 'single', label: '单列大图' },
  { value: 'triple', label: '三列紧凑' },
  { value: 'table', label: '表格视图' },
]

const PATH_MODE_OPTIONS: Array<{ value: PathMode; label: string }> = [
  { value: 'name', label: '仅文件名' },
  { value: 'relative', label: '相对路径' },
  { value: 'absolute', label: '绝对路径' },
]

const COLUMN_COUNT: Record<Exclude<Layout, 'table'>, number> = {
  single: 1,
  double: 2,
  triple: 3,
}

// 行高由内容（PDF 预览区按 A4 比例自适应）决定，这里只作为虚拟列表的首次估算值
const ROW_ESTIMATE: Record<Exclude<Layout, 'table'>, number> = {
  single: 1400,
  double: 1000,
  triple: 820,
}

function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return debounced
}

function useStored<T extends string>(key: string, fallback: T, allowed: readonly T[]) {
  const [value, setValue] = useState<T>(() => {
    const stored = localStorage.getItem(key) as T | null
    return stored && allowed.includes(stored) ? stored : fallback
  })
  useEffect(() => {
    localStorage.setItem(key, value)
  }, [key, value])
  return [value, setValue] as const
}

function safeHref(value: string): string | undefined {
  const url = value.trim()
  return /^(https?:\/\/|magnet:)/i.test(url) ? url : undefined
}

function basename(path: string): string {
  const normalized = path.replace(/\\/g, '/')
  return normalized.slice(normalized.lastIndexOf('/') + 1)
}

function displayPath(item: RecordItem, mode: PathMode): string {
  if (item.resolved_path) {
    if (mode === 'absolute') return item.resolved_path
    if (mode === 'name') return basename(item.resolved_path)
    return item.pdf_path || basename(item.resolved_path)
  }
  if (!item.pdf_path) return '-'
  return mode === 'name' ? basename(item.pdf_path) : item.pdf_path
}

function shortLink(value: string): string {
  if (!value) return '-'
  if (value.startsWith('magnet:?')) {
    const match = /xt=urn:btih:([a-zA-Z0-9]+)/i.exec(value)
    if (match) return `magnet:?xt=urn:btih:${match[1].slice(0, 8)}…${match[1].slice(-6)}`
  }
  return value.length > 50 ? `${value.slice(0, 45)}…` : value
}

// 与 /api/pdf-page 的 page 上限一致：按需滚动加载到文档末页，不再截断为前 30 页
const MAX_PREVIEW_PAGES = 1000
const LOAD_AHEAD = 240

function pageUrl(rawPath: string, page: number): string {
  // DPI / 质量与「PDF 封面缓存预热」默认参数保持一致，才能命中已预热的页图
  const params = new URLSearchParams({
    path: rawPath,
    page: String(page),
    dpi: '105',
    quality: '75',
  })
  return `/api/pdf-page?${params.toString()}`
}

/**
 * 卡片内 PDF 预览：按宽度铺满，支持在预览区内上下滑动逐页浏览整份 PDF。
 * 越界页面由 /api/pdf-page 返回 404 触发 onError，据此判定文档结束。
 */
function PdfPreview({ path }: { path: string }) {
  const [pageCount, setPageCount] = useState(1)
  const [ended, setEnded] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const handleScroll = () => {
    const element = scrollRef.current
    if (!element || ended) return
    const distanceToBottom = element.scrollHeight - element.scrollTop - element.clientHeight
    if (distanceToBottom > LOAD_AHEAD) return
    if (pageCount >= MAX_PREVIEW_PAGES) {
      setEnded(true)
      return
    }
    setPageCount(pageCount + 1)
  }

  if (pageCount === 0) {
    // 页图光栅化失败时回退到浏览器内置 PDF 查看器（对齐旧版 base64 iframe 兜底）
    return (
      <div className="card-preview">
        <iframe
          src={`${pdfFileUrl(path)}#toolbar=0&navpanes=0`}
          title="PDF 原文预览"
          loading="lazy"
        />
      </div>
    )
  }

  return (
    <div className="card-preview" ref={scrollRef} onScroll={handleScroll}>
      {Array.from({ length: pageCount }, (_, index) => (
        <img
          key={index}
          src={pageUrl(path, index)}
          alt={`PDF 第 ${index + 1} 页`}
          loading={index === 0 ? 'eager' : 'lazy'}
          onError={() => {
            // 单页渲染失败（损坏页/临时网络错误）只暂停自动加载，稍后可继续下滑重试
            setEnded(true)
            window.setTimeout(() => setEnded(false), 3000)
          }}
        />
      ))}
      {!ended && <div className="preview-hint">继续下滑加载更多页</div>}
    </div>
  )
}

interface CardProps {
  item: RecordItem
  pathMode: PathMode
  confirming: boolean
  deleting: boolean
  onAskDelete: (id: RecordItem['id']) => void
  onCancelDelete: () => void
  onConfirmDelete: (item: RecordItem) => void
  onReveal: (item: RecordItem) => void
}

function Card({
  item,
  pathMode,
  confirming,
  deleting,
  onAskDelete,
  onCancelDelete,
  onConfirmDelete,
  onReveal,
}: CardProps) {
  const links: Array<{ label: string; href: string }> = []
  for (const [label, value] of [
    ['原网页', item.url],
    ['资源链接', item.resource_link],
    ['PikPak', item.pikpak_link],
  ] as const) {
    const href = safeHref(value)
    if (href) links.push({ label, href })
  }
  const isOrphan = String(item.id).startsWith('ORPHAN-')
  const refsQuery = useQuery({
    queryKey: ['pdf-refs', String(item.id), item.pdf_path],
    queryFn: () => api.pdfRefs(item.id, item.pdf_path),
    enabled: confirming,
    staleTime: 60_000,
  })
  const otherRefs = refsQuery.data?.other_refs ?? 0

  return (
    <article className="card">
      <div className="card-head">
        <span className={`badge ${isOrphan ? 'badge-orphan' : 'badge-id'}`}>
          {isOrphan ? `孤儿 ${String(item.id).slice(7)}` : `#${item.id}`}
        </span>
        <h3 className="card-title" title={item.title}>
          {item.title || '（无标题）'}
        </h3>
      </div>

      {item.has_pdf ? (
        <PdfPreview key={item.pdf_path} path={item.pdf_path} />
      ) : (
        <div className="card-preview">
          <div className="preview-empty">
            <span>{item.pdf_path ? '本地 PDF 文件缺失' : '暂无本地 PDF 快照'}</span>
            <small>{item.pdf_status}</small>
          </div>
        </div>
      )}

      <div className="card-meta">
        <span className="badge badge-source">{item.source || '未知'}</span>
        {item.category && <span className="badge">{item.category}</span>}
        {item.format && <span className="badge">{item.format}</span>}
        {item.dup_cnt > 1 && (
          <span className="badge badge-dup" title={`包含 ${item.dup_cnt} 条相同重复记录`}>
            重复 ({item.dup_cnt}条)
          </span>
        )}
        <span className="meta-text">{item.size || '-'}</span>
        <span className="meta-text">{item.publish_time || '-'}</span>
      </div>

      <div className="card-footer">
        <div className="card-line" title={item.resolved_path || item.pdf_path || '-'}>
          <span className="card-line-label">文件:</span>
          <span className="card-line-value">{displayPath(item, pathMode)}</span>
        </div>
        <div className="card-line" title={item.resource_link || '-'}>
          <span className="card-line-label">链接:</span>
          {safeHref(item.resource_link) ? (
            <a
              className="card-line-value"
              href={item.resource_link}
              target="_blank"
              rel="noreferrer"
            >
              {shortLink(item.resource_link)}
            </a>
          ) : (
            <span className="card-line-value">{shortLink(item.resource_link)}</span>
          )}
        </div>
      </div>

      {confirming ? (
        <div className="card-confirm">
          <span>
            确认删除&nbsp;{isOrphan ? '该磁盘孤儿文件' : `记录 #${item.id}`}？
            {isOrphan
              ? ' 将永久删除本地 PDF 文件。'
              : otherRefs > 0
                ? ` 本地 PDF 仍被其他 ${otherRefs} 条记录共享引用，确认后仅删除本条数据库记录，物理文件将安全保留。`
                : item.has_pdf
                  ? ' 将永久删除本条数据库记录及对应的本地 PDF 文件（无其他引用），此操作不可撤销！'
                  : ' 将删除本条数据库记录（无关联本地 PDF），此操作不可撤销！'}
          </span>
          <div className="confirm-actions">
            <button className="danger" disabled={deleting} onClick={() => onConfirmDelete(item)}>
              {deleting ? '删除中…' : '确认删除'}
            </button>
            <button disabled={deleting} onClick={onCancelDelete}>
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="card-actions">
          {links.map((link) => (
            <a key={link.label} href={link.href} target="_blank" rel="noreferrer">
              {link.label}
            </a>
          ))}
          <button
            className="mini"
            disabled={!item.has_pdf && !item.resolved_path}
            onClick={() => onReveal(item)}
          >
            本地定位
          </button>
          <button className="mini danger" onClick={() => onAskDelete(item.id)}>
            删除
          </button>
          <span className={`pdf-state ${item.has_pdf ? 'ok' : 'missing'}`}>
            {item.pdf_status}
          </span>
        </div>
      )}
    </article>
  )
}

interface TableProps {
  items: RecordItem[]
  pathMode: PathMode
  deletingId: string
  confirmId: string | null
  onAskDelete: (id: RecordItem['id']) => void
  onCancelDelete: () => void
  onDelete: (item: RecordItem) => void
  onReveal: (item: RecordItem) => void
}

function TableDeleteConfirm({
  item, deleting, onConfirm, onCancel,
}: {
  item: RecordItem
  deleting: boolean
  onConfirm: (item: RecordItem) => void
  onCancel: () => void
}) {
  const refsQuery = useQuery({
    queryKey: ['pdf-refs', String(item.id), item.pdf_path],
    queryFn: () => api.pdfRefs(item.id, item.pdf_path),
    staleTime: 60_000,
  })
  const otherRefs = refsQuery.data?.other_refs ?? 0
  const hint = otherRefs > 0
    ? `本地 PDF 仍被其他 ${otherRefs} 条记录共享引用，确认后仅删除本条数据库记录，物理文件安全保留`
    : item.has_pdf
      ? '将永久删除本条记录及对应的本地 PDF 文件，此操作不可撤销'
      : '将删除本条数据库记录，此操作不可撤销'
  const label = otherRefs > 0 ? '仅删记录' : item.has_pdf ? '删记录+PDF' : '确认删除'
  return (
    <>
      <button
        className="mini danger"
        title={hint}
        disabled={deleting}
        onClick={() => onConfirm(item)}
      >
        {deleting ? '删除中…' : label}
      </button>
      <button className="mini" onClick={onCancel}>取消</button>
    </>
  )
}

function RecordTable({
  items, pathMode, deletingId, confirmId, onAskDelete, onCancelDelete, onDelete, onReveal,
}: TableProps) {
  return (
    <div className="table-scroll">
      <table className="record-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>查重</th>
            <th>标题</th>
            <th>来源</th>
            <th>分类</th>
            <th>大小</th>
            <th>PDF</th>
            <th>资源链接</th>
            <th>文件</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const link = safeHref(item.url)
            return (
              <tr key={item.id}>
                <td className="num">{String(item.id)}</td>
                <td>{item.dup_cnt > 1 ? `重复 (${item.dup_cnt}条)` : '唯一'}</td>
                <td className="title" title={item.title}>{item.title || '（无标题）'}</td>
                <td>{item.source || '-'}</td>
                <td>{item.category || '-'}</td>
                <td>{item.size || '-'}</td>
                <td className={item.has_pdf ? 'ok' : 'missing'}>{item.pdf_status}</td>
                <td className="path" title={item.resource_link || '-'}>
                  {safeHref(item.resource_link) ? (
                    <a href={item.resource_link} target="_blank" rel="noreferrer">
                      {shortLink(item.resource_link)}
                    </a>
                  ) : (
                    shortLink(item.resource_link)
                  )}
                </td>
                <td className="path" title={item.resolved_path || item.pdf_path}>
                  {displayPath(item, pathMode)}
                </td>
                <td className="ops">
                  {link && (
                    <a href={link} target="_blank" rel="noreferrer">打开</a>
                  )}
                  <button
                    className="mini"
                    disabled={!item.has_pdf && !item.resolved_path}
                    onClick={() => onReveal(item)}
                  >
                    定位
                  </button>
                  {confirmId === String(item.id) ? (
                    <TableDeleteConfirm
                      item={item}
                      deleting={deletingId === String(item.id)}
                      onConfirm={onDelete}
                      onCancel={onCancelDelete}
                    />
                  ) : (
                    <button className="mini danger" onClick={() => onAskDelete(item.id)}>
                      删除
                    </button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function Gallery() {
  const [keyword, setKeyword] = useState('')
  const debouncedKeyword = useDebounced(keyword)
  const [source, setSource] = useState('全部')
  const [category, setCategory] = useState('全部')
  const [pdfFilter, setPdfFilter] = useState('全部')
  const [fixFilter, setFixFilter] = useState('全部')
  const [sortBy, setSortBy] = useState('最新入库 (ID)')
  const [pageSize, setPageSize] = useState(20)
  const [page, setPage] = useState(1)
  const [jump, setJump] = useState('1')
  const [layout, setLayout] = useStored<Layout>('fuli-layout', 'double', [
    'double', 'single', 'triple', 'table',
  ])
  const [pathMode, setPathMode] = useStored<PathMode>('fuli-path-mode', 'name', [
    'name', 'relative', 'absolute',
  ])
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [toast, setToast] = useState('')
  const parentRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  const filterParams: FilterOptionsParams = {
    keyword: debouncedKeyword,
    source,
    category,
    pdf_filter: pdfFilter,
    fix_filter: fixFilter,
  }
  const optionsQuery = useQuery({
    queryKey: ['filter-options', filterParams],
    queryFn: () => api.filterOptions(filterParams),
    staleTime: 5 * 60_000,
    placeholderData: (previous) => previous,
  })
  const options = optionsQuery.data

  useEffect(() => {
    if (!options) return
    if (!options.sources.includes(source)) setSource('全部')
    if (!options.categories.includes(category)) setCategory('全部')
    if (!options.pdf_filters.includes(pdfFilter)) setPdfFilter('全部')
    if (!options.fix_filters.includes(fixFilter)) setFixFilter('全部')
    if (!options.sorts.includes(sortBy)) setSortBy(options.sorts[0] ?? '最新入库 (ID)')
  }, [options, source, category, pdfFilter, fixFilter, sortBy])

  const params: RecordsParams = {
    ...filterParams,
    sort_by: sortBy,
    page,
    page_size: pageSize,
  }
  const recordsQuery = useQuery({
    queryKey: ['records', params],
    queryFn: () => api.records(params),
    placeholderData: (previous) => previous,
  })

  const invalidateRecords = () => {
    queryClient.invalidateQueries({ queryKey: ['records'] })
    queryClient.invalidateQueries({ queryKey: ['stats'] })
    queryClient.invalidateQueries({ queryKey: ['analytics'] })
    queryClient.invalidateQueries({ queryKey: ['filter-options'] })
  }

  const deleteMutation = useMutation({
    mutationFn: (item: RecordItem) => api.deleteRecord(item.id, item.pdf_path),
    onSuccess: (result) => {
      setConfirmId(null)
      setToast(result.message || `#${result.record_id} 已删除`)
      invalidateRecords()
    },
    onError: (error) => setToast(String(error)),
  })

  const revealMutation = useMutation({
    mutationFn: (item: RecordItem) => api.reveal(item.resolved_path || item.pdf_path),
    onError: (error) => setToast(String(error)),
  })

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 4000)
    return () => window.clearTimeout(timer)
  }, [toast])

  const items = recordsQuery.data?.items ?? []
  const total = recordsQuery.data?.total ?? 0
  const totalPages = recordsQuery.data?.total_pages ?? 1
  const columns = layout === 'table' ? 1 : COLUMN_COUNT[layout]
  const rowCount = layout === 'table' ? 0 : Math.ceil(items.length / columns)
  const virtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => parentRef.current,
    estimateSize: () => (layout === 'table' ? 1000 : ROW_ESTIMATE[layout]),
    overscan: 3,
  })

  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages])

  useEffect(() => {
    setPage(1)
  }, [debouncedKeyword, source, category, pdfFilter, fixFilter, sortBy, pageSize])

  useEffect(() => {
    setJump(String(page))
    parentRef.current?.scrollTo({ top: 0 })
    // 数据变化后行高需重新测量（不同 PDF 页数/宽度下卡片高度不同）
    virtualizer.measure()
    virtualizer.scrollToOffset(0)
  }, [page, layout, debouncedKeyword, source, category, pdfFilter, fixFilter, sortBy, pageSize])

  const formatNumber = (value: number) => value.toLocaleString('zh-CN')
  const deletingId = deleteMutation.isPending ? String(deleteMutation.variables?.id ?? '') : ''

  const goToPage = (target: number) => {
    const next = Math.min(Math.max(1, Math.round(target) || 1), totalPages)
    setPage(next)
  }

  const askDelete = (id: RecordItem['id']) => setConfirmId(String(id))
  const confirmDelete = (item: RecordItem) => deleteMutation.mutate(item)

  const scrollArea = (
    <div ref={parentRef} className="gallery-scroll">
      {items.length === 0 && !recordsQuery.isFetching ? (
        <div className="empty">没有匹配的记录，试试调整筛选条件</div>
      ) : layout === 'table' ? (
        <RecordTable
          items={items}
          pathMode={pathMode}
          deletingId={deletingId}
          confirmId={confirmId}
          onAskDelete={askDelete}
          onCancelDelete={() => setConfirmId(null)}
          onDelete={confirmDelete}
          onReveal={(item) => revealMutation.mutate(item)}
        />
      ) : (
        <div
          key={layout}
          className="virtual-canvas"
          style={{ height: virtualizer.getTotalSize() }}
        >
          {virtualizer.getVirtualItems().map((row) => (
            <div
              key={row.key}
              data-index={row.index}
              ref={virtualizer.measureElement}
              className="gallery-row"
              style={{
                transform: `translateY(${row.start}px)`,
                gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
              }}
            >
              {items
                .slice(row.index * columns, row.index * columns + columns)
                .map((item) => (
                  <Card
                    key={item.id}
                    item={item}
                    pathMode={pathMode}
                    confirming={confirmId === String(item.id)}
                    deleting={deletingId === String(item.id)}
                    onAskDelete={askDelete}
                    onCancelDelete={() => setConfirmId(null)}
                    onConfirmDelete={confirmDelete}
                    onReveal={(target) => revealMutation.mutate(target)}
                  />
                ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )

  return (
    <>
      <div className="toolbar">
        <input
          className="kw"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder="搜索标题 / 链接 / 磁力哈希..."
        />
        <select value={source} onChange={(event) => setSource(event.target.value)}>
          {(options?.sources ?? ['全部']).map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
        <select value={category} onChange={(event) => setCategory(event.target.value)}>
          {(options?.categories ?? ['全部']).map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
        <select value={pdfFilter} onChange={(event) => setPdfFilter(event.target.value)}>
          {(options?.pdf_filters ?? ['全部']).map((value) => (
            <option key={value} value={value}>PDF: {value}</option>
          ))}
        </select>
        <select value={fixFilter} onChange={(event) => setFixFilter(event.target.value)}>
          {(options?.fix_filters ?? ['全部']).map((value) => (
            <option key={value} value={value}>质检: {value}</option>
          ))}
        </select>
        <select value={sortBy} onChange={(event) => setSortBy(event.target.value)}>
          {(options?.sorts ?? ['最新入库 (ID)']).map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
        <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}>
          {(options?.page_sizes ?? [10, 20, 30, 40, 50]).map((value) => (
            <option key={value} value={value}>{value} 条/页</option>
          ))}
        </select>
      </div>

      <div className="viewbar">
        <div className="view-control">
          <span>排版</span>
          <select value={layout} onChange={(event) => setLayout(event.target.value as Layout)}>
            {LAYOUT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div className="view-control">
          <span>路径</span>
          <select
            value={pathMode}
            onChange={(event) => setPathMode(event.target.value as PathMode)}
          >
            {PATH_MODE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        {pdfFilter === '数据库中没有的 PDF (磁盘孤儿)' && options && (
          <span>磁盘孤儿 <strong>{formatNumber(options.orphan_count)}</strong> 个</span>
        )}
        <span className="viewbar-note">
          检索匹配 <strong>{formatNumber(total)}</strong> 条 · 第 {page} / {totalPages} 页
        </span>
        {recordsQuery.isFetching && <span className="loading">加载中…</span>}
        {recordsQuery.isError && <span className="error">{String(recordsQuery.error)}</span>}
      </div>

      {scrollArea}

      <footer className="pagination">
        <span>共 {formatNumber(total)} 条</span>
        <select
          value={pageSize}
          onChange={(event) => setPageSize(Number(event.target.value))}
          title="每页条数（与顶部联动）"
        >
          {(options?.page_sizes ?? [10, 20, 30, 40, 50]).map((value) => (
            <option key={value} value={value}>{value} 条/页</option>
          ))}
        </select>
        <button disabled={page <= 1} onClick={() => setPage(page - 1)}>
          ‹ 上一页
        </button>
        <span>第 {page} / {totalPages} 页</span>
        <input
          className="jump"
          type="number"
          min={1}
          max={totalPages}
          value={jump}
          onChange={(event) => setJump(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') goToPage(Number(jump))
            if (event.key === 'Escape') setJump(String(page))
          }}
          onBlur={() => goToPage(Number(jump))}
          title="输入页码后回车跳转"
        />
        <button disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
          下一页 ›
        </button>
      </footer>

      {toast && <div className="toast">{toast}</div>}
    </>
  )
}
