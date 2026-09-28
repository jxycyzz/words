import { onUnmounted, ref, watch, type Ref } from 'vue'
import { api } from './api'

type Fields = { word: string; translation: string; phonetic: string }
type Result = Fields & { warning?: string }

export function useWordLookup(form: Fields, editId: Ref<number | null>, status: Ref<string>, error: Ref<string>) {
  const loading = ref(false)
  let timer = 0, generation = 0, controller: AbortController | null = null
  let filled: Partial<Fields> = {}

  function cancel() {
    clearTimeout(timer); generation++; controller?.abort(); controller = null; loading.value = false
  }

  async function lookup(manual = true) {
    cancel()
    const word = form.word.trim(), version = generation
    if (!word) { if (manual) error.value = '请输入英文单词或短语'; return }
    const previous = { translation: form.translation, phonetic: form.phonetic }
    const request = new AbortController(); controller = request; loading.value = true
    if (manual) error.value = ''
    status.value = `正在查询：${word}`
    try {
      const result = await api<Result>('/lookup?word=' + encodeURIComponent(word), { signal: request.signal })
      if (version !== generation || form.word.trim() !== word) return
      for (const field of ['translation', 'phonetic'] as const) {
        // Never erase partial results or overwrite a field edited while the request was pending.
        if (result[field] && form[field] === previous[field] && (manual || !form[field] || form[field] === filled[field])) {
          form[field] = result[field]; filled[field] = result[field]
        }
      }
      status.value = result.warning || '释义和音标已补全，请确认后保存'
    } catch (e) {
      if (version !== generation || request.signal.aborted) return
      const message = (e as Error).message
      status.value = message
      if (manual) error.value = message
    } finally {
      if (version === generation) { loading.value = false; controller = null }
    }
  }

  watch(() => form.word, () => {
    cancel()
    if (editId.value === null) {
      for (const field of ['translation', 'phonetic'] as const) {
        if (filled[field] && form[field] === filled[field]) form[field] = ''
      }
    }
    filled = {}
    if (editId.value !== null || !form.word.trim()) return
    timer = window.setTimeout(() => { void lookup(false) }, 650)
  }, { flush: 'sync' })

  onUnmounted(cancel)
  return { lookup, loading, cancel }
}
