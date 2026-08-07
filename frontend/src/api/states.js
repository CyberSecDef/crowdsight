/**
 * The backend's vocabulary, in one place.
 *
 * Two different words are in play and they are easy to conflate:
 *
 *  - a *simulation* has a `state`  — draft / running / complete / failed
 *  - a *background task* has a `status` — pending / running / awaiting_review /
 *    succeeded / failed
 *
 * They are not interchangeable, and neither of them is called `status` on a
 * simulation. Guessing here produces a UI that renders "unknown" for every run
 * and looks broken rather than wrong, so the names live here and views import
 * them instead of typing string literals.
 *
 * Mirrors backend/app/services/simulation_store.py.
 */

export const RunState = {
  DRAFT: 'draft',
  RUNNING: 'running',
  COMPLETE: 'complete',
  FAILED: 'failed',
}

/** A run that will not change again on its own. */
export const RUN_FINISHED = new Set([RunState.COMPLETE, RunState.FAILED])

/** Its config can no longer be edited; an edit forks a new simulation. */
export const RUN_LOCKED = new Set([RunState.RUNNING, RunState.COMPLETE, RunState.FAILED])

export function runFinished(state) {
  return RUN_FINISHED.has(state)
}

export function runLocked(state) {
  return RUN_LOCKED.has(state)
}

/** Which tag colour a run state gets. */
export function runStateClass(state) {
  if (state === RunState.COMPLETE) return 'tag--ok'
  if (state === RunState.RUNNING) return 'tag--warn'
  if (state === RunState.FAILED) return 'tag--bad'
  return ''
}
