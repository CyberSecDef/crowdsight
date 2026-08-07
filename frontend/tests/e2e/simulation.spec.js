import { expect, test } from '@playwright/test'

import { launchableSimulation } from './support.js'

/**
 * Stage 3 — configure, launch, watch.
 *
 * The fork test is the one that matters most. Editing a run that has started
 * does not change it: the server preserves the original and puts the edit in a
 * new simulation with a different id. A UI that stayed put would show a config
 * that does not match what was just saved, and a second edit would fork again.
 */

function watch(page) {
  const problems = { pageErrors: [], failed: [] }
  page.on('pageerror', (e) => problems.pageErrors.push(String(e)))
  page.on('response', (r) => {
    if (r.status() >= 500) problems.failed.push(`${r.status()} ${r.url()}`)
  })
  return problems
}

async function findByState(request, predicate) {
  const response = await request.get('/api/simulation/list?limit=50')
  const { simulations } = await response.json()
  return (simulations || []).find(predicate)
}

test.describe('the scenario', () => {
  let simId

  test.beforeAll(async ({ request }) => {
    test.setTimeout(10 * 60 * 1000)
    simId = await launchableSimulation(request)
  })

  test.beforeEach(async ({ page }) => {
    test.skip(!simId, 'no launchable simulation on disk')
    await page.goto(`/simulations/${simId}/run`)
    // Wait for the scenario itself, not just the page heading. The h1 renders
    // before the config has loaded, so a test that counted seed posts at that
    // moment found none and skipped itself — reporting "no seed posts" about a
    // scenario that has three.
    await expect(page.getByRole('heading', { name: 'Scenario' })).toBeVisible()
  })

  test('opens on the scenario for a run that has not started', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Scenario' })).toBeVisible()
    await expect(page.getByLabel(/The event the population/)).not.toBeEmpty()
  })

  test('offers platform selection and a round count', async ({ page }) => {
    await expect(page.getByLabel('Platform')).toBeVisible()
    await expect(page.getByLabel(/^Rounds/)).toBeVisible()
    await expect(page.getByLabel('Hours per round')).toBeVisible()
  })

  test('shows the action space as checkboxes for the chosen platform', async ({ page }) => {
    const actions = page.getByRole('group', { name: /Permitted actions/ })
    await expect(actions).toBeVisible()
    await expect(actions.getByRole('checkbox', { name: 'CREATE_POST' })).toBeVisible()
  })

  test('SWITCHING PLATFORM PRUNES ACTIONS AND SAYS WHICH', async ({ page }) => {
    // REPOST does not exist on Reddit. Carrying it over would have the server
    // refuse the whole config over an action nobody consciously chose.
    const actions = page.getByRole('group', { name: /Permitted actions/ })
    await expect(actions.getByRole('checkbox', { name: 'REPOST' })).toBeVisible()

    await page.getByLabel('Platform').selectOption('reddit')

    await expect(page.getByText(/REPOST.*removed — not available on reddit/)).toBeVisible()
    await expect(actions.getByRole('checkbox', { name: 'REPOST' })).toHaveCount(0)
    await expect(actions.getByRole('checkbox', { name: 'CREATE_COMMENT' })).toBeVisible()

    await page.getByLabel('Platform').selectOption('twitter')
  })

  test('an invalid config cannot be saved', async ({ page }) => {
    await page.getByLabel(/^Rounds/).fill('0')

    await expect(page.locator('.problems')).toContainText('Rounds')
    await expect(page.getByRole('button', { name: /Save scenario/ })).toBeDisabled()
  })

  test('warns that an event scheduled past the end will never fire', async ({ page }) => {
    const events = page.getByRole('spinbutton', { name: 'Fires in round' })
    test.skip((await events.count()) === 0, 'no scheduled events in this scenario')

    await page.getByLabel(/^Rounds/).fill('2')
    await events.first().fill('9')
    await expect(page.locator('.problems')).toContainText('after the run ends')
  })

  test('a seed post attributed to a named person explains the check', async ({ page }) => {
    const attribution = page.getByLabel('Attributed to').first()
    test.skip((await attribution.count()) === 0, 'no seed posts')

    await attribution.selectOption('named')
    await expect(page.getByText(/checked against the source document/).first()).toBeVisible()
  })
})

test.describe('launch controls', () => {
  test('a draft with a population offers a start button', async ({ page, request }) => {
    test.setTimeout(10 * 60 * 1000)
    const simId = await launchableSimulation(request)
    test.skip(!simId, 'no launchable simulation and none could be prepared')

    await page.goto(`/simulations/${simId}/run`)
    await expect(page.getByRole('button', { name: 'Start the run' })).toBeEnabled()
    await expect(page.getByText(/run\(s\) in flight/)).toBeVisible()
  })

  test('a completed run offers to resume from its checkpoint', async ({ page, request }) => {
    const done = await findByState(request, (s) => s.state === 'complete')
    test.skip(!done, 'no completed run')

    await page.goto(`/simulations/${done.sim_id}/run`)
    await expect(page.getByText(/resume from its last\s+checkpoint/)).toBeVisible()
  })
})

test.describe('a finished run', () => {
  let simId

  test.beforeAll(async ({ request }) => {
    const done = await findByState(request, (s) => s.state === 'complete')
    simId = done?.sim_id
  })

  test.beforeEach(async ({ page }) => {
    test.skip(!simId, 'no completed run on disk')
    await page.goto(`/simulations/${simId}/run`)
    await expect(page.getByRole('heading', { name: 'Simulation', level: 1 })).toBeVisible()
  })

  test('opens on the run rather than the scenario', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /Round \d+ of \d+/ })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Scenario' })).toHaveCount(0)
  })

  test('shows the progress bar, the feed and the agent table', async ({ page }) => {
    const problems = watch(page)

    await expect(page.getByRole('progressbar')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Action feed' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Agent activity' })).toBeVisible()

    expect(problems.pageErrors).toEqual([])
    expect(problems.failed).toEqual([])
  })

  test('THE FEED CARRIES REAL ACTIONS, NOT ENGINE ROWS', async ({ page }) => {
    // A 300-agent run opens with 300 sign_up rows; leading with those would
    // bury the first thing an agent actually did.
    const entries = page.locator('.entry')
    await expect(entries.first()).toBeVisible()
    const text = await entries.first().innerText()
    expect(text).not.toContain('sign_up')
  })

  test('the feed can be filtered', async ({ page }) => {
    // Wait for the feed to settle before counting it: the monitor drains it in
    // pages, so a count taken immediately is a count of part of it.
    await expect(page.locator('.entry').first()).toBeVisible()
    const before = await page.locator('.entry').count()
    await page.getByPlaceholder('filter by agent or action').fill('zzznothing')
    await expect(page.locator('.entry')).toHaveCount(0)

    await page.getByPlaceholder('filter by agent or action').fill('')
    await expect(page.locator('.entry')).toHaveCount(before)
  })

  test('counts silent agents rather than hiding them', async ({ page }) => {
    // A quiet population is a real outcome — the participation roll skips
    // low-activity agents on purpose.
    await expect(page.getByText(/\d+ agent\(s\), \d+ never acted/)).toBeVisible()
  })

  test('per-round detail is available without leaving the page', async ({ page }) => {
    await page.getByText('Per-round detail').click()
    await expect(page.getByRole('columnheader', { name: 'Invoked' })).toBeVisible()
  })

  test('the scenario is still reachable from a finished run', async ({ page }) => {
    await page.getByRole('button', { name: 'Show the scenario' }).click()
    await expect(page.getByRole('heading', { name: 'Scenario' })).toBeVisible()
    await expect(page.getByText(/this run has started — saving creates a copy/)).toBeVisible()
  })
})

test.describe('editing a started run', () => {
  test('FORKS, AND FOLLOWS THE EDIT TO THE NEW SIMULATION', async ({ page, request }) => {
    const done = await findByState(request, (s) => s.state === 'complete')
    test.skip(!done, 'no completed run')
    const problems = watch(page)

    await page.goto(`/simulations/${done.sim_id}/run`)
    await page.getByRole('button', { name: 'Show the scenario' }).click()
    await expect(page.getByRole('heading', { name: 'Scenario' })).toBeVisible()

    await page.getByLabel('Notes').fill(`edited by a test at ${Date.now()}`)
    // Wait for the form to be ready rather than for the heading: the button is
    // disabled while the config is still being validated, and clicking a
    // disabled button times out with no useful message.
    const save = page.getByRole('button', { name: /Save as a new simulation/ })
    await expect(save).toBeEnabled()
    await save.click()

    await expect(page.getByText(/your edit created a new simulation/)).toBeVisible()
    await expect(page.getByText(new RegExp(done.sim_id))).toBeVisible()
    // The URL followed the edit, so a second edit does not fork again.
    await expect(page).not.toHaveURL(new RegExp(done.sim_id))
    expect(page.url()).toMatch(/\/simulations\/sim-[\w-]+\/run/)

    expect(problems.pageErrors).toEqual([])
  })
})

test.describe('a live run, end to end', () => {
  test.setTimeout(20 * 60 * 1000)

  test('STARTS FROM THE UI AND THE FEED FILLS AS IT GOES', async ({ page, request }) => {
    const simId = await launchableSimulation(request)
    test.skip(!simId, 'no launchable simulation and none could be prepared')
    const problems = watch(page)

    await page.goto(`/simulations/${simId}/run`)
    await page.getByRole('button', { name: 'Start the run' }).click()

    // The view switches to the run and the bar appears.
    await expect(page.getByRole('progressbar')).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText('live')).toBeVisible({ timeout: 60_000 })

    // Actions arrive without the page being reloaded — that is the streaming.
    await expect(page.locator('.entry').first()).toBeVisible({ timeout: 10 * 60 * 1000 })

    // And it reaches a finished state on its own.
    await expect(page.getByText(/^complete$|^failed$/).first())
      .toBeVisible({ timeout: 15 * 60 * 1000 })

    expect(problems.pageErrors).toEqual([])
    expect(problems.failed).toEqual([])
  })
})
