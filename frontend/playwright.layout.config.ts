import { defineConfig } from '@playwright/test'
import base from './playwright.config'
import { resolve } from 'node:path'

export default defineConfig({
  ...base, testIgnore: [], testMatch: '**/game-layout.spec.ts',
  use: { ...base.use, baseURL: 'http://127.0.0.1:8767', viewport: { width: 980, height: 700 } },
  webServer: {
    command: '..\\.venv\\Scripts\\python.exe -m uvicorn tests.e2e_app:app --app-dir .. --host 127.0.0.1 --port 8767 --log-level warning',
    url: 'http://127.0.0.1:8767/api/health', reuseExistingServer: false,
    env: { WORDLEARNER_BS_DATA_DIR: resolve('..', 'test-results', 'layout-db-' + Date.now()) },
  },
})
