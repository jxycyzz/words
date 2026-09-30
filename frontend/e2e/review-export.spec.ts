import { expect, test } from '@playwright/test'

test('完成第一轮时自动下载当天单词并保留手动下载入口', async ({ page, request }) => {
  await request.post('/api/test-reset', { headers: { 'X-WordLearner-Request': '1' } })
  const day = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
  const saved = await request.post('/api/words', {
    headers: { 'X-WordLearner-Request': '1' },
    data: { word: 'cat', translation: '猫', phonetic: '/kæt/', created_on: day },
  })
  expect(saved.ok()).toBe(true)

  await page.goto('/')
  await page.getByLabel('免麦克风复习模式').check()
  await page.getByRole('button', { name: '一键复习', exact: true }).first().click()
  await expect(page.locator('canvas')).toBeVisible({ timeout: 30000 })
  const ownerHeaders = { 'X-WordLearner-Page': await page.evaluate(() => sessionStorage.getItem('wordlearner-page-id') || '') }
  const active = async () => {
    const boot = await (await page.request.get('/api/bootstrap', { headers: ownerHeaders })).json()
    const game = await (await page.request.get(`/api/games/${boot.active_game.id}`, { headers: ownerHeaders })).json()
    return game.active.length
  }
  await expect.poll(active, { timeout: 15000 }).toBeGreaterThan(0)

  const automatic = page.waitForEvent('download')
  await page.keyboard.type('cat')
  expect((await automatic).suggestedFilename()).toMatch(/^当天单词记录\d{8}\.xlsx$/)
  await expect(page.locator('.export-status')).toContainText('已下载')

  const manual = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载当天单词' }).click()
  expect((await manual).suggestedFilename()).toMatch(/^当天单词记录\d{8}\.xlsx$/)
})
