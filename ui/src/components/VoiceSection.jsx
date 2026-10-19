import { useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Lightning, CircleNotch, CheckCircle, Warning, CaretDown,
  PhoneOutgoing, Stack,
} from '@phosphor-icons/react'
import { API_BASE, Field, Toggle, Input, Select, ComboInput } from './SharedComponents'

// ── B 档模型：常用预设（均已在千问平台实测可连）；字段本身允许手填 ──
const MODELS = [
  { value: 'qwen3.8-omni-flash-realtime', desc: 'Qwen3.8 Omni Flash（默认）' },
  { value: 'qwen3.5-omni-flash-realtime', desc: 'Qwen3.5 Omni Flash' },
  { value: 'qwen3.5-omni-plus-realtime', desc: 'Qwen3.5 Omni Plus' },
  { value: 'qwen-audio-3.1-realtime-plus', desc: 'Qwen Audio 3.1 Plus' },
  { value: 'qwen-audio-3.0-realtime-plus', desc: 'Qwen Audio 3.0 Plus' },
  { value: 'qwen-audio-3.0-realtime-flash', desc: 'Qwen Audio 3.0 Flash' },
]
// Omni 家族音色（session.audio.output.voice）
const OMNI_VOICES = [
  { value: 'Tina', desc: '女声（默认）' },
  { value: 'Cici', desc: '女声' },
  { value: 'Mia', desc: '女声' },
  { value: 'Serena', desc: '女声' },
  { value: 'Maia', desc: '女声' },
  { value: 'Evan', desc: '男声' },
  { value: 'Zane', desc: '男声' },
  { value: 'Raymond', desc: '男声' },
  { value: 'Aiden', desc: '男声' },
  { value: 'Theo Calm', desc: '男声' },
]
// Qwen-Audio 家族音色（顶层 voice）
const AUDIO_VOICES = [
  { value: 'longanqian_v3.1', desc: '女声（默认）' },
  { value: 'long_v3.1', desc: '男声' },
  { value: 'longcheng_v3.1', desc: '男声' },
  { value: 'longshu_v3.1', desc: '女声' },
  { value: 'longjing_v3.1', desc: '女声' },
  { value: 'longmiao_v3.1', desc: '女声' },
]
const TURNS = [
  { value: 'smart_turn', desc: '语义断句' },
  { value: 'server_vad', desc: '声学停顿断句' },
]
const QWEN_TTS_VOICES = [
  { value: 'Cherry', desc: '女声（默认）' },
  { value: 'Serena', desc: '女声' },
  { value: 'Ethan', desc: '男声' },
]

const CARD = 'rounded-2xl border border-border-main bg-bg-card p-5 shadow-sm'

function Segmented({ value, onChange, options, disabled }) {
  return (
    <div className={`flex gap-1 rounded-xl bg-bg-raised p-1 ${disabled ? 'opacity-50 pointer-events-none' : ''}`}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={`flex-1 rounded-lg px-3 py-2 text-[12.5px] font-medium transition ${
            value === o.value
              ? 'bg-bg-card text-emerald-600 dark:text-emerald-400 shadow-sm'
              : 'text-text-muted hover:text-text-main'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

function CollapseHeader({ icon, title, subtitle, open, onToggle, badge }) {
  return (
    <button type="button" onClick={onToggle}
      className="flex w-full items-center gap-3 text-left">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-emerald-500/15 text-emerald-500">
        {icon}
      </div>
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <h4 className="text-[14px] font-semibold text-text-main">{title}</h4>
          {badge}
        </div>
        <p className="text-[12px] text-text-muted">{subtitle}</p>
      </div>
      <CaretDown size={16}
        className={`text-text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
    </button>
  )
}

export default function VoiceSection() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)
  // 是否有改动尚未保存（驱动吸底操作条上的提示）
  const [dirty, setDirty] = useState(false)
  const [openB, setOpenB] = useState(true)
  const [openA, setOpenA] = useState(true)
  // 折叠展开动画进行中才裁剪（overflow-hidden），结束后放开，避免内部 Select 下拉被裁掉
  const [collapseAnim, setCollapseAnim] = useState(true)

  // 总开关
  const [voiceEnabled, setVoiceEnabled] = useState(true)
  // B 档
  const [rtProvider, setRtProvider] = useState('qwen')       // 分段控件本地选择（保存后生效）
  const [storedProvider, setStoredProvider] = useState('qwen') // 服务端已保存、实际通话使用
  // 两家各自独立的启用开关
  const [qwenEnabled, setQwenEnabled] = useState(false)
  const [volcEnabled, setVolcEnabled] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [hasKey, setHasKey] = useState(false)
  const [keyHint, setKeyHint] = useState('')
  const [model, setModel] = useState(MODELS[0].value)
  const [voice, setVoice] = useState('Tina')
  const [turnMode, setTurnMode] = useState('smart_turn')
  // 两家的就绪状态（开关开 + Key 已保存），来自服务端
  const [qwenReady, setQwenReady] = useState(false)
  const [volcReady, setVolcReady] = useState(false)
  const [volcVoices, setVolcVoices] = useState([])
  const [volcApiKey, setVolcApiKey] = useState('')
  const [volcHasKey, setVolcHasKey] = useState(false)
  const [volcKeyHint, setVolcKeyHint] = useState('')
  const [volcVoice, setVolcVoice] = useState('zh_female_vv_uranus_bigtts')
  // A 档
  const [standardEnabled, setStandardEnabled] = useState(true)
  const [sttProvider, setSttProvider] = useState('browser')
  const [sttBaseUrl, setSttBaseUrl] = useState('')
  const [sttApiKey, setSttApiKey] = useState('')
  const [sttHasKey, setSttHasKey] = useState(false)
  const [sttModel, setSttModel] = useState('whisper-1')
  const [ttsProvider, setTtsProvider] = useState('edge')
  const [ttsBaseUrl, setTtsBaseUrl] = useState('')
  const [ttsApiKey, setTtsApiKey] = useState('')
  const [ttsHasKey, setTtsHasKey] = useState(false)
  const [ttsModel, setTtsModel] = useState('tts-1')
  const [ttsVoice, setTtsVoice] = useState('zh-CN-XiaoxiaoNeural')
  const [qwenTtsModel, setQwenTtsModel] = useState('qwen3-tts-flash-realtime')
  const [qwenTtsVoice, setQwenTtsVoice] = useState('Cherry')
  const [ttsRate, setTtsRate] = useState('+0%')

  // 包装 setter：用户任何改动都标记为"未保存"，并清掉上一次的"已保存"提示
  const bind = (setter) => (v) => { setter(v); setDirty(true); setSaved(false) }

  useEffect(() => {
    fetch(`${API_BASE}/api/voice/realtime/config`)
      .then((r) => r.json())
      .then((d) => {
        if (!d.ok) return
        setVoiceEnabled(d.voice_enabled !== false)
        const p = d.rt_provider === 'volc' ? 'volc' : 'qwen'
        setRtProvider(p); setStoredProvider(p)
        setQwenEnabled(!!d.qwen_enabled)
        setVolcEnabled(!!d.volc_enabled)
        setQwenReady(!!d.qwen_ready)
        setVolcReady(!!d.volc_ready)
        setHasKey(!!d.has_key); setKeyHint(d.key_hint || '')
        if (d.model) setModel(d.model)
        if (d.voice) setVoice(d.voice)
        if (d.turn_mode) setTurnMode(d.turn_mode)
        setVolcVoices(d.volc_voices || [])
        setVolcHasKey(!!d.volc_has_key); setVolcKeyHint(d.volc_key_hint || '')
        if (d.volc_voice) setVolcVoice(d.volc_voice)
        setStandardEnabled(d.standard_enabled !== false)
        setSttProvider(d.stt_provider || 'browser')
        setSttBaseUrl(d.stt_base_url || ''); setSttHasKey(!!d.stt_has_key)
        setSttModel(d.stt_model || 'whisper-1')
        setTtsProvider(d.tts_provider || 'edge')
        setTtsBaseUrl(d.tts_base_url || ''); setTtsHasKey(!!d.tts_has_key)
        setTtsModel(d.tts_model || 'tts-1')
        setTtsVoice(d.tts_voice || 'zh-CN-XiaoxiaoNeural')
        setQwenTtsModel(d.tts_qwen_model || 'qwen3-tts-flash-realtime')
        setQwenTtsVoice(d.tts_qwen_voice || 'Cherry')
        setTtsRate(d.tts_rate || '+0%')
        // 任一厂商已启用就默认展开 B 档
        const anyB = !!(d.qwen_enabled || d.volc_enabled)
        setOpenB(anyB); setOpenA(!anyB)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // 切换模型家族时，音色若不属于该家族则自动切到家族默认
  const onModelChange = (m) => {
    setModel(m)
    setDirty(true)
    setSaved(false)
    const list = m.includes('omni') ? OMNI_VOICES : AUDIO_VOICES
    if (!list.some((v) => v.value === voice)) {
      setVoice(list[0].value)
    }
  }

  // 切换实时服务商：只改本地选择，保存后才真正生效（徽标会明示）
  const selectRtProvider = (p) => {
    setRtProvider(p)
    setDirty(true)
    setSaved(false)
    setTestResult(null)
  }

  function payload() {
    return {
      voice_enabled: voiceEnabled,
      // B：两家独立开关；provider = 通话默认使用的一家
      rt_provider: rtProvider,
      qwen_enabled: qwenEnabled,
      volc_enabled: volcEnabled,
      // 千问
      api_key: apiKey || '********',
      model, voice, turn_mode: turnMode,
      // 火山
      volc_api_key: volcApiKey || '********',
      volc_voice: volcVoice,
      // A
      standard_enabled: standardEnabled,
      stt_provider: sttProvider, stt_base_url: sttBaseUrl,
      stt_api_key: sttApiKey || '********', stt_model: sttModel,
      tts_provider: ttsProvider, tts_base_url: ttsBaseUrl,
      tts_api_key: ttsApiKey || '********', tts_model: ttsModel,
      tts_voice: ttsVoice, tts_qwen_voice: qwenTtsVoice,
      tts_qwen_model: qwenTtsModel,
      // edge-tts 语速（仅 A 档 Edge 生效）
      tts_rate: ttsRate,
    }
  }

  async function save() {
    setSaving(true); setSaved(false); setTestResult(null)
    try {
      const r = await fetch(`${API_BASE}/api/voice/realtime/config`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload()),
      })
      const d = await r.json()
      if (d.ok) {
        setSaved(true)
        setDirty(false)
        setApiKey(''); setVolcApiKey(''); setSttApiKey(''); setTtsApiKey('')
        setHasKey(true); setVolcHasKey(true)
        setSttHasKey(sttProvider === 'openai' && !!sttBaseUrl)
        // 保存后：本地选择成为实际生效通道；就绪态跟随开关+Key（含本次新填的 Key）
        setStoredProvider(rtProvider)
        setQwenReady(qwenEnabled && (hasKey || !!apiKey))
        setVolcReady(volcEnabled && (volcHasKey || !!volcApiKey))
        setTimeout(() => setSaved(false), 2500)
      } else setTestResult({ ok: false, message: d.error || '保存失败' })
    } catch (e) {
      setTestResult({ ok: false, message: String(e) })
    } finally { setSaving(false) }
  }

  async function test() {
    setTesting(true); setTestResult(null)
    // 30 秒超时保护，避免请求挂起时用户完全无反馈
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 30000)
    try {
      const reqBody = rtProvider === 'volc'
        ? { provider: 'volc', volc_api_key: volcApiKey || '********', volc_voice: volcVoice }
        : { provider: 'qwen', api_key: apiKey || '********', model }
      const r = await fetch(`${API_BASE}/api/voice/realtime/test`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(reqBody), signal: ctrl.signal,
      })
      const d = await r.json()
      setTestResult({ ok: !!d.ok, message: d.ok ? d.message : d.error })
    } catch (e) {
      const msg = e.name === 'AbortError'
        ? '连接测试超时（30 秒无响应），请检查网络后重试'
        : `请求失败：${e.message || e}`
      setTestResult({ ok: false, message: msg })
    } finally { clearTimeout(timer); setTesting(false) }
  }

  if (loading) {
    return <div className="flex items-center gap-2 text-sm text-text-muted py-10 justify-center">
      <CircleNotch size={18} className="animate-spin" /> 加载语音配置…
    </div>
  }

  const voiceList = model.includes('omni') ? OMNI_VOICES : AUDIO_VOICES
  const dimmed = !voiceEnabled ? 'opacity-50 pointer-events-none select-none' : ''

  // ── B 档生效通道派生状态 ─────────────────────────────────────────
  const PROVIDER_LABEL = { qwen: '千问 Qwen Realtime', volc: '火山豆包 Seeduplex' }
  const providerDirty = rtProvider !== storedProvider
  const storedReady = storedProvider === 'volc' ? volcReady : qwenReady
  const anyBEnabled = qwenEnabled || volcEnabled

  // 某家厂商的配置状态：已就绪 / 缺 Key / 保存后生效 / 未启用
  const vendorState = (p) => {
    const on = p === 'volc' ? volcEnabled : qwenEnabled
    const hasK = p === 'volc' ? volcHasKey : hasKey
    const ready = p === 'volc' ? volcReady : qwenReady
    if (ready) return { text: '已就绪', cls: 'text-emerald-500' }
    if (!on) return { text: '未启用', cls: 'text-text-muted' }
    return { text: hasK ? '保存后生效' : '缺少 API Key', cls: 'text-amber-500' }
  }
  const qwenState = vendorState('qwen')
  const volcState = vendorState('volc')

  return (
    <div className="space-y-5">
      {/* ① 总开关 */}
      <div className={CARD}>
        <div className="flex items-center justify-between rounded-xl bg-bg-raised px-4 py-3.5">
          <div>
            <div className="text-[14px] font-semibold text-text-main">启用语音助手</div>
            <div className="text-[12px] text-text-muted mt-0.5">
              关闭后右下角麦克风入口隐藏；不影响微信/QQ 文字 Agent
            </div>
          </div>
          <Toggle enabled={voiceEnabled} onChange={bind(setVoiceEnabled)} />
        </div>
        <div className="mt-3 flex items-start gap-2 rounded-xl bg-emerald-500/8 px-3.5 py-2.5 text-[12px] leading-5 text-text-secondary">
          <Stack size={15} className="mt-0.5 shrink-0 text-emerald-500" />
          <span>
            优先级：<b className="text-text-main">实时全双工（B）</b> 与 <b className="text-text-main">标准模式（A）</b>
            都开启时，进通话优先走 B；B 连不上自动降级 A。只要任一通道可用就显示麦克风。
          </span>
        </div>
      </div>

      {/* ② B 档 实时全双工 */}
      <div className={`${CARD} ${dimmed}`}>
        <CollapseHeader
          icon={<Lightning size={20} weight="fill" />}
          title="B 档 · 实时全双工"
          subtitle="端到端语音模型，边听边说、可随时打断"
          open={openB} onToggle={() => setOpenB((v) => !v)}
          badge={storedReady
            ? <span className="rounded-full bg-emerald-400/15 px-2 py-0.5 text-[10px] font-medium text-emerald-500 ring-1 ring-emerald-400/30">
                生效中 · {storedProvider === 'volc' ? '火山' : '千问'}
              </span>
            : anyBEnabled
              ? <span className="rounded-full bg-amber-400/15 px-2 py-0.5 text-[10px] font-medium text-amber-500 ring-1 ring-amber-400/30">未就绪</span>
              : <span className="rounded-full bg-bg-raised px-2 py-0.5 text-[10px] text-text-muted">未启用</span>}
        />
        <AnimatePresence initial={false}>
          {openB && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }}
              onAnimationStart={() => setCollapseAnim(false)}
              onAnimationComplete={() => setCollapseAnim(true)}
              className={collapseAnim ? 'overflow-visible' : 'overflow-hidden'}>
              <div className="pt-4 space-y-4">
                <Field label="通话默认使用（保存后实际走这一条）">
                  <Segmented value={rtProvider} onChange={selectRtProvider}
                    options={[
                      { value: 'qwen', label: '千问 Qwen Realtime' },
                      { value: 'volc', label: '火山豆包 Seeduplex' },
                    ]} />
                </Field>

                {/* B 档语速不可调：端到端模型自行决定，避免用户误以为能像 A 档那样设置 */}
                <div className="rounded-xl bg-bg-raised px-3.5 py-2.5 text-[12px] leading-5 text-text-muted">
                  语速由实时模型（端到端）自行决定，不可调整；如需变速请在 A 档选择 Edge 合成。
                </div>

                {/* 当前生效通道明示：服务端状态为唯一来源，避免"以为用 B 实际走 A" */}
                {providerDirty ? (
                  <div className="flex items-start gap-2 rounded-xl bg-amber-500/10 px-3.5 py-2.5 text-[12px] leading-5 text-amber-600 dark:text-amber-400">
                    <Warning size={15} weight="bold" className="mt-0.5 shrink-0" />
                    <span>
                      切换待保存：点「保存全部配置」后，通话将使用
                      <b>「{PROVIDER_LABEL[rtProvider]}」</b>；当前实际生效仍是
                      <b>「{PROVIDER_LABEL[storedProvider]}」</b>。
                    </span>
                  </div>
                ) : storedReady ? (
                  <div className="flex items-start gap-2 rounded-xl bg-emerald-500/10 px-3.5 py-2.5 text-[12px] leading-5 text-emerald-600 dark:text-emerald-400">
                    <CheckCircle size={15} weight="fill" className="mt-0.5 shrink-0" />
                    <span>当前通话实际使用：<b>「{PROVIDER_LABEL[storedProvider]}」</b>，实时全双工已就绪。</span>
                  </div>
                ) : (
                  <div className="flex items-start gap-2 rounded-xl bg-amber-500/10 px-3.5 py-2.5 text-[12px] leading-5 text-amber-600 dark:text-amber-400">
                    <Warning size={15} weight="bold" className="mt-0.5 shrink-0" />
                    <span>
                      当前生效：<b>「{PROVIDER_LABEL[storedProvider]}」</b>，但状态为
                      <b>「{storedProvider === 'volc' ? volcState.text : qwenState.text}」</b>，
                      实时通道不可用时通话会自动回退到 A 档标准模式。
                    </span>
                  </div>
                )}

                {rtProvider === 'qwen' ? (
                  <>
                    {/* 千问独立开关（与火山互不影响，可同时启用） */}
                    <div className="flex items-center justify-between rounded-xl bg-bg-raised px-4 py-3">
                      <div>
                        <div className="text-[13.5px] font-medium text-text-main">启用千问实时全双工</div>
                        <div className="text-[12px] text-text-muted">
                          千问端到端实时模型，按音频 token 计费；两家开关互不影响
                        </div>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className={`text-[12px] font-medium ${qwenState.cls}`}>{qwenState.text}</span>
                        <Toggle enabled={qwenEnabled} onChange={bind(setQwenEnabled)} />
                      </div>
                    </div>

                    <Field label="API Key"
                      hint={hasKey && !apiKey ? `已配置（${keyHint}），留空不修改` : 'platform.qianwenai.com 创建'}>
                      <Input type="password" value={apiKey} onChange={bind(setApiKey)}
                        placeholder={hasKey ? '••••••••（已保存，留空不修改）' : '粘贴 sk- 开头的 Key'} />
                    </Field>

                    <Field label="实时模型">
                      <ComboInput value={model} onChange={onModelChange} options={MODELS} />
                    </Field>

                    <Field label="发音音色">
                      <ComboInput value={voice} onChange={bind(setVoice)} options={voiceList} />
                    </Field>

                    <Field label="轮次判断方式">
                      <Select variant="stack" value={turnMode} onChange={bind(setTurnMode)} options={TURNS} />
                    </Field>
                  </>
                ) : (
                  <>
                    {/* 火山独立开关（与千问互不影响，可同时启用） */}
                    <div className="flex items-center justify-between rounded-xl bg-bg-raised px-4 py-3">
                      <div>
                        <div className="text-[13.5px] font-medium text-text-main">启用火山豆包全双工</div>
                        <div className="text-[12px] text-text-muted">
                          Seeduplex 3.0，按音频时长计费；两家开关互不影响
                        </div>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className={`text-[12px] font-medium ${volcState.cls}`}>{volcState.text}</span>
                        <Toggle enabled={volcEnabled} onChange={bind(setVolcEnabled)} />
                      </div>
                    </div>

                    <Field label="火山 API Key"
                      hint={volcHasKey && !volcApiKey
                        ? `已配置（${volcKeyHint}），留空不修改`
                        : '火山控制台 → 豆包语音 → API Key 管理'}>
                      <Input type="password" value={volcApiKey} onChange={bind(setVolcApiKey)}
                        placeholder={volcHasKey ? '••••••••（已保存，留空不修改）' : '粘贴火山 X-Api-Key'} />
                    </Field>

                    <Field label="发音音色">
                      <ComboInput value={volcVoice} onChange={bind(setVolcVoice)}
                        options={volcVoices.length ? volcVoices : [
                          { value: 'zh_female_vv_uranus_bigtts', desc: 'Vivi 2.0 · 女声（默认）' },
                          { value: 'zh_female_xiaohe_uranus_bigtts', desc: '小何 2.0 · 女声' },
                          { value: 'zh_male_m191_uranus_bigtts', desc: '云舟 2.0 · 男声' },
                          { value: 'zh_male_taocheng_uranus_bigtts', desc: '小天 2.0 · 男声' },
                        ]} />
                    </Field>

                    <div className="rounded-xl bg-bg-raised px-3.5 py-2.5 text-[12px] leading-5 text-text-muted">
                      模型固定 1.2.6.1 全双工版，轮次由服务端自适应端点检测自动处理。
                    </div>
                  </>
                )}

                {/* 测试反馈：就地显示在按钮上方，点击后必有响应（进行中/成功/失败） */}
                {testing && (
                  <div className="flex items-center gap-2 rounded-xl bg-amber-500/10 px-3.5 py-2.5 text-[12.5px] text-amber-600 dark:text-amber-400">
                    <CircleNotch size={15} weight="bold" className="animate-spin shrink-0" />
                    <span>
                      正在连接{rtProvider === 'volc' ? '火山豆包' : '千问'}实时语音服务，通常 2~5 秒…
                    </span>
                  </div>
                )}
                {!testing && testResult && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }}
                    className={`flex items-start gap-2 rounded-xl px-3.5 py-2.5 text-[12.5px] ${
                      testResult.ok
                        ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                        : 'bg-red-500/10 text-red-600 dark:text-red-400'
                    }`}>
                    {testResult.ok
                      ? <CheckCircle size={16} weight="fill" className="mt-0.5 shrink-0" />
                      : <Warning size={16} weight="fill" className="mt-0.5 shrink-0" />}
                    <span>{testResult.message}</span>
                  </motion.div>
                )}

                <div className="flex items-center gap-3">
                  <button type="button" onClick={test} disabled={testing}
                    className="flex items-center gap-2 rounded-full border border-border-main bg-bg-raised px-4 py-2 text-[12.5px] font-medium text-text-main transition hover:bg-bg-hover disabled:opacity-60">
                    {testing
                      ? <><CircleNotch size={14} className="animate-spin" /> 测试中…</>
                      : '测试连接'}
                  </button>
                  {testResult?.ok && !testing && (
                    <span className="text-[12px] text-emerald-500">验证通过，记得保存</span>
                  )}
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* ③ A 档 标准模式 */}
      <div className={`${CARD} ${dimmed}`}>
        <CollapseHeader
          icon={<PhoneOutgoing size={19} weight="fill" />}
          title="A 档 · 标准模式（识别 + 你的 LLM + 合成）"
          subtitle="浏览器识别 + Edge 合成是免费通道；B 档不可用时自动走这里，也可单独使用"
          open={openA} onToggle={() => setOpenA((v) => !v)}
          badge={standardEnabled
            ? <span className="rounded-full bg-emerald-400/15 px-2 py-0.5 text-[10px] font-medium text-emerald-500 ring-1 ring-emerald-400/30">已启用</span>
            : <span className="rounded-full bg-bg-raised px-2 py-0.5 text-[10px] text-text-muted">未启用</span>}
        />
        <AnimatePresence initial={false}>
          {openA && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }}
              onAnimationStart={() => setCollapseAnim(false)}
              onAnimationComplete={() => setCollapseAnim(true)}
              className={collapseAnim ? 'overflow-visible' : 'overflow-hidden'}>
              <div className="pt-4 space-y-5">
                <div className="flex items-center justify-between rounded-xl bg-bg-raised px-4 py-3">
                  <div>
                    <div className="text-[13.5px] font-medium text-text-main">启用标准模式</div>
                    <div className="text-[12px] text-text-muted">作为 B 档降级通道</div>
                  </div>
                  <Toggle enabled={standardEnabled} onChange={bind(setStandardEnabled)} />
                </div>

                {/* STT */}
                <div className="space-y-3">
                  <div className="text-[13px] font-semibold text-text-main">耳朵 · 语音识别 STT</div>
                  <Segmented value={sttProvider} onChange={bind(setSttProvider)}
                    options={[
                      { value: 'browser', label: '浏览器识别（免费）' },
                      { value: 'openai', label: '云端识别' },
                    ]} />
                  {sttProvider === 'openai' && (
                    <div className="space-y-3 rounded-xl bg-bg-raised p-3.5">
                      <Field label="接口地址（OpenAI 兼容 /v1）">
                        <Input value={sttBaseUrl} onChange={bind(setSttBaseUrl)}
                          placeholder="https://api.siliconflow.cn/v1" />
                      </Field>
                      <Field label="API Key"
                        hint={sttHasKey && !sttApiKey ? '已配置，留空不修改' : ''}>
                        <Input type="password" value={sttApiKey} onChange={bind(setSttApiKey)}
                          placeholder={sttHasKey ? '••••••••（已保存）' : '云端识别 Key'} />
                      </Field>
                      <Field label="识别模型">
                        <Input value={sttModel} onChange={bind(setSttModel)} placeholder="FunAudioLLM/SenseVoiceSmall" />
                      </Field>
                    </div>
                  )}
                </div>

                {/* TTS */}
                <div className="space-y-3">
                  <div className="text-[13px] font-semibold text-text-main">嘴 · 语音合成 TTS</div>
                  <Segmented value={ttsProvider} onChange={bind(setTtsProvider)}
                    options={[
                      { value: 'edge', label: 'Edge 免费' },
                      { value: 'qwen', label: '千问流式' },
                      { value: 'openai', label: 'OpenAI 兼容' },
                    ]} />
                  {ttsProvider === 'edge' && (
                    <>
                      <Field label="Edge 音色">
                        <ComboInput value={ttsVoice} onChange={bind(setTtsVoice)} options={[
                          { value: 'zh-CN-XiaoxiaoNeural', desc: '晓晓 · 女声（默认）' },
                          { value: 'zh-CN-YunxiNeural', desc: '云希 · 男声' },
                          { value: 'zh-CN-XiaoyiNeural', desc: '晓伊 · 女声' },
                          { value: 'zh-CN-YunyangNeural', desc: '云扬 · 男声' },
                          { value: 'zh-CN-XiaoxuanNeural', desc: '晓萱 · 女声' },
                          { value: 'zh-CN-YunjianNeural', desc: '云健 · 男声' },
                        ]} />
                      </Field>
                      <Field label="语速（仅 Edge 生效）">
                        <Select variant="stack" value={ttsRate} onChange={bind(setTtsRate)} options={[
                          { value: '-20%', desc: '慢 · -20%' },
                          { value: '-10%', desc: '略慢 · -10%' },
                          { value: '+0%', desc: '默认 · 不变速' },
                          { value: '+15%', desc: '略快 · +15%' },
                          { value: '+30%', desc: '快 · +30%' },
                        ]} />
                      </Field>
                    </>
                  )}
                  {ttsProvider === 'qwen' && (
                    <div className="space-y-3 rounded-xl bg-bg-raised p-3.5">
                      <p className="text-[12px] text-text-muted">
                        复用上方 B 档的千问 Key。
                      </p>
                      <Field label="合成模型">
                        <Input value={qwenTtsModel} onChange={bind(setQwenTtsModel)}
                          placeholder="qwen3-tts-flash-realtime" />
                      </Field>
                      <Field label="音色">
                        <ComboInput value={qwenTtsVoice} onChange={bind(setQwenTtsVoice)}
                          options={QWEN_TTS_VOICES} />
                      </Field>
                    </div>
                  )}
                  {ttsProvider === 'openai' && (
                    <div className="space-y-3 rounded-xl bg-bg-raised p-3.5">
                      <Field label="接口地址（OpenAI 兼容 /v1）">
                        <Input value={ttsBaseUrl} onChange={bind(setTtsBaseUrl)}
                          placeholder="https://api.example.com/v1" />
                      </Field>
                      <Field label="API Key"
                        hint={ttsHasKey && !ttsApiKey ? '已配置，留空不修改' : ''}>
                        <Input type="password" value={ttsApiKey} onChange={bind(setTtsApiKey)}
                          placeholder={ttsHasKey ? '••••••••（已保存）' : '云端合成 Key'} />
                      </Field>
                      <Field label="合成模型">
                        <Input value={ttsModel} onChange={bind(setTtsModel)} placeholder="tts-1" />
                      </Field>
                    </div>
                  )}
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <div className="rounded-2xl border border-border-main bg-bg-card/60 p-4">
        <p className="text-[12px] leading-6 text-text-muted">
          两条通道与微信/QQ 文字 Agent 相互独立：实时模型与标准模式都只以只读方式借用查询工具
          （查推送、查摘要、联网搜索等），不触发添加预警、新建定时任务等写操作，不影响原有文字对话和记忆。
        </p>
      </div>

      {/* 保存：吸底常驻，滚动时不消失；有未保存改动时给出提示 */}
      <div className="sticky bottom-4 z-30">
        <div className="flex flex-wrap items-center gap-3 rounded-full border border-border-main bg-bg-card/95 px-4 py-2 shadow-lg backdrop-blur-md">
          <motion.button whileTap={{ scale: 0.97 }} onClick={save} disabled={saving}
            className="flex items-center gap-2 rounded-full bg-brand-green-hover px-6 py-2.5 text-[13.5px] font-semibold text-white transition hover:bg-[#0d8c5c] disabled:opacity-60">
            {saving ? <><CircleNotch size={16} className="animate-spin" /> 保存中…</> : '保存全部配置'}
          </motion.button>
          {dirty && !saving && !saved && (
            <span className="flex items-center gap-1.5 text-[12.5px] text-amber-500">
              <Warning size={15} weight="fill" /> 有未保存的改动
            </span>
          )}
          {saved && !testResult && (
            <span className="flex items-center gap-1.5 text-[12.5px] text-emerald-500">
              <CheckCircle size={15} weight="fill" /> 已保存并即时生效
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
