import { useMutation, useQuery } from '@tanstack/react-query'
import { api, artifactDownloadUrl } from './api'

export default function Artifacts() {
  const query = useQuery({
    queryKey: ['artifacts'],
    queryFn: api.artifacts,
    staleTime: 30_000,
  })
  const reveal = useMutation({
    mutationFn: (path: string) => api.reveal(path),
  })
  const items = query.data?.artifacts ?? []

  return (
    <div className="aux-panel">
      <div className="usage-head">
        <div className="panel-title">产物文件（备份 / 导出库 / 审计表 / 报告）</div>
        <button onClick={() => query.refetch()} disabled={query.isFetching}>
          {query.isFetching ? '刷新中…' : '刷新清单'}
        </button>
      </div>
      <span className="hint">
        旧版只在日志里打印落盘路径，这里可直接下载或在资源管理器中定位。
        {query.data?.roots.length ? ` 扫描目录：${query.data.roots.join('、')}` : ''}
      </span>
      {query.isError && <div className="panel-error">{String(query.error)}</div>}
      {reveal.isError && <div className="panel-error">{String(reveal.error)}</div>}
      {items.length === 0 ? (
        <span className="hint">暂无产物文件</span>
      ) : (
        <table className="aux-table">
          <thead>
            <tr><th>类型 / 文件</th><th>体积</th><th>修改时间</th><th>操作</th></tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.path}>
                <td title={item.path}>
                  <span className="artifact-kind">{item.kind}</span> {item.name}
                </td>
                <td>{item.size_mb} MB</td>
                <td>{new Date(item.mtime * 1000).toLocaleString('zh-CN')}</td>
                <td className="artifact-ops">
                  <a href={artifactDownloadUrl(item.path)} download={item.name}>下载</a>
                  <button className="mini" onClick={() => reveal.mutate(item.path)}>定位</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
