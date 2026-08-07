/**
 * Scenario config rules, mirrored from the backend.
 *
 * Three of the server's rules are restated here so the form can offer the
 * right controls rather than let someone type something that will be refused:
 *
 * 1. The action set is per platform. Twitter has no comments and no voting;
 *    Reddit has both and no reposts. Choosing a platform changes which actions
 *    exist at all, so switching has to prune the selection rather than carry
 *    over actions the new platform has never heard of.
 * 2. Rounds and agents have hard ceilings from the server's config.
 * 3. A seed post is either the broadcaster's own words or a quote attributed
 *    to a named person — and the server checks the second against the source
 *    document, demoting anything it cannot find. That correction is reported
 *    in `changes`, and the UI has to show it or edits are altered silently.
 *
 * `scripts/verify_frontend.sh` checks these lists against the running backend,
 * because a mirror that has drifted offers a choice the server will reject.
 */

/** Mirrors TWITTER_ACTIONS in backend/app/services/action_space.py. */
export const TWITTER_ACTIONS = [
  'CREATE_POST',
  'LIKE_POST',
  'REPOST',
  'FOLLOW',
  'QUOTE_POST',
  'DO_NOTHING',
]

/** Mirrors REDDIT_ACTIONS. */
export const REDDIT_ACTIONS = [
  'CREATE_POST',
  'CREATE_COMMENT',
  'LIKE_POST',
  'DISLIKE_POST',
  'LIKE_COMMENT',
  'DISLIKE_COMMENT',
  'SEARCH_POSTS',
  'SEARCH_USER',
  'TREND',
  'REFRESH',
  'FOLLOW',
  'MUTE',
  'DO_NOTHING',
]

export const PLATFORM_ACTIONS = {
  twitter: TWITTER_ACTIONS,
  reddit: REDDIT_ACTIONS,
}

export const PLATFORMS = Object.keys(PLATFORM_ACTIONS)

/** Mirrors Config.MAX_ROUNDS and MAX_AGENTS. */
export const MAX_ROUNDS = 10
export const MAX_AGENTS = 100

export const ATTRIBUTIONS = ['broadcaster', 'named']

export const actionsFor = (platform) => PLATFORM_ACTIONS[platform] || []

/**
 * Keep only the actions the platform actually has.
 *
 * Switching platform with the old selection intact would send Reddit actions
 * to a Twitter run, which the server refuses at validation — correctly, but
 * after the fact and with a message about an action the operator never chose.
 */
export function pruneActions(actions, platform) {
  const allowed = new Set(actionsFor(platform))
  return (actions || []).filter((action) => allowed.has(action))
}

/** Actions dropped by a platform switch, so the change can be announced. */
export function actionsLostBySwitching(actions, platform) {
  const allowed = new Set(actionsFor(platform))
  return (actions || []).filter((action) => !allowed.has(action))
}

/**
 * Everything wrong with a config, as messages. Empty means submittable.
 *
 * Not the authority — the server revalidates and re-verifies against the
 * source document — but a form the server will certainly refuse is a wasted
 * round trip and a worse message.
 */
export function validateConfig(config) {
  const problems = []
  if (!config) return ['There is no configuration to check.']

  if (!String(config.event || '').trim()) {
    problems.push('The event is what the population is reacting to; it cannot be empty.')
  }
  if (!PLATFORMS.includes(config.platform)) {
    problems.push(`${config.platform} is not a platform this build supports.`)
  }

  const rounds = Number(config.rounds)
  if (!Number.isInteger(rounds) || rounds < 1) {
    problems.push('Rounds must be a whole number of at least 1.')
  } else if (rounds > MAX_ROUNDS) {
    problems.push(`Rounds cannot exceed ${MAX_ROUNDS}.`)
  }

  if (!(Number(config.hours_per_round) > 0)) {
    problems.push('Hours per round must be greater than zero.')
  }

  const actions = config.action_space?.actions || []
  if (!actions.length) {
    problems.push('An agent with no permitted actions cannot do anything.')
  }
  const stray = actionsLostBySwitching(actions, config.platform)
  if (stray.length) {
    problems.push(`${stray.join(', ')} ${stray.length === 1 ? 'is' : 'are'} not a ${config.platform} action.`)
  }

  ;(config.seed_posts || []).forEach((post, index) => {
    if (!String(post.content || '').trim()) {
      problems.push(`Seed post ${index + 1} has no content.`)
    }
    if (post.attribution === 'named' && !String(post.speaker || '').trim()) {
      problems.push(`Seed post ${index + 1} is attributed to a named person but names nobody.`)
    }
  })

  ;(config.scheduled_events || []).forEach((event, index) => {
    const round = Number(event.round)
    if (!Number.isInteger(round) || round < 0) {
      problems.push(`Scheduled event ${index + 1} has no valid round.`)
    } else if (rounds && round > rounds) {
      // The engine keeps events where round <= rounds — an event in the final
      // round is valid. This was `>=` first, which rejected configs the
      // generator itself produces and blocked saving them.
      problems.push(
        `Scheduled event ${index + 1} fires in round ${round}, after the run ends at ${rounds}.`,
      )
    }
  })

  return problems
}

/** A seed post the server demoted, so it can be shown as such. */
export const wasDemoted = (post) => Boolean(post?.demoted_reason)

export const blankSeedPost = () => ({
  attribution: 'broadcaster',
  content: '',
  speaker: '',
  demoted_reason: '',
  source_start: null,
  source_end: null,
})

export const blankScheduledEvent = () => ({
  round: 1,
  description: '',
  content: '',
  counterfactual: true,
  enabled: false,
})
