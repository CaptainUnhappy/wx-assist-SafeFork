// Qwen-Audio-Realtime 端到端全双工会话（浏览器侧）。
//
// 音频链路：
//   上行 麦克风 → AudioWorklet 采集 16kHz/16bit 单声道 PCM → WS 二进制帧
//   下行 WS 二进制帧（24kHz/16bit PCM）→ 排队顺序播放（打断时整队列丢弃）
//
// 控制链路：WS 文本帧 JSON（识别字幕、AI 字幕、轮次、工具状态、错误）。
//
// 不依赖任何第三方库：PCM 播放在主线程用 AudioBufferSourceNode 顺序调度；
// 采集优先 AudioWorklet（无毛刺），不可用时回退 ScriptProcessor。

const TARGET_IN_RATE = 16000
const OUT_RATE = 24000

export class RealtimeSession {
  constructor() {
    this.ws = null
    this.audioCtx = null
    this.micStream = null
    this.workletNode = null
    this.procNode = null
    this.onState = () => {}        // 'connecting'|'listening'|'thinking'|'speaking'
    this.onUserTranscript = () => {}
    this.onAssistantTranscript = () => {}
    this.onTool = () => {}         // {phase:'start'|'done', name, preview}
    this.onError = () => {}
    this.onClose = () => {}
    this.onLevel = () => {}
    this.onFatalDowngrade = () => {}   // 就绪前连接失败（无Key/401/网络）→ 调用方降级
    this._closed = false
    this._ready = false
    // 播放队列与调度
    this._playQueue = []
    this._playing = false
    this._nextStartTime = 0
    this._curSource = null
    this._toolRunning = false
  }

  async start(wsUrl) {
    this.onState('connecting')
    const Ctx = window.AudioContext || window.webkitAudioContext
    this.audioCtx = new Ctx()
    if (this.audioCtx.state === 'suspended') {
      try { await this.audioCtx.resume() } catch {}
    }

    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })

    await this._setupCapture()

    // WebSocket（binaryType=arraybuffer 接收 PCM）
    console.info('[Voice] opening WS', wsUrl)
    this.ws = new WebSocket(wsUrl)
    this.ws.binaryType = 'arraybuffer'
    this.ws.onopen = () => { console.info('[Voice] WS open, waiting realtime.ready') }
    this.ws.onmessage = (ev) => this._onMessage(ev)
    this.ws.onerror = () => {
      console.warn('[Voice] WS error event')
      this.onError('实时语音连接出错')
    }
    this.ws.onclose = (ev) => {
      const stage = this._ready ? 'in-call' : 'before-ready'
      if (this._closed) {
        console.info(`[Voice] WS closed locally (${stage}) code=${ev.code}`)
      } else {
        console.warn(`[Voice] WS closed (${stage}) code=${ev.code} reason=${ev.reason || '(empty)'}`)
        // 还没 ready 就断开 = 建连失败（未配置/鉴权失败/网络不通），通知调用方降级
        if (!this._ready) this.onFatalDowngrade('实时语音通道连接失败')
        this.onClose()
      }
    }
  }

  // ── 采集：优先 AudioWorklet，否则 ScriptProcessor ─────────────────
  async _setupCapture() {
    const src = this.audioCtx.createMediaStreamSource(this.micStream)
    const srcRate = this.audioCtx.sampleRate
    this._srcRate = srcRate

    // 电平表（用 AnalyserNode，不干扰采集）
    const analyser = this.audioCtx.createAnalyser()
    analyser.fftSize = 512
    src.connect(analyser)
    const levelBuf = new Uint8Array(analyser.fftSize)
    const levelTimer = setInterval(() => {
      if (this._closed) { clearInterval(levelTimer); return }
      analyser.getByteTimeDomainData(levelBuf)
      let sum = 0
      for (let i = 0; i < levelBuf.length; i++) {
        const v = (levelBuf[i] - 128) / 128
        sum += v * v
      }
      this.onLevel(Math.min(1, Math.sqrt(sum / levelBuf.length) * 3.2))
    }, 80)

    const deliver = (float32) => {
      // 主线程做降采样 + int16 编码
      const pcm16 = this._encodeDownsample(float32, srcRate, TARGET_IN_RATE)
      this._sendBinary(pcm16)
    }
    this._deliverCapture = deliver

    try {
      // 用 Blob URL 注册内联 worklet（避免额外静态资源路径）
      const workletCode = `
        class MicProcessor extends AudioWorkletProcessor {
          process(inputs) {
            const ch = inputs[0] && inputs[0][0]
            if (ch && ch.length) this.port.postMessage(ch.slice(0))
            return true
          }
        }
        registerProcessor('wx-mic-worklet', MicProcessor);
      `
      const blob = new Blob([workletCode], { type: 'application/javascript' })
      const url = URL.createObjectURL(blob)
      await this.audioCtx.audioWorklet.addModule(url)
      URL.revokeObjectURL(url)
      this.workletNode = new AudioWorkletNode(this.audioCtx, 'wx-mic-worklet')
      src.connect(this.workletNode)
      this.workletNode.port.onmessage = (e) => deliver(e.data)
    } catch (e) {
      // 回退 ScriptProcessor（已废弃但兼容性最好）
      const proc = this.audioContext.createScriptProcessor(4096, 1, 1)
      this.procNode = proc
      src.connect(proc)
      // 必须连到 destination 才触发 onaudioprocess（经零增益静音）
      const mute = this.audioCtx.createGain()
      mute.gain.value = 0
      proc.connect(mute); mute.connect(this.audioCtx.destination)
      proc.onaudioprocess = (e) => deliver(e.inputBuffer.getChannelData(0))
    }
  }

  // ── 降采样（线性插值）+ 转 16bit PCM 小端 ─────────────────────────
  _encodeDownsample(input, inRate, outRate) {
    if (inRate === outRate) {
      return this._floatTo16(input)
    }
    const ratio = inRate / outRate
    const outLen = Math.floor(input.length / ratio)
    const out = new Int16Array(outLen)
    let pos = 0
    for (let i = 0; i < outLen; i++) {
      const idx = i * ratio
      const i0 = Math.floor(idx)
      const frac = idx - i0
      const a = input[i0] || 0
      const b = input[i0 + 1] !== undefined ? input[i0 + 1] : a
      let s = a + (b - a) * frac
      s = Math.max(-1, Math.min(1, s))
      out[pos++] = s < 0 ? s * 0x8000 : s * 0x7fff
    }
    return out.buffer
  }

  _floatTo16(f32) {
    const out = new Int16Array(f32.length)
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]))
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
    }
    return out.buffer
  }

  _sendBinary(buf) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN && buf.byteLength) {
      try { this.ws.send(buf) } catch {}
    }
  }

  // ── 下行消息 ─────────────────────────────────────────────────────
  _onMessage(ev) {
    if (typeof ev.data !== 'string') {
      this._playPcm(ev.data)   // 二进制 = 24k PCM
      return
    }
    let evt
    try { evt = JSON.parse(ev.data) } catch { return }
    switch (evt.type) {
      case 'realtime.ready':
        this._ready = true
        console.info('[Voice] realtime.ready — session live')
        this.onState('listening')
        break
      case 'realtime.unavailable':
        console.warn('[Voice] realtime.unavailable:', evt.message)
        this.onError(evt.message || '实时语音未启用')
        this.onFatalDowngrade(evt.message || '实时语音未启用')
        break
      case 'realtime.error':
        console.warn('[Voice] realtime.error:', evt.message)
        this.onError(evt.message || '实时语音异常')
        break
      case 'input_audio_buffer.speech_started':
        // 用户开口（可能在打断 AI）：立即停止当前播放 + 清空待播音频
        this._clearPlayback()
        this.onState('listening')
        break
      case 'conversation.item.input_audio_transcription.completed':
        if (evt.transcript) this.onUserTranscript(evt.transcript)
        break
      case 'response.audio_transcript.delta':
        if (evt.delta) this.onAssistantTranscript(evt.delta, true)
        break
      case 'response.audio_transcript.done':
        if (evt.transcript) this.onAssistantTranscript(evt.transcript, false)
        break
      case 'response.created':
        // 模型开始产出（可能要先调工具，音频未到前先显示思考态由工具事件控制）
        break
      case 'response.audio.delta':
        // 理论上走二进制帧；服务端若以文本下发则忽略（中继已转二进制）
        break
      case 'voice.tool_start':
        this._toolRunning = true
        this.onState('thinking')
        this.onTool({ phase: 'start', name: evt.name })
        break
      case 'voice.tool_done':
        this._toolRunning = false
        this.onTool({ phase: 'done', name: evt.name, preview: evt.preview })
        break
      case 'response.done':
        // 一轮语音输出结束；播放队列排空后由播放调度切回聆听
        break
      case 'error':
        console.warn('[Voice] server error event:', evt.error)
        this.onError((evt.error && evt.error.message) || '服务端错误')
        break
      default:
        break
    }

    // 任意音频即将到来的隐式信号：收到首个 audio 分片时切 speaking（在 _playPcm）
  }

  // ── 24k PCM 顺序无缝播放 ─────────────────────────────────────────
  _playPcm(arrayBuffer) {
    const i16 = new Int16Array(arrayBuffer)
    if (!i16.length) return
    const f32 = new Float32Array(i16.length)
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000

    // 重采样到 AudioContext 采样率
    let audio = f32
    if (this.audioCtx.sampleRate !== OUT_RATE) {
      audio = this._resampleToCtx(f32, OUT_RATE, this.audioCtx.sampleRate)
    }

    const buf = this.audioCtx.createBuffer(1, audio.length, this.audioCtx.sampleRate)
    buf.copyToChannel(audio, 0)
    this._playQueue.push(buf)
    if (!this._playing) this._pump()
  }

  _resampleToCtx(input, inRate, outRate) {
    const ratio = inRate / outRate
    const outLen = Math.floor(input.length / ratio)
    const out = new Float32Array(outLen)
    for (let i = 0; i < outLen; i++) {
      const idx = i * ratio
      const i0 = Math.floor(idx)
      const frac = idx - i0
      const a = input[i0] || 0
      const b = input[i0 + 1] !== undefined ? input[i0 + 1] : a
      out[i] = a + (b - a) * frac
    }
    return out
  }

  _pump() {
    const next = this._playQueue.shift()
    if (!next) {
      this._playing = false
      // 工具执行中保持思考态；否则语音播完回到聆听
      if (!this._toolRunning) this.onState('listening')
      return
    }
    this._playing = true
    this.onState('speaking')
    const src = this.audioCtx.createBufferSource()
    src.buffer = next
    src.connect(this.audioCtx.destination)
    const now = this.audioCtx.currentTime
    // 无缝：上一片结束时刻继续排，最早不早于当前
    const startAt = Math.max(now + 0.02, this._nextStartTime)
    src.start(startAt)
    this._nextStartTime = startAt + next.duration
    this._curSource = src
    src.onended = () => {
      if (this._curSource === src) this._curSource = null
      this._pump()
    }
  }

  _clearPlayback() {
    this._playQueue = []
    this._nextStartTime = 0
    // 立即停掉正在响的那一片（barge-in 闭嘴的关键）
    if (this._curSource) {
      try {
        this._curSource.onended = null
        this._curSource.stop()
      } catch {}
      this._curSource = null
    }
    this._playing = false
  }

  sendCancel() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: 'response.cancel' })) } catch {}
    }
  }

  stop() {
    this._closed = true
    this._playQueue = []
    try { this.workletNode && this.workletNode.disconnect() } catch {}
    try { this.procNode && this.procNode.disconnect() } catch {}
    try { this.micStream && this.micStream.getTracks().forEach((t) => t.stop()) } catch {}
    try { this.ws && (this.ws.onclose = null, this.ws.close()) } catch {}
    try { this.audioCtx && this.audioCtx.close() } catch {}
  }
}
