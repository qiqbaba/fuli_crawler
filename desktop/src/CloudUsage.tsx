import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, type CloudUsageData, type CloudUsageParams } from './api'

const YEARS = ['', '2026', '2025', '2024', '2023', '2022', '2021']

export default function CloudUsage() {
  const [params, setParams] = useState<CloudUsageParams>({
    supabase: true, r2: true, dynamodb: true, year: '',
  })
  const [data, setData] = useState<CloudUsageData | null>(null)
  const query = useMutation({
    mutationFn: () => api.cloudUsage(params),
    onMutate: () => setData(null),
    onSuccess: setData,
  })

  const toggle = (key: 'supabase' | 'r2' | 'dynamodb') =>
    setParams((previous) => ({ ...previous, [key]: !previous[key] }))

  return (
    <div className="aux-panel usage-panel">
      <div className="usage-head">
        <div className="panel-title">多云用量监控（只读）</div>
        <div className="usage-controls">
          <label><input type="checkbox" checked={params.supabase} onChange={() => toggle('supabase')} /> Supabase</label>
          <label><input type="checkbox" checked={params.r2} onChange={() => toggle('r2')} /> R2</label>
          <label><input type="checkbox" checked={params.dynamodb} onChange={() => toggle('dynamodb')} /> DynamoDB</label>
          <select
            value={params.year}
            onChange={(event) => setParams((previous) => ({ ...previous, year: event.target.value }))}
            title="R2 统计年份"
          >
            {YEARS.map((year) => (
              <option key={year} value={year}>{year ? `${year} 年` : '全部年份'}</option>
            ))}
          </select>
          <button
            className="primary"
            disabled={query.isPending || !params.supabase && !params.r2 && !params.dynamodb}
            onClick={() => query.mutate()}
          >
            {query.isPending ? '查询中…' : '立即查询'}
          </button>
        </div>
      </div>

      <span className="hint">
        只读查询云端配额水位，不写入任何云端数据；R2 全量扫描可能耗时较久。
        {data && ` 上次更新 ${data.generated_at} · 耗时 ${data.elapsed_ms} ms`}
      </span>

      {query.isError && <div className="panel-error">{String(query.error)}</div>}
      {data && data.errors.length > 0 && (
        <div className="panel-error">{data.errors.join('；')}</div>
      )}

      {data && (
        <>
          <div className="aux-stats">
            <div>
              <span>Supabase 记录 / 配额 500 MB</span>
              <strong>{data.supabase.ok ? `${(data.supabase.record_count ?? 0).toLocaleString('zh-CN')} 条 · ${data.supabase.size_human ?? '-'}` : '不可用'}</strong>
            </div>
            <div>
              <span>R2 PDF / 配额 10 GB</span>
              <strong>{data.r2.ok ? `${(data.r2.file_count ?? 0).toLocaleString('zh-CN')} 个 · ${data.r2.size_human ?? '-'}` : '不可用'}</strong>
            </div>
            <div>
              <span>DynamoDB 条目 / 配额 25 GB</span>
              <strong>{data.dynamodb.ok ? `${(data.dynamodb.item_count ?? 0).toLocaleString('zh-CN')} 条 · ${data.dynamodb.size_human ?? '-'}` : '不可用'}</strong>
            </div>
          </div>

          <div className="usage-tables">
            <div>
              <div className="panel-title">R2 年份分布</div>
              {data.r2.years.length === 0 ? (
                <span className="hint">无数据</span>
              ) : (
                <table className="aux-table">
                  <thead><tr><th>年份</th><th>文件数</th><th>占用</th></tr></thead>
                  <tbody>
                    {data.r2.years.map((row) => (
                      <tr key={row.year}>
                        <td>{row.year}</td>
                        <td>{row.count.toLocaleString('zh-CN')}</td>
                        <td>{row.size_human}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div>
              <div className="panel-title">Supabase 来源分布</div>
              {data.supabase.sources.length === 0 ? (
                <span className="hint">无数据</span>
              ) : (
                <table className="aux-table">
                  <thead><tr><th>来源</th><th>记录数</th></tr></thead>
                  <tbody>
                    {data.supabase.sources.slice(0, 12).map((row) => (
                      <tr key={row.source}>
                        <td>{row.source}</td>
                        <td>{row.count.toLocaleString('zh-CN')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
