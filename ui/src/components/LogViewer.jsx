import { useState, useEffect, useRef, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ArrowDown, Trash, MagnifyingGlass, CaretRight, CaretDown } from '@phosphor-icons/react'
import { API_BASE } from './SharedComponents'

const LEVEL_STYLES = {
  DEBUG:   { color: '#888888', bg: 'rgba(136, 136, 136, 0.12)' },
  INFO:    { color: 'var(--status-info)', bg: 'var(--status-info-soft)' },
  WARNING: { color: 'var(--status-warn)', bg: 'var(--status-warn-soft)' },
  ERROR:   { color: 'var(--status-error)', bg: 'var(--status-error-soft)' },
}
const LEVEL_LABELS = { ALL: '全部', LLM: 'LLM', VOICE: '语音', OP: '操作', AGENT: 'Agent', INFO: 'INFO', WARNING: '警告', ERROR: '错误', DEBUG: 'DEBUG' }
const FILTER_OPTIONS = ['ALL', 'OP', 'LLM', 'VOICE', 'AGENT', 'INFO', 'WARNING', 'ERROR', 'DEBUG']

// Operation tags that appear in [TAG] format — used for filtering and highlighting
// 注意：VOICE-* 标签不属于“操作”分类（只在「全部/语音/级别」过滤中呈现），
// 其着色由下方 TAG_STYLES 单独提供，故不放进 OP_TAGS。
const OP_TAGS = [
  'BOOT', 'KEY-EXTRACT', 'DB', 'HOOK',
  'MSG-POLL', 'MSG-RECV', 'MSG-DEDUP', 'SEND', 'SEND-FAIL',
  'LLM', 'AI-CHAT',
  'API', 'EXPORT',
  'ALERT', 'DIGEST',
  'WND',
  'Agent', 'RAG',
]
const OP_TAG_REGEX = new RegExp(`^\\[(${OP_TAGS.join('|')})\\]`)

// Tag-specific styling
const TAG_STYLES = {
  'BOOT':        { color: 'var(--brand-green)', bg: 'rgba(24, 226, 153, 0.12)' },
  'KEY-EXTRACT': { color: '#f59e0b', bg: 'rgba(245, 158, 11, 0.12)' },
  'HOOK':        { color: '#f59e0b', bg: 'rgba(245, 158, 11, 0.12)' },
  'DB':          { color: '#3b82f6', bg: 'rgba(59, 130, 246, 0.12)' },
  'MSG-POLL':    { color: '#8b5cf6', bg: 'rgba(139, 92, 246, 0.12)' },
  'MSG-RECV':    { color: '#8b5cf6', bg: 'rgba(139, 92, 246, 0.12)' },
  'SEND':        { color: 'var(--brand-green)', bg: 'rgba(24, 226, 153, 0.12)' },
  'SEND-FAIL':   { color: '#ef4444', bg: 'rgba(239, 68, 68, 0.12)' },
  'API':         { color: '#6366f1', bg: 'rgba(99, 102, 241, 0.12)' },
  'EXPORT':      { color: '#6366f1', bg: 'rgba(99, 102, 241, 0.12)' },
  'ALERT':       { color: '#f97316', bg: 'rgba(249, 115, 22, 0.12)' },
  'DIGEST':      { color: '#14b8a6', bg: 'rgba(20, 184, 166, 0.12)' },
  'WND':         { color: '#a78bfa', bg: 'rgba(167, 139, 250, 0.12)' },
  'Agent':       { color: '#f472b6', bg: 'rgba(244, 114, 182, 0.12)' },
  'RAG':         { color: '#34d399', bg: 'rgba(52, 211, 153, 0.12)' },
  'VOICE-CALL':  { color: '#34d399', bg: 'rgba(52, 211, 153, 0.12)' },
  'VOICE-WS':    { color: '#38bdf8', bg: 'rgba(56, 189, 248, 0.12)' },
  'VOICE-RT':    { color: '#a78bfa', bg: 'rgba(167, 139, 250, 0.12)' },
  'VOICE-VOLC':  { color: '#fbbf24', bg: 'rgba(251, 191, 36, 0.12)' },
  'VOICE-A':     { color: '#38bdf8', bg: 'rgba(56, 189, 248, 0.12)' },
}

// ── 语音通话日志归组 ─────────────────────────────────────────────────
// 同时兼容散点行 sid=voice_... 与 DETAIL JSON 中的 "sid": "voice_..."
const VOICE_SID_RE = /(?:sid=|"sid":\s*")(voice_\d{8}_\d{6}_\d{4})/
const VOICE_PREFIX_RE = /^\[VOICE-[A-Z]+\]/
const VOICE_CONTENT_RE = /(user >>|assistant <<|function_call)/

// 解析 [VOICE-CALL] 行的 key=value 字段
function parseVoiceCallKv(msg) {
  const out = {}
  const re = /(\w+)=(\S+)/g
  let m
  while ((m = re.exec(msg)) !== null) out[m[1]] = m[2]
  const em = /\serror=(.*)$/.exec(msg)
  if (em) out.error = em[1]
  return out
}

// 从全部日志行中按 sid 归组通话卡片（不受当前过滤条件影响，保证卡片完整）
function groupVoiceCalls(logs) {
  const cards = new Map() // sid 保持插入顺序
  const ensure = (sid) => {
    let c = cards.get(sid)
    if (!c) {
      c = {
        sid, provider: '', voice: '', model: '',
        open: null, close: null,
        details: [], frame: [], content: [],
        levels: new Set(), searchText: '',
      }
      cards.set(sid, c)
    }
    return c
  }
  const ingestText = (c, t) => { c.searchText += ` ${t || ''}` }

  for (const log of logs) {
    const msg = log.msg || ''
    if (!msg.startsWith('[VOICE-')) continue
    const sidM = VOICE_SID_RE.exec(msg)
    const sid = sidM ? sidM[1] : null

    // DETAIL JSON：解析后进对应卡片（孤立 detail 丢弃，不单独渲染）
    if (msg.startsWith('[VOICE-DETAIL] ')) {
      if (!sid) continue
      try {
        const d = JSON.parse(msg.slice('[VOICE-DETAIL] '.length))
        const c = ensure(sid)
        c.details.push(d)
        c.levels.add(log.level || 'INFO')
        ingestText(c, msg)
        if (!c.provider && d.provider) c.provider = d.provider
      } catch { /* 畸形 JSON 忽略 */ }
      continue
    }

    // 无 sid 的语音行（如配置不可用、A 档散点）不归组
    if (!sid) continue

    const c = ensure(sid)
    c.levels.add(log.level || 'INFO')
    ingestText(c, msg)

    if (msg.startsWith('[VOICE-CALL] ')) {
      const kv = parseVoiceCallKv(msg)
      if (msg.includes(' OPEN ')) {
        c.open = { log, kv }
        c.provider = kv.provider || c.provider
        c.voice = kv.voice && kv.voice !== '-' ? kv.voice : c.voice
        c.model = kv.model && kv.model !== '-' ? kv.model : c.model
      } else if (msg.includes(' CLOSED ')) {
        c.close = { log, kv }
      }
    } else if (VOICE_CONTENT_RE.test(msg)) {
      c.content.push(log)
    } else {
      c.frame.push(log)
    }
  }
  return cards
}

// 卡片是否通过当前过滤/搜索条件
function voiceCardMatches(card, filter, searchQuery) {
  switch (filter) {
    // 通话卡片不属于这些既有分类，避免改变其原有过滤结果
    case 'LLM':
    case 'AGENT':
    case 'OP':
      return false
    case 'INFO':
    case 'WARNING':
    case 'ERROR':
    case 'DEBUG': {
      // 卡片内最高级别命中即保留（ERROR > WARNING > INFO）
      if (filter === 'ERROR') return card.levels.has('ERROR')
      if (filter === 'WARNING') return card.levels.has('WARNING') || card.levels.has('ERROR')
      if (filter === 'DEBUG') return card.levels.has('DEBUG')
      return true // INFO
    }
    default: // ALL / OP / VOICE
      break
  }
  if (searchQuery) {
    return card.searchText.toLowerCase().includes(searchQuery.toLowerCase())
  }
  return true
}

function renderHighlightedMsg(msg) {
  if (!msg) return ''
  const regex = /(\[[^\]]+\]|收到消息|发送消息|\b\d+(?:\.\d+)?(?:ms|s|毫秒|秒)\b|\b(?:OK|SUCCESS|ERROR|FAIL)\b|成功|失败)/g
  const parts = msg.split(regex)
  if (parts.length === 1) return <span>{msg}</span>

  return (
    <span>
      {parts.map((part, i) => {
        if (!part) return null
        if (part.startsWith('[') && part.endsWith(']')) {
          // Check if this is an operation/voice tag with a custom color
          const tagContent = part.slice(1, -1)
          if (OP_TAGS.includes(tagContent) || TAG_STYLES[tagContent]) {
            const style = TAG_STYLES[tagContent] || { color: '#6366f1', bg: 'rgba(99, 102, 241, 0.12)' }
            return (
              <span key={i} className="px-1.5 py-0.5 rounded-full border text-xs font-semibold font-mono mx-0.5"
                style={{ color: style.color, backgroundColor: style.bg, borderColor: `${style.color}25` }}>
                {part}
              </span>
            )
          }
          // Special styling for LLM/MEMORY/OA-DIGEST tags
          const isLLM = part === '[LLM]' || part === '[LLM-ROUTE]'
          const isMemory = part === '[MEMORY]'
          const isOADigest = part === '[OA-DIGEST]'
          const tagStyle = isLLM
            ? 'bg-purple-500/10 text-purple-400 border-purple-500/20'
            : isMemory
              ? 'bg-teal-500/10 text-teal-400 border-teal-500/20'
              : isOADigest
                ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
                : 'bg-status-info-soft text-status-info border-status-info/20'
          return (
            <span key={i} className={`px-1.5 py-0.5 rounded-full border text-xs font-semibold font-mono mx-0.5 ${tagStyle}`}>
              {part}
            </span>
          )
        }
        if (part === '收到消息' || part === '发送消息') {
          const isRecv = part === '收到消息'
          return (
            <span key={i} className={`px-1.5 py-0.5 rounded-full text-xs font-semibold font-mono mx-0.5 ${isRecv ? 'bg-[var(--brand-green)]/10 text-[var(--brand-green)] border border-[var(--brand-green)]/20' : 'bg-status-warn-soft text-status-warn border border-status-warn/20'}`}>
              {part}
            </span>
          )
        }
        if (/^\d+(?:\.\d+)?(?:ms|s|毫秒|秒)$/.test(part)) {
          return (
            <span key={i} className="px-1.5 py-0.5 rounded-full bg-bg-inset border border-border-main text-text-main text-xs font-semibold font-mono mx-0.5">
              {part}
            </span>
          )
        }
        if (part === '成功' || part === 'OK' || part === 'SUCCESS') {
          return (
            <span key={i} className="text-[var(--brand-green)] font-semibold font-mono mx-0.5">
              {part}
            </span>
          )
        }
        if (part === '失败' || part === 'ERROR' || part === 'FAIL') {
          return (
            <span key={i} className="text-status-error font-semibold font-mono mx-0.5">
              {part}
            </span>
          )
        }
        return <span key={i}>{part}</span>
      })}
    </span>
  )
}

// ── Collapsible LLM Detail Section ──────────────────────────────────

function LLMSection({ label, content, defaultCollapsed = true }) {
  const [open, setOpen] = useState(!defaultCollapsed)
  if (!content) return null
  const isLong = content.length > 200
  return (
    <div className="mt-1.5">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-xs font-semibold text-text-muted hover:text-text-main transition-colors cursor-pointer"
      >
        {open ? <CaretDown size={10} weight="fill" /> : <CaretRight size={10} weight="fill" />}
        <span className="uppercase tracking-wider">{label}</span>
        <span className="text-text-muted font-normal">({content.length} chars)</span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <pre className="mt-1 p-2.5 bg-bg-main/60 dark:bg-black/30 border border-border-main rounded-lg text-xs leading-relaxed text-text-main whitespace-pre-wrap break-all max-h-64 overflow-y-auto font-mono">
              {content}
            </pre>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── LLM Log Entry (collapsible) ─────────────────────────────────────

function LLMLogEntry({ log, expanded, onToggle }) {
  const detail = log.llmDetail
  const isRoute = log.msg?.startsWith('[LLM-ROUTE]')

  return (
    <div className="flex flex-col">
      <div
        className="flex items-center gap-4 px-4 py-2 hover:bg-bg-main/50 dark:hover:bg-white/5 transition-colors border-0 cursor-pointer"
        onClick={detail ? onToggle : undefined}
      >
        <span className="text-text-muted shrink-0 text-xs font-mono" style={{ width: 70 }}>{log.ts}</span>
        <span
          className="shrink-0 font-bold rounded-full text-center text-xs tracking-wider inline-flex items-center justify-center border font-mono uppercase"
          style={{
            width: 68,
            height: 20,
            color: LEVEL_STYLES[log.level]?.color || '#888888',
            backgroundColor: LEVEL_STYLES[log.level]?.bg || 'var(--bg-main)',
            borderColor: LEVEL_STYLES[log.level]?.color ? `${LEVEL_STYLES[log.level].color}25` : 'var(--border-main)',
          }}
        >
          {log.level}
        </span>
        <span className="text-text-main break-all select-all font-mono flex-1">
          {renderHighlightedMsg(log.msg)}
        </span>
        {detail && (
          <span className="shrink-0 text-text-muted hover:text-purple-400 transition-colors">
            {expanded ? <CaretDown size={14} weight="fill" /> : <CaretRight size={14} weight="fill" />}
          </span>
        )}
      </div>

      {/* Expanded detail panel */}
      <AnimatePresence>
        {expanded && detail && !isRoute && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="ml-[70px] mr-4 mb-2 p-3 bg-purple-500/5 dark:bg-purple-500/10 border border-purple-500/15 rounded-xl space-y-1">
              {/* Metadata bar */}
              <div className="flex flex-wrap items-center gap-2 text-xs font-mono text-text-muted">
                <span className="px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">
                  {detail.backend}/{detail.model}
                </span>
                <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">
                  {detail.call_type}
                </span>
                {detail.latency_ms != null && (
                  <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">
                    {detail.latency_ms >= 1000
                      ? `${(detail.latency_ms / 1000).toFixed(1)}s`
                      : `${Math.round(detail.latency_ms)}ms`}
                  </span>
                )}
                {(detail.token_in > 0 || detail.token_out > 0) && (
                  <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">
                    {detail.token_in}→{detail.token_out} tokens
                  </span>
                )}
                {detail.extra?.requester && (
                  <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">
                    requester: {detail.extra.requester}
                  </span>
                )}
                {detail.extra?.group && (
                  <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">
                    group: {detail.extra.group}
                  </span>
                )}
                {detail.extra?.mode && (
                  <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">
                    mode: {detail.extra.mode}
                  </span>
                )}
              </div>

              {/* Tool definitions (collapsible) */}
              {detail.extra?.tool_defs && detail.extra.tool_defs.length > 0 && (
                <div className="mt-2 mb-1">
                  <LLMSection
                    label={`Tool Definitions (${detail.extra.tool_defs.length})`}
                    content={JSON.stringify(detail.extra.tool_defs, null, 2)}
                    defaultCollapsed={true}
                  />
                </div>
              )}

              {/* Prompt & Response sections */}
              <LLMSection label="System Prompt" content={detail.system_prompt} defaultCollapsed={true} />
              <LLMSection label="User Prompt" content={detail.user_prompt} defaultCollapsed={true} />
              <LLMSection label="Response" content={detail.response} defaultCollapsed={false} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Voice Call Log Entry (按 sid 归组的通话卡片) ─────────────────────

const PROVIDER_LABEL_CN = { volc: '火山豆包', qwen: '千问' }
const CALL_STATUS = {
  ok:        { text: '已结束', cls: 'text-emerald-400' },
  downgrade: { text: '已降级', cls: 'text-amber-400' },
  failed:    { text: '失败',   cls: 'text-red-400' },
}

function VoiceToolBlock({ tool }) {
  const [open, setOpen] = useState(false)
  const hasBody = !!(tool.args || tool.result)
  return (
    <div className="ml-5 rounded-lg border border-sky-500/20 bg-sky-500/5 px-2.5 py-1.5 text-xs">
      <button
        onClick={() => hasBody && setOpen(!open)}
        className={`flex w-full items-center gap-2 text-left ${hasBody ? 'cursor-pointer hover:text-sky-300' : 'cursor-default'}`}
      >
        {hasBody
          ? (open ? <CaretDown size={10} weight="fill" className="text-sky-400" />
                  : <CaretRight size={10} weight="fill" className="text-sky-400" />)
          : <span className="inline-block w-[10px]" />}
        <span className="rounded bg-sky-500/15 px-1.5 py-0.5 font-mono text-[11px] text-sky-300 border border-sky-500/20">
          {tool.name}
        </span>
        {typeof tool.ms === 'number' && (
          <span className="rounded bg-bg-main/60 px-1.5 py-0.5 font-mono text-[10px] text-text-muted border border-border-main">
            {tool.ms >= 1000 ? `${(tool.ms / 1000).toFixed(1)}s` : `${Math.round(tool.ms)}ms`}
          </span>
        )}
      </button>
      <AnimatePresence>
        {open && hasBody && (
          <motion.div
            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            {tool.args && (
              <div className="mt-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">参数</div>
                <pre className="mt-0.5 whitespace-pre-wrap break-all rounded bg-black/25 p-1.5 font-mono text-[11px] text-text-secondary">{tool.args}</pre>
              </div>
            )}
            {tool.result && (
              <div className="mt-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">返回预览</div>
                <pre className="mt-0.5 max-h-32 overflow-y-auto whitespace-pre-wrap break-all rounded bg-black/25 p-1.5 font-mono text-[11px] text-text-secondary">{tool.result}</pre>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function VoiceTurn({ detail }) {
  return (
    <div className="space-y-1.5 border-l-2 border-emerald-500/25 pl-3">
      <div className="flex items-center gap-2">
        <span className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-mono text-emerald-300 border border-emerald-500/25">
          第 {detail.turn} 轮
        </span>
        {detail.interrupted && (
          <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-300 border border-amber-500/25">
            中断/未答完
          </span>
        )}
        {typeof detail.latency_ms === 'number' && (
          <span className="text-[10px] font-mono text-text-muted">
            {detail.latency_ms >= 1000 ? `${(detail.latency_ms / 1000).toFixed(1)}s` : `${Math.round(detail.latency_ms)}ms`}
          </span>
        )}
      </div>
      {detail.user && (
        <div className="text-xs leading-relaxed">
          <span className="mr-1 font-semibold text-emerald-400">你 ›</span>
          <span className="text-text-main select-all">{detail.user}</span>
        </div>
      )}
      {detail.tools?.length > 0 && (
        <div className="space-y-1">
          {detail.tools.map((t, i) => <VoiceToolBlock key={i} tool={t} />)}
        </div>
      )}
      {detail.assistant && (
        <div className="text-xs leading-relaxed">
          <span className="mr-1 font-semibold text-teal-300">答 ‹</span>
          <span className="text-text-main select-all">{detail.assistant}</span>
        </div>
      )}
    </div>
  )
}

function VoiceCallEntry({ card, expanded, onToggle }) {
  const closeKv = card.close?.kv
  const status = closeKv?.status ? CALL_STATUS[closeKv.status] : null
  const ts = card.open?.log?.ts || card.frame[0]?.ts || card.close?.log?.ts || ''
  const pName = PROVIDER_LABEL_CN[card.provider] || card.provider || '实时语音'
  const duration = closeKv?.duration ? `${closeKv.duration}s` : null
  const turns = closeKv?.turns != null ? `${closeKv.turns}轮` : (card.details.length ? `${card.details.length}轮` : null)
  const level = card.levels.has('ERROR') ? 'ERROR' : card.levels.has('WARNING') ? 'WARNING' : 'INFO'
  // 旧日志兼容：没有 DETAIL 行时直接展示内容散点
  const legacyContent = card.details.length === 0 ? card.content : []

  return (
    <div className="flex flex-col">
      <div
        className="flex items-center gap-4 px-4 py-2 hover:bg-bg-main/50 dark:hover:bg-white/5 transition-colors border-0 cursor-pointer"
        onClick={onToggle}
      >
        <span className="text-text-muted shrink-0 text-xs font-mono" style={{ width: 70 }}>{ts.slice(11)}</span>
        <span
          className="shrink-0 font-bold rounded-full text-center text-xs tracking-wider inline-flex items-center justify-center border font-mono uppercase"
          style={{
            width: 68, height: 20,
            color: LEVEL_STYLES[level]?.color || '#888888',
            backgroundColor: LEVEL_STYLES[level]?.bg || 'var(--bg-main)',
            borderColor: LEVEL_STYLES[level]?.color ? `${LEVEL_STYLES[level].color}25` : 'var(--border-main)',
          }}
        >
          VOICE
        </span>
        <span className="text-text-main break-all select-all font-mono flex-1 text-xs">
          <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 mr-1.5 text-emerald-400 border border-emerald-500/20 font-semibold not-italic">
            通话 · {pName}
          </span>
          {card.voice && card.voice !== '-' && (
            <span className="text-text-secondary">{card.voice} </span>
          )}
          {duration && <span className="text-text-muted">· {duration} </span>}
          {turns && <span className="text-text-muted">· {turns} </span>}
          {status
            ? <span className={`font-semibold ${status.cls}`}>· {status.text}</span>
            : <span className="text-sky-400 font-semibold">· 进行中…</span>}
        </span>
        <span className="shrink-0 text-text-muted hover:text-emerald-400 transition-colors">
          {expanded ? <CaretDown size={14} weight="fill" /> : <CaretRight size={14} weight="fill" />}
        </span>
      </div>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="ml-[70px] mr-4 mb-2 p-3 bg-emerald-500/5 dark:bg-emerald-500/10 border border-emerald-500/15 rounded-xl space-y-3">
              {/* 元信息徽章条 */}
              <div className="flex flex-wrap items-center gap-2 text-xs font-mono">
                <span className="px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">{pName}</span>
                {card.voice && card.voice !== '-' && (
                  <span className="px-1.5 py-0.5 rounded bg-bg-main/60 text-text-secondary border border-border-main">{card.voice}</span>
                )}
                {card.model && card.model !== '-' && (
                  <span className="px-1.5 py-0.5 rounded bg-bg-main/60 text-text-secondary border border-border-main">{card.model}</span>
                )}
                {duration && <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">{duration}</span>}
                {closeKv?.by && <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">{closeKv.by}</span>}
                <span className="px-1.5 py-0.5 rounded bg-bg-main/60 border border-border-main">{card.details.length} 轮记录</span>
              </div>

              {closeKv?.error && (
                <div className="rounded-lg border border-red-500/25 bg-red-500/10 px-2.5 py-1.5 text-xs text-red-300 break-all">
                  异常：{closeKv.error}
                </div>
              )}

              {/* 轮次（结构化） */}
              {card.details.length > 0 && (
                <div className="space-y-3">
                  {card.details.map((d, i) => <VoiceTurn key={i} detail={d} />)}
                </div>
              )}

              {/* 旧日志兜底：无 DETAIL 时显示内容散点 */}
              {legacyContent.length > 0 && (
                <div className="space-y-1">
                  {legacyContent.map((l, i) => (
                    <div key={i} className="text-xs font-mono text-text-secondary">
                      <span className="text-text-muted mr-2">{l.ts?.slice(11)}</span>
                      {renderHighlightedMsg(l.msg)}
                    </div>
                  ))}
                </div>
              )}

              {/* 框架事件时间线 */}
              {card.frame.length > 0 && (
                <div className="space-y-1 border-t border-emerald-500/10 pt-2">
                  {card.frame.map((l, i) => (
                    <div key={i} className="text-[11px] font-mono text-text-muted">
                      <span className="mr-2 opacity-70">{l.ts?.slice(11)}</span>
                      {renderHighlightedMsg(l.msg)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Main LogViewer Component ────────────────────────────────────────

export default function LogViewer() {
  const [filter, setFilter] = useState('ALL')
  const [searchQuery, setSearchQuery] = useState('')
  const [logs, setLogs] = useState([])
  const [clearedManually, setClearedManually] = useState(false)
  const [expandedLLM, setExpandedLLM] = useState({})
  const [expandedVoice, setExpandedVoice] = useState({})
  const scrollRef = useRef(null)
  const seenRef = useRef(new Set())
  const [autoScroll, setAutoScroll] = useState(true)
  const SCROLL_THRESHOLD = 50  // px from bottom considered "at bottom"

  const fetchLogs = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/logs`)
      const data = await res.json()
      if (data.ok && data.logs?.length) {
        setLogs(prev => {
          const merged = [...prev]
          let added = false
          for (const entry of data.logs) {
            const key = entry.raw
            if (!seenRef.current.has(key)) {
              seenRef.current.add(key)
              // Parse LLM markers
              if (entry.msg?.startsWith('[LLM-DETAIL] ')) {
                try {
                  entry.llmDetail = JSON.parse(entry.msg.slice('[LLM-DETAIL] '.length))
                  entry.isLLMDetail = true
                } catch {}
              }
              if (entry.msg?.startsWith('[LLM] ') || entry.msg?.startsWith('[LLM-ROUTE]')) {
                entry.isLLMSummary = true
              }
              merged.push(entry)
              added = true
            }
          }
          if (added) {
            setClearedManually(false)
          }
          return merged.length > 2000 ? merged.slice(-2000) : merged
        })
      }
    } catch {}
  }, [])

  useEffect(() => {
    fetchLogs()
    const timer = setInterval(fetchLogs, 10000)
    return () => clearInterval(timer)
  }, [fetchLogs])

  function handleScroll() {
    const el = scrollRef.current
    if (!el) return
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    setAutoScroll(distFromBottom < SCROLL_THRESHOLD)
  }

  function scrollToBottom() {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    setAutoScroll(true)
  }

  useEffect(() => {
    if (autoScroll) {
      const el = scrollRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [logs, autoScroll])

  // 过滤普通日志（LLM 摘要/详情合并、语音按 sid 归组为通话卡片）
  const processed = (() => {
    const q = searchQuery.toLowerCase()
    // 语音卡片从"全部日志"归组，保证一通通话的轮次不被过滤条件截断
    const voiceCards = groupVoiceCalls(logs)
    const placedSids = new Set()

    const matchNormal = (l) => {
      const msg = l.msg || ''
      let matchesLevel
      if (filter === 'ALL') matchesLevel = true
      else if (filter === 'LLM') matchesLevel = l.isLLMSummary || l.isLLMDetail
      else if (filter === 'VOICE') matchesLevel = msg.startsWith('[VOICE-')
      else if (filter === 'AGENT') matchesLevel = msg.startsWith('[Agent]')
      else if (filter === 'OP') matchesLevel = OP_TAG_REGEX.test(msg)
      else matchesLevel = l.level === filter
      const matchesSearch = !q ||
        msg.toLowerCase().includes(q) ||
        l.ts?.toLowerCase().includes(q) ||
        l.level?.toLowerCase().includes(q)
      return matchesLevel && matchesSearch
    }

    const result = []
    for (const l of logs) {
      const msg = l.msg || ''

      // ① 语音行：带 sid 的一律由通话卡片承载（DETAIL 只入卡片）
      if (msg.startsWith('[VOICE-')) {
        const sidM = VOICE_SID_RE.exec(msg)
        const sid = sidM ? sidM[1] : null
        if (sid) {
          if (!placedSids.has(sid)) {
            placedSids.add(sid)
            const card = voiceCards.get(sid)
            if (card && voiceCardMatches(card, filter, searchQuery)) {
              result.push({ _voiceCard: card })
            }
          }
          continue
        }
        // 无 sid 的语音散点（A 档、配置不可用）→ 普通行
        if (matchNormal(l)) result.push(l)
        continue
      }

      // ② LLM 详情合并到前一条摘要（保持原逻辑）
      if (!matchNormal(l)) continue
      if (l.isLLMDetail && result.length > 0) {
        const prev = result[result.length - 1]
        if (prev.isLLMSummary) {
          prev.llmDetail = l.llmDetail
          continue
        }
      }
      result.push(l)
    }
    return result
  })()

  return (
    <div className="max-w-4xl space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <div className="flex items-center gap-1 bg-bg-card border border-border-main rounded-full p-1 shrink-0">
          {FILTER_OPTIONS.map(level => (
            <button
              key={level}
              onClick={() => setFilter(level)}
              className={`px-3 py-1.25 rounded-full text-xs font-semibold transition-all cursor-pointer ${
                filter === level
                  ? level === 'LLM'
                    ? 'bg-purple-500/15 text-purple-400 font-semibold shadow-sm'
                    : level === 'VOICE'
                      ? 'bg-emerald-500/15 text-emerald-400 font-semibold shadow-sm'
                      : 'bg-bg-raised text-text-main font-semibold shadow-sm'
                  : 'text-text-muted hover:text-text-main'
              }`}
            >
              {LEVEL_LABELS[level]}
            </button>
          ))}
        </div>

        {/* Log Search Input */}
        <div className="relative flex-1 max-w-sm">
          <span className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none text-text-muted">
            <MagnifyingGlass size={14} />
          </span>
          <input
            type="text"
            placeholder="搜索日志关键字、事件、耗时..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-8 py-1.5 bg-bg-card border border-border-main rounded-full text-xs placeholder:text-text-muted focus:outline-none focus:border-brand-green/60 text-text-main transition-all font-mono"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              className="absolute inset-y-0 right-0 pr-3.5 flex items-center text-text-muted hover:text-status-error text-xs font-sans font-semibold cursor-pointer"
            >
              清除
            </button>
          )}
        </div>

        <div className="flex-1 hidden sm:block" />
        <div className="flex items-center gap-2.5 ml-auto sm:ml-0 shrink-0">
          <span className="text-xs text-text-muted font-mono bg-bg-card border border-border-main px-3 py-1 rounded-full">{processed.length} 条</span>
          <button onClick={scrollToBottom} title="滚动到底部" className="p-2 rounded-full text-text-muted hover:text-text-main hover:bg-bg-raised transition-colors cursor-pointer border border-transparent hover:border-border-main">
            <ArrowDown size={16} />
          </button>
          <button onClick={() => { setLogs([]); seenRef.current.clear(); setClearedManually(true); setExpandedLLM({}); setExpandedVoice({}) }} title="仅清空屏幕展示日志（不删除后台日志文件）" className="p-2 rounded-full text-text-muted hover:text-status-error hover:bg-status-error-soft border border-transparent hover:border-status-error/20 transition-colors cursor-pointer">
            <Trash size={16} />
          </button>
        </div>
      </div>

      <motion.div
        initial={{ opacity: 0 }} animate={{ opacity: 1 }}
        ref={scrollRef}
        onScroll={handleScroll}
        className="bg-bg-inset border border-border-main rounded-2xl overflow-hidden max-h-[600px] overflow-y-auto font-mono text-[13px] leading-relaxed p-4 divide-y divide-border-main shadow-sm text-text-main"
      >
        {processed.length === 0 ? (
          clearedManually ? (
            <div className="p-16 text-center text-text-muted">
              <p className="text-base font-semibold text-text-main font-mono">Terminal Cleared</p>
              <p className="text-xs mt-1.5 font-medium text-text-muted">等待新事件写入日志文件...</p>
            </div>
          ) : (
            <div className="p-16 text-center text-text-muted">
              <p className="text-base font-semibold text-text-main font-mono">Console Offline</p>
              <p className="text-xs mt-1.5 font-medium text-text-muted">启动机器人后，日志数据流将在此输出</p>
              <p className="text-xs mt-1 font-mono text-text-muted">位置: data/bot.log</p>
            </div>
          )
        ) : (
          processed.map((log, i) => {
            // 语音通话卡片（按 sid 归组）
            if (log._voiceCard) {
              const sid = log._voiceCard.sid
              return (
                <VoiceCallEntry
                  key={`vc-${sid}`}
                  card={log._voiceCard}
                  expanded={!!expandedVoice[sid]}
                  onToggle={() => setExpandedVoice(prev => ({ ...prev, [sid]: !prev[sid] }))}
                />
              )
            }
            // LLM entries with detail — render as collapsible
            if (log.isLLMSummary) {
              return (
                <LLMLogEntry
                  key={`llm-${i}`}
                  log={log}
                  expanded={!!expandedLLM[i]}
                  onToggle={() => setExpandedLLM(prev => ({ ...prev, [i]: !prev[i] }))}
                />
              )
            }
            // Normal log entries
            return (
              <motion.div
                key={i}
                initial={{ opacity: 0, x: -4 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: Math.min(i * 0.003, 0.2) }}
                className="flex items-center gap-4 px-4 py-2 hover:bg-bg-main/50 dark:hover:bg-white/5 transition-colors border-0"
              >
                <span className="text-text-muted shrink-0 text-xs font-mono" style={{ width: 70 }}>{log.ts}</span>
                <span
                  className="shrink-0 font-bold rounded-full text-center text-xs tracking-wider inline-flex items-center justify-center border font-mono uppercase"
                  style={{
                    width: 68,
                    height: 20,
                    color: LEVEL_STYLES[log.level]?.color || '#888888',
                    backgroundColor: LEVEL_STYLES[log.level]?.bg || 'var(--bg-main)',
                    borderColor: LEVEL_STYLES[log.level]?.color ? `${LEVEL_STYLES[log.level].color}25` : 'var(--border-main)',
                  }}
                >
                  {log.level}
                </span>
                <span className="text-text-main break-all whitespace-pre-wrap select-all font-mono">{renderHighlightedMsg(log.msg)}</span>
              </motion.div>
            )
          })
        )}
      </motion.div>
    </div>
  )
}
