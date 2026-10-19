import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Microphone, PhoneDisconnect, Waveform, ArrowCounterClockwise,
  CaretDown, Sparkle, Warning, X,
} from '@phosphor-icons/react'
import { API_BASE } from './SharedComponents'
import { getWsUrl } from './SharedComponents'
import { RealtimeSession } from './voice/RealtimeSession'

// ── 工具名 → 语音提示（tool_start 时显示"正在 xxx"）─────────────────
const TOOL_LABELS = {
  query_my_feed: '翻看昨天的推送',
  search_chat_history: '搜索聊天记录',
  search_oa_articles: '搜索公众号文章',
  search_moments: '搜索朋友圈',
  search_favorites: '搜索收藏',
  run_digest: '生成群聊摘要',
  run_oa_digest: '生成公众号摘要',
  get_latest_oa_digest: '查找公众号摘要',
  get_latest_group_digest: '查找群聊摘要',
  list_digests: '查看摘要配置',
  list_alerts: '查看预警配置',
  list_oa_groups: '查看公众号分组',
  list_oa_monitors: '查看实时推送列表',
  list_tasks: '查看任务记录',
  list_crons: '查看定时任务',
  get_status: '查看运行状态',
  confirm_action: '等待确认',
}
const MCP_TOOL_HINTS = [
  [/bocha/i, '联网搜索'],
  [/fetch/i, '读取网页内容'],
  [/filesystem/i, '读取文件'],
]

function toolLabel(name) {
  if (TOOL_LABELS[name]) return TOOL_LABELS[name]
  for (const [re, label] of MCP_TOOL_HINTS) if (re.test(name)) return label
  return name.replace(/_/g, ' ')
}

const PHASE_TEXT = {
  idle: '点击开始，像打电话一样说话',
  connecting: '正在接通…',
  listening: '聆听中，随时可以开口',
  thinking: '思考中…',
  speaking: '正在回答',
}

// ── 通道推导：根据服务端实时配置 + A 档配置，得出本次通话实际通道 ──────
// 返回 { mode:'realtime', provider } 或
//      { mode:'fallback', stt:'browser'|'cloud', reason, provider }
function deriveChannel(rtInfo, voiceCfg) {
  const stt = voiceCfg?.stt_cloud ? 'cloud' : 'browser'
  if (rtInfo && rtInfo.ok) {
    const provider = rtInfo.rt_provider === 'volc' ? 'volc' : 'qwen'
    if (rtInfo.enabled) return { mode: 'realtime', provider }
    let reason = '实时通道未就绪'
    if (provider === 'volc') {
      if (!rtInfo.volc_enabled) reason = '火山实时全双工开关未开启'
      else if (!rtInfo.volc_has_key) reason = '火山 API Key 未保存'
    } else {
      if (!rtInfo.qwen_enabled) reason = '千问实时全双工开关未开启'
      else if (!rtInfo.has_key) reason = '千问 API Key 未保存'
    }
    return { mode: 'fallback', stt, reason, provider }
  }
  return { mode: 'fallback', stt, reason: '实时通道状态获取失败', provider: null }
}

// 通道名牌文案/样式
const CHANNEL_BADGE = {
  'realtime-volc': { text: '实时双工 · 火山豆包', cls: 'bg-emerald-400/15 text-emerald-300 ring-emerald-400/30' },
  'realtime-qwen': { text: '实时双工 · 千问', cls: 'bg-emerald-400/15 text-emerald-300 ring-emerald-400/30' },
  'fallback-browser': { text: '标准模式 · 浏览器识别', cls: 'bg-sky-400/15 text-sky-300 ring-sky-400/30' },
  'fallback-cloud': { text: '标准模式 · 云端识别', cls: 'bg-sky-400/15 text-sky-300 ring-sky-400/30' },
}

function channelBadge(ch) {
  if (!ch) return null
  if (ch.mode === 'realtime') return CHANNEL_BADGE[`realtime-${ch.provider}`]
  return CHANNEL_BADGE[`fallback-${ch.stt}`]
}

// ── 文本处理：切句 + 清洗 Markdown（朗读用）─────────────────────────
const SENT_END_RE = /[。！？!?；\n…]/

function stripForSpeech(s) {
  return (s || '')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/[*#`_>]/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/https?:\/\/\S+/g, '链接')
    .replace(/\|/g, '，')
    .replace(/\s+/g, ' ')
    .trim()
}

// ── WAV 编码（16bit PCM mono，云端 STT 上传用）──────────────────────
function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const writeStr = (off, str) => { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)) }
  writeStr(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeStr(8, 'WAVE'); writeStr(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  writeStr(36, 'data'); view.setUint32(40, samples.length * 2, true)
  let off = 44
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([view], { type: 'audio/wav' })
}

// ── 云端 STT 引擎：本地 VAD + 整句上传 ──────────────────────────────
class CloudVadEngine {
  constructor() {
    this.onLevel = () => {}
    this.onSpeechStart = () => {}
    this.onPartial = () => {}
    this.onUtterance = () => {}
    this.onError = () => {}
    this.speakingGate = () => false
    this._spoke = false
    this._paused = false
  }

  async start() {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    this._stream = stream
    const Ctx = window.AudioContext || window.webkitAudioContext
    this._ctx = new Ctx()
    const sr = this._ctx.sampleRate
    this._sr = sr
    this._bufLen = 4096
    this._preRoll = []
    this._maxPre = Math.max(2, Math.round((sr * 0.35) / this._bufLen))
    this._cur = []
    this._inSpeech = false
    this._silenceFrames = 0
    this._hot = 0
    this._noise = 0.012

    const src = this._ctx.createMediaStreamSource(stream)
    const proc = this._ctx.createScriptProcessor(this._bufLen, 1, 1)
    this._proc = proc
    // 必须连入图才触发 onaudioprocess；经 zero-gain 节点避免麦克风外放
    const mute = this._ctx.createGain()
    mute.gain.value = 0
    src.connect(proc); proc.connect(mute); mute.connect(this._ctx.destination)
    proc.onaudioprocess = (e) => this._onFrame(e.inputBuffer.getChannelData(0))
  }

  _onFrame(data) {
    if (this._paused) return   // 半双工：AI 出声期间不收帧，杜绝外放回声
    let sum = 0
    for (let i = 0; i < data.length; i += 2) sum += data[i] * data[i]
    const rms = Math.sqrt(sum / (data.length / 2))
    this.onLevel(Math.min(1, rms * 4.5))

    let threshold = Math.max(this._noise * 3.5, 0.012)
    if (this.speakingGate()) threshold *= 2.3 // AI 外放时提高门限，靠 AEC + 门限双保险

    if (!this._inSpeech) {
      this._preRoll.push(data.slice(0))
      if (this._preRoll.length > this._maxPre) this._preRoll.shift()
      this._noise = this._noise * 0.96 + rms * 0.04
      if (rms > threshold) {
        this._hot++
        if (this._hot >= 2) this._beginSpeech()
      } else this._hot = 0
    } else {
      this._cur.push(data.slice(0))
      if (rms < threshold) {
        this._silenceFrames++
        if ((this._silenceFrames * this._bufLen) / this._sr > 0.65) this._endSpeech()
      } else {
        this._silenceFrames = 0
      }
    }
  }

  _beginSpeech() {
    this._inSpeech = true
    this._cur = this._preRoll.map((f) => f.slice())
    this._preRoll = []
    this._hot = 0; this._silenceFrames = 0
    if (!this._spoke) { this._spoke = true; this.onSpeechStart() }
  }

  _endSpeech() {
    if (!this._inSpeech) return
    this._inSpeech = false
    const frames = this._cur
    const dur = (frames.length * this._bufLen) / this._sr
    this._cur = []
    if (dur < 0.25) return // 噪声误触
    const total = frames.reduce((n, f) => n + f.length, 0)
    const pcm = new Float32Array(total)
    let off = 0
    frames.forEach((f) => { pcm.set(f, off); off += f.length })
    this._upload(encodeWav(pcm, this._sr))
  }

  async _upload(blob) {
    try {
      const resp = await fetch(`${API_BASE}/api/voice/stt`, {
        method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: blob,
      })
      const data = await resp.json()
      if (data.ok && data.text) {
        const t = data.text.trim()
        if (t) this.onUtterance(t)
      } else {
        this.onError(data.error || '没有听清，请再说一次')
      }
    } catch {
      this.onError('语音识别服务不可用')
    }
  }

  resetTurn() { this._spoke = false }

  /** 半双工：AI 说话时暂停收音（麦克风保持打开，只是不再处理帧） */
  pause() {
    this._paused = true
    this._inSpeech = false
    this._cur = []
    this._preRoll = []
    this._silenceFrames = 0
    this._hot = 0
  }

  resume() { this._paused = false }

  stop() {
    try { this._proc && this._proc.disconnect() } catch {}
    try { this._stream && this._stream.getTracks().forEach((t) => t.stop()) } catch {}
    try { this._ctx && this._ctx.close() } catch {}
  }
}

// ── 浏览器内置 STT 引擎（Edge 国内可用，零配置兜底）────────────────
class WebSpeechEngine {
  constructor() {
    this.onLevel = () => {}
    this.onSpeechStart = () => {}
    this.onPartial = () => {}
    this.onUtterance = () => {}
    this.onError = () => {}
    this.speakingGate = () => false
    this._spoke = false
    this._finalBuf = ''
    this._paused = false
    this._lastCommit = { text: '', at: 0 }
  }

  start() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR) throw new Error('当前浏览器不支持语音识别，请用 Edge 打开，或配置云端语音识别')
    this._active = true
    this._startRecognition()
  }

  _startRecognition() {
    const rec = new (window.SpeechRecognition || window.webkitSpeechRecognition)()
    rec.lang = 'zh-CN'
    rec.continuous = true
    rec.interimResults = true
    this._rec = rec

    rec.onresult = (e) => {
      if (this._paused || this._rec !== rec) return
      let interim = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript
        if (e.results[i].isFinal) this._finalBuf += t
        else interim += t
      }
      const full = (this._finalBuf + interim).replace(/^\s+|\s+$/g, '')
      if (interim && !this._spoke) { this._spoke = true; this.onSpeechStart() }
      if (interim) this.onPartial(full)
      if (full) {
        clearTimeout(this._commitTimer)
        this._commitTimer = setTimeout(() => this._commit(full), 750)
      }
    }
    rec.onerror = (ev) => {
      if (this._paused) return   // 暂停期间的 aborted 等属正常
      if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
        this.onError('麦克风权限被拒绝，请在浏览器地址栏允许麦克风')
      } else if (ev.error === 'network') {
        this.onError('浏览器语音服务网络不通，建议用 Edge 或配置云端识别')
      }
      // no-speech / aborted 为正常事件，不打扰
    }
    rec.onend = () => {
      // continuous 模式浏览器也可能自动结束，主动重启以保持持续聆听
      if (this._paused || !this._active || this._rec !== rec) return
      setTimeout(() => {
        if (this._active && !this._paused && this._rec === rec) try { rec.start() } catch {}
      }, 120)
    }
    try { rec.start() } catch (e) { /* 重复 start 忽略 */ }
  }

  _commit(text) {
    clearTimeout(this._commitTimer)
    this._finalBuf = ''; this._spoke = false
    this.onPartial('')
    try { this._rec.stop() } catch {} // onend 会自动重启，resultIndex 归零
    // 同一段音频会被 repeated 提交（continuous 模式的已知行为）→ 2.5 秒内同句只发一次
    const norm = (text || '').replace(/\s/g, '')
    const now = Date.now()
    if (norm && norm === this._lastCommit.text && now - this._lastCommit.at < 2500) return
    this._lastCommit = { text: norm, at: now }
    if (text) this.onUtterance(text)
  }

  resetTurn() { this._spoke = false }

  /** 丢弃尚未提交的识别缓冲（AI 回合结束时调用，防止把自己的尾音当成用户输入） */
  discardTurn() {
    clearTimeout(this._commitTimer)
    this._finalBuf = ''
    this._spoke = false
    this.onPartial('')
  }

  /** 半双工：AI 说话时彻底停止收音，重启时再开（浏览器识别拿不到自己的音频流做 AEC） */
  pause() {
    if (this._paused) return
    this._paused = true
    clearTimeout(this._commitTimer)
    this._finalBuf = ''
    this._spoke = false
    this.onPartial('')
    try { this._rec && this._rec.abort() } catch {}
  }

  resume() {
    if (!this._paused) return
    this._paused = false
    if (!this._active) return
    this._startRecognition()
  }

  stop() {
    this._active = false
    clearTimeout(this._commitTimer)
    try { this._rec && this._rec.abort() } catch {}
  }
}

// ── 主组件 ─────────────────────────────────────────────────────────
let _msgSeq = 0

export default function VoiceCall({ open, onClose }) {
  const [phase, setPhase] = useState('idle')
  const [level, setLevel] = useState(0)
  const [partial, setPartial] = useState('')
  const [messages, setMessages] = useState([])
  const [toolHint, setToolHint] = useState('')
  const [cfg, setCfg] = useState(null)
  const [voice, setVoice] = useState(() => localStorage.getItem('voiceTtsVoice') || '')
  const [banner, setBanner] = useState('')
  const [showVoices, setShowVoices] = useState(false)
  const [rtMode, setRtMode] = useState(false)  // 是否处于端到端全双工模式
  const [rtAvailable, setRtAvailable] = useState(null)  // null=探测中 true/false
  const [channel, setChannel] = useState(null)  // 本次通话实际通道（见 deriveChannel）
  const voiceCfgRef = useRef(null)
  const channelRef = useRef(null)

  const phaseRef = useRef('idle')
  const spokenRef = useRef('')      // 本轮实际念出去的文案（回声比对基准）
  const speakEndAtRef = useRef(0)   // 离开 speaking 的时刻（残留回声时间窗）
  const lastUtterRef = useRef({ text: '', at: 0 })  // 最近一次接受的用户话（识别器重复提交去重）
  const resumeTimerRef = useRef(null)
  const setPhaseBoth = useCallback((p) => {
    const prev = phaseRef.current
    // 只有真正离开「AI 回合」（speaking/thinking 之间互切不算）才算回合结束
    if ((prev === 'speaking' || prev === 'thinking') && p !== 'speaking' && p !== 'thinking') {
      speakEndAtRef.current = Date.now()
      // 丢弃识别器里尚未提交的缓冲（含 750ms 待触发的回声提交），防止把自己的尾音当成用户的话
      try {
        const eng = engineRef.current
        if (eng && typeof eng.discardTurn === 'function') eng.discardTurn()
      } catch {}
    }
    phaseRef.current = p; setPhase(p)
    // 半双工：整个 AI 回合（thinking + speaking）都暂停收音，只在回到 listening 时恢复。
    // 恢复延迟 500ms，避免句间缝隙来回重启识别器。
    clearTimeout(resumeTimerRef.current)
    const eng = engineRef.current
    if (!eng || typeof eng.pause !== 'function') return
    try {
      if (p === 'thinking' || p === 'speaking') eng.pause()
      else {
        resumeTimerRef.current = setTimeout(() => {
          if (phaseRef.current === 'listening') { try { eng.resume() } catch {} }
        }, 500)
      }
    } catch {}
  }, [])
  const messagesRef = useRef([])
  const listRef = useRef(null)
  const engineRef = useRef(null)
  const realtimeRef = useRef(null)
  const modeRef = useRef('fallback')   // 'realtime' | 'fallback'
  const abortRef = useRef(null)
  const sentenceBufRef = useRef('')
  const assistantIdRef = useRef(null)
  const bannerTimer = useRef(null)
  const wakeLockRef = useRef(null)
  const ttsRef = useRef({ gen: 0, queue: [], busy: false, audio: null })
  const levelSmoothRef = useRef(0)

  const showBanner = useCallback((text, ms = 3500) => {
    setBanner(text)
    clearTimeout(bannerTimer.current)
    bannerTimer.current = setTimeout(() => setBanner(''), ms)
  }, [])

  const patchMessage = useCallback((id, patch) => {
    setMessages((prev) => {
      const next = prev.map((m) => (m.id === id ? { ...m, ...patch } : m))
      messagesRef.current = next
      return next
    })
  }, [])

  const appendMessage = useCallback((msg) => {
    setMessages((prev) => {
      const next = [...prev, msg]
      messagesRef.current = next
      return next
    })
  }, [])

  // 打开面板时预取语音配置（空闲态下拉框/模式判断需要，避免点开音色是空列表）
  useEffect(() => {
    if (!open) return
    let alive = true
    ;(async () => {
      try {
        const [c, r] = await Promise.all([
          fetch(`${API_BASE}/api/voice/config`).then((x) => x.json()).catch(() => null),
          fetch(`${API_BASE}/api/voice/realtime/config`).then((x) => x.json()).catch(() => null),
        ])
        if (!alive) return
        if (c && c.ok) {
          setCfg(c)
          voiceCfgRef.current = c
          if (!voice) {
            setVoice(c.tts_voice)
            localStorage.setItem('voiceTtsVoice', c.tts_voice)
          }
        }
        setRtAvailable(r && r.ok ? !!r.enabled : false)
        setChannel(deriveChannel(r, c && c.ok ? c : null))
      } catch { /* 通话启动时会再取一次 */ }
    })()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // 自动滚到底部
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, partial, toolHint])

  // 关闭即结束通话（仅在曾经打开过时执行，避免挂载初始值 false 触发）
  const wasOpenRef = useRef(false)
  useEffect(() => {
    if (open) {
      wasOpenRef.current = true
    } else if (wasOpenRef.current) {
      wasOpenRef.current = false
      endCall()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => () => {
    try { engineRef.current && engineRef.current.stop() } catch {}
    try { wakeLockRef.current && wakeLockRef.current.release() } catch {}
  }, [])

  // ── TTS 播放队列（代际控制：新一轮/打断立即让旧队列失活）────────
  const newSpeakingGeneration = useCallback(() => {
    const s = ttsRef.current
    s.gen += 1
    s.queue = []
    spokenRef.current = ''   // 新一轮开始，回声比对基准清空
    if (s.audio) { try { s.audio.pause() } catch {} }
    if (s.abort) { try { s.abort.abort() } catch {} }
    s.abort = new AbortController()
    s.busy = false
  }, [])

  const playBlob = useCallback((blob, gen) =>
    new Promise((resolve) => {
      const url = URL.createObjectURL(blob)
      const audio = new Audio()
      ttsRef.current.audio = audio
      let finished = false
      const finish = () => {
        if (finished) return
        finished = true
        URL.revokeObjectURL(url)
        if (ttsRef.current.audio === audio) ttsRef.current.audio = null
        resolve()
      }
      audio.onended = finish
      audio.onerror = finish
      audio.src = url
      audio.play().catch(finish)
      // 代际被替换时由外部 pause 触发 onerror/abort
      void gen
    }), [])

  const fetchSpeech = useCallback((sentence, gen) => {
    const s = ttsRef.current
    const ctrl = s.abort
    return fetch(
      `${API_BASE}/api/voice/tts?text=${encodeURIComponent(sentence)}&voice=${encodeURIComponent(voice)}`,
      ctrl ? { signal: ctrl.signal } : undefined,
    ).then((resp) => {
      if (s.gen !== gen) throw new Error('generation changed')
      if (!resp.ok) throw new Error('tts failed')
      return resp.blob()
    })
  }, [voice])

  const pumpTts = useCallback(async (gen) => {
    const s = ttsRef.current
    while (s.gen === gen) {
      const item = s.queue.shift()
      if (!item) {
        s.busy = false
        // 队列暂时空了但助手还在出字：继续留在 AI 回合内（thinking），
        // 不要提前切 listening —— 否则回声过滤会在句间缝隙里失效
        const stillStreaming = messagesRef.current.some((m) => m.streaming)
        if (phaseRef.current !== 'idle') setPhaseBoth(stillStreaming ? 'thinking' : 'listening')
        return
      }
      // 合成请求在入队时就已发出（见 enqueueSpeech），这里直接等结果
      // 记录即将念出去的文案，作为"听见自己"的回声比对基准
      spokenRef.current = (spokenRef.current + item.text).slice(-800)
      setPhaseBoth('speaking')
      setToolHint('')
      try {
        let blob
        try {
          blob = await item.promise
        } catch (e) {
          if (s.gen !== gen || e?.name === 'AbortError') return
          // 预取阶段就失败了（合成端点偶发连接重置）：现取一次兜底
          blob = await fetchSpeech(item.text, gen)
        }
        if (s.gen !== gen) return
        await playBlob(blob, gen)
      } catch (e) {
        if (s.gen !== gen || e?.name === 'AbortError') return
      }
    }
  }, [fetchSpeech, playBlob, setPhaseBoth])

  const enqueueSpeech = useCallback((text) => {
    const clean = stripForSpeech(text)
    if (!clean) return
    const s = ttsRef.current
    // 一入队就立刻发合成请求，不等轮到播放：LLM 边出字边合成，
    // 这样上一句还在播时下一句已经合成好，句间不再空等一次往返。
    const promise = fetchSpeech(clean, s.gen)
    promise.catch(() => {})   // 失败在播放时兜底重取，这里只防未处理的 rejection
    s.queue.push({ text: clean, promise })
    if (!s.busy) { s.busy = true; pumpTts(s.gen) }
  }, [fetchSpeech, pumpTts])

  // token → 字幕 + 按句切分送 TTS
  const feedToken = useCallback((token) => {
    const id = assistantIdRef.current
    if (id == null) return
    setMessages((prev) => {
      const next = prev.map((m) => (m.id === id ? { ...m, content: m.content + token } : m))
      messagesRef.current = next
      return next
    })
    let buf = sentenceBufRef.current + token
    const ready = []
    while (true) {
      const m = SENT_END_RE.exec(buf)
      if (!m) break
      ready.push(buf.slice(0, m.index + 1))
      buf = buf.slice(m.index + 1)
    }
    // 首句用更早的切分阈值，让声音尽快出来；后续用原阈值，避免切得太碎
    const firstChunk = spokenRef.current === ''
    const gate = firstChunk ? 20 : 45
    if (buf.length > gate) {
      const soft = Math.max(buf.lastIndexOf('，', gate), buf.lastIndexOf('、', gate),
                           buf.lastIndexOf('”', gate), buf.lastIndexOf(' ', gate))
      if (soft > (firstChunk ? 6 : 12)) {
        ready.push(buf.slice(0, soft + 1))
        buf = buf.slice(soft + 1)
      } else if (buf.length > (firstChunk ? 40 : 75)) {
        ready.push(buf.slice(0, gate))
        buf = buf.slice(gate)
      }
    }
    sentenceBufRef.current = buf
    ready.forEach(enqueueSpeech)
  }, [enqueueSpeech])

  const flushSentences = useCallback(() => {
    const rest = stripForSpeech(sentenceBufRef.current)
    sentenceBufRef.current = ''
    if (rest) enqueueSpeech(rest)
  }, [enqueueSpeech])

  const abortFetch = useCallback(() => {
    if (abortRef.current) {
      try { abortRef.current.abort() } catch {}
      abortRef.current = null
    }
  }, [])

  // ── 发起一轮对话（SSE）─────────────────────────────────────────
  const sendUtterance = useCallback(async (text) => {
    abortFetch()
    newSpeakingGeneration()
    sentenceBufRef.current = ''
    setToolHint('')
    setPartial('')

    const uid = `u${++_msgSeq}`
    const aid = `a${++_msgSeq}`
    appendMessage({ id: uid, role: 'user', content: text })
    appendMessage({ id: aid, role: 'assistant', content: '', streaming: true })
    assistantIdRef.current = aid
    setPhaseBoth('thinking')
    runTurn(text, aid, 0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const runTurn = useCallback(async (text, aid, retry) => {
    const controller = new AbortController()
    abortRef.current = controller

    let resp
    try {
      resp = await fetch(`${API_BASE}/api/voice/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
        signal: controller.signal,
      })
    } catch (e) {
      if (e.name === 'AbortError') return
      patchMessage(aid, { content: '网络异常，请重试', streaming: false, isError: true })
      setPhaseBoth('listening')
      return
    }

    if (!resp.ok || !resp.body) {
      patchMessage(aid, { content: '语音服务连接失败', streaming: false, isError: true })
      setPhaseBoth('listening')
      return
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let sseBuf = ''
    let eventName = ''
    let finished = false
    let lockBusy = false

    const handleEvent = (name, data) => {
      if (name === 'token') {
        setToolHint('')
        feedToken(data.content || '')
      } else if (name === 'tool_start') {
        // 工具轮正文不朗读；清掉尚未开播的碎句（正常情况下这里没有内容）
        sentenceBufRef.current = ''
        ttsRef.current.queue = []
        setToolHint(`正在${toolLabel(data.name)}`)
        setPhaseBoth('thinking')
      } else if (name === 'confirm') {
        const q = data.question || '请确认这个操作'
        // 清掉尚未开播的碎句，完整问题只播一遍
        ttsRef.current.queue = []
        sentenceBufRef.current = ''
        patchMessage(aid, { content: q, streaming: false, confirm: true })
        enqueueSpeech(stripForSpeech(q))
        finished = true
      } else if (name === 'final') {
        const text2 = data.content || ''
        const cur = messagesRef.current.find((m) => m.id === aid)
        // token 流没收到内容时（非流式降级后端）用 final 文本兜底
        if (text2 && !(cur && cur.content)) patchMessage(aid, { content: text2 })
        patchMessage(aid, { streaming: false })
        flushSentences()
        finished = true
      } else if (name === 'error') {
        const msg = data.message || '出错了，请再说一次'
        // barge-in 打断时旧轮可能正卡在工具执行里、锁未释放：
        // 自动延迟重试（复用同一对气泡，不重复刷屏），最多 4 次
        if (msg.includes('上一轮') && retry < 4) {
          lockBusy = true
          patchMessage(aid, { content: '正在接过话头…', streaming: true, isError: false })
          setTimeout(() => runTurn(text, aid, retry + 1), 600)
          finished = true
          return
        }
        patchMessage(aid, {
          content: msg, streaming: false, isError: true,
        })
        showBanner(msg)
        newSpeakingGeneration()
        setPhaseBoth('listening')
        finished = true
      }
    }

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        sseBuf += decoder.decode(value, { stream: true })
        const lines = sseBuf.split('\n')
        sseBuf = lines.pop() || ''
        for (const line of lines) {
          if (line.startsWith('event: ')) eventName = line.slice(7).trim()
          else if (line.startsWith('data: ')) {
            try { handleEvent(eventName, JSON.parse(line.slice(6))) } catch {}
            eventName = ''
          }
        }
        if (finished) { try { reader.cancel() } catch {}; break }
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        patchMessage(aid, { content: '连接中断，请重试', streaming: false, isError: true })
      }
    }
    if (!finished && !lockBusy) {
      patchMessage(aid, (m) => ({ ...m, streaming: false }))
      if (phaseRef.current === 'thinking') {
        flushSentences()
        if (!ttsRef.current.queue.length && !ttsRef.current.audio) setPhaseBoth('listening')
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── STT 引擎回调（半双工：整个 AI 回合都不听）──────────────────
  const handleUtterance = useCallback((text) => {
    const p = phaseRef.current
    if (p === 'idle' || p === 'connecting') return
    // 半双工：AI 回合内（思考/调用工具/出声）一律不接受输入。
    // 免费/云端识别的拾音都没有可靠 AEC，外放会被识别成"用户在插话"；
    // 而且识别器对同一段音频会重复提交（日志里同一句 4 秒发 5 轮），
    // 每来一次就 abort 一次 → TTS 永远起不来。所以整轮一律丢弃。
    if (p === 'thinking' || p === 'speaking') { engineRef.current?.discardTurn?.(); return }
    // AI 刚说完的残留窗口：外放尾音还可能在识别缓冲里
    if (Date.now() - speakEndAtRef.current < 1200) { engineRef.current?.discardTurn?.(); return }
    // 识别器重复提交同一句（continuous 模式的已知行为）→ 丢弃
    const norm = (text || '').replace(/\s/g, '')
    const last = lastUtterRef.current
    if (norm && norm === last.text && Date.now() - last.at < 3000) return
    lastUtterRef.current = { text: norm, at: Date.now() }
    engineRef.current && engineRef.current.resetTurn()
    sendUtterance(text)
  }, [sendUtterance])

  // AI 回合内不接受输入，这里只留空（旧逻辑会在 thinking 阶段打断自己）
  const handleSpeechStart = useCallback(() => {}, [])

  /** 半双工下的手动打断：停掉当前播放，回到聆听 */
  const interruptSpeaking = useCallback(() => {
    abortFetch(); newSpeakingGeneration()
    sentenceBufRef.current = ''
    setPhaseBoth('listening')
  }, [abortFetch, newSpeakingGeneration, setPhaseBoth])

  // ── 通话生命周期 ────────────────────────────────────────────────
  // 端到端全双工
  const startRealtime = useCallback(async () => {
    const session = new RealtimeSession()
    let curUser = null
    let curAi = null

    const ensureUser = (text) => {
      if (curUser) {
        patchMessage(curUser, { content: text })
        return curUser
      }
      const id = `u${++_msgSeq}`
      appendMessage({ id, role: 'user', content: text })
      curUser = id
      return id
    }
    const ensureAi = () => {
      if (curAi) return curAi
      const id = `a${++_msgSeq}`
      appendMessage({ id, role: 'assistant', content: '', streaming: true })
      curAi = id
      return id
    }
    // 用户说完一句 → 封定当前用户气泡，下一句开新气泡
    const commitUser = () => { curUser = null }
    // 一轮 AI 输出结束 → 封定 AI 气泡
    const commitAi = () => {
      if (curAi) patchMessage(curAi, { streaming: false })
      curAi = null
    }

    session.onState = (s) => {
      // realtime 状态映射：connecting/listening/thinking/speaking
      setPhaseBoth(s === 'connecting' ? 'connecting' : s)
    }
    session.onLevel = (v) => {
      levelSmoothRef.current = levelSmoothRef.current * 0.6 + v * 0.4
      setLevel(levelSmoothRef.current)
    }
    session.onUserTranscript = (t) => {
      console.info('[Voice] user >>', t)
      setPartial('')
      ensureUser(t)
      commitUser()
    }
    session.onAssistantTranscript = (text, isDelta) => {
      const id = ensureAi()
      if (isDelta) {
        const cur = messagesRef.current.find((m) => m.id === id)
        patchMessage(id, { content: (cur?.content || '') + text, streaming: true })
      } else {
        if (text) console.info('[Voice] assistant <<', text)
        patchMessage(id, { content: text, streaming: false })
        commitAi()
      }
    }
    session.onTool = ({ phase, name }) => {
      if (phase === 'start') {
        console.info('[Voice] tool start:', name)
        commitAi()
        setToolHint(`正在${toolLabel(name)}`)
      } else {
        console.info('[Voice] tool done:', name)
        setToolHint('')
      }
    }
    session.onError = (msg) => {
      console.warn('[Voice] realtime error:', msg)
      showBanner(msg, 4000)
    }
    session.onClose = () => {
      console.info('[Voice] realtime session closed by server')
      if (phaseRef.current !== 'idle') showBanner('实时通话已断开', 3000)
    }
    // 就绪前连不上（Key 失效/网络问题）→ 自动降级到标准语音模式
    let downgraded = false
    session.onFatalDowngrade = async (reason) => {
      if (downgraded) return
      downgraded = true
      const wasProvider = channelRef.current?.provider
      console.warn('[Voice] realtime downgrade:', reason, 'provider=', wasProvider)
      try { session.stop() } catch {}
      if (realtimeRef.current === session) realtimeRef.current = null
      const vc = voiceCfgRef.current
      const fb = {
        mode: 'fallback',
        stt: vc?.stt_cloud ? 'cloud' : 'browser',
        reason: String(reason || '实时通道连接失败'),
        provider: wasProvider,
      }
      channelRef.current = fb
      setRtMode(false); modeRef.current = 'fallback'
      setRtAvailable(false)
      setChannel(fb)
      const pName = wasProvider === 'volc' ? '火山豆包' : '千问'
      // 标准引擎启动也可能失败（浏览器无识别能力/麦克风权限被拒/设备占用）。
      // 必须收口，否则界面会永远停在"正在接通"；成功路径行为保持不变。
      if (!vc) {
        showBanner(`${pName}实时通道不可用（${reason}），且标准语音配置缺失`, 6000)
        setPhaseBoth('idle')
        return
      }
      showBanner(`${pName}实时通道不可用（${reason}），已切换标准语音模式`, 6000)
      try {
        await startFallback(vc)
      } catch (e) {
        console.warn('[Voice] fallback engine start failed after downgrade:', e)
        modeRef.current = 'fallback'
        showBanner(`已切到标准语音模式，但麦克风启动失败：${e?.message || e}`, 6000)
        setPhaseBoth('idle')
      }
    }

    await session.start(getWsUrl('/ws/voice'))
    realtimeRef.current = session
    modeRef.current = 'realtime'
    setRtMode(true)
    // 服务端 ready 事件会切到 listening；先标个态
    setPhaseBoth('connecting')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appendMessage, patchMessage, setPhaseBoth, showBanner])

  // 拼装模式（edge-tts + 浏览器/云端识别）
  const startFallback = useCallback(async (voiceCfg) => {
    modeRef.current = 'fallback'
    setRtMode(false)
    const engine = voiceCfg.stt_cloud ? new CloudVadEngine() : new WebSpeechEngine()
    engine.onLevel = (v) => {
      levelSmoothRef.current = levelSmoothRef.current * 0.6 + v * 0.4
      setLevel(levelSmoothRef.current)
    }
    engine.onSpeechStart = handleSpeechStart
    engine.onPartial = (t) => setPartial(t)
    engine.onUtterance = handleUtterance
    engine.onError = (msg) => showBanner(msg, 3000)
    engine.speakingGate = () => phaseRef.current === 'speaking'
    await engine.start()
    engineRef.current = engine
    setPhaseBoth('listening')
  }, [handleSpeechStart, handleUtterance, setPhaseBoth, showBanner])

  const startCall = useCallback(async () => {
    setBanner(''); setMessages([]); messagesRef.current = []
    setToolHint(''); setPartial('')
    setPhaseBoth('connecting')
    try {
      const resp = await fetch(`${API_BASE}/api/voice/config`)
      const voiceCfg = await resp.json()
      if (!voiceCfg.ok) throw new Error(voiceCfg.error || '语音服务不可用')
      setCfg(voiceCfg)
      voiceCfgRef.current = voiceCfg
      if (!voice) {
        setVoice(voiceCfg.tts_voice)
        localStorage.setItem('voiceTtsVoice', voiceCfg.tts_voice)
      }

      // 屏幕常亮
      try {
        if ('wakeLock' in navigator) {
          wakeLockRef.current = await navigator.wakeLock.request('screen')
        }
      } catch {}

      // 优先端到端全双工实时通道
      let rtInfo = null
      try {
        const r = await fetch(`${API_BASE}/api/voice/realtime/config`)
        rtInfo = await r.json()
      } catch { rtInfo = null }
      const ch = deriveChannel(rtInfo, voiceCfg)
      channelRef.current = ch
      setChannel(ch)
      setRtAvailable(ch.mode === 'realtime')
      console.info('[Voice] call channel:', ch)

      if (ch.mode === 'realtime') {
        await startRealtime()
      } else {
        // 不再静默走 A 档：明确告诉用户为什么没用实时通道
        const sttName = ch.stt === 'cloud' ? '云端识别' : '浏览器识别'
        showBanner(`本次使用标准语音模式（${sttName}）：${ch.reason}`, 6000)
        await startFallback(voiceCfg)
      }
    } catch (e) {
      console.warn('[Voice] start call failed:', e)
      showBanner(e.message || '麦克风启动失败')
      setPhaseBoth('idle')
    }
  }, [setPhaseBoth, showBanner, voice, startRealtime, startFallback])

  const endCall = useCallback(() => {
    clearTimeout(resumeTimerRef.current)
    try { realtimeRef.current && realtimeRef.current.stop() } catch {}
    realtimeRef.current = null
    try { engineRef.current && engineRef.current.stop() } catch {}
    engineRef.current = null
    abortFetch()
    newSpeakingGeneration()
    try { wakeLockRef.current && wakeLockRef.current.release() } catch {}
    wakeLockRef.current = null
    setPhaseBoth('idle')
    setRtMode(false); modeRef.current = 'fallback'
    setPartial(''); setToolHint(''); setLevel(0)
    if (onClose) onClose()
  }, [abortFetch, newSpeakingGeneration, onClose, setPhaseBoth])

  const restartConversation = useCallback(() => {
    if (modeRef.current === 'realtime' && realtimeRef.current) {
      // 实时模式：重连一条干净会话
      const wasRt = true
      try { realtimeRef.current.stop() } catch {}
      realtimeRef.current = null
      setMessages([]); messagesRef.current = []
      setPartial(''); setToolHint('')
      if (wasRt) startRealtime()
      return
    }
    fetch(`${API_BASE}/api/voice/reset`, { method: 'POST' }).catch(() => {})
    abortFetch(); newSpeakingGeneration()
    setMessages([]); messagesRef.current = []
    setPartial(''); setToolHint('')
    sentenceBufRef.current = ''
    if (phaseRef.current !== 'idle') setPhaseBoth('listening')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [abortFetch, newSpeakingGeneration, setPhaseBoth, startRealtime])

  // 屏幕常亮：页面回到前台时重新申请
  useEffect(() => {
    const onVis = () => {
      if (phaseRef.current !== 'idle' && 'wakeLock' in navigator && !wakeLockRef.current) {
        navigator.wakeLock.request('screen').then((w) => { wakeLockRef.current = w }).catch(() => {})
      }
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  // ── 渲染 ────────────────────────────────────────────────────────
  const inCall = phase !== 'idle'
  const badge = channelBadge(channel)
  const coreColor = phase === 'thinking'
    ? 'from-amber-300 to-orange-500'
    : phase === 'speaking'
      ? 'from-emerald-300 to-teal-500'
      : 'from-teal-300 to-emerald-500'
  const glowColor = phase === 'thinking' ? 'bg-amber-400/25' : 'bg-emerald-400/25'
  const bars = [0.55, 0.8, 1.15, 0.8, 0.55]

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
          className="fixed inset-0 z-[300] flex items-center justify-center bg-black/60 backdrop-blur-md sm:p-6"
        >
          <div className="relative flex h-full w-full flex-col overflow-hidden bg-gradient-to-b from-stone-900 via-stone-900 to-stone-950 sm:h-[min(880px,calc(100vh-3rem))] sm:max-w-[430px] sm:rounded-[36px] sm:border sm:border-white/10 sm:shadow-2xl">

            {/* 顶部栏 */}
            <div className="flex items-center justify-between px-5 pt-5 pb-2">
              <div className="flex items-center gap-2">
                <span className={`h-2 w-2 rounded-full ${inCall ? 'bg-emerald-400' : 'bg-stone-500'}`} />
                <span className="text-[15px] font-medium text-stone-200">摘星语音</span>
                {badge && (
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium tracking-wide ring-1 ${badge.cls}`}
                    title={channel?.mode === 'fallback' && channel.reason
                      ? `未使用实时通道：${channel.reason}`
                      : '本次通话实际使用的语音通道'}>
                    {badge.text}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                {/* 音色（仅标准/降级模式显示；实时模式音色在系统配置里选） */}
                {cfg && channel?.mode === 'fallback' && (
                <div className="relative">
                  <button
                    onClick={() => setShowVoices((v) => !v)}
                    className="flex items-center gap-1 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-stone-300 transition hover:bg-white/10"
                  >
                    {(cfg?.voices || []).find((v) => v.id === voice)?.name?.split(' ')[0] || '晓晓'}
                    <CaretDown size={12} weight="bold" />
                  </button>
                  {showVoices && (
                    <div className="absolute right-0 top-9 z-10 w-52 overflow-hidden rounded-2xl border border-white/10 bg-stone-800 shadow-xl">
                      {(cfg?.voices || []).map((v) => (
                        <button
                          key={v.id}
                          onClick={() => {
                            setVoice(v.id)
                            localStorage.setItem('voiceTtsVoice', v.id)
                            setShowVoices(false)
                          }}
                          className={`block w-full px-4 py-2.5 text-left text-[13px] transition hover:bg-white/10 ${
                            voice === v.id ? 'text-emerald-300' : 'text-stone-300'
                          }`}
                        >
                          {v.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                )}
                {/* 重新开始 */}
                <button
                  onClick={restartConversation}
                  title="清空对话记忆，重新开始"
                  className="flex h-8 w-8 items-center justify-center rounded-full border border-white/10 bg-white/5 text-stone-300 transition hover:bg-white/10"
                >
                  <ArrowCounterClockwise size={15} />
                </button>
                {/* 挂断（通话中）/ 关闭（空闲态） */}
                {inCall ? (
                  <motion.button
                    initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
                    onClick={endCall}
                    title="结束通话"
                    className="flex h-9 w-9 items-center justify-center rounded-full bg-red-500 text-white shadow-lg shadow-red-500/30 transition hover:bg-red-400 active:scale-90"
                  >
                    <PhoneDisconnect size={17} weight="fill" />
                  </motion.button>
                ) : (
                  <button
                    onClick={endCall}
                    title="关闭"
                    className="flex h-9 w-9 items-center justify-center rounded-full border border-white/10 bg-white/5 text-stone-300 transition hover:bg-white/10 active:scale-90"
                  >
                    <X size={17} weight="bold" />
                  </button>
                )}
              </div>
            </div>

            {/* 中央视觉区 */}
            <div className="relative flex flex-1 flex-col items-center justify-center px-6">
              <div className="relative flex h-56 w-56 items-center justify-center">
                {inCall && (
                  <motion.div
                    className={`absolute -inset-6 rounded-full ${glowColor} blur-3xl`}
                    animate={{
                      scale: phase === 'thinking' ? [1, 1.12, 1] : [1, 1.18, 1],
                      opacity: phase === 'listening' ? [0.5, 0.9, 0.5] : [0.7, 1, 0.7],
                    }}
                    transition={{
                      duration: phase === 'thinking' ? 1.0 : 2.0,
                      repeat: Infinity, ease: 'easeInOut',
                    }}
                  />
                )}
                <motion.div
                  animate={inCall ? {
                    scale: phase === 'thinking' ? [1, 1.05, 1] : [1, 1.08, 1],
                  } : { scale: 1 }}
                  transition={{ duration: 1.4, repeat: inCall ? Infinity : 0, ease: 'easeInOut' }}
                  className={`relative flex h-36 w-36 items-center justify-center rounded-full bg-gradient-to-br ${
                    inCall ? coreColor : 'from-stone-700 to-stone-800'
                  } shadow-2xl`}
                >
                  {phase === 'thinking' ? (
                    <motion.span
                      animate={{ rotate: 360 }}
                      transition={{ duration: 1.1, repeat: Infinity, ease: 'linear' }}
                      className="h-9 w-9 rounded-full border-[3px] border-white/30 border-t-white"
                    />
                  ) : (
                    <Microphone size={46} weight="fill" className={inCall ? 'text-white' : 'text-stone-400'} />
                  )}
                </motion.div>
              </div>

              {/* 音浪 */}
              <div className="mt-7 flex h-10 items-center gap-1.5">
                {bars.map((mul, i) => {
                  const h = inCall
                    ? phase === 'thinking'
                      ? 10 + (i === 2 ? 14 : 6)
                      : 10 + (level * 42 + (phase === 'speaking' ? 8 : 4)) * mul
                    : 6
                  return (
                    <motion.span
                      key={i}
                      animate={{ height: h }}
                      transition={{ type: 'spring', stiffness: 320, damping: 22 }}
                      className={`w-1.5 rounded-full ${
                        phase === 'thinking' ? 'bg-amber-300/80' : inCall ? 'bg-emerald-300/80' : 'bg-stone-600'
                      }`}
                      style={{ height: h }}
                    />
                  )
                })}
              </div>

              <div className="mt-5 flex h-12 flex-col items-center gap-1">
                <span className="text-[15px] font-medium text-stone-200">
                  {phase === 'idle' ? '摘星语音助手' : PHASE_TEXT[phase]}
                </span>
                {toolHint && phase === 'thinking' && (
                  <motion.span
                    initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                    className="flex items-center gap-1 text-[13px] text-emerald-300/90"
                  >
                    <Sparkle size={13} weight="fill" />
                    {toolHint}…
                  </motion.span>
                )}
                {/* 标准模式是半双工：AI 回合内不收麦克风，打断/取消要手动点 */}
                <AnimatePresence>
                  {(phase === 'speaking' || phase === 'thinking') && channel?.mode !== 'realtime' && (
                    <motion.button
                      initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
                      onClick={interruptSpeaking}
                      className="rounded-full border border-white/15 bg-white/10 px-3.5 py-1 text-[12px] text-stone-200 transition hover:bg-white/15 active:scale-95"
                    >
                      {phase === 'thinking' ? '取消这轮' : '点这里打断'}
                    </motion.button>
                  )}
                </AnimatePresence>
              </div>

              {phase === 'idle' && (
                <motion.button
                  whileHover={{ scale: 1.04 }}
                  whileTap={{ scale: 0.95 }}
                  onClick={startCall}
                  className="mt-4 flex items-center gap-2 rounded-full bg-gradient-to-r from-emerald-400 to-teal-500 px-7 py-3.5 text-[15px] font-semibold text-stone-900 shadow-xl shadow-emerald-500/25"
                >
                  <Waveform size={19} weight="fill" />
                  开始通话
                </motion.button>
              )}
            </div>

            {/* 提示横幅 */}
            <AnimatePresence>
              {banner && (
                <motion.div
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: 8 }}
                  className="mx-5 mb-2 flex items-start gap-2 rounded-2xl border border-amber-300/20 bg-amber-400/10 px-4 py-2.5 text-[13px] text-amber-100"
                >
                  <Warning size={16} weight="fill" className="mt-0.5 shrink-0" />
                  <span>{banner}</span>
                </motion.div>
              )}
            </AnimatePresence>

            {/* 字幕区 */}
            <div className="px-4 pb-5">
              <div
                ref={listRef}
                className="h-36 space-y-2.5 overflow-y-auto rounded-3xl border border-white/5 bg-white/[0.03] px-4 py-3 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
              >
                {messages.length === 0 && !partial && (
                  <p className="pt-9 text-center text-[13px] leading-6 text-stone-500">
                    试试说：<br />
                    「帮我总结一下昨天的内容」<br />
                    「昨天有什么重要的公众号更新」<br />
                    {channel?.mode === 'realtime'
                      ? '回答时直接开口就能打断我'
                      : '我回答时点一下「打断」再说话'}
                  </p>
                )}
                {messages.map((m) => (
                  <div key={m.id} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                    <div
                      className={`max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-[13.5px] leading-relaxed ${
                        m.role === 'user'
                          ? 'rounded-br-md bg-emerald-500/90 text-stone-900'
                          : m.isError
                            ? 'rounded-bl-md bg-red-500/15 text-red-200'
                            : 'rounded-bl-md bg-white/[0.07] text-stone-200'
                      }`}
                    >
                      {m.content || (m.streaming ? '…' : '')}
                      {m.confirm && (
                        <div className="mt-2 flex gap-2">
                          <button
                            onClick={() => handleUtterance('确定')}
                            className="rounded-full bg-emerald-500 px-4 py-1 text-xs font-medium text-stone-900 active:scale-95"
                          >
                            确定
                          </button>
                          <button
                            onClick={() => handleUtterance('取消')}
                            className="rounded-full bg-white/10 px-4 py-1 text-xs text-stone-200 active:scale-95"
                          >
                            取消
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
                {partial && (
                  <div className="flex justify-end">
                    <div className="max-w-[85%] rounded-2xl rounded-br-md border border-emerald-400/30 px-3.5 py-2 text-[13.5px] italic text-emerald-200/80">
                      {partial}
                    </div>
                  </div>
                )}
              </div>
              <p className="mt-2 text-center text-[11px] leading-4 text-stone-600">
                {channel?.mode === 'realtime'
                  ? `端到端实时全双工（${channel.provider === 'volc' ? '火山豆包 Seeduplex' : '千问 Realtime'}）· 像打电话一样自然对话，随时开口就能打断我`
                  : channel?.mode === 'fallback'
                    ? `${channel.stt === 'cloud' ? '云端语音识别' : '浏览器语音识别 · 推荐 Edge 获得最佳效果'} · 我说话时不收音，点「打断」可插话`
                    : '正在探测可用语音通道…'}
              </p>
              {channel?.mode === 'fallback' && channel.reason && (
                <p className="mt-1 text-center text-[11px] leading-4 text-amber-500/70">
                  未使用实时全双工：{channel.reason}
                </p>
              )}
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
