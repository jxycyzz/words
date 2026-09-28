<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, localDate, duration, type Word } from './api'
import AppModal from './components/AppModal.vue'
import GamePanel from './components/GamePanel.vue'
import { useWordLookup } from './useWordLookup'

const words = ref<Word[]>([]), boot = ref<any>(null), selected = ref(new Set<number>()), focused = ref<number | null>(null)
const wordCache = reactive(new Map<number, Word>())
const search = ref(''), start = ref(localDate(-6)), end = ref(localDate()), busy = ref(false), status = ref('就绪'), error = ref('')
const form = reactive({ word: '', translation: '', phonetic: '', created_on: localDate() })
const editId = ref<number | null>(null), gameId = ref(''), modal = ref('')
const debugMode = ref(false)
let debugLoaded = false
function saveDebugPreference() { localStorage.setItem('wordlearner-debug-mode', String(debugMode.value)) }
const { lookup, loading: lookupLoading, cancel: cancelLookup } = useWordLookup(form, editId, status, error)
const parent = reactive({ password: '', confirmation: '', word_count: 75, round_count: 2, perfect_reward_money: 4 })
const report = ref<any>(null), reportStart = ref(localDate(-30)), reportEnd = ref(localDate()), reportTab = ref('daily')
const logStart = ref(localDate(-30)), logEnd = ref(localDate()), logType = ref('')
const logs = ref<any[]>([]), logSearch = ref(''), detail = ref(''), settlements = ref<any[]>([])
const aiText = ref(''), aiQuestion = ref(''), aiBusy = ref(false), aiCached = ref(false), aiTitle = ref('AI 助教'), aiWord = ref<Word | null>(null)
const reportAI = ref(''), jobProgress = ref(''), entryWarnings = ref<string[]>([])
const updated = ref(false), version = ref('0.2.8')
let presenceTimer = 0, versionTimer = 0, disposed = false, initialBuild = ''
async function checkVersion() {
  try { const health = await api('/health'); version.value = health.version; if (!initialBuild) initialBuild = health.build_id; else updated.value = initialBuild !== health.build_id } catch { /* Retry when the server returns. */ }
}
function applyUpdate() { window.location.reload() }
async function waitJob(id: number, progress = false) {
  while (!disposed) {
    const job = await api(`/jobs/${id}`)
    if (progress) jobProgress.value = `正在检查学习卡 ${job.progress} / ${job.total || '…'}`
    if (job.status === 'completed') return job.result
    if (job.status === 'failed') throw new Error(job.error || '后台任务失败')
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error('页面已关闭')
}
async function watchSavedWord(id: number, word: string) {
  try {
    const result = await waitJob(id)
    for (const item of result.items || []) if (item.kind === 'entry' && /疑似错误|拼写错误|不自然表达/.test(item.content) && !item.content.startsWith('【录入检测】正常')) entryWarnings.value.push(`${word}：${item.content}`)
  } catch (e) { if (!disposed) entryWarnings.value.push(`${word} 已保存，AI 检测未完成：${(e as Error).message}`) }
}
async function presence() {
  if (document.hidden) return
  try { const result = await api('/presence', { method: 'POST', body: '{}' }); if (boot.value) { boot.value.summary.usage_seconds = result.usage_seconds; boot.value.mail_status = result.mail_status } } catch { /* Next heartbeat retries without changing study results. */ }
}
const current = computed(() => wordCache.get([...selected.value].sort((a,b) => a-b)[0] || focused.value || 0) || null)
const ids = computed(() => selected.value.size ? [...selected.value] : focused.value ? [focused.value] : [])
const reportCounts = computed(() => report.value?.daily.reduce((a: any, d: any) => ({ practiced: a.practiced + d.practiced, seconds: a.seconds + d.elapsed_seconds }), { practiced: 0, seconds: 0 }) || { practiced: 0, seconds: 0 })
const curve = computed(() => report.value?.curve.map((p: any) => `${40 + p.day * 850 / 30},${145 - p.retention * 1.1}`).join(' ') || '')

async function run(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await fn() } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function loadWords() {
  const query = new URLSearchParams({ search: search.value })
  if (start.value) query.set('start', start.value)
  if (end.value) query.set('end', end.value)
  words.value = await api<Word[]>('/words?' + query)
  for (const w of words.value) wordCache.set(w.id, w)
  for (const id of selected.value) if (!words.value.some(w => w.id === id)) wordCache.set(id, await api<Word>(`/words/${id}`))
}
async function refresh() {
  boot.value = await api('/bootstrap')
  if (!debugLoaded) {
    const saved = localStorage.getItem('wordlearner-debug-mode')
    debugMode.value = saved === null ? !!boot.value.debug_default : saved === 'true'
    debugLoaded = true
  }
  await loadWords()
}
async function claimCurrentPage() {
  await api('/session/claim', { method: 'POST', body: '{}' })
  await refresh()
}
function toggle(id: number) { const next = new Set(selected.value); next.has(id) ? next.delete(id) : next.add(id); selected.value = next }
function chooseAll() { selected.value = new Set([...selected.value, ...words.value.map(w => w.id)]) }
function clearForm() { Object.assign(form, { word: '', translation: '', phonetic: '', created_on: boot.value?.summary.today || localDate() }); editId.value = null }
async function save() {
  await run(async () => {
    if (!form.word.trim()) throw new Error('请输入英文单词或短语')
    const savingWord = form.word.trim()
    if (!editId.value && (!form.translation.trim() || !form.phonetic.trim())) await lookup(false)
    if (form.word.trim() !== savingWord) throw new Error('英文内容已改变，请确认词条后再次保存')
    cancelLookup()
    const saved = await api<Word & { ai_job_id: number }>(editId.value ? `/words/${editId.value}` : '/words', { method: editId.value ? 'PUT' : 'POST', body: JSON.stringify(form) })
    status.value = `已保存：${saved.word}`; clearForm(); await refresh()
    void watchSavedWord(saved.ai_job_id, saved.word)
  })
}
function edit(word = current.value) {
  if (selected.value.size > 1 && word === current.value) { error.value = '编辑时请只选择一个词条'; return }
  if (!word) { error.value = '请先选择一个词条'; return }
  editId.value = word.id; Object.assign(form, { word: word.word, translation: word.translation, phonetic: word.phonetic, created_on: word.created_on })
  document.getElementById('word-input')?.focus()
}
function speak(value = form.word) {
  if (!value.trim()) { error.value = '请先输入或选择一个词条'; return }
  if (!('speechSynthesis' in window)) { error.value = '当前浏览器不支持发音'; return }
  speechSynthesis.cancel(); const utterance = new SpeechSynthesisUtterance(value); utterance.lang = 'en-US'; utterance.rate = .85; speechSynthesis.speak(utterance)
}
async function archive() {
  if (!ids.value.length) { error.value = '请先选择词条'; return }
  if (!confirm(`确认从词库移除 ${ids.value.length} 个词条？已有学习历史将保留。`)) return
  await run(async () => { await api('/words/archive', { method: 'POST', body: JSON.stringify({ ids: ids.value }) }); selected.value = new Set(); focused.value = null; await refresh(); status.value = '词条已归档，历史记录已保留' })
}
async function startGame(mode: 'practice' | 'review' | 'debug') {
  await run(async () => {
    const result = await api('/games', { method: 'POST', body: JSON.stringify({ mode: debugMode.value ? 'debug' : mode, word_ids: [...selected.value] }) })
    try { gameId.value = result.id || (await waitJob(result.job_id, true)).id } finally { jobProgress.value = '' }
  })
}
async function gameClosed() { gameId.value = ''; await run(refresh) }
function gameRevoked() {
  gameId.value = ''
  if (boot.value) boot.value.active_game = null
  error.value = '游戏已由另一页面接管，本页已自动退出。'
}
function parentSettings() {
  Object.assign(parent, boot.value?.policy || {}); parent.password = ''; parent.confirmation = ''; error.value = ''; modal.value = 'parent'
}
async function savePolicy() {
  await run(async () => {
    if (!boot.value.has_parent_password) {
      await api('/parent/password', { method: 'POST', body: JSON.stringify({ password: parent.password, confirmation: parent.confirmation }) })
      boot.value.has_parent_password = true
    }
    await api('/parent/policy', { method: 'PUT', body: JSON.stringify({ password: parent.password, word_count: parent.word_count, round_count: parent.round_count, perfect_reward_money: parent.perfect_reward_money }) })
    await refresh(); modal.value = ''; status.value = '家长设置已保存；已开始的当天仍使用冻结规则'
  })
}
async function openReport() { modal.value = 'report'; reportAI.value = ''; await run(loadReport) }
async function loadReport() { report.value = await api('/reports?' + new URLSearchParams({ start: reportStart.value, end: reportEnd.value })) }
async function openLogs() { modal.value = 'logs'; await run(async () => { logs.value = await api('/logs?' + new URLSearchParams({ search: logSearch.value, start: logStart.value, end: logEnd.value, event_type: logType.value })); settlements.value = await api('/settlements') }) }
function openAI(title = 'AI 助教') { aiTitle.value = title; aiText.value = ''; aiCached.value = false; aiWord.value = current.value; error.value = ''; modal.value = 'ai' }
async function generate(task: string, show = false) {
  if (show) openAI(task === 'note' ? 'AI 查词增强' : 'AI 助教')
  if (aiBusy.value) return
  error.value = ''; aiBusy.value = true
  try {
    const word = aiWord.value || current.value
    const result = await api('/ai', { method: 'POST', body: JSON.stringify({ task, word_id: word?.id || null, draft: !word && form.word.trim() && task !== 'error' ? { ...form } : null, question: aiQuestion.value, start: reportStart.value, end: reportEnd.value }) })
    if (task === 'report') reportAI.value = result.content
    else aiText.value = result.content
    aiCached.value = result.cached
  } catch (e) { error.value = (e as Error).message } finally { aiBusy.value = false }
}
async function batchCards() {
  openAI('批量学习卡')
  const batch = ids.value.map(id => wordCache.get(id)).filter((w): w is Word => !!w)
  if (!batch.length) { error.value = '请先勾选要生成学习卡的词条'; return }
  aiBusy.value = true
  try {
    for (const word of batch) {
      const result = await api('/ai', { method: 'POST', body: JSON.stringify({ task: 'note', word_id: word.id }) })
      aiText.value += `${word.word}\n${result.content}\n\n────────────────\n\n`
    }
  } catch (e) { error.value = (e as Error).message } finally { aiBusy.value = false }
}
onMounted(() => { void run(claimCurrentPage); void presence(); void checkVersion(); presenceTimer = window.setInterval(presence, 3000); versionTimer = window.setInterval(checkVersion, 30000) })
onUnmounted(() => { disposed = true; clearInterval(presenceTimer); clearInterval(versionTimer) })
</script>

<template>
  <GamePanel v-if="gameId" :id="gameId" :asr="!!boot?.capabilities.asr" @close="gameClosed" @revoked="gameRevoked" />
  <div v-else class="app-shell">
    <aside class="entry-panel">
      <div class="brand"><span class="brand-mark">W</span><span>WordLearner <small>网页版</small></span></div>
      <h1>单词学习</h1>
      <p class="summary-line">总分 <b>{{ boot?.summary.total_score || 0 }}</b><span>·</span>累计使用 <b>{{ duration(boot?.summary.usage_seconds || 0) }}</b></p>
      <label class="debug-toggle"><input type="checkbox" v-model="debugMode" @change="saveDebugPreference" /> 免麦克风复习模式</label>
      <p v-if="debugMode" class="debug-description">测试期间不限轮次、无需朗读锁定；实际打字、计时、正确率、总分、奖励和结算邮件均按正式复习记录，每日奖励不超过家长设置上限。</p>
      <form class="entry-form" @submit.prevent="save">
        <div v-if="editId" class="editing">正在编辑词条 <button type="button" class="text-button" @click="clearForm">取消编辑</button></div>
        <label for="word-input">英文</label><input id="word-input" v-model="form.word" maxlength="120" placeholder="输入单词或短语" autocomplete="off" spellcheck="false" lang="en" />
        <label for="translation-input">释义</label><input id="translation-input" v-model="form.translation" maxlength="1000" placeholder="中文释义" />
        <label for="phonetic-input">音标</label><input id="phonetic-input" v-model="form.phonetic" maxlength="200" placeholder="/ fəˈnetɪk /" />
        <label for="date-input">日期</label><input id="date-input" v-model="form.created_on" type="date" required />
        <div class="button-grid">
          <button type="button" :disabled="busy || lookupLoading" @click="lookup(true)">{{ lookupLoading ? '查询中…' : '查询' }}</button><button class="primary" :disabled="busy">{{ editId ? '保存修改' : '保存' }}</button>
          <button type="button" @click="speak()">发音</button><button type="button" class="primary" :disabled="busy" @click="startGame('practice')">开始练习</button>
          <button type="button" class="primary" :disabled="busy" @click="startGame('review')">一键复习</button><button type="button" class="danger" disabled title="真实累计分数保留，不提供手工清零">清零总分</button>
          <button type="button" class="span-two" @click="openReport">每日报告</button>
          <button type="button" @click="generate('note', true)">AI 查词增强</button><button type="button" @click="openAI()">AI 助教</button>
          <button type="button" @click="parentSettings">家长设置</button><button type="button" @click="openLogs">操作日志</button>
        </div>
      </form>
      <p class="status-text" role="status">{{ jobProgress || (busy ? '正在处理…' : status) }}</p>
      <div class="today-card"><span>今日奖励金</span><strong>¥{{ Number(boot?.summary.reward_money || 0).toFixed(2) }}</strong><small>{{ boot?.summary.daily ? '当天规则已冻结' : '开始复习时冻结当天规则' }} · {{ boot?.policy.word_count || 75 }} 词 / {{ boot?.policy.round_count || 2 }} 个奖励槽位 · 复习不限轮</small></div>
      <p class="version">WordLearner B/S · {{ version }}</p>
    </aside>
    <main class="workspace">
      <header class="workspace-heading"><div><span class="eyebrow">MY VOCABULARY</span><h2>我的词库 <span class="count">{{ boot?.summary.word_count || 0 }}</span></h2></div><span class="date-label">{{ boot?.summary.today || localDate() }}</span></header>
      <div v-if="boot?.mail_status?.latest" class="info-box" role="status">
        <template v-if="!boot.mail_status.enabled && boot.mail_status.pending_count">结算已保存，但邮件服务未启用，{{ boot.mail_status.pending_count }} 封邮件尚未发送。</template>
        <template v-else-if="boot.mail_status.latest.status === 'sent'">最近一封结算邮件已提交邮件服务器。若收件箱未显示，请检查垃圾邮件。</template>
        <template v-else-if="boot.mail_status.latest.status === 'pending' || boot.mail_status.latest.status === 'sending'">结算已保存，邮件正在排队发送；关闭浏览器后后台仍会处理。</template>
        <template v-else-if="boot.mail_status.latest.status === 'failed'">结算已保存，邮件发送失败，后台将自动重试。{{ boot.mail_status.latest.last_error }}</template>
        <template v-else>结算已保存，邮件需要检查：{{ boot.mail_status.latest.last_error || boot.mail_status.latest.status }}。请查看操作日志。</template>
      </div>
      <div v-if="updated" class="resume-banner"><span>服务器已更新，刷新页面即可使用新版本。</span><button @click="applyUpdate">刷新版本</button></div>
      <div v-if="boot?.active_game" class="resume-banner"><div><strong>有一场尚未完成的{{ boot.active_game.mode === 'practice' ? '练习' : boot.active_game.mode === 'debug' ? '免麦克风复习' : '复习' }}</strong><p>已保存轮次、活动词和实际学习进度。</p></div><button class="primary" :disabled="!boot.active_game.owned" @click="startGame(boot.active_game.mode)">{{ boot.active_game.owned ? '恢复进度' : '请在原浏览器继续' }}</button></div>
      <div v-for="(warning, index) in entryWarnings" :key="warning" class="alert error" role="alert">{{ warning }}<button class="text-button" @click="entryWarnings.splice(index, 1)">关闭</button></div>
      <div v-if="error && !modal" class="alert error" role="alert">{{ error }}<button class="text-button" @click="error = ''">关闭</button></div>
      <form class="filters" @submit.prevent="run(loadWords)"><label class="search-filter">搜索<input v-model="search" placeholder="单词或释义" /></label><label>开始<input v-model="start" type="date" /></label><label>结束<input v-model="end" type="date" /></label><button :disabled="busy">筛选</button><button type="button" @click="search = ''; start = localDate(-6); end = localDate(); run(loadWords)">重置</button></form>
      <div class="toolbar"><button @click="chooseAll">全选可见</button><button @click="selected = new Set()">取消选择</button><span class="toolbar-divider" /><button @click="edit()">编辑</button><button class="danger" @click="archive">删除</button><button class="primary" :disabled="busy" @click="startGame('review')">一键复习</button><button @click="openReport">每日报告</button><button @click="openLogs">操作日志</button><button @click="run(refresh)">刷新</button></div>
      <div class="table-topline"><span>显示 {{ words.length }} 个词条<span v-if="selected.size"> · 已选 {{ selected.size }} 个</span></span><div><button class="text-button" @click="batchCards">批量学习卡</button><span>·</span><button class="text-button" @click="openAI()">AI 助教</button></div></div>
      <div class="table-container"><table class="word-table"><thead><tr><th class="check-column"><span class="sr-only">选择</span></th><th>日期</th><th>单词</th><th>音标</th><th>释义</th><th>练习</th><th>正确率</th><th>掌握度</th><th>下次复习</th></tr></thead>
        <tbody><tr v-for="word in words" :key="word.id" :class="{ selected: selected.has(word.id), focused: focused === word.id }" tabindex="0" @click="focused = word.id" @keydown.space.prevent="toggle(word.id)" @dblclick="edit(word)"><td><input type="checkbox" :aria-label="`选择 ${word.word}`" :checked="selected.has(word.id)" @click.stop @change="toggle(word.id)" /></td><td class="date-cell">{{ word.created_on }}</td><td class="word-cell">{{ word.word }}</td><td class="phonetic-cell">{{ word.phonetic || '—' }}</td><td class="translation-cell" :title="word.translation">{{ word.translation || '—' }}</td><td>{{ word.practice_count }}</td><td>{{ word.accuracy }}%</td><td><span class="mastery-value">{{ word.mastery }}%</span><div class="mastery-track"><i :style="{ width: word.mastery + '%' }" /></div></td><td class="date-cell">{{ word.due_on }}</td></tr></tbody>
      </table><div v-if="!words.length" class="empty-state"><div class="empty-icon">Aa</div><h3>{{ boot?.summary.word_count ? '没有符合条件的词条' : '从第一个单词开始' }}</h3><p>{{ boot?.summary.word_count ? '调整搜索条件或日期范围，查看其他单词。' : '在左侧录入英文、释义和音标，保存后即可开始练习。' }}</p><button v-if="boot?.summary.word_count" @click="search = ''; start = ''; end = ''; run(loadWords)">查看全部词条</button><label v-else for="word-input" class="text-button">录入第一个单词 →</label></div></div>
      <footer class="workspace-footer"><span>勾选单词开始练习，双击词条编辑</span><span>学习记录自动保存</span></footer>
    </main>
  </div>

  <AppModal v-if="modal === 'parent'" title="家长设置" @close="modal = ''; error = ''">
    <p class="muted">设置每日复习量和奖励。当天首次开始后规则冻结，修改只影响尚未开始的新一天。</p>
    <div v-if="boot?.summary.daily" class="info-box">今日使用：{{ boot.summary.daily.policy.word_count }} 词 · {{ boot.summary.daily.policy.round_count }} 个奖励槽位 · 全对 ¥{{ boot.summary.daily.policy.perfect_reward_money.toFixed(2) }} · 复习不限轮</div>
    <form class="settings-form" @submit.prevent="savePolicy"><label>{{ boot?.has_parent_password ? '家长密码' : '设置家长密码（至少 6 位）' }}<input v-model="parent.password" type="password" minlength="6" required autocomplete="current-password" /></label><label v-if="!boot?.has_parent_password">再次输入密码<input v-model="parent.confirmation" type="password" minlength="6" required autocomplete="new-password" /></label><label>每日单词数<input v-model.number="parent.word_count" type="number" min="10" max="200" required /><small>10–200 个；必选新词过多时可超过目标数量</small></label><label>奖励槽位<select v-model.number="parent.round_count"><option :value="1">1 个</option><option :value="2">2 个</option><option :value="3">3 个</option></select><small>复习可以继续多轮；每轮按顺序计入槽位，并取各槽位最佳实际成绩。</small></label><label>全对奖励金额（元）<input v-model.number="parent.perfect_reward_money" type="number" min="0" max="100" step="0.01" required /></label><p v-if="error" role="alert" class="alert error">{{ error }}</p><div class="modal-actions"><button type="button" @click="modal = ''">取消</button><button class="primary" :disabled="busy">保存设置</button></div></form>
  </AppModal>

  <AppModal v-if="modal === 'report'" title="每日报告" wide @close="modal = ''; error = ''">
    <form class="report-controls" @submit.prevent="run(loadReport)"><label>开始<input v-model="reportStart" type="date" required /></label><label>结束<input v-model="reportEnd" type="date" required /></label><button class="primary" :disabled="busy">刷新报告</button></form>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div class="report-summary"><div><span>练习次数</span><strong>{{ reportCounts.practiced }}</strong></div><div><span>复习用时</span><strong>{{ duration(reportCounts.seconds) }}</strong></div><div><span>奖励金</span><strong>¥{{ Number(report?.reward_money || 0).toFixed(2) }}</strong></div></div>
    <p v-if="report?.mastery_summary" class="muted">正确率 {{ reportCounts.practiced ? Math.round(report.daily.reduce((n: number, d: any) => n + d.correct, 0) / reportCounts.practiced * 100) : 0 }}% · 到期 {{ report.mastery_summary.due }} · 高掌握 {{ report.mastery_summary.high }} · 中等 {{ report.mastery_summary.medium }} · 薄弱 {{ report.mastery_summary.low }} · 新词 {{ report.mastery_summary.new }}</p>
    <div class="retention"><span>记忆保持趋势 <small>根据当前学习状态估算</small></span><svg viewBox="0 0 920 180" role="img" aria-label="未来30天记忆保持趋势"><line x1="40" y1="145" x2="890" y2="145" stroke="#d2d2d7" /><line x1="40" y1="35" x2="890" y2="35" stroke="#ececf0" stroke-dasharray="4" /><text x="0" y="40">100%</text><text x="10" y="149">0%</text><polyline :points="curve" fill="none" stroke="#0071e3" stroke-width="2.5" /><text x="40" y="172">今天</text><text x="445" y="172">15 天后</text><text x="835" y="172">30 天后</text></svg></div>
    <div class="tabs"><button v-for="tab in [{ id: 'daily', label: '每日统计' }, { id: 'history', label: '历史记录' }, { id: 'mastery', label: '掌握程度' }, { id: 'ai', label: 'AI 总结' }]" :key="tab.id" :class="{ active: reportTab === tab.id }" @click="reportTab = tab.id">{{ tab.label }}</button></div>
    <div class="report-table" v-if="reportTab === 'daily'"><table><thead><tr><th>日期</th><th>一键清单</th><th>练习次数</th><th>不重复单词</th><th>正确</th><th>正确率</th><th>奖励金</th><th>奖励轮次</th></tr></thead><tbody><tr v-for="row in report?.daily" :key="row.day"><td>{{ row.day }}</td><td>{{ row.review_count }} 词（新 {{ row.required_count }} / 旧 {{ row.old_count }}）</td><td>{{ row.practiced }}</td><td>{{ row.unique_words }}</td><td>{{ row.correct }}</td><td>{{ row.accuracy }}%</td><td>¥{{ row.reward_money.toFixed(2) }} / {{ row.review_policy ? '¥' + row.review_policy.perfect_reward_money.toFixed(2) : '—' }}</td><td>{{ row.rounds.length }} / {{ row.review_policy?.round_count || '—' }}</td></tr></tbody></table><p v-if="!report?.daily.length" class="empty-small">所选日期暂无学习记录</p></div>
    <div class="report-table" v-if="reportTab === 'history'"><table><thead><tr><th>时间</th><th>单词</th><th>释义</th><th>结果</th><th>质量</th><th>间隔</th><th>下次复习</th></tr></thead><tbody><tr v-for="row in report?.history" :key="row.id"><td>{{ row.practiced_at.replace('T', ' ').slice(0, 19) }}</td><td class="word-cell">{{ row.word }}</td><td>{{ row.translation }}</td><td :class="row.correct ? 'success-text' : 'danger-text'">{{ row.correct ? '正确' : '错误' }}</td><td>{{ row.quality }}</td><td>{{ row.interval_days }} 天</td><td>{{ row.due_on }}</td></tr></tbody></table><p v-if="!report?.history.length" class="empty-small">所选日期暂无学习记录</p><p v-if="report?.history_total > 1000" class="muted">显示最近 1,000 条，请缩小日期范围查看更多。</p></div>
    <div class="report-table" v-if="reportTab === 'mastery'"><table><thead><tr><th>单词</th><th>释义</th><th>掌握度</th><th>正确率</th><th>间隔</th><th>下次复习</th></tr></thead><tbody><tr v-for="row in report?.mastery" :key="row.id"><td class="word-cell">{{ row.word }} <small v-if="row.archived">已归档</small></td><td>{{ row.translation }}</td><td>{{ row.mastery }}%</td><td>{{ row.accuracy }}%</td><td>{{ row.interval_days }} 天</td><td>{{ row.due_on }}</td></tr></tbody></table></div>
    <div v-if="reportTab === 'ai'" class="ai-report"><button class="primary" :disabled="aiBusy" @click="generate('report')">{{ aiBusy ? '正在生成…' : '生成 AI 总结' }}</button><pre class="ai-output">{{ reportAI || '根据所选日期范围的真实学习记录，生成学习建议。' }}</pre></div>
  </AppModal>

  <AppModal v-if="modal === 'ai'" :title="aiTitle" wide @close="modal = ''; error = ''">
    <div class="ai-context"><span>当前词条</span><strong>{{ aiWord?.word || form.word || '未选择' }}</strong><span>{{ aiWord?.translation }}</span></div>
    <div class="toolbar"><button :disabled="aiBusy" @click="generate('note')">学习卡</button><button :disabled="aiBusy" @click="generate('error')">错词提示</button><button :disabled="aiBusy" @click="generate('entry')">录入检测</button><button :disabled="aiBusy" @click="aiWord = current; aiText = ''">刷新选中词</button><button :disabled="aiBusy" @click="batchCards">批量学习卡</button><span class="spacer" /><small v-if="aiCached">已读取缓存</small></div>
    <p v-if="!boot?.capabilities.ai" class="info-box">AI 服务尚未配置。配置完成后可使用学习卡、错词提示和问答。</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p><pre class="ai-output">{{ aiBusy && !aiText ? '正在生成，请稍候…' : aiText || '选择一个词条生成学习卡，或在下方输入学习问题。' }}</pre>
    <form class="ai-chat" @submit.prevent="generate('chat')"><input v-model="aiQuestion" placeholder="问问助教：这个词应该怎么记？" aria-label="学习问题" maxlength="2000" required /><button class="primary" :disabled="aiBusy">发送</button></form>
  </AppModal>

  <AppModal v-if="modal === 'logs'" title="操作日志" wide @close="modal = ''; error = ''">
    <form class="report-controls" @submit.prevent="openLogs"><label class="search-filter">搜索<input v-model="logSearch" placeholder="操作类型或关键词" /></label><label>开始<input v-model="logStart" type="date" /></label><label>结束<input v-model="logEnd" type="date" /></label><label>类型<input v-model="logType" placeholder="全部类型" /></label><button>刷新</button></form>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div class="report-table log-table"><table><thead><tr><th>时间</th><th>操作类型</th><th>说明</th></tr></thead><tbody><tr v-for="log in logs" :key="log.id" tabindex="0" @click="detail = JSON.stringify(JSON.parse(log.detail_json), null, 2)" @keydown.enter="detail = JSON.stringify(JSON.parse(log.detail_json), null, 2)"><td>{{ log.occurred_at.replace('T', ' ').slice(0, 19) }}</td><td>{{ log.event_type }}</td><td>{{ log.summary }}</td></tr></tbody></table><p v-if="!logs.length" class="empty-small">暂无匹配的操作日志</p></div><pre v-if="detail" class="log-detail">{{ detail }}</pre>
    <h3 class="subheading">邮件结算事件</h3><p class="muted">{{ boot?.capabilities.email ? '邮件服务已启用，失败任务按退避间隔自动重试。' : '结算快照已保存。邮件尚未启用，可在独立配置中启用。' }}</p><div class="report-table"><table><thead><tr><th>日期</th><th>创建时间</th><th>状态</th></tr></thead><tbody><tr v-for="item in settlements" :key="item.id"><td>{{ item.day }}</td><td>{{ item.created_at.replace('T', ' ').slice(0, 19) }}</td><td>{{ ({ pending: '待发送', sending: '发送中', sent: '已发送', failed: '失败，等待重试', uncertain: '发送结果待确认', invalid: '数据校验失败' } as Record<string, string>)[item.status] || item.status }}<small v-if="item.last_error"> · {{ item.last_error }}</small></td></tr></tbody></table><p v-if="!settlements.length" class="empty-small">暂无结算事件</p></div>
  </AppModal>
</template>
