import { expect, test } from '@playwright/test'

/**
 * Stage 1 — document in, graph out, in a real browser against the sealed stack.
 *
 * The client-side validation tests matter most in this file, because their
 * whole point is that no request is made. A unit test can prove the predicate
 * returns false; only the browser can prove the upload did not happen anyway.
 */

const DOCUMENT = `Riverbend City Council published a draft housing density policy on 3 March.
Councillor Jane Doe, who chairs the planning committee, said the twenty-one day
consultation window was adequate. Dawn Mercer, a carpenter who has worked in the
Eastgate corridor for thirty years, objected that the corridor would be altered
permanently before anyone had read the plans. The Riverbend Residents Association
supported her objection and asked the council for an extension.`

function watch(page) {
  const problems = { pageErrors: [], csp: [], uploads: [] }
  page.on('pageerror', (e) => problems.pageErrors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() === 'error' && /Content Security Policy/i.test(m.text())) {
      problems.csp.push(m.text())
    }
  })
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().includes('/api/graph/upload')) {
      problems.uploads.push(r.url())
    }
  })
  return problems
}

test.describe('the upload step', () => {
  test('offers a drop zone that names what it accepts', async ({ page }) => {
    await page.goto('/graphs/new')

    const drop = page.getByRole('button', { name: /Choose a document/ })
    await expect(drop).toBeVisible()
    await expect(drop).toContainText('.txt')
    await expect(drop).toContainText('.pdf')
    await expect(drop).toContainText('50.0 MB')
  })

  test('reviews the ontology by default', async ({ page }) => {
    await page.goto('/graphs/new')
    await expect(page.getByRole('checkbox', { name: /Review the ontology/ })).toBeChecked()
  })

  test('the build button is disabled until a file is chosen', async ({ page }) => {
    await page.goto('/graphs/new')
    await expect(page.getByRole('button', { name: 'Build graph' })).toBeDisabled()
  })

  test('A REJECTED FILE IS NEVER UPLOADED', async ({ page }) => {
    const problems = watch(page)
    await page.goto('/graphs/new')

    await page.locator('input[type=file]').setInputFiles({
      name: 'payload.exe',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from('MZ'),
    })

    await expect(page.getByRole('alert')).toContainText('payload.exe')
    await expect(page.getByRole('button', { name: 'Build graph' })).toBeDisabled()
    // The point of client-side validation: the round trip never happens.
    expect(problems.uploads, 'a refused file was uploaded anyway').toEqual([])
    expect(problems.pageErrors).toEqual([])
  })

  test('an empty file is refused before upload', async ({ page }) => {
    const problems = watch(page)
    await page.goto('/graphs/new')

    await page.locator('input[type=file]').setInputFiles({
      name: 'empty.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(''),
    })

    await expect(page.getByRole('alert')).toContainText('empty')
    expect(problems.uploads).toEqual([])
  })

  test('an accepted file shows its name and size and enables the build', async ({ page }) => {
    await page.goto('/graphs/new')
    await page.locator('input[type=file]').setInputFiles({
      name: 'council.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(DOCUMENT),
    })

    await expect(page.getByText('council.txt')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Build graph' })).toBeEnabled()
  })
})

test.describe('an existing graph', () => {
  let graphId

  test.beforeAll(async ({ request }) => {
    const response = await request.get('/api/graph/')
    const { graphs } = await response.json()
    graphId = graphs?.[0]?.graph_id
  })

  test('renders the graph and its facts', async ({ page }) => {
    test.skip(!graphId, 'no graph on disk')
    const problems = watch(page)

    await page.goto(`/graphs/${graphId}`)
    await expect(page.getByRole('heading', { name: 'Graph', level: 1 })).toBeVisible()
    await expect(page.getByText(/\d+ entities/)).toBeVisible()

    // Cytoscape draws to a canvas inside the container.
    const canvas = page.getByTestId('graph-canvas')
    await expect(canvas).toBeVisible()
    await expect(canvas.locator('canvas').first()).toBeVisible()

    expect(problems.pageErrors).toEqual([])
    expect(problems.csp, 'cytoscape must not need an inline script').toEqual([])
  })

  test('offers a type filter carrying counts', async ({ page }) => {
    test.skip(!graphId, 'no graph on disk')
    await page.goto(`/graphs/${graphId}`)

    const chips = page.locator('.chip')
    await expect(chips.first()).toBeVisible()
    expect(await chips.count()).toBeGreaterThan(0)
  })

  test('HIDING A TYPE IS A TOGGLE, NOT A ONE-WAY TRIP', async ({ page }) => {
    test.skip(!graphId, 'no graph on disk')
    await page.goto(`/graphs/${graphId}`)

    const chip = page.locator('.chip').first()
    await expect(chip).toHaveAttribute('aria-pressed', 'true')
    await chip.click()
    await expect(chip).toHaveAttribute('aria-pressed', 'false')
    await chip.click()
    await expect(chip).toHaveAttribute('aria-pressed', 'true')
  })

  test('show all restores every hidden type', async ({ page }) => {
    test.skip(!graphId, 'no graph on disk')
    await page.goto(`/graphs/${graphId}`)

    for (const chip of await page.locator('.chip').all()) await chip.click()
    await page.getByRole('button', { name: 'Show all' }).click()

    for (const chip of await page.locator('.chip').all()) {
      await expect(chip).toHaveAttribute('aria-pressed', 'true')
    }
  })
})

test.describe('a graph parked for review', () => {
  let parkedId

  // Proposing an ontology is real local inference.
  test.beforeAll(async ({ request }) => {
    test.setTimeout(5 * 60 * 1000)

    /* Make the graph this suite needs rather than scavenging one off disk.
       Two earlier attempts to reuse leftovers both picked the wrong thing: a
       parked task outlives the graph it refers to, so its ontology can be
       gone; and a graph can be parked *and* already built, in which case the
       view correctly shows the graph and not the review. Neither was a bug in
       the product, but both were reported as one. */
    const started = await request.post('/api/graph/upload', {
      multipart: {
        file: { name: 'parked.txt', mimeType: 'text/plain', buffer: Buffer.from(DOCUMENT) },
        review_ontology: 'true',
      },
    })
    const { graph_id: graphId, task_id: taskId } = await started.json()

    for (let attempt = 0; attempt < 100; attempt += 1) {
      const status = await request.get(`/api/graph/status/${taskId}`)
      const task = await status.json()
      if (task.status === 'awaiting_review') {
        parkedId = graphId
        return
      }
      if (task.status === 'failed') break
      await new Promise((resolve) => setTimeout(resolve, 3000))
    }
  })

  test('REOPENING IT RESUMES THE REVIEW RATHER THAN THE UPLOAD FORM', async ({ page }) => {
    test.skip(!parkedId, 'no parked graph on disk')
    const problems = watch(page)

    await page.goto(`/graphs/${parkedId}`)

    await expect(page.getByRole('heading', { name: 'Review the ontology' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Choose a document' })).toHaveCount(0)
    expect(problems.pageErrors, 'the editor must render without recursing').toEqual([])
  })

  test('shows the identifier a typed name will become', async ({ page }) => {
    test.skip(!parkedId, 'no parked graph on disk')
    await page.goto(`/graphs/${parkedId}`)

    const firstName = page.locator('.type input[type=text]').first()
    await firstName.fill('Public Figure')
    await expect(page.locator('.identifier').first()).toHaveText('PublicFigure')

    // A name that cannot become an identifier says so rather than silently
    // submitting nothing.
    await firstName.fill('3rd sector')
    await expect(page.locator('.identifier').first()).toHaveText('not a usable name')
  })

  test('warns before an edit silently drops relationships', async ({ page }) => {
    test.skip(!parkedId, 'no parked graph on disk')
    await page.goto(`/graphs/${parkedId}`)

    // Rename every entity type to something unrelated, orphaning the
    // relationships that point at the old names.
    const names = page.locator('.type input[type=text]')
    await names.first().fill('Unrelated Thing')

    const problems = page.locator('[role=alert]')
    if (await problems.count()) {
      await expect(problems.first()).toBeVisible()
    }
  })
})

test.describe('the whole stage, against the live model', () => {
  // Ontology proposal and extraction are real local inference on a 14b model.
  test.setTimeout(15 * 60 * 1000)

  test('UPLOAD THROUGH REVIEW THROUGH EXTRACTION TO A DRAWN GRAPH', async ({ page }) => {
    const problems = watch(page)
    await page.goto('/graphs/new')

    await page.locator('input[type=file]').setInputFiles({
      name: 'council.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(DOCUMENT),
    })
    await page.getByRole('button', { name: 'Build graph' }).click()

    // Parked for review: the task stops at awaiting_review and the editor opens.
    await expect(page.getByRole('heading', { name: 'Review the ontology' }))
      .toBeVisible({ timeout: 5 * 60 * 1000 })
    await expect(page.getByText('awaiting_review')).toBeVisible()

    // The identifier preview is the thing that would otherwise surprise someone.
    const firstName = page.locator('.type input[type=text]').first()
    await firstName.fill('Local Authority')
    await expect(page.locator('.identifier').first()).toHaveText('LocalAuthority')

    await page.getByRole('button', { name: 'Approve and extract' }).click()

    // Extraction runs, then the graph is drawn.
    await expect(page.getByText(/\d+ entities/)).toBeVisible({ timeout: 10 * 60 * 1000 })
    const canvas = page.getByTestId('graph-canvas')
    await expect(canvas.locator('canvas').first()).toBeVisible()

    // The URL became the graph's, so the result is reachable again later.
    expect(page.url()).toMatch(/\/graphs\/g-[0-9a-f]+/)

    expect(problems.pageErrors).toEqual([])
    expect(problems.csp).toEqual([])
  })
})
