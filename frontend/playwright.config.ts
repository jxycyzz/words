import { defineConfig } from '@playwright/test'
import { resolve } from 'node:path'

export default defineConfig({
  testDir: './e2e', workers: 1, fullyParallel: false, timeout: 45000,
  testIgnore: '**/game-layout.spec.ts',
  outputDir: '../test-results/browser-artifacts',
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8766', channel: 'chrome', headless: true, viewport: { width: 1440, height: 1000 }, screenshot: 'only-on-failure', trace: 'retain-on-failure',
    launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] } },
  webServer: {
    command: '..\\.venv\\Scripts\\python.exe -m uvicorn tests.e2e_app:app --app-dir .. --host 127.0.0.1 --port 8766 --log-level warning',
    url: 'http://127.0.0.1:8766/api/health', reuseExistingServer: false,
    env: { WORDLEARNER_BS_DATA_DIR: resolve('..', 'test-results', 'browser-db-' + Date.now()) },
  },
})
