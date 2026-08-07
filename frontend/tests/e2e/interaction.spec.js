import { expect, test } from '@playwright/test'

import { launchableSimulation, longRunningSimulation } from './support.js'

/**
 * Stage 5 — interviews.
 *
 * The constraint that shapes everything: an interview needs a live worker,
 * because the agent answers from memory held in the running process. A
 * finished run has nobody to ask, but its history is still readable. Getting
 * that pair right — refuse the ask, keep the history, say why — is what these
 * tests are mostly about.
 */

function watch(page) {
  const problems = { pageErrors: [], failed: [] }
  page.on('pageerror', (e) => problems.pageErrors.push(String(e)))
  page.on('response', (r) => {
    if (r.status() >= 500) problems.failed.push(`${r.status()} ${r.url()}`)
  })
  return problems
}

async function findFinished(request) {
  const { simulations } = await (
    await request.get('/api/simulation/list?limit=50')
  ).json()
  return (simulations || []).find((s) => s.state === 'complete' && s.started_at)
}

test.describe('a run that is not live', () => {
  let simId

  test.beforeAll(async ({ request }) => {
    simId = (await findFinished(request))?.sim_id
  })

  test.beforeEach(async ({ page }) => {
    test.skip(!simId, 'no finished run on disk')
    await page.goto(`/simulations/${simId}/interview`)
    await expect(page.getByRole('heading', { name: 'Interaction', level: 1 })).toBeVisible()
  })

  test('renders without errors', async ({ page }) => {
    const problems = watch(page)
    await page.waitForLoadState('networkidle')
    expect(problems.pageErrors).toEqual([])
    expect(problems.failed).toEqual([])
  })

  test('SAYS WHY IT CANNOT BE ASKED, RATHER THAN HIDING THE FORM', async ({ page }) => {
    await expect(page.getByText('Interviews need a live run.')).toBeVisible()
    await expect(page.getByText(/no longer in memory/)).toBeVisible()
    // The form is present so the reason has something to attach to.
    await expect(page.getByRole('heading', { name: 'Ask the population' })).toBeVisible()
  })

  test('every ask control is disabled', async ({ page }) => {
    await expect(page.getByLabel('Question')).toBeDisabled()
    await expect(page.getByRole('button', { name: /Ask the selected agent/ })).toBeDisabled()
    await expect(page.getByRole('button', { name: /Ask everyone/ })).toBeDisabled()
  })

  test('points at stage 3, where the run can be restarted', async ({ page }) => {
    const link = page.getByRole('link', { name: 'Go to stage 3' })
    await expect(link).toBeVisible()
    await expect(link).toHaveAttribute('href', new RegExp(`${simId}/run`))
  })

  test('HISTORY IS STILL READABLE ON A FINISHED RUN', async ({ page }) => {
    // It is written to the run's database, so it outlives the worker.
    await expect(page.getByRole('heading', { name: 'Interview history' })).toBeVisible()
    await expect(page.getByText(/\d+ recorded/)).toBeVisible()
  })

  test('offers to filter history by agent', async ({ page }) => {
    const filter = page.getByLabel('Agent')
    await expect(filter).toBeVisible()
    await expect(filter.locator('option')).not.toHaveCount(0)
  })
})

test.describe('a draft run', () => {
  test('says nobody has been asked yet, not that the agents are gone', async ({
    page,
    request,
  }) => {
    test.setTimeout(10 * 60 * 1000)
    const simId = await launchableSimulation(request)
    test.skip(!simId, 'no draft simulation')

    await page.goto(`/simulations/${simId}/interview`)
    await expect(page.getByText(/has not started, so there is nobody to ask yet/))
      .toBeVisible()
    await expect(page.getByText(/no longer in memory/)).toHaveCount(0)
  })
})

test.describe('interviewing a live run', () => {
  test.setTimeout(20 * 60 * 1000)

  test('ASKS AN AGENT AND THE ANSWER LANDS IN HISTORY', async ({ page, request }) => {
    // Long enough to still be running when the question is asked: a short run
    // finishes faster than a browser can click.
    const simId = await longRunningSimulation(request)
    test.skip(!simId, 'no launchable simulation')
    const problems = watch(page)

    // A run has to be live to be interviewed at all.
    const started = await request.post('/api/simulation/start', {
      data: { sim_id: simId },
    })
    expect(started.status()).toBe(202)

    /* Wait for the worker to be *answering*, not merely spawned. `start`
       returns as soon as the process exists; the worker then builds the OASIS
       environment and only opens its control socket at the end of that. An
       interview in that gap fails with "no worker listening", which looks
       exactly like a run that has already ended. */
    let live = false
    for (let attempt = 0; attempt < 60 && !live; attempt += 1) {
      const probe = await request.post('/api/simulation/env-status', {
        data: { sim_id: simId },
      })
      if (probe.ok()) {
        // `accepting_commands` is the precise signal: the process being alive
        // is not the same as its control socket being open.
        live = (await probe.json()).accepting_commands === true
      }
      if (!live) await new Promise((resolve) => setTimeout(resolve, 3000))
    }
    expect(live, 'the worker never started answering').toBe(true)

    await page.goto(`/simulations/${simId}/interview`)
    await expect(page.getByRole('heading', { name: 'Interaction', level: 1 })).toBeVisible()

    // Wait for the view to see the run as live.
    await expect(page.getByLabel('Question')).toBeEnabled({ timeout: 60_000 })
    await expect(page.getByText('Interviews need a live run.')).toHaveCount(0)

    await page.getByLabel('Question').fill('What worries you most about this proposal?')
    await page.getByText(/Choose agents/).click()
    await page.locator('.agents input[type=checkbox]').first().check()

    const ask = page.getByRole('button', { name: 'Ask the selected agent' })
    await expect(ask).toBeEnabled()
    await ask.click()

    // A real answer, from a real agent, through the live worker.
    const answer = page.locator('.answer').first()
    await expect(answer).toBeVisible({ timeout: 5 * 60 * 1000 })
    await expect(answer.locator('p')).not.toBeEmpty()

    // And it is in the durable record, not only on screen.
    await expect(page.getByText(/[1-9]\d* recorded/)).toBeVisible({ timeout: 60_000 })

    expect(problems.pageErrors).toEqual([])
    expect(problems.failed).toEqual([])

    await request.post('/api/simulation/stop', { data: { sim_id: simId } })
  })
})
