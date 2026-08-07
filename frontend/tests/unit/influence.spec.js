import { describe, expect, it } from 'vitest'

import {
  AMPLIFYING_ACTIONS,
  actionDistribution,
  buildInfluenceGraph,
} from '../../src/api/influence.js'

/**
 * The influence graph is derived from what agents did, not from what the report
 * says about them. That is the point: the picture can disagree with the model,
 * and an agent the report calls influential who amplified nobody and was
 * amplified by nobody is exactly what a reader should be able to see.
 */

const POSTS = [
  { post_id: 1, user_id: 0, name: 'Dawn' },
  { post_id: 2, user_id: 1, name: 'Ray' },
  { post_id: 3, user_id: 2, name: 'Fergus' },
]

const repost = (userId, targetPost, name) => ({
  action: 'repost', user_id: userId, name,
  info: { reposted_id: targetPost, new_post_id: 90 + targetPost },
})
const quote = (userId, targetPost, name) => ({
  action: 'quote_post', user_id: userId, name,
  info: { quoted_id: targetPost, new_post_id: 80 + targetPost },
})

describe('which actions amplify', () => {
  it('reposts and quotes, and nothing else', () => {
    expect([...AMPLIFYING_ACTIONS].sort()).toEqual(['quote_post', 'repost'])
  })

  it('A LIKE IS NOT AN AMPLIFICATION', () => {
    const graph = buildInfluenceGraph({
      posts: POSTS,
      actions: [{ action: 'like_post', user_id: 1, info: { post_id: 1 } }],
    })
    expect(graph.edges).toEqual([])
  })
})

describe('buildInfluenceGraph', () => {
  it('runs an edge from the amplifier to the original author', () => {
    const graph = buildInfluenceGraph({ posts: POSTS, actions: [repost(1, 1, 'Ray')] })

    expect(graph.edges).toHaveLength(1)
    expect(graph.edges[0]).toMatchObject({ source: '1', target: '0' })
  })

  it('reads a quote as well as a repost', () => {
    const graph = buildInfluenceGraph({ posts: POSTS, actions: [quote(2, 1, 'Fergus')] })
    expect(graph.edges[0]).toMatchObject({ source: '2', target: '0' })
  })

  it('counts repeated amplification as weight rather than duplicate edges', () => {
    const graph = buildInfluenceGraph({
      posts: POSTS,
      actions: [repost(1, 1, 'Ray'), quote(1, 1, 'Ray')],
    })
    expect(graph.edges).toHaveLength(1)
    expect(graph.edges[0].weight).toBe(2)
  })

  it('DROPS SELF-AMPLIFICATION, WHICH SAYS NOTHING ABOUT INFLUENCE', () => {
    const graph = buildInfluenceGraph({ posts: POSTS, actions: [repost(0, 1, 'Dawn')] })
    expect(graph.edges).toEqual([])
  })

  it('COUNTS AN AMPLIFIED POST IT CANNOT RESOLVE RATHER THAN DRAWING A LIE', () => {
    // The amplified post is outside the page of posts held; an edge to nowhere
    // would be a fabrication.
    const graph = buildInfluenceGraph({ posts: POSTS, actions: [repost(1, 4242, 'Ray')] })

    expect(graph.edges).toEqual([])
    expect(graph.unresolved).toBe(1)
  })

  it('sizes a node by how often it was amplified', () => {
    const graph = buildInfluenceGraph({
      posts: POSTS,
      actions: [repost(1, 1, 'Ray'), repost(2, 1, 'Fergus')],
    })
    const dawn = graph.nodes.find((n) => n.uuid === '0')
    expect(dawn.mention_count).toBe(2)
  })

  it('names nodes from the posts and actions it was given', () => {
    const graph = buildInfluenceGraph({ posts: POSTS, actions: [repost(1, 1, 'Ray')] })
    expect(graph.nodes.find((n) => n.uuid === '0').name).toBe('Dawn')
  })

  it('falls back to an id when nothing named the agent', () => {
    const graph = buildInfluenceGraph({
      posts: [{ post_id: 1, user_id: 0 }],
      actions: [{ action: 'repost', user_id: 7, info: { reposted_id: 1 } }],
    })
    expect(graph.nodes.find((n) => n.uuid === '7').name).toBe('agent 7')
  })

  it('marks the agents the report singled out', () => {
    const graph = buildInfluenceGraph({
      posts: POSTS,
      actions: [repost(1, 1, 'Ray')],
      influential: [{ user_id: 0 }],
    })
    expect(graph.nodes.find((n) => n.uuid === '0').type).toBe('named by the report')
    expect(graph.nodes.find((n) => n.uuid === '1').type).toBe('other')
  })

  it('FLAGS AN INFLUENTIAL AGENT WITH NO AMPLIFICATION EITHER WAY', () => {
    // The picture is allowed to disagree with the report; that is why it is
    // derived rather than drawn from the report's own claims.
    const graph = buildInfluenceGraph({
      posts: POSTS,
      actions: [repost(1, 1, 'Ray')],
      influential: [{ user_id: 2 }],
    })
    expect(graph.isolatedClaims).toEqual([2])
  })

  it('flags nothing when the report agrees with the run', () => {
    const graph = buildInfluenceGraph({
      posts: POSTS,
      actions: [repost(1, 1, 'Ray')],
      influential: [{ user_id: 0 }],
    })
    expect(graph.isolatedClaims).toEqual([])
  })

  it('survives being given nothing', () => {
    const graph = buildInfluenceGraph()
    expect(graph).toMatchObject({ nodes: [], edges: [], unresolved: 0 })
  })
})

describe('actionDistribution', () => {
  const TIMELINE = [
    { round: 0, action_counts: { create_post: 3 } },
    { round: 1, action_counts: { create_post: 2, repost: 4 } },
  ]

  it('totals each action across every round', () => {
    expect(actionDistribution(TIMELINE)).toEqual([
      { action: 'repost', count: 4 },
      { action: 'create_post', count: 5 },
    ].sort((a, b) => b.count - a.count))
  })

  it('sorts largest first', () => {
    const counts = actionDistribution(TIMELINE).map((entry) => entry.count)
    expect(counts).toEqual([...counts].sort((a, b) => b - a))
  })

  it('handles an empty timeline', () => {
    expect(actionDistribution([])).toEqual([])
    expect(actionDistribution()).toEqual([])
  })

  it('ignores a round that recorded no counts', () => {
    expect(actionDistribution([{ round: 0 }])).toEqual([])
  })
})
