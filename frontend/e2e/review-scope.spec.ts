import { expect, test } from '@playwright/test'

test('初二上范围显示标签并启动75词复习', async ({ page, request }) => {
  await request.post('/api/test-reset', { headers: { 'X-WordLearner-Request': '1' } })
  const seeded = await request.post('/api/test-seed-grade8', { headers: { 'X-WordLearner-Request': '1' } })
  expect((await seeded.json()).tagged).toBe(80)
  await page.goto('/')
  await expect(page.getByLabel('一键复习范围')).toContainText('初二上（80词）')
  await expect(page.locator('.tag-badge').first()).toHaveText('初二上')
  await page.getByLabel('一键复习范围').selectOption('grade8_upper')
  await page.getByRole('button', { name: '一键复习', exact: true }).first().click()
  await expect(page.locator('.game-stage .sr-only')).toContainText('一键复习，范围 初二上', { timeout: 30000 })
  await expect(page.locator('canvas')).toBeVisible()
})
