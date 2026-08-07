import { expect, test } from '@playwright/test'

/**
 * The shell, in a real browser.
 *
 * The HTTP-level checks in scripts/verify_frontend.sh prove the right bytes are
 * served. They cannot prove the page renders, that the app mounted, or that the
 * fields the views read are the fields the API sends — a view reading a field
 * that does not exist still returns 200 and still paints, it just paints
 * "unknown" everywhere. That failure mode is the reason this file exists.
 */

const ROUTES = [
  { path: '/', heading: 'Projects' },
  { path: '/runs', heading: 'Run history' },
  { path: '/graphs/new', heading: 'Build a graph' },
]

/** Collect everything the browser complains about, so a test can assert none. */
function watch(page) {
  const problems = { console: [], pageErrors: [], failedRequests: [], csp: [] }

  page.on('console', (message) => {
    if (message.type() !== 'error') return
    const text = message.text()
    if (/Content Security Policy/i.test(text)) problems.csp.push(text)
    else problems.console.push(text)
  })
  page.on('pageerror', (error) => problems.pageErrors.push(String(error)))
  page.on('requestfailed', (request) =>
    problems.failedRequests.push(`${request.method()} ${request.url()} — ${request.failure()?.errorText}`),
  )
  page.on('response', (response) => {
    if (response.status() >= 400) {
      problems.failedRequests.push(`${response.status()} ${response.url()}`)
    }
  })
  return problems
}

test.describe('application shell', () => {
  for (const route of ROUTES) {
    test(`${route.path} renders cleanly`, async ({ page }) => {
      const problems = watch(page)
      await page.goto(route.path)

      await expect(page.locator('#app')).not.toBeEmpty()
      await expect(page.getByRole('heading', { name: route.heading, level: 1 })).toBeVisible()

      // The masthead is part of the shell, so its absence means App.vue failed
      // rather than the view.
      await expect(page.getByRole('link', { name: /CrowdSight/ })).toBeVisible()

      expect(problems.pageErrors, 'uncaught exceptions').toEqual([])
      expect(problems.csp, 'CSP violations').toEqual([])
      expect(problems.console, 'console errors').toEqual([])
      expect(problems.failedRequests, 'failed requests').toEqual([])
    })
  }

  test('a deep link into a stage is served by the SPA, not a 404', async ({ page }) => {
    const problems = watch(page)
    await page.goto('/runs')
    await page.waitForLoadState('networkidle')

    // Reloading a deep URL is the case history-mode routing gets wrong.
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Run history', level: 1 })).toBeVisible()
    expect(problems.pageErrors).toEqual([])
  })

  test('an unrouted path shows the app 404, not nginx', async ({ page }) => {
    await page.goto('/no/such/place')
    await expect(page.getByRole('heading', { name: 'Not found', level: 1 })).toBeVisible()
    await expect(page.getByText('/no/such/place')).toBeVisible()
  })
})

test.describe('the fields the views read', () => {
  test('THE PROJECT LIST SHOWS REAL DATA, NOT PLACEHOLDERS', async ({ page }) => {
    const problems = watch(page)
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // A graph card must carry an id that came from the API.
    const graphCards = page.locator('.project')
    const graphCount = await graphCards.count()
    expect(graphCount, 'no graphs on disk to check against').toBeGreaterThan(0)
    await expect(graphCards.first()).toContainText(/^g-[0-9a-f]+/)

    const rows = page.locator('tbody tr')
    expect(await rows.count(), 'no simulations on disk to check against').toBeGreaterThan(0)

    // The bug this catches: reading `status` on a record that reports `state`
    // renders the tag as "unknown" for every single run, and the page looks
    // fine otherwise.
    const states = await page.locator('tbody tr td:nth-child(2)').allInnerTexts()
    expect(states.length).toBeGreaterThan(0)
    for (const state of states) {
      expect(
        ['draft', 'running', 'complete', 'failed'],
        `run state "${state.trim()}" is not one the backend produces`,
      ).toContain(state.trim())
    }

    expect(problems.pageErrors).toEqual([])
    expect(problems.failedRequests).toEqual([])
  })

  test('run history renders real states and no empty columns', async ({ page }) => {
    await page.goto('/runs')
    await page.waitForLoadState('networkidle')

    const rows = page.locator('tbody tr')
    expect(await rows.count()).toBeGreaterThan(0)

    const first = rows.first()
    await expect(first.locator('td').first()).toContainText(/^sim-\d{8}-\d{6}-/)
    await expect(first.locator('td').nth(1)).not.toContainText('unknown')
    await expect(first.locator('td').nth(2)).not.toHaveText('—')
  })

  test('a run view reads the run-status shape it expects', async ({ page }) => {
    // Not simply the newest: forks are drafts that never ran, so the first
    // entry in the list often has no run data at all.
    const response = await page.request.get('/api/simulation/list?limit=50')
    const { simulations } = await response.json()
    const ran = (simulations || []).filter((s) => s.started_at)
    test.skip(!ran.length, 'no run with data on disk')
    simulations.length = 0
    simulations.push(...ran)

    const problems = watch(page)
    await page.goto(`/simulations/${simulations[0].sim_id}/run`)
    await page.waitForLoadState('networkidle')

    await expect(page.getByRole('heading', { name: 'Simulation', level: 1 })).toBeVisible()
    const tag = page.locator('.tag').first()
    await expect(tag).toBeVisible()
    await expect(tag).not.toHaveText('unknown')
    // total_rounds, not rounds: an em dash here means the field name is wrong.
    await expect(page.getByRole('heading', { name: /Round \d+ of \d+/ })).toBeVisible()

    expect(problems.pageErrors).toEqual([])
  })
})

test.describe('the workflow indicator', () => {
  test('locks the stages a run has not reached', async ({ page }) => {
    await page.goto('/graphs/new')
    // Nothing selected yet, so stages 2-5 must not be links.
    const locked = page.locator('.stages .is-locked')
    expect(await locked.count()).toBeGreaterThan(0)
  })

  test('opens report and interview once a finished run is selected', async ({ page }) => {
    const response = await page.request.get('/api/simulation/list?limit=20')
    const { simulations } = await response.json()
    const finished = (simulations || []).find((s) => s.state === 'complete')
    test.skip(!finished, 'no completed run on disk')

    await page.goto(`/simulations/${finished.sim_id}/run`)
    await page.waitForLoadState('networkidle')

    // The store learns the run is finished from run-status, which unlocks 4 and 5.
    await expect(page.locator('.stages a', { hasText: 'Report' })).toBeVisible()
  })
})
