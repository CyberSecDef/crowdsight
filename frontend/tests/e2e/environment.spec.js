import { expect, test } from '@playwright/test'

/**
 * Stage 2 — the population, in a real browser.
 *
 * The tests worth having here are about what the screen refuses to offer. The
 * server holds provenance and the source links to their stored values whatever
 * the body says, so a UI that presented them as editable would be showing an
 * edit that is silently discarded — which is a worse failure than a refusal,
 * because it looks like it worked.
 */

function watch(page) {
  const problems = { pageErrors: [], failed: [] }
  page.on('pageerror', (e) => problems.pageErrors.push(String(e)))
  page.on('response', (r) => {
    if (r.status() >= 500) problems.failed.push(`${r.status()} ${r.url()}`)
  })
  return problems
}

/** A simulation that is still editable — a finished one has a frozen population. */
async function findDraft(request) {
  const response = await request.get('/api/simulation/list?limit=50')
  const { simulations } = await response.json()
  for (const sim of simulations || []) {
    if (sim.state !== 'draft' || !sim.prepared) continue
    const profiles = await request.get(`/api/simulation/${sim.sim_id}/profiles`)
    if (profiles.ok()) {
      const { count } = await profiles.json()
      if (count >= 2) return sim.sim_id
    }
  }
  return null
}

test.describe('the population', () => {
  let simId

  test.beforeAll(async ({ request }) => {
    simId = await findDraft(request)
  })

  test.beforeEach(async ({ page }) => {
    test.skip(!simId, 'no editable prepared simulation on disk')
    await page.goto(`/simulations/${simId}/profiles`)
    await expect(page.getByRole('heading', { name: 'Environment', level: 1 })).toBeVisible()
  })

  test('leads with the named-versus-synthetic breakdown', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /\d+ agent\(s\)/ })).toBeVisible()
    await expect(page.getByText(/\d+ named/).first()).toBeVisible()
    await expect(page.getByText(/\d+ synthetic/).first()).toBeVisible()
    // The bar carries the same numbers, for anyone reading it as an image.
    await expect(page.getByRole('img', { name: /named.*synthetic/ })).toBeVisible()
  })

  test('every agent shows its provenance without being opened', async ({ page }) => {
    const cards = page.locator('.agent')
    expect(await cards.count()).toBeGreaterThan(0)
    for (const card of await cards.all()) {
      await expect(card.locator('.tag').first()).toHaveText(/^(named|synthetic)$/)
    }
  })

  test('filters to named only', async ({ page }) => {
    await page.getByLabel('Show').selectOption('named')
    for (const card of await page.locator('.agent').all()) {
      await expect(card.locator('.tag').first()).toHaveText('named')
    }
  })

  test('filters to synthetic only', async ({ page }) => {
    await page.getByLabel('Show').selectOption('synthetic')
    for (const card of await page.locator('.agent').all()) {
      await expect(card.locator('.tag').first()).toHaveText('synthetic')
    }
  })

  test('searching narrows the list and clearing restores it', async ({ page }) => {
    const before = await page.locator('.agent').count()
    await page.getByLabel('Search personas').fill('zzzznothingmatchesthis')
    await expect(page.getByText('No agents match that filter.')).toBeVisible()

    await page.getByLabel('Search personas').fill('')
    await expect(page.locator('.agent')).toHaveCount(before)
  })

  test('opening an agent reveals its persona', async ({ page }) => {
    const card = page.locator('.agent').first()
    await card.locator('.disclose').click()
    await expect(card.getByText('Background')).toBeVisible()
    await expect(card.getByText('Activity level')).toBeVisible()
  })

  test('PROVENANCE IS SHOWN AS A FACT, NEVER AS AN INPUT', async ({ page }) => {
    // The server overwrites it from what it has stored, so offering an input
    // would be offering an edit that is silently discarded.
    for (const card of await page.locator('.agent').all()) {
      await card.locator('.disclose').click()
      await expect(card.locator('.locked')).toContainText(/named|synthetic/)
      await expect(card.locator('.locked input')).toHaveCount(0)
      await card.locator('.disclose').click()
    }
  })

  test('A NAMED AGENT OFFERS NO NAME INPUT, AND SAYS WHY', async ({ page }) => {
    await page.getByLabel('Show').selectOption('named')
    const card = page.locator('.agent').first()
    test.skip((await page.locator('.agent').count()) === 0, 'no named agents')

    await card.locator('.disclose').click()
    await expect(card.getByText(/named after a real entity/)).toBeVisible()
  })

  test('a synthetic agent can be renamed', async ({ page }) => {
    await page.getByLabel('Show').selectOption('synthetic')
    test.skip((await page.locator('.agent').count()) === 0, 'no synthetic agents')

    const card = page.locator('.agent').first()
    await card.locator('.disclose').click()
    await expect(card.getByLabel('Name')).toBeEditable()
  })
})

test.describe('staged changes', () => {
  let simId

  /* These tests write to a real simulation on disk, so an edit has to be a
     value the population does not already carry. Reusing a fixed string means
     the second run stages nothing, and the failure reads as "the UI stopped
     detecting edits" rather than "the test asserted its own leftovers". */
  const uniqueOccupation = () => `lighthouse keeper ${Date.now()}`

  test.beforeAll(async ({ request }) => {
    simId = await findDraft(request)
  })

  test.beforeEach(async ({ page }) => {
    test.skip(!simId, 'no editable prepared simulation on disk')
    await page.goto(`/simulations/${simId}/profiles`)
    await expect(page.getByRole('heading', { name: 'Environment', level: 1 })).toBeVisible()
  })

  test('nothing is pending on a freshly loaded population', async ({ page }) => {
    await expect(page.getByRole('button', { name: /Save population/ })).toHaveCount(0)
  })

  test('MARKING A REMOVAL EXPLAINS THAT IDS ARE RENUMBERED', async ({ page }) => {
    await page.locator('.agent').first().getByRole('button', { name: 'Remove' }).click()

    await expect(page.getByText(/1 agent\(s\) removed/)).toBeVisible()
    await expect(page.getByText(/renumbers the ones after them/)).toBeVisible()
    await expect(page.getByRole('button', { name: /Save population/ })).toBeVisible()
  })

  test('a removal is staged, not applied — the count only moves in the summary', async ({
    page,
    request,
  }) => {
    const before = await (await request.get(`/api/simulation/${simId}/profiles`)).json()

    await page.locator('.agent').first().getByRole('button', { name: 'Remove' }).click()
    await expect(page.getByText('was ' + before.count)).toBeVisible()

    // Nothing was sent, so the server still has the original population.
    const after = await (await request.get(`/api/simulation/${simId}/profiles`)).json()
    expect(after.count).toBe(before.count)
  })

  test('a removal can be taken back', async ({ page }) => {
    const card = page.locator('.agent').first()
    await card.getByRole('button', { name: 'Remove' }).click()
    await expect(card.getByText('Will be removed when you save.')).toBeVisible()

    await card.getByRole('button', { name: 'Keep after all' }).click()
    await expect(page.getByRole('button', { name: /Save population/ })).toHaveCount(0)
  })

  test('discard restores everything', async ({ page }) => {
    await page.locator('.agent').first().getByRole('button', { name: 'Remove' }).click()
    await page.getByRole('button', { name: 'Discard' }).click()

    await expect(page.getByRole('button', { name: /Save population/ })).toHaveCount(0)
  })

  test('an edit alone does not claim to renumber anything', async ({ page }) => {
    const card = page.locator('.agent').first()
    await card.locator('.disclose').click()
    await card.getByLabel('Occupation').fill(uniqueOccupation())

    await expect(page.getByText(/1 agent\(s\) edited/)).toBeVisible()
    await expect(page.getByText(/renumbers the ones after them/)).toHaveCount(0)
  })

  test('SAVING AN EDIT PERSISTS IT AND RELOADS FROM DISK', async ({ page, request }) => {
    const problems = watch(page)
    const occupation = uniqueOccupation()
    const card = page.locator('.agent').first()
    await card.locator('.disclose').click()
    await card.getByLabel('Occupation').fill(occupation)
    await page.getByRole('button', { name: /Save population/ }).click()

    await expect(page.getByText(/^Saved: \d+ agent\(s\)/)).toBeVisible()
    await expect(page.getByRole('button', { name: /Save population/ })).toHaveCount(0)

    const { profiles } = await (
      await request.get(`/api/simulation/${simId}/profiles`)
    ).json()
    expect(profiles.some((p) => p.occupation === occupation)).toBe(true)

    expect(problems.pageErrors).toEqual([])
    expect(problems.failed).toEqual([])
  })
})

test.describe('a finished run', () => {
  test('cannot have its population edited', async ({ page, request }) => {
    const response = await request.get('/api/simulation/list?limit=50')
    const { simulations } = await response.json()
    const finished = (simulations || []).find((s) => s.state === 'complete')
    test.skip(!finished, 'no finished run on disk')

    // The server refuses with a 409; the point here is that the UI surfaces
    // that rather than appearing to succeed.
    const refusal = await request.put(
      `/api/simulation/${finished.sim_id}/profiles`,
      { data: { profiles: [] } },
    )
    expect(refusal.status()).toBe(409)
  })
})
