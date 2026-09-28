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
  await expect(page.getByTestId('voice-transcript')).toContainText('等待朗读')
  await expect(page.getByLabel('输入设备')).toBeVisible()
  await expect(page.getByTestId('voice-transcript')).toContainText('按住空格时可在这里查看实时采集')
  await page.evaluate(() => {
    const mediaDevices = navigator.mediaDevices
    const original = mediaDevices.getUserMedia.bind(mediaDevices)
    ;(window as any).__getUserMediaCalls = 0
    Object.defineProperty(mediaDevices, 'getUserMedia', {
      configurable: true,
      value: async (constraints: MediaStreamConstraints) => {
        ;(window as any).__getUserMediaCalls += 1
        if ((window as any).__getUserMediaCalls === 1) await new Promise(resolve => setTimeout(resolve, 450))
        return original(constraints)
      },
    })
  })
  await page.locator('canvas').focus()
  await page.keyboard.type('voiceword')
  await expect.poll(async () => (await game()).score).toBe(0)
  await page.keyboard.down('Space')
  await page.waitForTimeout(70)
  await page.keyboard.up('Space')
  await expect(page.getByTestId('voice-transcript')).toContainText('等待麦克风就绪后自动识别')
  await expect.poll(async () => !!(await game()).locked).toBe(true)
  await expect(page.locator('.game-message')).toContainText('已锁定')
  await expect(page.getByTestId('voice-transcript')).toContainText('voiceword')
  await expect(page.getByTestId('voice-transcript')).toContainText('已锁定')
  await expect(page.getByTestId('voice-transcript')).toContainText('已上传')
  await expect(page.getByTestId('voice-transcript')).toContainText('采集')
  await expect(page.getByTestId('voice-transcript')).toContainText('帧')
  await page.keyboard.type('voiceword', { delay: 30 })
  await expect.poll(async () => (await game()).saved_reward_money).toBe(2)
  await expect.poll(async () => (await game()).round).toBe(2)
  await expect.poll(async () => (await game()).active.length).toBe(1)
  await expect.poll(async () => (await game()).locked).toBeNull()
  await page.locator('canvas').focus()
  await page.keyboard.down('Space')
  await page.waitForTimeout(300)
  await page.keyboard.up('Space')
  await expect.poll(async () => !!(await game()).locked).toBe(true)
  expect(await page.evaluate(() => (window as any).__getUserMediaCalls)).toBe(1)
  await expect(page.getByTestId('voice-transcript')).toContainText('服务识别')
  await expect(page.getByTestId('voice-transcript')).toContainText('总响应')
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

test('no-microphone review can diagnose microphone without locking or recording a result', async ({ page }) => {
  await page.request.post('/api/test-reset', { headers: { 'X-WordLearner-Request': '1' } })
  await page.goto('/')
  const ownerHeaders = { 'X-WordLearner-Page': await page.evaluate(() => sessionStorage.getItem('wordlearner-page-id') || '') }
  const headers = { 'X-WordLearner-Request': '1' }
  const existing = await (await page.request.get('/api/words')).json()
  if (existing.length) await page.request.post('/api/words/archive', { headers, data: { ids: existing.map((w: any) => w.id) } })
  await page.request.post('/api/words', { headers, data: { word: 'voiceword', translation: '语音测试词', phonetic: '[test]', created_on: new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }) } })
  await page.reload()
  await page.getByRole('checkbox', { name: '免麦克风复习模式' }).check()
  await page.getByRole('button', { name: '一键复习', exact: true }).first().click()
  await expect(page.locator('canvas')).toBeVisible()
  const bootstrap = await (await page.request.get('/api/bootstrap', { headers: ownerHeaders })).json()
  const id = bootstrap.active_game.id
  const game = async () => (await page.request.get(`/api/games/${id}`, { headers: ownerHeaders })).json()
  await expect.poll(async () => (await game()).active.length).toBe(1)
  const historyBefore = (await (await page.request.get('/api/reports', { headers: ownerHeaders })).json()).history_total
  const microphone = page.locator('.voice-button')
  await expect(microphone).toHaveText('按住测试麦克风')

  await microphone.dispatchEvent('pointerdown')
  await expect(page.getByText('请读出屏幕上的英文单词…')).toBeVisible()
  await page.waitForTimeout(350)
  await microphone.dispatchEvent('pointerup')

  await expect(page.getByTestId('voice-transcript')).toContainText('voiceword')
  await expect(page.getByTestId('voice-transcript')).toContainText('麦克风采音正常')
  await expect(page.getByTestId('voice-transcript')).toContainText('已上传')
  const after = await game()
  expect(after.locked).toBeNull()
  expect(after.score).toBe(0)
  expect((await (await page.request.get('/api/reports', { headers: ownerHeaders })).json()).history_total).toBe(historyBefore)
  page.on('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '保存并返回' }).click()
})
