<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, duration, pageId, type GameView } from '../api'
import { Recorder, listAudioInputs, type AudioInputOption, type CaptureProgress } from '../recorder'
import { wordLayouts, fitText, type WordLayout } from '../gameLayout'

const props = defineProps<{ id: string; asr: boolean }>()
const emit = defineEmits<{ close: []; revoked: [] }>()
const state = ref<GameView | null>(null), canvas = ref<HTMLCanvasElement>(), stage = ref<HTMLDivElement>()
const notice = ref(''), tooltip = ref('')
let noticeTimer = 0, lastNotice = '', layout: WordLayout[] = [], width = 980, height = 630
const connected = ref(false), error = ref(''), recording = ref(false), processing = ref(false)
const voiceTranscript = ref(''), voiceStatus = ref('尚未识别；按住麦克风按钮开始测试')
const voiceMatched = ref<boolean | null>(null), voiceAudioBytes = ref(0)
const audioInputs = ref<AudioInputOption[]>([])
const selectedAudioInput = ref(localStorage.getItem('wordlearner-audio-input') || '')
const voiceDevice = ref(''), voiceLevel = ref(0), voiceDuration = ref(0), voiceFrames = ref(0)
let socket: WebSocket | null = null, heartbeat = 0, animation = 0, lastSpoken = '', stopped = false
let sequence = 0, inflight: Record<string, any> | null = null, queue: Record<string, any>[] = []
let recorder: Recorder | null = null, voiceHeld = false, pendingVoiceStop = false, recordTimer = 0, closing = false
let beams: { at: number; x: number; y: number }[] = []
const wordPositions = new Map<string, { x: number; y: number }>()
const isReviewMode = (mode?: string) => mode === 'review' || mode === 'debug'
function sendNext() {
  if (!connected.value || inflight || !queue.length) return
  inflight = { ...queue.shift(), seq: sequence + 1 }
  socket?.send(JSON.stringify(inflight))
}
function command(event: Record<string, any>) {
  if (!connected.value) { error.value = '连接已断开，请恢复连接后继续'; return }
  queue.push(event); sendNext()
  if (['speed', 'retry', 'restart'].includes(event.type)) canvas.value?.focus()
}
function connect() {
  if (stopped || socket?.readyState === WebSocket.OPEN) return
  error.value = ''; queue = []; inflight = null
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/games/${props.id}/socket?page=${encodeURIComponent(pageId)}`)
  socket.onopen = () => { connected.value = true; canvas.value?.focus(); socket?.send(JSON.stringify({ type: 'heartbeat' })) }
  socket.onmessage = event => {
    const msg = JSON.parse(event.data)
    if (msg.type === 'state') {
      state.value = msg.state
      if (msg.state.message !== lastNotice) {
        lastNotice = msg.state.message; notice.value = lastNotice
        clearTimeout(noticeTimer); noticeTimer = window.setTimeout(() => { notice.value = '' }, 2600)
      }
      // Once the authoritative state shows the completed lock, typing is ready
      // even if the parallel HTTP response has not reached the browser yet.
      if (processing.value && !msg.state.pause && msg.state.locked) processing.value = false
      if (!inflight) sequence = msg.state.last_seq
      const complete = msg.state.completed_word
      if (complete && complete.runtime_id !== lastSpoken) {
        lastSpoken = complete.runtime_id
        if ('speechSynthesis' in window) { const u = new SpeechSynthesisUtterance(complete.word); u.lang = 'en-US'; speechSynthesis.speak(u) }
      }
    } else if (msg.type === 'ack') {
      sequence = msg.seq
      const justClosed = inflight?.type === 'close'
      if (typeof msg.hit_runtime_id === 'string') {
        const position = wordPositions.get(msg.hit_runtime_id)
        if (position) beams.push({ at: performance.now(), ...position })
      }
      inflight = null; sendNext()
      if (justClosed && closing) emit('close')
    } else if (msg.type === 'error') {
      const failedCommand = inflight?.type
      error.value = msg.message; sequence = msg.seq; inflight = null; queue = []; closing = false
      if (failedCommand === 'voice_start') {
        cancelRecording(); voiceMatched.value = false; voiceStatus.value = msg.message
      }
    }
  }
  socket.onclose = event => {
    connected.value = false; queue = []; inflight = null; cancelRecording()
    if (!stopped && event.code === 4001) { stopped = true; emit('revoked'); return }
    if (!stopped) error.value = '连接已暂停，已保存确认过的进度。点击恢复连接继续。'
  }
  socket.onerror = () => { error.value = '无法连接游戏，请检查服务是否运行，或关闭另一个游戏页面' }
}
function cancelRecording() {
  voiceHeld = false; pendingVoiceStop = false; recording.value = false; processing.value = false
  clearTimeout(recordTimer); recorder?.dispose(); recorder = null
}
async function refreshAudioInputs() {
  try { audioInputs.value = await listAudioInputs() } catch { audioInputs.value = [] }
}
function chooseAudioInput() {
  if (selectedAudioInput.value) localStorage.setItem('wordlearner-audio-input', selectedAudioInput.value)
  else localStorage.removeItem('wordlearner-audio-input')
  const selected = audioInputs.value.find(item => item.deviceId === selectedAudioInput.value)
  voiceStatus.value = selected ? `已选择 ${selected.label}，按住空格开始采集` : '已选择系统默认麦克风，按住空格开始采集'
  canvas.value?.focus()
}
function updateCapture(progress: CaptureProgress) {
  voiceFrames.value = progress.frameCount
  voiceDuration.value = progress.durationSeconds
  voiceLevel.value = Math.max(progress.peakLevel, progress.rmsLevel * 2)
  voiceStatus.value = progress.peakLevel >= .003 ? '已检测到声音；松开空格后识别' : '麦克风已连接，尚未检测到明显声音'
}
function microphoneErrorMessage(reason: unknown) {
  const problem = reason as DOMException
  if (problem?.name === 'NotAllowedError' || problem?.name === 'SecurityError') return '浏览器没有麦克风权限，请在地址栏的网站权限中允许麦克风'
  if (problem?.name === 'NotFoundError' || problem?.name === 'OverconstrainedError') return '没有找到可用麦克风，请连接输入设备或改选系统默认麦克风'
  if (problem?.name === 'NotReadableError') return '麦克风正被其他程序独占，或设备无法读取'
  return problem?.message || '麦克风未采集到音频，请检查浏览器录音权限及输入设备'
}
async function beginVoice() {
  if (recording.value || processing.value || state.value?.pause || !connected.value) return
  if (state.value?.voice_required && state.value.locked) {
    voiceStatus.value = '当前单词已经语音锁定，请先完成输入'
    canvas.value?.focus(); return
  }
  voiceTranscript.value = ''; voiceAudioBytes.value = 0; voiceMatched.value = null
  voiceFrames.value = 0; voiceDuration.value = 0; voiceLevel.value = 0; pendingVoiceStop = false
  if (!props.asr) {
    voiceStatus.value = '语音识别服务未配置'
    error.value = '语音识别尚未配置；请先配置语音服务后使用一键复习'
    return
  }
  voiceStatus.value = '正在打开麦克风…'
  voiceHeld = true; error.value = ''
  const current = new Recorder(selectedAudioInput.value, progress => { if (recorder === current) updateCapture(progress) })
  recorder = current
  command({ type: 'voice_start' })
  try {
    await current.start()
    if (recorder !== current) { current.dispose(); return }
    recording.value = true
    voiceDevice.value = current.deviceLabel
    if (current.deviceId) {
      selectedAudioInput.value = current.deviceId
      localStorage.setItem('wordlearner-audio-input', current.deviceId)
    }
    void refreshAudioInputs()
    voiceStatus.value = current.durationSeconds > 0 ? '麦克风已连接，请朗读屏幕上的英文单词' : '正在等待麦克风音频…'
    recordTimer = window.setTimeout(endVoice, 14500)
    if (!voiceHeld || pendingVoiceStop) {
      pendingVoiceStop = false
      const wait = Math.max(0, 220 - current.durationSeconds * 1000)
      if (wait) await new Promise(resolve => setTimeout(resolve, wait))
      if (recorder === current) await endVoice()
    }
  } catch (reason) {
    if (recorder !== current) { current.dispose(); return }
    cancelRecording(); if (connected.value) command({ type: 'voice_cancel' })
    const message = microphoneErrorMessage(reason)
    voiceStatus.value = message; error.value = message; voiceMatched.value = false
  }
}
async function endVoice() {
  voiceHeld = false; clearTimeout(recordTimer)
  if (!recorder) return
  if (!recording.value) {
    pendingVoiceStop = true
    voiceStatus.value = '已松开空格，等待麦克风就绪后自动识别…'
    return
  }
  recording.value = false; processing.value = true
  const capture = recorder.stop(); recorder = null
  voiceAudioBytes.value = capture.audio.byteLength; voiceFrames.value = capture.frameCount
  voiceDuration.value = capture.durationSeconds; voiceLevel.value = Math.max(capture.peakLevel, capture.rmsLevel * 2)
  voiceDevice.value = capture.deviceLabel
  if (!capture.frameCount || capture.durationSeconds < .08) {
    voiceMatched.value = false; processing.value = false
    voiceStatus.value = '采集时间太短，没有取得可识别的麦克风音频，请按住空格朗读后再松开'
    if (connected.value) command({ type: 'voice_cancel' })
    canvas.value?.focus(); return
  }
  voiceStatus.value = '音频已采集，正在识别…'
  // Wait for the server-generated recording ticket, never submit a transcript from the browser.
  for (let i = 0; i < 30 && !state.value?.voice_ticket && connected.value; i++) await new Promise(r => setTimeout(r, 30))
  try {
    const ticket = state.value?.voice_ticket
    if (!ticket) throw new Error('录音未开始，请等待单词出现后再试')
    const result = await api<{ transcript: string; normalized_transcript: string; recognized: boolean; matched: boolean; audio_bytes: number; message: string; state: GameView }>(`/games/${props.id}/voice`, { method: 'POST', body: capture.audio,
      headers: { 'Content-Type': 'audio/wav', 'X-Voice-Ticket': ticket } })
    voiceTranscript.value = result.transcript.trim() || '（未识别到文字）'
    voiceAudioBytes.value = result.audio_bytes
    voiceMatched.value = result.state.voice_required ? result.matched : result.recognized
    voiceStatus.value = result.message
    if (!stopped && result.state && result.state.last_seq >= (state.value?.last_seq || 0)) state.value = result.state
  } catch (e) {
    const message = (e as Error).message
    voiceTranscript.value ||= '（识别服务未返回文字）'
    voiceMatched.value = false; voiceStatus.value = `识别失败：${message}`; error.value = message
    if (connected.value) command({ type: 'voice_cancel' })
  } finally { processing.value = false; canvas.value?.focus() }
}
function keydown(event: KeyboardEvent) {
  if ((event.target as HTMLElement).matches('input,select,textarea,button')) return
  if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing || !state.value) return
  if (event.code === 'Space' && state.value.voice_required && !state.value.locked) {
    event.preventDefault(); if (!event.repeat) void beginVoice(); return
  }
  if (isReviewMode(state.value.mode) && /^[1-5]$/.test(event.key)) {
    event.preventDefault(); if (!event.repeat) command({ type: 'hint_start', badge: Number(event.key) }); return
  }
  if (recording.value || processing.value || state.value.pause || !state.value.game_active) return
  if (event.key.length === 1) {
    event.preventDefault(); command({ type: 'key', char: event.key })
  }
}
function keyup(event: KeyboardEvent) {
  if (event.code === 'Space' && voiceHeld) { event.preventDefault(); void endVoice() }
  if (isReviewMode(state.value?.mode) && /^[1-5]$/.test(event.key)) command({ type: 'hint_end' })
}
function blur() {
  cancelRecording()
  if (state.value?.pause === 'voice') command({ type: 'voice_cancel' })
  if (state.value?.pause === 'hint') command({ type: 'hint_end' })
}
function visibility() { if (document.hidden) { blur(); socket?.close() } }
function devicesChanged() { void refreshAudioInputs() }
function close() {
  if (!connected.value || ['completed', 'closed'].includes(state.value?.status || '')) { emit('close'); return }
  if (isReviewMode(state.value?.mode) && !confirm('退出将保存进度，并按原规则记一次本轮重试。确认退出？')) return
  blur(); closing = true; command({ type: 'close' })
}
function hover(event: PointerEvent) {
  const rect = canvas.value?.getBoundingClientRect()
  if (!rect) return
  const x = event.clientX - rect.left, y = event.clientY - rect.top
  const item = layout.find(item => Math.abs(item.x - x) <= item.width / 2 && y >= item.y - 24 && y <= item.y + 56)
  tooltip.value = item ? item.word.prompt : ''
}
function draw() {
  const element = canvas.value, rect = stage.value?.getBoundingClientRect(), s = state.value
  if (element && rect) {
    width = Math.max(820, Math.round(rect.width)); height = Math.max(520, Math.round(rect.height))
    const ratio = window.devicePixelRatio || 1
    if (element.width !== Math.round(width * ratio) || element.height !== Math.round(height * ratio)) {
      element.width = Math.round(width * ratio); element.height = Math.round(height * ratio)
    }
    const ctx = element.getContext('2d')!
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
    const baseline = height - 74
    ctx.fillStyle = '#0b0d12'; ctx.fillRect(0, 0, width, height)
    ctx.strokeStyle = '#2c2c2e'; ctx.lineWidth = 1
    for (let y = 80; y < baseline; y += 80) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke() }
    ctx.strokeStyle = '#ffd60a'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(0, baseline); ctx.lineTo(width, baseline); ctx.stroke()
    ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.font = 'bold 17px SimSun, serif'; ctx.fillStyle = '#f5f5f7'
    if (s) {
      const hud = [s.mode === 'debug' ? '免麦克风复习·不限轮' : s.mode === 'review' ? '一键复习·不限轮' : '普通练习', `生命 ${s.lives}`, `第 ${s.round} 轮`, `进度 ${s.processed}/${s.total}`, `速度 ${s.speed.toFixed(1)}x`]
      if (!isReviewMode(s.mode)) hud.push(`本局 ${s.score}`, `总分 ${s.total_score}`)
      ctx.fillText(hud.join('    '), 18, 16)
      if (isReviewMode(s.mode)) {
        const details = [`已获得 ¥${s.reward_money.toFixed(2)}；每日上限 ¥${s.perfect_reward_money.toFixed(2)}；本轮预计 +¥${Math.max(0, s.reward_money - s.saved_reward_money).toFixed(2)}`, `用时 ${duration(s.elapsed_seconds)}`]
        details.push(s.mode === 'debug' ? '免朗读锁定，不限轮；每日奖励封顶' : '不限轮；每日奖励封顶')
        if (s.remaining_seconds !== null && s.remaining_seconds <= 300) details.push(`倒计时 ${duration(s.remaining_seconds)}`)
        if (recording.value) details.push('录音中')
        else if (processing.value) details.push('语音识别中')
        else if (s.locked) details.push('语音已锁定')
        ctx.fillText(fitText(ctx, details.join('    '), width - 36), 18, 42)
      }
    }
    ctx.textBaseline = 'middle'
    layout = s ? wordLayouts(ctx, s, width, height) : []
    for (const item of layout) wordPositions.set(item.word.runtime_id, { x: item.x / width, y: (item.y + 30) / height })
    for (const [i, item] of layout.entries()) {
      const { word, x, y, prompt, display, width: boxWidth } = item
      const locked = word.runtime_id === s?.locked, hint = word.runtime_id === s?.hint_runtime
      ctx.textAlign = 'center'
      if (locked || hint) { ctx.lineWidth = 3; ctx.strokeStyle = locked ? '#0a84ff' : '#ffd60a'; ctx.strokeRect(x - boxWidth / 2, y - 24, boxWidth, 80) }
      if (isReviewMode(s?.mode) && i < 5) {
        const bx = x + boxWidth / 2 - 8, by = y - 24
        ctx.fillStyle = '#0a84ff'; ctx.beginPath(); ctx.arc(bx, by, 13, 0, Math.PI * 2); ctx.fill()
        ctx.font = 'bold 17px SimSun, serif'; ctx.fillStyle = '#fff'; ctx.fillText(String(i + 1), bx, by)
      }
      // No Canvas maxWidth argument: that squeezes glyphs and makes long definitions unreadable.
      ctx.font = '24px SimSun, serif'; ctx.fillStyle = '#a1a1a6'; ctx.fillText(prompt, x, y)
      ctx.font = 'bold 33px "Times New Roman", serif'; ctx.fillStyle = '#f5f5f7'; ctx.fillText(display, x, y + 30)
    }
    ctx.fillStyle = '#0071e3'; ctx.beginPath(); ctx.moveTo(width / 2, baseline + 12); ctx.lineTo(width / 2 - 34, baseline + 48); ctx.lineTo(width / 2 + 34, baseline + 48); ctx.closePath(); ctx.fill()
    ctx.fillStyle = '#005bb5'; ctx.beginPath(); ctx.ellipse(width / 2, baseline + 34, 18, 18, 0, 0, Math.PI * 2); ctx.fill()
    beams = beams.filter(beam => performance.now() - beam.at < 180)
    for (const beam of beams) {
      const progress = Math.min((performance.now() - beam.at) / 180, 1)
      const x = width / 2 + (beam.x * width - width / 2) * progress, y = baseline + 18 + (beam.y * height - baseline - 18) * progress
      ctx.strokeStyle = '#0a84ff'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(width / 2, baseline + 18); ctx.lineTo(x, y); ctx.stroke()
      ctx.fillStyle = '#0a84ff'; ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill()
    }
    if (s && (!s.game_active || s.status === 'completed' || !connected.value)) {
      ctx.fillStyle = '#00000088'; ctx.fillRect(0, 0, width, height); ctx.textAlign = 'center'; ctx.fillStyle = '#f5f5f7'; ctx.font = 'bold 42px SimSun, serif'
      ctx.fillText(s.status === 'completed' ? '复习完成' : !connected.value ? '进度已保存' : '本轮失败', width / 2, height / 2 - 20)
      ctx.font = '24px SimSun, serif'; ctx.fillStyle = '#0a84ff'
      ctx.fillText(s.status === 'completed' ? '本次结果已按实际学习记录保存' : !connected.value ? '恢复连接后继续' : `本局得分 ${s.score}，可点击“重试本轮”继续挑战当前轮`, width / 2, height / 2 + 30)
    }
  }
  animation = requestAnimationFrame(draw)
}
onMounted(() => {
  connect(); draw(); void refreshAudioInputs()
  heartbeat = window.setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'heartbeat' })) }, 1000)
  window.addEventListener('keydown', keydown); window.addEventListener('keyup', keyup); window.addEventListener('blur', blur)
  document.addEventListener('visibilitychange', visibility)
  navigator.mediaDevices?.addEventListener?.('devicechange', devicesChanged)
})
onUnmounted(() => {
  stopped = true; socket?.close(); clearInterval(heartbeat); cancelAnimationFrame(animation); clearTimeout(noticeTimer); cancelRecording()
  window.removeEventListener('keydown', keydown); window.removeEventListener('keyup', keyup); window.removeEventListener('blur', blur)
  document.removeEventListener('visibilitychange', visibility)
  navigator.mediaDevices?.removeEventListener?.('devicechange', devicesChanged)
})
</script>
<template>
  <section class="game-shell" aria-label="打字挑战">
    <h1 class="sr-only">打字挑战</h1>
    <div ref="stage" class="game-stage">
      <canvas ref="canvas" tabindex="0" aria-label="单词下落游戏区域，使用键盘输入英文" @pointermove="hover" @pointerleave="tooltip = ''" />
      <div class="sr-only" v-if="state" aria-live="off">{{ state.mode === 'practice' ? '普通练习' : state.mode === 'debug' ? '免麦克风复习' : '一键复习' }}，生命 {{ state.lives }}，第 {{ state.round }} 轮，进度 {{ state.processed }}/{{ state.total }}，本局 {{ state.score }}，总分 {{ state.total_score }}</div>
      <p v-show="recording || processing || notice" class="game-message" :class="{ review: isReviewMode(state?.mode) }" aria-live="polite">{{ recording ? '请读出屏幕上的英文单词…' : processing ? '语音识别中' : notice }}</p>
      <p v-if="error" class="game-error" role="alert">{{ error }}</p>
      <div v-if="tooltip" class="game-definition" role="tooltip">{{ tooltip }}</div>
    </div>
    <div class="game-controls">
      <label>速度 <select :value="state?.speed" :disabled="!connected" @change="command({ type: 'speed', value: Number(($event.target as HTMLSelectElement).value) })"><option value="0.2">低</option><option value="0.5">中</option><option value="0.7">高</option></select></label>
      <span class="muted speed-detail" v-if="state">{{ ({'0.2':'低','0.5':'中','0.7':'高'} as Record<string,string>)[String(state.speed)] }} {{ state.speed.toFixed(2) }}x（{{ isReviewMode(state.mode) ? '一键复习' : '固定档位' }}，约 {{ state.spawn_interval.toFixed(1) }}s/词）</span>
      <button v-if="isReviewMode(state?.mode)" class="voice-button" :disabled="!connected || processing || (!!state?.voice_required && !!state?.locked)" @pointerdown.prevent="beginVoice" @pointerup="endVoice" @pointerleave="voiceHeld && endVoice()">{{ recording ? '录音中 · 松开识别' : processing ? '识别中…' : state?.locked ? '已锁定 · 请输入' : state?.voice_required ? '按住读词（空格）' : '按住测试麦克风' }}</button>
      <span v-if="state?.mode === 'debug'" class="muted">直接输入；麦克风测试只显示结果，不锁定单词</span>
      <div class="spacer" />
      <button v-if="!connected" class="primary" @click="connect">恢复连接</button>
      <template v-else-if="state?.status === 'running'"><button @click="command({ type: 'restart' })">重新开始</button><button @click="command({ type: 'retry' })">重试本轮</button></template>
      <button @click="close">保存并返回</button>
    </div>
    <div v-if="isReviewMode(state?.mode)" class="voice-transcript" role="status" aria-live="polite" data-testid="voice-transcript">
      <div class="voice-result-row">
        <span class="voice-transcript-label">
          <i :class="{ active: recording || processing, success: voiceMatched === true, warning: voiceMatched === false }" />
          麦克风识别
        </span>
        <strong>{{ voiceTranscript || '（等待朗读）' }}</strong>
        <span class="voice-transcript-status">{{ voiceStatus }}</span>
      </div>
      <div class="voice-input-row">
        <label for="voice-input-device">输入设备</label>
        <select id="voice-input-device" v-model="selectedAudioInput" :disabled="recording || processing" @change="chooseAudioInput">
          <option value="">系统默认麦克风</option>
          <option v-for="input in audioInputs" :key="input.deviceId" :value="input.deviceId">{{ input.label }}</option>
        </select>
        <button type="button" class="text-button voice-refresh" :disabled="recording || processing" @click="refreshAudioInputs">刷新设备</button>
        <span class="voice-meter" :class="{ active: recording }" aria-hidden="true"><i :style="{ width: `${Math.min(100, Math.round(voiceLevel * 250))}%` }" /></span>
        <small v-if="recording">实时音量 {{ Math.min(100, Math.round(voiceLevel * 250)) }}% · {{ voiceDuration.toFixed(1) }} 秒 · {{ voiceFrames }} 帧</small>
        <small v-else-if="voiceFrames">{{ voiceDevice || '系统默认麦克风' }} · 采集 {{ voiceDuration.toFixed(2) }} 秒 · {{ voiceFrames }} 帧<span v-if="voiceAudioBytes"> · 已上传 {{ (voiceAudioBytes / 1024).toFixed(1) }} KB</span></small>
        <small v-else>{{ voiceDevice || '选择麦克风后，按住空格时可在这里查看实时采集' }}</small>
      </div>
    </div>
  </section>
</template>
