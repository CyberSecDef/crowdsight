/**
 * Polling helpers.
 *
 * Nearly everything slow in CrowdSight is a background task with a task id and
 * a status endpoint: graph extraction, profile generation, run preparation,
 * report generation. They all report the same shape, so they can all be
 * watched by the same machine instead of each view growing its own setInterval
 * and its own subtly different idea of when to stop.
 *
 * Three things this handles that a bare setInterval does not:
 *
 * 1. `awaiting_review` is not finished and not failing. Ontology review parks a
 *    task deliberately and waits for a human, so a poller that only knows
 *    "running or terminal" spins forever on a task nobody is working on.
 * 2. A backend hiccup should not kill a watch that has been running for an
 *    hour. Errors back off and only give up after several consecutive
 *    failures — but a 404 is different, because a task id that does not exist
 *    is not going to start existing.
 * 3. A run takes hours, and a hidden tab polling every two seconds all
 *    afternoon is pure waste. Polling pauses while the page is hidden and
 *    resumes — immediately, not after another interval — when it comes back.
 */

import { get, ApiError } from './client.js'

/** Mirrors backend/app/services/tasks.py. */
export const TaskStatus = {
  PENDING: 'pending',
  RUNNING: 'running',
  AWAITING_REVIEW: 'awaiting_review',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
}

export const TERMINAL = new Set([TaskStatus.SUCCEEDED, TaskStatus.FAILED])
/** Reached the end of its work and is deliberately waiting on a person. */
export const PARKED = new Set([TaskStatus.AWAITING_REVIEW])

export function isTerminal(status) {
  return TERMINAL.has(status)
}

export function isParked(status) {
  return PARKED.has(status)
}

/** True when there is no point asking again until something else happens. */
export function isSettled(status) {
  return isTerminal(status) || isParked(status)
}

export const DEFAULT_INTERVAL = 1500
export const MAX_INTERVAL = 15000
export const MAX_CONSECUTIVE_ERRORS = 5

const sleep = (ms, signal) =>
  new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason ?? new Error('aborted'))
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(signal.reason ?? new Error('aborted'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })

/** Resolves when the page is visible. Resolves immediately if it already is. */
function whenVisible(signal) {
  if (typeof document === 'undefined' || !document.hidden) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const done = () => {
      if (document.hidden) return
      document.removeEventListener('visibilitychange', done)
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }
    const onAbort = () => {
      document.removeEventListener('visibilitychange', done)
      reject(signal.reason ?? new Error('aborted'))
    }
    document.addEventListener('visibilitychange', done)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * Poll `fetcher` until `done` says to stop.
 *
 * @param {() => Promise<any>} fetcher   one request
 * @param {object}   options
 * @param {(value:any) => boolean} options.done      stop when this is true
 * @param {(value:any) => void}    options.onUpdate  called on every answer
 * @param {(err:Error) => void}    options.onError   called on a tolerated error
 * @param {AbortSignal}            options.signal    stop immediately
 * @returns {Promise<any>} the last value seen
 */
export async function pollUntil(fetcher, options = {}) {
  const {
    done = () => true,
    onUpdate,
    onError,
    interval = DEFAULT_INTERVAL,
    maxInterval = MAX_INTERVAL,
    maxErrors = MAX_CONSECUTIVE_ERRORS,
    signal,
  } = options

  let errors = 0
  let wait = interval
  let last = null

  for (;;) {
    if (signal?.aborted) throw signal.reason ?? new Error('aborted')
    await whenVisible(signal)

    try {
      last = await fetcher()
      errors = 0
      wait = interval
      onUpdate?.(last)
      if (done(last)) return last
    } catch (error) {
      if (error?.name === 'AbortError') throw error
      // A task that is not there will not appear later, and neither will a
      // refusal fix itself. Retrying those is just noise in the log.
      const permanent = error instanceof ApiError && (error.notFound || error.refusal)
      errors += 1
      if (permanent || errors >= maxErrors) throw error
      onError?.(error)
      wait = Math.min(wait * 2, maxInterval)
    }

    await sleep(wait, signal)
  }
}

/**
 * Watch one background task to a standstill.
 *
 * Resolves on success, on `awaiting_review` (the caller decides what to do
 * about a parked task), and on failure — a failed task is an answer, not an
 * exception, because the message it carries is the thing worth showing.
 *
 * @param {string} statusPath e.g. "/graph/status" or "/report/status"
 */
export function watchTask(statusPath, taskId, options = {}) {
  return pollUntil(() => get(`${statusPath}/${encodeURIComponent(taskId)}`), {
    ...options,
    done: (task) => isSettled(task?.status),
  })
}
