const PAGE_STORAGE_KEY = 'wordlearner-page-id'
export const pageId = sessionStorage.getItem(PAGE_STORAGE_KEY) || crypto.randomUUID().replaceAll('-', '')
sessionStorage.setItem(PAGE_STORAGE_KEY, pageId)

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, {
    ...options, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-WordLearner-Request': '1', 'X-WordLearner-Page': pageId, ...options.headers },
  })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '输入格式不正确，请检查后重试')
  return result as T
}

export function localDate(offset = 0) {
  const value = new Date()
  value.setDate(value.getDate() + offset)
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(value)
}

export function duration(seconds: number) {
  const s = Math.floor(seconds || 0)
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export function durationHms(seconds: number) {
  const s = Math.floor(seconds || 0)
  const hours = Math.floor(s / 3600)
  const minutes = Math.floor((s % 3600) / 60)
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export interface Word {
  id: number; word: string; translation: string; phonetic: string; created_on: string
  practice_count: number; accuracy: number; mastery: number; due_on: string; archived: number
}

export interface GameView {
  id: string; mode: string; voice_required: boolean; status: string; day: string; last_seq: number; round: number
  round_limit: number | null; lives: number; score: number; speed: number; processed: number; total: number
  accuracy: number; elapsed_seconds: number; reward_money: number; saved_reward_money: number
  perfect_reward_money: number; pause: string | null; voice_ticket: string | null; locked: string | null
  message: string; game_active: boolean; completed_word: { word: string; runtime_id: string } | null
  total_score: number; remaining_seconds: number | null; hint_runtime: string | null; spawn_interval: number; restarts: number; retries: Record<string, number>
  active: { runtime_id: string; word_id: number; prompt: string; display: string; x: number; y: number; progress: number; hinted: boolean }[]
}
