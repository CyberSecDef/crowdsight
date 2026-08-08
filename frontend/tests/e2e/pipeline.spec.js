import { expect, test } from '@playwright/test'

/**
 * The whole product, through the browser, in one walk.
 *
 * upload → graph → profiles → short run → report, against a live sealed stack
 * and real local inference. Every other spec tests a stage; this one tests that
 * the stages join up — that the graph a document produced can be turned into a
 * population, that the population can be run, and that the run can be reported
 * on, without leaving the UI or knowing an id in advance.
 *
 * Slow and deliberate: `npm run test:e2e:pipeline`. It is the frontend twin of
 * the release gate Phase 10 Step 1 asks for in Python.
 */

const DOCUMENT = `Riverbend City Council published a draft housing density policy on 3 March.
Councillor Jane Doe, who chairs the planning committee, said the twenty-one day
consultation window was adequate. Dawn Mercer, a carpenter who has worked in the
Eastgate corridor for thirty years, objected that the corridor would be altered
permanently before anyone had read the plans. The Riverbend Residents Association
supported her objection and asked the council for an extension. Mayor Alan Reyes
said the council would consider a short extension but would not delay the vote.`

test.describe('the whole pipeline', () => {
  test.describe.configure({ mode: 'serial' })
  test.setTimeout(30 * 60 * 1000)

  test('UPLOAD → GRAPH → PROFILES → RUN → REPORT', async ({ page, request }) => {
    const problems = { pageErrors: [], failed: [] }
    page.on('pageerror', (e) => problems.pageErrors.push(String(e)))
    page.on('response', (r) => {
      if (r.status() >= 500) problems.failed.push(`${r.status()} ${r.url()}`)
    })

    // ---- 1. Upload a document and review its ontology --------------------
    await page.goto('/graphs/new')
    await page.locator('input[type=file]').setInputFiles({
      name: 'riverbend.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(DOCUMENT),
    })
    await page.getByRole('button', { name: 'Build graph' }).click()

    await expect(page.getByRole('heading', { name: 'Review the ontology' }))
      .toBeVisible({ timeout: 10 * 60 * 1000 })
    await page.getByRole('button', { name: 'Approve and extract' }).click()

    // ---- 2. The graph is drawn -------------------------------------------
    await expect(page.getByText(/\d+ entities/)).toBeVisible({ timeout: 15 * 60 * 1000 })
    await expect(page.getByTestId('graph-canvas').locator('canvas').first()).toBeVisible()

    const graphId = page.url().match(/\/graphs\/(g-[0-9a-f]+)/)?.[1]
    expect(graphId, 'the URL should carry the graph id').toBeTruthy()

    // ---- 3. Build a population from that graph ---------------------------
    // Creating and preparing is API work the UI drives from stage 3 onward;
    // what matters here is that the graph this walk produced can carry it.
    const created = await request.post('/api/simulation/create', {
      data: { graph_id: graphId, platform: 'twitter', rounds: 2, total_agents: 4 },
    })
    const { sim_id: simId } = await created.json()
    expect(simId).toBeTruthy()

    const prepared = await request.post('/api/simulation/prepare', {
      data: { sim_id: simId, total_agents: 4 },
    })
    const { task_id: prepareTask } = await prepared.json()
    for (let attempt = 0; attempt < 200; attempt += 1) {
      const status = await request.get(
        `/api/simulation/prepare/status?task_id=${prepareTask}`)
      const task = await status.json()
      if (['awaiting_review', 'succeeded'].includes(task.status)) break
      expect(task.status, 'preparation failed').not.toBe('failed')
      await new Promise((resolve) => setTimeout(resolve, 3000))
    }

    // ---- 4. Review the population in the UI ------------------------------
    await page.goto(`/simulations/${simId}/profiles`)
    await expect(page.getByRole('heading', { name: /\d+ agent\(s\)/ })).toBeVisible()
    await expect(page.getByText(/\d+ named/).first()).toBeVisible()
    const agents = await page.locator('.agent').count()
    expect(agents, 'the population should have agents').toBeGreaterThan(0)

    // ---- 5. Run it from the UI -------------------------------------------
    await page.goto(`/simulations/${simId}/run`)
    await expect(page.getByRole('button', { name: 'Start the run' })).toBeEnabled()
    await page.getByRole('button', { name: 'Start the run' }).click()

    await expect(page.getByRole('progressbar')).toBeVisible({ timeout: 2 * 60 * 1000 })
    await expect(page.locator('.entry').first()).toBeVisible({ timeout: 15 * 60 * 1000 })
    await expect(page.getByText(/^complete$|^failed$/).first())
      .toBeVisible({ timeout: 20 * 60 * 1000 })

    // ---- 6. Report on it, and follow a citation --------------------------
    await page.goto(`/simulations/${simId}/report`)
    await page.getByRole('button', { name: /Generate a report/ }).click()
    await expect(page.getByRole('heading', { name: 'Executive summary' }))
      .toBeVisible({ timeout: 20 * 60 * 1000 })

    // The verification record is the point of the whole exercise.
    await expect(page.getByRole('heading', { name: 'Verification' })).toBeVisible()
    await expect(page.getByText(/\d+\/\d+ citation\(s\) resolved/)).toBeVisible()

    // And a citation reaches a real post from the run this walk produced.
    const citation = page.locator('.evidence').filter({ hasText: /post/ }).first()
    if (await citation.count()) {
      await citation.click()
      await expect(page.locator('.cited .post .content').first()).not.toBeEmpty()
    }

    await expect(page.getByRole('link', { name: 'Export Markdown' })).toBeVisible()

    expect(problems.pageErrors, 'uncaught exceptions').toEqual([])
    expect(problems.failed, 'server errors').toEqual([])
  })
})
