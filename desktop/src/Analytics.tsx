import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption } from 'echarts'
import { api } from './api'
import './analytics.css'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
])

type Theme = 'dark' | 'light'

interface Palette {
  text: string
  grid: string
  primary: string
  secondary: string
  muted: string
}

function paletteFor(theme: Theme): Palette {
  return theme === 'dark'
    ? { text: '#94a3b8', grid: 'rgba(148,163,184,0.12)', primary: '#38bdf8', secondary: '#818cf8', muted: '#334155' }
    : { text: '#64748b', grid: 'rgba(15,23,42,0.08)', primary: '#0284c7', secondary: '#6366f1', muted: '#cbd5e1' }
}

function Chart({ option, height = 280 }: { option: EChartsOption; height?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const element = ref.current
    if (!element) return
    const chart = echarts.init(element, undefined, { renderer: 'canvas' })
    chart.setOption(option)
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(element)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [option])
  return <div ref={ref} className="chart" style={{ height }} />
}

export default function Analytics({ theme }: { theme: Theme }) {
  const query = useQuery({
    queryKey: ['analytics'],
    queryFn: api.analytics,
    staleTime: 5 * 60_000,
  })
  const data = query.data
  const palette = useMemo(() => paletteFor(theme), [theme])
  const fmt = (value: number) => value.toLocaleString('zh-CN')

  const sourceOption = useMemo<EChartsOption>(() => {
    const rows = (data?.source_data ?? []).slice(0, 12).reverse()
    return {
      backgroundColor: 'transparent',
      grid: { left: 110, right: 24, top: 12, bottom: 28 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: {
        type: 'value',
        axisLabel: { color: palette.text },
        splitLine: { lineStyle: { color: palette.grid } },
      },
      yAxis: {
        type: 'category',
        data: rows.map((row) => row.source),
        axisLabel: { color: palette.text },
      },
      series: [{
        type: 'bar',
        data: rows.map((row) => row.count),
        barMaxWidth: 18,
        itemStyle: { color: palette.primary, borderRadius: [0, 4, 4, 0] },
      }],
    }
  }, [data, palette])

  const categoryOption = useMemo<EChartsOption>(() => {
    const rows = (data?.cat_data ?? []).slice().reverse()
    return {
      backgroundColor: 'transparent',
      grid: { left: 110, right: 24, top: 12, bottom: 28 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: {
        type: 'value',
        axisLabel: { color: palette.text },
        splitLine: { lineStyle: { color: palette.grid } },
      },
      yAxis: {
        type: 'category',
        data: rows.map((row) => row.category),
        axisLabel: { color: palette.text },
      },
      series: [{
        type: 'bar',
        data: rows.map((row) => row.count),
        barMaxWidth: 18,
        itemStyle: { color: palette.secondary, borderRadius: [0, 4, 4, 0] },
      }],
    }
  }, [data, palette])

  const timelineOption = useMemo<EChartsOption>(() => {
    const rows = data?.timeline_data ?? []
    return {
      backgroundColor: 'transparent',
      grid: { left: 56, right: 24, top: 16, bottom: 52 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: rows.map((row) => row.month),
        axisLabel: { color: palette.text, rotate: 45 },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: palette.text },
        splitLine: { lineStyle: { color: palette.grid } },
      },
      series: [{
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: rows.map((row) => row.count),
        lineStyle: { color: palette.primary, width: 2 },
        itemStyle: { color: palette.primary },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: theme === 'dark' ? 'rgba(56,189,248,0.4)' : 'rgba(2,132,199,0.28)' },
              { offset: 1, color: 'rgba(56,189,248,0)' },
            ],
          },
        },
      }],
    }
  }, [data, palette, theme])

  const pieOption = useMemo<EChartsOption>(() => {
    const total = data?.total_records ?? 0
    const archived = data?.has_pdf_records ?? 0
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      legend: { bottom: 0, textStyle: { color: palette.text } },
      series: [{
        type: 'pie',
        radius: ['48%', '72%'],
        center: ['50%', '44%'],
        label: { color: palette.text },
        data: [
          { name: '已归档本地快照', value: archived, itemStyle: { color: palette.primary } },
          { name: '仅线上详情链接', value: Math.max(0, total - archived), itemStyle: { color: palette.muted } },
        ],
      }],
    }
  }, [data, palette])

  if (query.isError) {
    return <div className="analytics"><div className="panel-error">{String(query.error)}</div></div>
  }
  if (query.isLoading) {
    return <div className="analytics"><div className="empty">正在加载统计数据…</div></div>
  }

  return (
    <div className="analytics">
      <div className="kpi-grid">
        <div className="kpi">
          <span>全网资产总记录</span>
          <strong>{fmt(data?.total_records ?? 0)}</strong>
          <small>本地 SQLite 实时索引</small>
        </div>
        <div className="kpi">
          <span>PDF 镜像归档量</span>
          <strong>{fmt(data?.has_pdf_records ?? 0)}</strong>
          <div className="kpi-bar"><i style={{ width: `${Math.min(data?.pdf_rate ?? 0, 100)}%` }} /></div>
          <small>归档覆盖率 {data?.pdf_rate ?? 0}%</small>
        </div>
        <div className="kpi">
          <span>数据库体积</span>
          <strong>{data?.db_size_mb ?? 0} MB</strong>
          <small>SQLite 本地存储引擎</small>
        </div>
        <div className="kpi">
          <span>渠道 / 内容分类</span>
          <strong>{data?.sources_count ?? 0} / {data?.categories_count ?? 0}</strong>
          <small>已收录的数据维度</small>
        </div>
      </div>

      <div className="chart-grid">
        <section className="chart-card">
          <h3>渠道资产容量分布 (Top Sources)</h3>
          <Chart option={sourceOption} />
        </section>
        <section className="chart-card">
          <h3>核心内容形态与大类分布 (Categories)</h3>
          <Chart option={categoryOption} />
        </section>
        <section className="chart-card">
          <h3>月度数据采集与沉淀趋势 (Monthly Timeline)</h3>
          <Chart option={timelineOption} />
        </section>
        <section className="chart-card">
          <h3>PDF 资产镜像状态对比 (Archival State)</h3>
          <Chart option={pieOption} />
        </section>
      </div>

      <section className="chart-card matrix-card">
        <h3>全渠道数据资产明细矩阵 (Channel Matrix)</h3>
        <table className="matrix-table">
          <thead>
            <tr><th>来源渠道</th><th>总记录</th><th>PDF 归档</th><th>归档覆盖率</th></tr>
          </thead>
          <tbody>
            {(data?.matrix_data ?? []).map((row) => (
              <tr key={row.source}>
                <td>{row.source}</td>
                <td>{fmt(row.total)}</td>
                <td>{fmt(row.pdf_count)}</td>
                <td>
                  <div className="rate-cell">
                    <div className="rate-track">
                      <i style={{ width: `${Math.min(row.rate, 100)}%` }} />
                    </div>
                    <span>{row.rate}%</span>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
