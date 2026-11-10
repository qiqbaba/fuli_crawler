import { lazy, Suspense, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import Gallery from './Gallery'
import JobsPanel from './Jobs'
import './panels.css'

const Analytics = lazy(() => import('./Analytics'))

type Theme = 'dark' | 'light'
type Tab = 'gallery' | 'maintenance' | 'sync' | 'analytics'

function useTheme() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem('fuli-theme') as Theme | null) ?? 'dark',
  )
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('fuli-theme', theme)
  }, [theme])
  return [theme, setTheme] as const
}

function basename(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '')
  return normalized.slice(normalized.lastIndexOf('/') + 1)
}

const TABS: Array<{ key: Tab; label: string }> = [
  { key: 'gallery', label: '画廊浏览' },
  { key: 'maintenance', label: '批量管理' },
  { key: 'sync', label: '云端同步' },
  { key: 'analytics', label: '统计大盘' },
]

export default function App() {
  const [theme, setTheme] = useTheme()
  const [tab, setTab] = useState<Tab>('gallery')
  const statsQuery = useQuery({ queryKey: ['stats'], queryFn: api.stats })
  const formatNumber = (value: number) => value.toLocaleString('zh-CN')

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">◈</span>
          <div>
            <h1>资源库工作台</h1>
            <p
              title={statsQuery.data
                ? `${statsQuery.data.db_path}\n${statsQuery.data.pdf_dir}`
                : undefined}
            >
              {statsQuery.data
                ? `库 ${basename(statsQuery.data.db_path)} · PDF ${basename(statsQuery.data.pdf_dir)} · ${formatNumber(statsQuery.data.total)} 条`
                : '本地数据资产检索、PDF 快照预览与运维任务'}
            </p>
          </div>
        </div>

        <div className="stat-strip">
          <div className="stat">
            <span className="stat-value">{formatNumber(statsQuery.data?.total ?? 0)}</span>
            <span className="stat-label">总记录</span>
          </div>
          <div className="stat">
            <span className="stat-value">{formatNumber(statsQuery.data?.has_pdf_record ?? 0)}</span>
            <span className="stat-label">关联 PDF</span>
          </div>
          <div className="stat">
            <span className="stat-value">{statsQuery.data?.sources.length ?? 0}</span>
            <span className="stat-label">来源渠道</span>
          </div>
          <div className="stat">
            <span className="stat-value">{statsQuery.data?.categories.length ?? 0}</span>
            <span className="stat-label">内容分类</span>
          </div>
        </div>

        <div className="theme-switch">
          <button
            className={theme === 'dark' ? 'active' : ''}
            onClick={() => setTheme('dark')}
          >
            深色
          </button>
          <button
            className={theme === 'light' ? 'active' : ''}
            onClick={() => setTheme('light')}
          >
            浅色
          </button>
        </div>
      </header>

      <nav className="tabs">
        {TABS.map((item) => (
          <button
            key={item.key}
            className={tab === item.key ? 'active' : ''}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {tab === 'gallery' ? (
        <Gallery />
      ) : tab === 'analytics' ? (
        <Suspense fallback={<div className="empty">正在加载统计大盘…</div>}>
          <Analytics theme={theme} />
        </Suspense>
      ) : (
        <JobsPanel group={tab} />
      )}
    </div>
  )
}
