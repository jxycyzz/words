import { test, expect } from '@playwright/test'

test('automatic lookup, manual query, partial responses and user edits', async ({ page }) => {
  let calls = 0
  await page.route('**/api/lookup?*', async route => {
    calls++
    const word = new URL(route.request().url()).searchParams.get('word')
    await new Promise(resolve => setTimeout(resolve, 250))
    await route.fulfill({ json: { word, translation: word === 'bed' ? 'n. 床' : 'n. 太阳', phonetic: word === 'bed' ? '[bed]' : '[sʌn]' } })
  })
  await page.goto('/')
  await page.getByLabel('英文', { exact: true }).fill('bed')
  await expect(page.getByLabel('释义', { exact: true })).toHaveValue('n. 床')
  await expect(page.getByLabel('音标', { exact: true })).toHaveValue('[bed]')
  await page.getByLabel('英文', { exact: true }).fill('sun')
  await expect(page.getByLabel('释义', { exact: true })).toHaveValue('n. 太阳')
  await expect(page.getByLabel('音标', { exact: true })).toHaveValue('[sʌn]')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await page.getByLabel('释义', { exact: true }).fill('我手动输入的释义')
  await expect(page.getByRole('button', { name: '查询', exact: true })).toBeEnabled()
  await expect(page.getByLabel('释义', { exact: true })).toHaveValue('我手动输入的释义')
  expect(calls).toBe(3)
  await page.unroute('**/api/lookup?*')
  await page.route('**/api/lookup?*', route => route.fulfill({ json: { word: 'sun', translation: '太阳', phonetic: '', warning: '未查到音标，请手动补充' } }))
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.getByLabel('释义', { exact: true })).toHaveValue('太阳')
  await expect(page.getByLabel('音标', { exact: true })).toHaveValue('[sʌn]')
})

test('late responses cannot fill another word; save awaits pending lookup', async ({ page }) => {
  let saved: any
  await page.route('**/api/lookup?*', async route => {
    const word = new URL(route.request().url()).searchParams.get('word')
    await new Promise(resolve => setTimeout(resolve, word === 'bed' ? 800 : 30))
    await route.fulfill({ json: { word, translation: word === 'bed' ? '床' : '太阳', phonetic: word === 'bed' ? '[bed]' : '[sʌn]' } })
  })
  await page.route('**/api/words', async route => {
    if (route.request().method() !== 'POST') return route.continue()
    saved = route.request().postDataJSON()
    await route.fulfill({ json: { ...saved, id: 999 } })
  })
  await page.goto('/')
  await page.getByLabel('英文', { exact: true }).fill('bed')
  await expect(page.getByRole('button', { name: '查询中…' })).toBeVisible()
  await page.getByLabel('英文', { exact: true }).fill('sun')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => saved?.phonetic).toBe('[sʌn]')
  expect(saved.word).toBe('sun')
  expect(saved.translation).toBe('太阳')
  await expect(page.getByText('已保存：sun', { exact: true })).toBeVisible()
  await expect(page.getByLabel('释义', { exact: true })).toHaveValue('')
})
