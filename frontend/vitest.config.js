import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

/* Two suites with different needs.
 *
 * `tests/unit` is module tests: plain functions, no DOM. The polling helper is
 * deliberately DOM-optional — it guards its visibility check with
 * `typeof document === 'undefined'` — so those run in node with nothing loaded.
 *
 * `tests/component` mounts real Vue components, which needs a DOM. happy-dom
 * rather than jsdom: it is markedly faster to start and everything these
 * components touch (inputs, files, drag events, confirm) is supported.
 *
 * They are one project so `npm test` runs both; the environment is chosen per
 * directory rather than globally, because giving the module tests a DOM would
 * quietly hide the fact that the polling helper works without one.
 */
export default defineConfig({
  plugins: [vue()],
  test: {
    include: ['tests/unit/**/*.spec.js', 'tests/component/**/*.spec.js'],
    // node by default; component files opt in with a `@vitest-environment
    // happy-dom` docblock. environmentMatchGlobs used to do this centrally and
    // was removed in Vitest 4 — silently, so every mount failed with
    // "document is not defined" rather than a config error.
    environment: 'node',
  },
})
