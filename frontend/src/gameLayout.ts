import type { GameView } from './api'

export type FallingView = GameView['active'][number]
export interface WordLayout {
  word: FallingView; x: number; y: number; width: number; prompt: string; display: string
}

// Display coordinates only. The server's baseline (500), word positions,
// collision decisions and measured learning time are never changed here.
export function wordLayouts(ctx: CanvasRenderingContext2D, state: GameView, width: number, height: number): WordLayout[] {
  const baseline = height - 74
  const top = state.mode === 'practice' ? 74 : 104
  const laneWidth = Math.max(100, width * .32 - 40)
  return state.active.map(word => {
    const x = Math.min(width - laneWidth / 2 - 18, Math.max(laneWidth / 2 + 18, word.x * width))
    const y = top + (word.y - 44) * (baseline - top) / (500 - 44)
    ctx.font = '24px SimSun, serif'
    const prompt = fitText(ctx, word.prompt, laneWidth)
    const promptWidth = ctx.measureText(prompt).width
    ctx.font = 'bold 33px "Times New Roman", serif'
    const display = fitText(ctx, state.hint_runtime === word.runtime_id ? `[${word.display}]` : word.display, laneWidth)
    return { word, x, y, prompt, display, width: Math.max(promptWidth, ctx.measureText(display).width, 92) + 34 }
  })
}

export function fitText(ctx: CanvasRenderingContext2D, text: string, width: number): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  if (ctx.measureText(clean).width <= width) return clean
  const chars = [...clean]
  let low = 0, high = chars.length
  while (low < high) {
    const mid = Math.ceil((low + high) / 2)
    if (ctx.measureText(chars.slice(0, mid).join('') + '…').width <= width) low = mid
    else high = mid - 1
  }
  return chars.slice(0, low).join('') + '…'
}
