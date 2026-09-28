export class Recorder {
  private stream: MediaStream | null = null
  private context: AudioContext | null = null
  private chunks: Float32Array[] = []
  private frames = 0
  private cancelled = false

  async start() {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('当前浏览器无法录音，请使用本机地址或 HTTPS')
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true } })
    if (this.cancelled) { this.dispose(); return }
    this.context = new AudioContext({ sampleRate: 16000 })
    await this.context.audioWorklet.addModule('/pcm-worklet.js')
    if (this.cancelled) { this.dispose(); return }
    const node = new AudioWorkletNode(this.context, 'pcm-collector')
    node.port.onmessage = event => {
      if (this.frames >= (this.context?.sampleRate || 16000) * 15) return
      const pcm = event.data as Float32Array
      this.chunks.push(pcm); this.frames += pcm.length
    }
    const mute = this.context.createGain(); mute.gain.value = 0
    this.context.createMediaStreamSource(this.stream).connect(node).connect(mute).connect(this.context.destination)
    await this.context.resume()
  }

  stop(): ArrayBuffer {
    const rate = this.context?.sampleRate || 16000
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
    this.dispose()
    return buffer
  }

  dispose() {
    this.cancelled = true
    this.stream?.getTracks().forEach(track => track.stop()); this.stream = null
    void this.context?.close().catch(() => {}); this.context = null
  }
}
