/**
 * Fixtures these suites need, made rather than scavenged.
 *
 * Scavenging leftover state off disk has gone wrong three times now, and each
 * time the failure blamed the product:
 *
 *  * a parked task outlives the graph it refers to, so its ontology is gone;
 *  * a graph can be parked *and* already built, in which case the view
 *    correctly shows the graph and not the review;
 *  * `prepared` on a simulation means "has a scenario", NOT "has a population"
 *    — a fork is prepared the moment it is created and has no agents at all.
 *
 * And the live run test consumes its simulation by running it to completion,
 * so a suite that only scavenged would pass once and then skip itself for
 * ever, reporting "nothing to test against" about a stack that works.
 */

/** A simulation with a scenario *and* agents, so it can actually be launched. */
export async function findLaunchable(request) {
  const response = await request.get('/api/simulation/list?limit=50')
  const { simulations } = await response.json()
  for (const sim of simulations || []) {
    if (sim.state !== 'draft' || !sim.prepared) continue
    const profiles = await request.get(`/api/simulation/${sim.sim_id}/profiles?limit=1`)
    if (profiles.ok()) return sim.sim_id
  }
  return null
}

/**
 * Create and prepare a small simulation. Real local inference, so ~1 minute.
 *
 * @param {number} agents kept small on purpose; these suites test the UI, not
 *                        the population size.
 */
export async function provision(request, { agents = 4, rounds = 2 } = {}) {
  const graphs = await (await request.get('/api/graph/')).json()
  const graphId = graphs?.graphs?.[0]?.graph_id
  if (!graphId) return null

  const created = await request.post('/api/simulation/create', {
    data: { graph_id: graphId, platform: 'twitter', rounds, total_agents: agents },
  })
  const { sim_id: simId } = await created.json()
  if (!simId) return null

  const started = await request.post('/api/simulation/prepare', {
    data: { sim_id: simId, total_agents: agents },
  })
  const { task_id: taskId } = await started.json()
  if (!taskId) return null

  for (let attempt = 0; attempt < 120; attempt += 1) {
    const status = await request.get(`/api/simulation/prepare/status?task_id=${taskId}`)
    const task = await status.json()
    if (['awaiting_review', 'succeeded'].includes(task.status)) return simId
    if (task.status === 'failed') return null
    await new Promise((resolve) => setTimeout(resolve, 3000))
  }
  return null
}

/** Find one, or make one. */
export async function launchableSimulation(request) {
  return (await findLaunchable(request)) || provision(request)
}
