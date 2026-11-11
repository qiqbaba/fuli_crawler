import { useQuery } from '@tanstack/react-query'
import { api } from './api'

export default function R2CleanupPreview({ prefix, localDir }: { prefix: string; localDir: string }) {
  const query = useQuery({
    queryKey: ['r2-cleanup-preview', prefix, localDir],
    queryFn: () => api.r2CleanupPreview(prefix, localDir),
    enabled: false,
    staleTime: 0,
  })
  const data = query.data

  return (
    <div className="aux-panel">
      <div className="usage-head">
        <div className="panel-title">R2 云端清理预检（只读，不删除任何对象）</div>
        <button onClick={() => query.refetch()} disabled={query.isFetching}>
          {query.isFetching ? '预检中…' : '预检候选对象'}
        </button>
      </div>
      <span className="hint">
        与旧版一致：先核对本地副本情况，再决定是否勾选「连本地无副本的对象一并删除」和「正式删除」。
      </span>
      {query.isError && <div className="panel-error">{String(query.error)}</div>}
      {data && !data.ok && <div className="panel-error">{data.reason}</div>}
      {data?.ok && (
        <>
          <div className="aux-stats">
            <div>
              <span>候选对象（前缀 {data.prefix}）</span>
              <strong>{data.total.toLocaleString('zh-CN')} 个</strong>
            </div>
            <div>
              <span>本地已有副本（可安全清理）</span>
              <strong>{data.have_count.toLocaleString('zh-CN')} 个</strong>
            </div>
            <div>
              <span>本地无副本（唯一数据）</span>
              <strong>{data.missing_count.toLocaleString('zh-CN')} 个 · {data.missing_size_human}</strong>
            </div>
          </div>
          {data.missing_count > 0 ? (
            <>
              <div className="panel-error">
                检测到 {data.missing_count.toLocaleString('zh-CN')} 个本地无副本对象（{data.missing_size_human}），
                默认策略会跳过它们；只有显式勾选高风险选项才会一并删除。
              </div>
              <table className="aux-table">
                <thead><tr><th>对象 Key（前 200 条）</th><th>体积</th></tr></thead>
                <tbody>
                  {data.missing_preview.map((item) => (
                    <tr key={item.key}>
                      <td title={item.key}>{item.key}</td>
                      <td>{item.size_human}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <span className="hint">所有候选对象在本地都有副本，未发现唯一数据。</span>
          )}
          <span className="hint">比对目录：{data.local_dir} · 耗时 {data.elapsed_ms} ms</span>
        </>
      )}
    </div>
  )
}
