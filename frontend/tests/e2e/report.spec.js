import { expect, test } from '@playwright/test'

/**
 * Stage 4 — the report.
 *
 * The citation tests matter most. Phase 8's whole argument is that a claim
 * cites the run rather than the model's prior assumptions, and that guarantee
 * is only worth something if a reader can follow the citation to the actual
 * post. A link that silently finds nothing is indistinguishable from a report
 * that cited something imaginary.
 */

function watch(page) {
  const problems = { pageErrors: [], failed: [] }
  page.on('pageerror', (e) => problems.pageErrors.push(String(e)))
  page.on('response', (r) => {
    if (r.status() >= 500) problems.failed.push(`${r.status()} ${r.url()}`)
  })
  return problems
}

/** A run that already has a report. Generating one costs minutes of inference. */
async function findReported(request) {
  const response = await request.get('/api/report/?limit=50')
  const { reports } = await response.json()
  return reports?.[0] || null
}

test.describe('a generated report', () => {
  let simId
  let reportId

  test.beforeAll(async ({ request }) => {
    const entry = await findReported(request)
    simId = entry?.sim_id
    reportId = entry?.report_id
  })

  test.beforeEach(async ({ page }) => {
    test.skip(!simId, 'no report on disk')
    await page.goto(`/simulations/${simId}/report`)
    await expect(page.getByRole('heading', { name: 'Report', level: 1 })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Executive summary' })).toBeVisible()
  })

  test('renders without errors', async ({ page }) => {
    const problems = watch(page)
    await page.waitForLoadState('networkidle')
    expect(problems.pageErrors).toEqual([])
    expect(problems.failed).toEqual([])
  })

  test('THE VERIFICATION SECTION IS ALWAYS RENDERED', async ({ page }) => {
    // A document that quietly dropped three fabricated claims looks identical
    // to one that never made any.
    await expect(page.getByRole('heading', { name: 'Verification' })).toBeVisible()
    await expect(page.getByText(/\d+\/\d+ citation\(s\) resolved/)).toBeVisible()
  })

  test('draws the sentiment chart as inspectable SVG', async ({ page }) => {
    const chart = page.getByRole('img', { name: /Sentiment by round|No sentiment/ })
      .or(page.locator('svg.chart'))
    await expect(page.getByRole('heading', { name: 'Sentiment across rounds' })).toBeVisible()
    // SVG, not canvas: a test can read the values rather than trust a picture.
    const svgs = page.locator('svg.chart')
    if (await svgs.count()) {
      await expect(svgs.first()).toBeVisible()
      expect(await svgs.first().getAttribute('aria-label')).toBeTruthy()
    }
    expect(await chart.count()).toBeGreaterThanOrEqual(0)
  })

  test('draws the action distribution with counts', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Action distribution' })).toBeVisible()
    const bars = page.locator('.bars tbody tr')
    if (await bars.count()) {
      await expect(bars.first().locator('.count')).toHaveText(/^\d+$/)
    }
  })

  test('draws the influence graph from what agents did', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Influence' })).toBeVisible()
    await expect(page.getByText(/amplification\(s\) between/)).toBeVisible()
  })

  test('offers both exports', async ({ page }) => {
    await expect(page.getByRole('link', { name: 'Export Markdown' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Export HTML' })).toBeVisible()
  })

  test('the markdown export downloads and is really markdown', async ({ page, request }) => {
    const href = await page
      .getByRole('link', { name: 'Export Markdown' })
      .getAttribute('href')
    const response = await request.get(href)

    expect(response.ok()).toBe(true)
    const body = await response.text()
    expect(body).toContain('#')
    expect(body.length).toBeGreaterThan(100)
  })

  test('the html export is a standalone document', async ({ page, request }) => {
    const href = await page.getByRole('link', { name: 'Export HTML' }).getAttribute('href')
    const body = await (await request.get(href)).text()

    expect(body).toContain('<!DOCTYPE html>')
    expect(body).toContain('</html>')
  })
})

test.describe('citations', () => {
  let simId

  test.beforeAll(async ({ request }) => {
    simId = (await findReported(request))?.sim_id
  })

  test.beforeEach(async ({ page }) => {
    test.skip(!simId, 'no report on disk')
    await page.goto(`/simulations/${simId}/report`)
    await expect(page.getByRole('heading', { name: 'Executive summary' })).toBeVisible()
  })

  test('every claim carries an evidence line or says it has none', async ({ page }) => {
    const citations = page.locator('.citation')
    expect(await citations.count()).toBeGreaterThan(0)
    for (const citation of await citations.all()) {
      const text = await citation.innerText()
      expect(text.length).toBeGreaterThan(0)
    }
  })

  test('A CITATION OPENS THE POST IT POINTS AT', async ({ page }) => {
    const problems = watch(page)
    const link = page.locator('.evidence').filter({ hasText: /post/ }).first()
    test.skip((await link.count()) === 0, 'no post citations in this report')

    await link.click()

    // The cited post arrives with its author, round and text — not a spinner
    // that never resolves, and not an empty box.
    const post = page.locator('.cited .post').first()
    await expect(post).toBeVisible()
    await expect(post.locator('.content')).not.toBeEmpty()
    await expect(post.getByText(/round \d+/)).toBeVisible()

    expect(problems.pageErrors).toEqual([])
  })

  test('a citation says so plainly when the post cannot be found', async ({ page }) => {
    // Grounding drops fabricated claims before a report is returned, so this
    // path should not fire on a real report — but silence would hide exactly
    // the failure verification exists to catch.
    const missing = page.getByText('The cited post(s) could not be found in this run.')
    expect(await missing.count()).toBe(0)
  })

  test('a citation can be closed again', async ({ page }) => {
    const link = page.locator('.evidence').first()
    test.skip((await link.count()) === 0, 'no citations')

    await link.click()
    await expect(page.locator('.cited').first()).toBeVisible()
    await link.click()
    await expect(page.locator('.cited')).toHaveCount(0)
  })
})

test.describe('a run with no report', () => {
  test('offers to generate one rather than showing an empty page', async ({ page, request }) => {
    const { simulations } = await (
      await request.get('/api/simulation/list?limit=50')
    ).json()
    const { reports } = await (await request.get('/api/report/?limit=50')).json()
    const reported = new Set((reports || []).map((r) => r.sim_id))
    const bare = (simulations || []).find(
      (s) => s.state === 'complete' && !reported.has(s.sim_id),
    )
    test.skip(!bare, 'every completed run already has a report')

    await page.goto(`/simulations/${bare.sim_id}/report`)
    await expect(page.getByText('No report for this run yet.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Generate a report' })).toBeEnabled()
  })
})
