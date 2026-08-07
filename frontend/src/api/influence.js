/**
 * The influence graph, derived from what agents actually did.
 *
 * A report names influential agents and describes propagation in prose, but it
 * carries no edges. The run does: every repost and quote records the post it
 * amplified, so an edge runs from whoever amplified to whoever wrote the
 * original. That is literally how influence propagated.
 *
 * Deriving it rather than drawing the report's claims means the picture can
 * *disagree* with the model — an agent the report calls influential who turns
 * out to have amplified nobody and been amplified by nobody is exactly the
 * thing a reader should be able to see.
 */

/** OASIS records the amplified post under a different key per action. */
const AMPLIFIED_KEYS = ['reposted_id', 'quoted_id', 'original_post_id', 'post_id']

export const AMPLIFYING_ACTIONS = new Set(['repost', 'quote_post'])

function amplifiedId(action) {
  const info = action?.info || {}
  for (const key of AMPLIFIED_KEYS) {
    const value = info[key]
    if (value !== undefined && value !== null) return Number(value)
  }
  return null
}

/**
 * Build nodes and edges for the graph.
 *
 * @param {Array} posts    every post, to map post_id -> author
 * @param {Array} actions  every action, for the amplifications
 * @param {Array} influential  the agents the report singled out
 */
export function buildInfluenceGraph({ posts = [], actions = [], influential = [] } = {}) {
  const authorOf = new Map()
  const nameOf = new Map()
  for (const post of posts) {
    authorOf.set(Number(post.post_id), Number(post.user_id))
    if (post.name) nameOf.set(Number(post.user_id), post.name)
  }
  for (const action of actions) {
    if (action.name) nameOf.set(Number(action.user_id), action.name)
  }

  const highlighted = new Set(
    influential.map((agent) => Number(agent.user_id)).filter((id) => !Number.isNaN(id)),
  )

  /** amplifier -> original author, counted. */
  const weights = new Map()
  let unresolved = 0

  for (const action of actions) {
    if (!AMPLIFYING_ACTIONS.has(action.action)) continue
    const postId = amplifiedId(action)
    if (postId === null) continue
    const target = authorOf.get(postId)
    if (target === undefined) {
      // The amplified post is outside the page of posts we hold. Counting it
      // as an edge to nowhere would draw a lie; it is reported instead.
      unresolved += 1
      continue
    }
    const source = Number(action.user_id)
    // Self-amplification is real but says nothing about influence between
    // agents, and it draws as a loop that obscures the rest.
    if (source === target) continue
    const key = `${source}->${target}`
    weights.set(key, (weights.get(key) || 0) + 1)
  }

  const involved = new Set()
  const edges = []
  for (const [key, weight] of weights) {
    const [source, target] = key.split('->').map(Number)
    involved.add(source)
    involved.add(target)
    edges.push({ uuid: key, source: String(source), target: String(target), weight })
  }
  for (const id of highlighted) involved.add(id)

  const amplifiedBy = new Map()
  for (const edge of edges) {
    amplifiedBy.set(Number(edge.target), (amplifiedBy.get(Number(edge.target)) || 0) + edge.weight)
  }

  const nodes = [...involved].sort((a, b) => a - b).map((id) => ({
    uuid: String(id),
    name: nameOf.get(id) || `agent ${id}`,
    type: highlighted.has(id) ? 'named by the report' : 'other',
    mention_count: amplifiedBy.get(id) || 0,
    inferred: false,
  }))

  return {
    nodes,
    edges,
    unresolved,
    /** Agents the report called influential who amplified and were amplified by nobody. */
    isolatedClaims: [...highlighted].filter((id) => !involved.has(id) || (
      !edges.some((e) => Number(e.source) === id || Number(e.target) === id)
    )),
  }
}

/** Totals for the action distribution chart, largest first. */
export function actionDistribution(timeline = []) {
  const totals = new Map()
  for (const round of timeline) {
    for (const [action, count] of Object.entries(round.action_counts || {})) {
      totals.set(action, (totals.get(action) || 0) + Number(count || 0))
    }
  }
  return [...totals.entries()]
    .map(([action, count]) => ({ action, count }))
    .sort((a, b) => b.count - a.count)
}
