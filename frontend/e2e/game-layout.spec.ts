import { test, expect } from '@playwright/test'

test('desktop canvas layout: long definitions stay in distinct lanes and survive resize', async ({ page }) => {
  await page.addInitScript(() => {
    const proto = CanvasRenderingContext2D.prototype
    const fillRect = proto.fillRect, fillText = proto.fillText, moveTo = proto.moveTo, lineTo = proto.lineTo, stroke = proto.stroke
    ;(window as any).drawnText = []
    ;(window as any).beamLines = []
    proto.fillRect = function (x, y, w, h) {
      if (x === 0 && y === 0 && w > 800 && h > 500) (window as any).drawnText = []
      return fillRect.call(this, x, y, w, h)
    }
    proto.fillText = function (text, x, y, maxWidth?) {
      ;(window as any).drawnText.push({ text, x, y, maxWidth, width: this.measureText(text).width, font: this.font })
      if (maxWidth === undefined) return fillText.call(this, text, x, y)
      return fillText.call(this, text, x, y, maxWidth)
    }
    proto.moveTo = function (x, y) {
      ;(this as any).__testMove = { x, y }
      return moveTo.call(this, x, y)
    }
    proto.lineTo = function (x, y) {
      ;(this as any).__testLine = { x, y }
      return lineTo.call(this, x, y)
    }
    proto.stroke = function () {
      if (this.strokeStyle === '#0a84ff' && this.lineWidth === 2 && (this as any).__testMove && (this as any).__testLine) {
        ;(window as any).beamLines.push({ from: (this as any).__testMove, to: (this as any).__testLine })
      }
      return stroke.call(this)
    }
  })
  await page.goto('/')
  const ownerHeaders = { 'X-WordLearner-Page': await page.evaluate(() => sessionStorage.getItem('wordlearner-page-id') || '') }
  const definitions = [
    ['bed', 'n. 床，床铺，床位；底部，基座，花坛，苗圃；河床，岩层；固定，嵌入，与某人上床'],
    ['car', 'n. 汽车，轿车，车厢；电梯厢，吊舱；交通工具，铁路车辆，赛车和其他各种车型'],
    ['book', 'n. 书，书籍，本子，簿册；著作，卷，篇；预订，登记，预约；把旅客记入名单'],
  ]
  for (const [word, translation] of definitions) {
    const response = await page.request.post('/api/words', { headers: { 'X-WordLearner-Request': '1' }, data: { word, translation, phonetic: '[test]', created_on: new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }) } })
    expect(response.ok()).toBe(true)
  }
  await page.reload()
  await page.getByRole('button', { name: '全选可见' }).click()
  await page.getByRole('button', { name: '开始练习', exact: true }).click()
  await expect(page.locator('canvas')).toBeVisible()
  const id = (await (await page.request.get('/api/bootstrap', { headers: ownerHeaders })).json()).active_game.id
  const game = async () => (await page.request.get(`/api/games/${id}`, { headers: ownerHeaders })).json()
  await expect.poll(async () => (await game()).active.length, { timeout: 15000 }).toBe(3)
  const check = async () => {
    const info = await page.evaluate(() => {
      const canvas = document.querySelector('canvas')!, rect = canvas.getBoundingClientRect()
      return { rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, text: (window as any).drawnText,
        canvasWidth: canvas.width, dpr: devicePixelRatio, documentHeight: document.documentElement.scrollHeight }
    })
    expect(info.rect.x).toBe(0); expect(info.rect.y).toBe(0)
    expect(info.canvasWidth).toBe(Math.round(info.rect.width * info.dpr))
    expect(info.text.find((item: any) => item.text.startsWith('普通练习'))).toMatchObject({ x: 18, y: 16 })
    const prompts = info.text.filter((item: any) => item.font.includes('24px') && item.text.startsWith('n.'))
    expect(prompts).toHaveLength(3)
    expect(prompts.every((item: any) => item.maxWidth === undefined && item.text.endsWith('…'))).toBe(true)
    prompts.sort((a: any, b: any) => a.x - b.x)
    for (let i = 1; i < prompts.length; i++) expect(prompts[i - 1].x + prompts[i - 1].width / 2).toBeLessThan(prompts[i].x - prompts[i].width / 2)
    await expect(page.getByRole('button', { name: '保存并返回' })).toBeInViewport()
    return prompts
  }
  await check()
  await page.screenshot({ path: '../test-results/game-layout-980.png' })
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect.poll(async () => (await page.locator('canvas').boundingBox())?.width).toBe(1440)
  const prompts = await check()
  await page.screenshot({ path: '../test-results/game-layout-1440.png' })
  await page.mouse.move(prompts[0].x, prompts[0].y)
  await expect(page.getByRole('tooltip')).toBeVisible()
  const fullText = await page.getByRole('tooltip').innerText()
  expect(definitions.some(([, text]) => text === fullText)).toBe(true)
  const carPrompt = prompts.find((item: any) => item.text.startsWith('n. 汽车'))
  expect(carPrompt).toBeTruthy()
  await page.evaluate(() => { (window as any).beamLines = [] })
  await page.keyboard.press('z')
  await page.waitForTimeout(250)
  expect(await page.evaluate(() => (window as any).beamLines.length)).toBe(0)
  await page.keyboard.press('c')
  await expect.poll(async () => page.evaluate(() => (window as any).beamLines.length)).toBeGreaterThan(0)
  await page.waitForTimeout(220)
  const finalBeam = await page.evaluate(() => (window as any).beamLines.at(-1))
  const beamX = finalBeam.to.x - finalBeam.from.x, beamY = finalBeam.to.y - finalBeam.from.y
  const targetX = carPrompt.x - finalBeam.from.x, targetY = carPrompt.y + 30 - finalBeam.from.y
  const distanceFromTargetRay = Math.abs(beamX * targetY - beamY * targetX) / Math.hypot(beamX, beamY)
  expect(beamX * targetX + beamY * targetY).toBeGreaterThan(0)
  expect(distanceFromTargetRay).toBeLessThan(12)
  expect((await game()).score).toBe(0)
  await page.getByRole('button', { name: '保存并返回' }).click()
  const words = await (await page.request.get('/api/words')).json()
  expect(words.every((word: any) => definitions.some(([text, translation]) => word.word === text && word.translation === translation))).toBe(true)
})
