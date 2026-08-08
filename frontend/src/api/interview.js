/**
 * Interview rules, mirrored from the backend.
 *
 * The one that shapes the whole screen: an interview needs a live worker. The
 * agent answers in character from its accumulated memory, and that memory
 * lives in the running process — so a run that has stopped has nobody to ask,
 * and the server refuses with a 409. History is different: it is written to the
 * run's own database and stays readable long after the run has ended.
 *
 * That distinction is why the ask controls are shown disabled rather than
 * hidden on a finished run. "Why can't I ask?" is the first question, and an
 * absent form answers it by leaving the reader to guess.
 */

import { RunState } from './states.js'

/**
 * Whether anyone can be asked anything right now.
 *
 * Not the same as "is the run running". A finished run keeps its worker up for
 * a while so its agents can still be interviewed, and during that window the
 * run is `complete` but entirely answerable — a UI keyed on state alone would
 * refuse to use the very window that exists for it. `run-status` reports
 * `interviewable` for exactly this, and it is the authority; the state is only
 * a fallback for a response that predates the field.
 */
export function canInterview(state, interviewable) {
  if (interviewable !== undefined && interviewable !== null) return Boolean(interviewable)
  return state === RunState.RUNNING
}

export function whyNotInterviewable(state) {
  if (state === RunState.RUNNING) return ''
  if (!state) return 'This run has no status yet.'
  if (state === RunState.DRAFT) {
    return 'This run has not started, so there is nobody to ask yet.'
  }
  return (
    `This run is ${state}, so its agents are no longer in memory and there is ` +
    'nobody to ask. Its interview history is still readable below, and ' +
    'restarting the run in stage 3 would make its agents answerable again.'
  )
}

/** A finished run that is still answering, so the window can be shown. */
export const inInterviewWindow = (state, interviewable) =>
  Boolean(interviewable) && state !== RunState.RUNNING

export const WINDOW_HINT =
  'This run has finished, but its agents are still in memory and can be asked ' +
  'questions for a short while. The window stays open as long as you keep ' +
  'using it, and closes once nothing has been asked for a couple of minutes.'

/** Asking everyone is a model call per agent. Say so before it happens. */
export function fanOutWarning(count) {
  return (
    `Ask all ${count} agent(s)?\n\n` +
    `Each one is a separate call to the local model, so this is ${count} ` +
    'inference call(s) competing with anything else running on the GPU. ' +
    'It cannot be called back once it starts.'
  )
}

/**
 * The run says running but the worker did not answer.
 *
 * `start` returns as soon as the process is spawned; the worker then builds
 * the environment and only opens its control socket at the end of that. An
 * interview in the gap gets "no worker listening", which reads as though the
 * run is over when it has not begun answering yet.
 */
export function isStartingUp(state, error) {
  return (
    state === 'running' &&
    /no worker listening|did not answer/i.test(String(error?.message || ''))
  )
}

export const STARTING_UP_HINT =
  'The run is live but its worker is still building the environment, which ' +
  'takes a little while on a cold start. Try again in a moment.'

export const MAX_QUESTION_LENGTH = 2000

export function validateQuestion(question) {
  const text = String(question || '').trim()
  if (!text) return 'Ask something.'
  if (text.length > MAX_QUESTION_LENGTH) {
    return `A question cannot be longer than ${MAX_QUESTION_LENGTH} characters.`
  }
  return ''
}

/**
 * Merge fresh answers with stored history for display.
 *
 * An interview that failed has an error to show but nothing written to the
 * trace, so it exists only in the fresh results. Dropping it would leave the
 * operator with fewer answers than agents asked and no reason why.
 */
export function failedAnswers(answers) {
  return (answers || []).filter((answer) => answer.error)
}

export function succeededAnswers(answers) {
  return (answers || []).filter((answer) => !answer.error)
}

/** Group history newest-first into one entry per question asked. */
export function groupByQuestion(interviews) {
  const groups = new Map()
  for (const entry of interviews || []) {
    const key = `${entry.question}`
    if (!groups.has(key)) {
      groups.set(key, { question: entry.question, answers: [], rounds: new Set() })
    }
    const group = groups.get(key)
    group.answers.push(entry)
    if (entry.round !== null && entry.round !== undefined) group.rounds.add(entry.round)
  }
  return [...groups.values()].map((group) => ({
    ...group,
    rounds: [...group.rounds].sort((a, b) => a - b),
  }))
}
