import { describe, expect, it } from 'vitest'

import {
  MAX_QUESTION_LENGTH,
  canInterview,
  failedAnswers,
  fanOutWarning,
  groupByQuestion,
  succeededAnswers,
  validateQuestion,
  whyNotInterviewable,
} from '../../src/api/interview.js'

/**
 * Interview rules.
 *
 * The one that shapes the screen: an interview needs a live worker, because
 * the agent answers from memory held in the running process. History is
 * different — it is written to the run's database and outlives the worker.
 * Conflating the two would either hide readable history or offer an ask that
 * always fails.
 */

describe('when an interview is possible', () => {
  it('only while the run is live', () => {
    expect(canInterview('running')).toBe(true)
  })

  it.each(['draft', 'complete', 'failed', ''])('not while %s', (state) => {
    expect(canInterview(state)).toBe(false)
  })
})

describe('why it is not possible', () => {
  it('says nothing when it is', () => {
    expect(whyNotInterviewable('running')).toBe('')
  })

  it('A DRAFT HAS NOBODY TO ASK YET, WHICH IS NOT THE SAME AS BEING OVER', () => {
    expect(whyNotInterviewable('draft')).toContain('not started')
    expect(whyNotInterviewable('draft')).not.toContain('no longer')
  })

  it.each(['complete', 'failed'])('explains that %s means the agents are gone', (state) => {
    const reason = whyNotInterviewable(state)
    expect(reason).toContain(state)
    expect(reason).toContain('no longer in memory')
  })

  it('POINTS AT HISTORY AND AT RESTARTING, RATHER THAN JUST REFUSING', () => {
    const reason = whyNotInterviewable('complete')
    expect(reason).toContain('history is still readable')
    expect(reason).toContain('restarting the run')
  })
})

describe('the fan-out warning', () => {
  it('names the number of agents', () => {
    expect(fanOutWarning(42)).toContain('42')
  })

  it('SAYS IT IS A MODEL CALL EACH, NOT JUST "ARE YOU SURE"', () => {
    const warning = fanOutWarning(42)
    expect(warning).toContain('inference call')
    expect(warning).toContain('cannot be called back')
  })
})

describe('validateQuestion', () => {
  it('accepts a real question', () => {
    expect(validateQuestion('What do you think?')).toBe('')
  })

  it.each(['', '   ', null, undefined])('refuses %j', (value) => {
    expect(validateQuestion(value)).toBe('Ask something.')
  })

  it('refuses one longer than the limit', () => {
    expect(validateQuestion('x'.repeat(MAX_QUESTION_LENGTH + 1))).toContain('longer than')
  })

  it('accepts one exactly at the limit', () => {
    expect(validateQuestion('x'.repeat(MAX_QUESTION_LENGTH))).toBe('')
  })
})

describe('answers', () => {
  const ANSWERS = [
    { user_id: 0, response: 'Too fast.' },
    { user_id: 1, error: 'the worker did not answer' },
    { user_id: 2, response: 'Seems reasonable.' },
  ]

  it('separates the ones that failed', () => {
    expect(failedAnswers(ANSWERS).map((a) => a.user_id)).toEqual([1])
  })

  it('separates the ones that worked', () => {
    expect(succeededAnswers(ANSWERS).map((a) => a.user_id)).toEqual([0, 2])
  })

  it('A FAILED ANSWER IS KEPT, BECAUSE IT NEVER REACHES THE HISTORY', () => {
    // Only refreshing history would make the failure vanish, leaving fewer
    // answers than agents asked and no reason why.
    expect(failedAnswers(ANSWERS)).toHaveLength(1)
  })

  it('handles nothing at all', () => {
    expect(failedAnswers()).toEqual([])
    expect(succeededAnswers(null)).toEqual([])
  })
})

describe('groupByQuestion', () => {
  const HISTORY = [
    { user_id: 0, question: 'Q1', response: 'a', round: 1 },
    { user_id: 1, question: 'Q1', response: 'b', round: 1 },
    { user_id: 0, question: 'Q2', response: 'c', round: 2 },
  ]

  it('gathers answers under the question they answer', () => {
    const groups = groupByQuestion(HISTORY)
    expect(groups).toHaveLength(2)
    expect(groups[0].question).toBe('Q1')
    expect(groups[0].answers).toHaveLength(2)
  })

  it('collects the rounds a question was asked in', () => {
    const groups = groupByQuestion([
      ...HISTORY,
      { user_id: 2, question: 'Q1', response: 'd', round: 3 },
    ])
    expect(groups[0].rounds).toEqual([1, 3])
  })

  it('does not choke on a missing round', () => {
    const groups = groupByQuestion([{ user_id: 0, question: 'Q', response: 'a' }])
    expect(groups[0].rounds).toEqual([])
  })

  it('handles an empty history', () => {
    expect(groupByQuestion([])).toEqual([])
    expect(groupByQuestion()).toEqual([])
  })
})
