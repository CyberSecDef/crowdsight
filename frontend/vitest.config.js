import { defineConfig } from 'vitest/config'

/* Node environment: the polling helper is deliberately DOM-optional (it guards
   its visibility check with `typeof document === 'undefined'`), so it can be
   tested without pulling in a DOM implementation. Component tests in Step 7
   will need one; these do not. */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/unit/**/*.spec.js'],
  },
})
