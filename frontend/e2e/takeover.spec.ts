import { test, expect } from '@playwright/test'

test('latest page takes over and the previous game page exits automatically', async ({ page, context }) => {
  await page.request.post('/api/test-reset', { headers: { 'X-WordLearner-Request': '1' } })
  await page.goto('/')
  await page.getByLabel('英文', { exact: true }).fill('cat')
  await page.getByLabel('释义', { exact: true }).fill('猫')
  await page.getByLabel('音标', { exact: true }).fill('[kæt]')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await page.getByLabel('选择 cat', { exact: true }).check()
  await page.getByRole('button', { name: '开始练习', exact: true }).click()
  await expect(page.locator('canvas')).toBeVisible()

  const latest = await context.newPage()
  await latest.goto('/')
  await expect(latest.getByRole('button', { name: '恢复进度', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '单词学习', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toContainText('游戏已由另一页面接管，本页已自动退出')

  await latest.getByRole('button', { name: '恢复进度', exact: true }).click()
  await expect(latest.locator('canvas')).toBeVisible()
  await expect(latest.getByRole('button', { name: '保存并返回' })).toBeVisible()
})
