export interface AudioInputOption {
  deviceId: string
  label: string
}

export interface CaptureProgress {
  frameCount: number
  durationSeconds: number
  peakLevel: number
  rmsLevel: number
}

export interface RecordingResult extends CaptureProgress {
  audio: ArrayBuffer
  deviceId: string
  deviceLabel: string
  sampleRate: number
}

export async function listAudioInputs(): Promise<AudioInputOption[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return []
  const devices = await navigator.mediaDevices.enumerateDevices()
  let number = 0
  return devices.filter(device => device.kind === 'audioinput').map(device => {
    number += 1
    return { deviceId: device.deviceId, label: device.label || `麦克风 ${number}` }
  })
}

export class Recorder {
  private stream: MediaStream | null = null
  private context: AudioContext | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private worklet: AudioWorkletNode | null = null
  private mute: GainNode | null = null
  private chunks: Float32Array[] = []
  private frames = 0
  private sumSquares = 0
  private peak = 0
  private cancelled = false
  private lastProgressAt = 0
  deviceId = ''
  deviceLabel = ''
  sampleRate = 0

  constructor(private preferredDeviceId = '', private onProgress?: (progress: CaptureProgress) => void) {}

  get durationSeconds() {
    return this.frames / Math.max(this.sampleRate || this.context?.sampleRate || 16000, 1)
  }

  private constraints(): MediaTrackConstraints {
    const result: MediaTrackConstraints = {
      channelCount: { ideal: 1 },
      echoCancellation: { ideal: true },
      noiseSuppression: { ideal: true },
      autoGainControl: { ideal: true },
    }
    if (this.preferredDeviceId) result.deviceId = { exact: this.preferredDeviceId }
    return result
  }

  private progress(): CaptureProgress {
    return {
      frameCount: this.frames,
      durationSeconds: this.durationSeconds,
      peakLevel: this.peak,
      rmsLevel: this.frames ? Math.sqrt(this.sumSquares / this.frames) : 0,
    }
  }

  async start() {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('当前浏览器无法录音，请使用本机地址或 HTTPS')
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: this.constraints(), video: false })
    } catch (error) {
      if (!this.preferredDeviceId || !['NotFoundError', 'OverconstrainedError'].includes((error as DOMException).name)) throw error
      // A remembered device may have been unplugged or received a new browser id.
      this.preferredDeviceId = ''
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: this.constraints(), video: false })
    }
    if (this.cancelled) { this.dispose(); throw new Error('录音已取消') }
    const track = this.stream.getAudioTracks()[0]
    if (!track) { this.dispose(); throw new Error('浏览器没有返回麦克风音轨') }
    const settings = track.getSettings()
    this.deviceId = settings.deviceId || ''
    this.deviceLabel = track.label || '系统默认麦克风'
    this.context = new AudioContext()
    this.sampleRate = this.context.sampleRate
    await this.context.audioWorklet.addModule('/pcm-worklet.js')
    if (this.cancelled) { this.dispose(); throw new Error('录音已取消') }

    let ready: (() => void) | null = null
    const firstFrame = new Promise<void>(resolve => { ready = resolve })
    this.worklet = new AudioWorkletNode(this.context, 'pcm-collector')
    this.worklet.port.onmessage = event => {
      if (this.cancelled || this.frames >= this.sampleRate * 15) return
      const pcm = event.data as Float32Array
      if (!(pcm instanceof Float32Array) || !pcm.length) return
      this.chunks.push(pcm)
      this.frames += pcm.length
      let localPeak = 0, localSquares = 0
      for (const sample of pcm) {
        const magnitude = Math.abs(sample)
        if (magnitude > localPeak) localPeak = magnitude
        localSquares += sample * sample
      }
      this.peak = Math.max(this.peak, localPeak)
      this.sumSquares += localSquares
      ready?.(); ready = null
      const now = performance.now()
      if (now - this.lastProgressAt >= 80) {
        this.lastProgressAt = now
        this.onProgress?.(this.progress())
      }
    }
    this.source = this.context.createMediaStreamSource(this.stream)
    this.mute = this.context.createGain(); this.mute.gain.value = 0
    this.source.connect(this.worklet).connect(this.mute).connect(this.context.destination)
    await this.context.resume()
    await Promise.race([
      firstFrame,
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('麦克风已打开，但没有收到音频数据')), 2500)),
    ])
    if (this.cancelled) { this.dispose(); throw new Error('录音已取消') }
    this.onProgress?.(this.progress())
  }

  stop(): RecordingResult {
    const rate = this.sampleRate || this.context?.sampleRate || 16000
    const stats = this.progress()
    const pcm = new Float32Array(this.frames)
    let offset = 0
    for (const chunk of this.chunks) { pcm.set(chunk, offset); offset += chunk.length }
    const length = Math.min(240000, Math.floor(pcm.length * 16000 / rate))
    const buffer = new ArrayBuffer(44 + length * 2)
    const view = new DataView(buffer)
    const ascii = (at: number, str: string) => [...str].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)))
    ascii(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); ascii(8, 'WAVE'); ascii(12, 'fmt ')
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
    view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
    ascii(36, 'data'); view.setUint32(40, length * 2, true)
    for (let i = 0; i < length; i++) {
      const position = i * rate / 16000, index = Math.floor(position), fraction = position - index
      const sample = Math.max(-1, Math.min(1, (pcm[index] || 0) * (1 - fraction) + (pcm[index + 1] || 0) * fraction))
      view.setInt16(44 + i * 2, sample * (sample < 0 ? 32768 : 32767), true)
    }
    const result = { audio: buffer, ...stats, deviceId: this.deviceId, deviceLabel: this.deviceLabel, sampleRate: rate }
    this.dispose()
    return result
  }

  dispose() {
    this.cancelled = true
    if (this.worklet) { this.worklet.port.onmessage = null; this.worklet.disconnect(); this.worklet = null }
    this.source?.disconnect(); this.source = null
    this.mute?.disconnect(); this.mute = null
    this.stream?.getTracks().forEach(track => track.stop()); this.stream = null
    void this.context?.close().catch(() => {}); this.context = null
  }
}
