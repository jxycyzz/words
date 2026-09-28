import { test, expect } from '@playwright/test'

test('browser microphone WAV, voice lock, real typing, close settlement and resume', async ({ page }) => {
  await page.request.post('/api/test-reset', { headers: { 'X-WordLearner-Request': '1' } })
  await page.goto('/')
  const ownerHeaders = { 'X-WordLearner-Page': await page.evaluate(() => sessionStorage.getItem('wordlearner-page-id') || '') }
  // This server is the isolated tests.e2e_app; never the user's running server.
  const headers = { 'X-WordLearner-Request': '1' }
  const existing = await (await page.request.get('/api/words')).json()
  if (existing.length) await page.request.post('/api/words/archive', { headers, data: { ids: existing.map((w: any) => w.id) } })
  await page.request.post('/api/words', { headers, data: { word: 'voiceword', translation: '语音测试词', phonetic: '[test]', created_on: new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }) } })
  await page.reload()
  await page.getByRole('button', { name: '一键复习', exact: true }).first().click()
  await expect(page.locator('canvas')).toBeVisible()
  const bootstrap = await (await page.request.get('/api/bootstrap', { headers: ownerHeaders })).json()
  const id = bootstrap.active_game.id
  const game = async () => (await page.request.get(`/api/games/${id}`, { headers: ownerHeaders })).json()
  await expect.poll(async () => (await game()).active.length).toBe(1)
  await page.locator('canvas').focus()
  await page.keyboard.type('voiceword')
  await expect.poll(async () => (await game()).score).toBe(0)
  await page.keyboard.down('Space')
  await expect(page.getByText('请读出屏幕上的英文单词…')).toBeVisible()
  await page.waitForTimeout(350)
  await page.keyboard.up('Space')
  await expect.poll(async () => !!(await game()).locked).toBe(true)
  await expect(page.locator('.game-message')).toContainText('已锁定')
  await page.keyboard.type('voiceword', { delay: 30 })
  await expect.poll(async () => (await game()).saved_reward_money).toBe(2)
  page.on('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '保存并返回' }).click()
  await expect(page.getByRole('heading', { name: '单词学习', exact: true })).toBeVisible()
  const events = await (await page.request.get('/api/settlements')).json()
  const snapshot = events.find((event: any) => event.session_id === id).payload
  expect(snapshot.reward_money).toBe(2)
  expect(snapshot.practiced).toBe(1)
  await page.getByRole('button', { name: '恢复进度', exact: true }).click()
  await expect(page.locator('canvas')).toBeVisible()
  await expect.poll(async () => (await game()).round).toBe(2)
  await page.getByRole('button', { name: '保存并返回' }).click()
  await expect(page.getByRole('heading', { name: '单词学习', exact: true })).toBeVisible()
})
