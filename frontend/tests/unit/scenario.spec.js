import { describe, expect, it } from 'vitest'

import {
  MAX_ROUNDS,
  PLATFORMS,
  PLATFORM_ACTIONS,
  actionsFor,
  actionsLostBySwitching,
  blankScheduledEvent,
  blankSeedPost,
  pruneActions,
  validateConfig,
  wasDemoted,
} from '../../src/api/scenario.js'

/**
 * Scenario config rules.
 *
 * The platform-switching tests are the load-bearing ones. Twitter has no
 * comments and no voting; Reddit has both and no reposts. Carrying a selection
 * across a platform change sends actions the new platform has never heard of,
 * and the server refuses the whole config with a message about an action the
 * operator never consciously chose.
 */

const CONFIG = () => ({
  event: 'The council published a draft housing density policy.',
  platform: 'twitter',
  rounds: 3,
  hours_per_round: 6,
  notes: '',
  broadcaster: { name: 'Riverbend City News', handle: 'rb_city_news', description: '' },
  action_space: { platform: 'twitter', actions: ['CREATE_POST', 'LIKE_POST', 'DO_NOTHING'] },
  seed_posts: [
    { attribution: 'broadcaster', content: 'Council publishes a draft policy.',
      speaker: '', demoted_reason: '' },
  ],
  scheduled_events: [
    { round: 1, description: 'The mayor responds.', content: 'A statement.',
      counterfactual: true, enabled: false },
  ],
})

describe('the action sets', () => {
  it('are per platform', () => {
    expect(PLATFORMS).toEqual(['twitter', 'reddit'])
    expect(actionsFor('twitter')).toContain('REPOST')
    expect(actionsFor('reddit')).toContain('CREATE_COMMENT')
  })

  it('TWITTER HAS NO COMMENTS AND REDDIT HAS NO REPOSTS', () => {
    expect(actionsFor('twitter')).not.toContain('CREATE_COMMENT')
    expect(actionsFor('reddit')).not.toContain('REPOST')
  })

  it('both keep DO_NOTHING, which is a real choice an agent can make', () => {
    for (const platform of PLATFORMS) {
      expect(PLATFORM_ACTIONS[platform]).toContain('DO_NOTHING')
    }
  })

  it('an unknown platform has no actions rather than throwing', () => {
    expect(actionsFor('myspace')).toEqual([])
  })
})

describe('switching platform', () => {
  it('KEEPS ONLY THE ACTIONS THE NEW PLATFORM HAS', () => {
    const kept = pruneActions(['CREATE_POST', 'REPOST', 'DO_NOTHING'], 'reddit')
    expect(kept).toEqual(['CREATE_POST', 'DO_NOTHING'])
  })

  it('names what it dropped, so the change can be announced', () => {
    expect(actionsLostBySwitching(['CREATE_POST', 'REPOST'], 'reddit')).toEqual(['REPOST'])
  })

  it('drops nothing when everything survives', () => {
    expect(actionsLostBySwitching(['CREATE_POST'], 'reddit')).toEqual([])
  })

  it('survives an empty selection', () => {
    expect(pruneActions(undefined, 'twitter')).toEqual([])
  })
})

describe('validateConfig', () => {
  it('passes a sound config', () => {
    expect(validateConfig(CONFIG())).toEqual([])
  })

  it('refuses an empty event', () => {
    const config = CONFIG()
    config.event = '   '
    expect(validateConfig(config).join(' ')).toContain('event')
  })

  it('refuses a platform this build does not support', () => {
    const config = CONFIG()
    config.platform = 'myspace'
    expect(validateConfig(config).join(' ')).toContain('not a platform')
  })

  it.each([0, -1, 1.5])('refuses %s rounds', (rounds) => {
    const config = CONFIG()
    config.rounds = rounds
    expect(validateConfig(config).join(' ')).toContain('Rounds')
  })

  it('refuses more rounds than the server allows', () => {
    const config = CONFIG()
    config.rounds = MAX_ROUNDS + 1
    expect(validateConfig(config).join(' ')).toContain(`exceed ${MAX_ROUNDS}`)
  })

  it('AN AGENT WITH NO PERMITTED ACTIONS CANNOT DO ANYTHING', () => {
    const config = CONFIG()
    config.action_space.actions = []
    expect(validateConfig(config).join(' ')).toContain('cannot do anything')
  })

  it('catches an action left behind by a platform switch', () => {
    const config = CONFIG()
    config.action_space.actions = ['CREATE_POST', 'CREATE_COMMENT']
    expect(validateConfig(config).join(' ')).toContain('not a twitter action')
  })

  it('refuses a seed post with no content', () => {
    const config = CONFIG()
    config.seed_posts[0].content = ''
    expect(validateConfig(config).join(' ')).toContain('no content')
  })

  it('refuses a named seed post that names nobody', () => {
    const config = CONFIG()
    config.seed_posts[0].attribution = 'named'
    expect(validateConfig(config).join(' ')).toContain('names nobody')
  })

  it('REFUSES AN EVENT SCHEDULED AFTER THE RUN ENDS', () => {
    // It would never fire, and nothing else would say so.
    const config = CONFIG()
    config.rounds = 2
    config.scheduled_events[0].round = 5
    expect(validateConfig(config).join(' ')).toContain('after the run ends')
  })

  it('ALLOWS AN EVENT IN THE FINAL ROUND', () => {
    /* The engine keeps events where round <= rounds. This was `>=` first,
       which rejected configs the generator itself produces — the fork screen
       could not be saved at all, and the block looked like a real objection. */
    const config = CONFIG()
    config.rounds = 2
    config.scheduled_events[0].round = 2
    expect(validateConfig(config)).toEqual([])
  })

  it('allows an event before the final round', () => {
    const config = CONFIG()
    config.rounds = 3
    config.scheduled_events[0].round = 2
    expect(validateConfig(config)).toEqual([])
  })

  it('handles a missing config rather than throwing', () => {
    expect(validateConfig(null)).toHaveLength(1)
  })
})

describe('demotion', () => {
  it('recognises a post the server demoted', () => {
    expect(wasDemoted({ demoted_reason: 'not found in the source' })).toBe(true)
  })

  it('does not flag an ordinary post', () => {
    expect(wasDemoted({ demoted_reason: '' })).toBe(false)
    expect(wasDemoted(undefined)).toBe(false)
  })
})

describe('blank items', () => {
  it('a new seed post starts as the broadcaster, which needs no verification', () => {
    expect(blankSeedPost().attribution).toBe('broadcaster')
  })

  it('a new scheduled event is disabled, so adding one changes nothing yet', () => {
    expect(blankScheduledEvent().enabled).toBe(false)
  })
})
