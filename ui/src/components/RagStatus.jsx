import { useEffect, useState } from 'react'
import { API_BASE } from './SharedComponents'

const PHASE_LABELS = {
  disabled: '未启用',
  loading: '正在加载模型与索引',
  preparing: '准备中',
  indexing: '正在建立索引',
  ready: '索引就绪',
  error: '索引异常',
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  if (minutes >= 60) return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
  return minutes ? `${minutes} 分 ${rest} 秒` : `${rest} 秒`
}

/** RAG 进度卡片：每 2 秒轮询，只在启用时挂载。 */
export default function RagStatus({ enabled }) {
  const [status, setStatus] = useState(null)

  useEffect(() => {
    if (!enabled) {
      setStatus(null)
      return undefined
    }
    let cancelled = false
    let timer = null

    async function poll() {
      try {
        const res = await fetch(`${API_BASE}/api/rag/status`, { cache: 'no-store' })
        const data = await res.json()
        if (!cancelled) setStatus(data)
      } catch {
        // 网络瞬时失败保持上一次状态，不清空进度
      } finally {
        if (!cancelled) timer = setTimeout(poll, 2000)
      }
    }

    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [enabled])

  if (!enabled || !status) return null

  const jobs = Object.values(status.jobs || {}).filter(job => job.total > 0 || job.completed > 0)
  const phase = PHASE_LABELS[status.phase] || status.phase

  return (
    <div className="mt-3 rounded-xl border border-border-main bg-bg-raised/50 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium text-text-main">{phase}</span>
        <span className="text-text-muted">设备：CPU · 每批 {status.batch_size || '-'} 条</span>
        {status.elapsed > 0 && (
          <span className="text-text-muted">已用 {formatDuration(status.elapsed)}</span>
        )}
      </div>

      {status.error && <p className="mt-1 text-status-error">{status.error}</p>}

      {jobs.map(job => (
        <div key={job.label} className="mt-2">
          <div className="flex items-center justify-between text-text-muted">
            <span>{job.label}</span>
            <span>{job.completed.toLocaleString()}/{job.total.toLocaleString()}（{job.percent}%）</span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-bg-main">
            <div
              className="h-full rounded-full bg-brand-green transition-all duration-500"
              style={{ width: `${Math.min(100, job.percent)}%` }}
            />
          </div>
        </div>
      ))}

      <p className="mt-2 leading-relaxed text-text-muted">
        首次建立索引可能较久，期间可继续使用其他功能；重启会从已保存的进度继续。
      </p>
    </div>
  )
}
