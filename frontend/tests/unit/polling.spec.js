import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  DEFAULT_INTERVAL,
  TaskStatus,
  isParked,
  isSettled,
  isTerminal,
  pollUntil,
  watchTask,
} from '../../src/api/polling.js'
import { ApiError } from '../../src/api/client.js'

/**
 * The polling state machine.
 *
 * The spec describes this as idle -> running -> complete -> error, which is one
 * state short. `awaiting_review` is a real backend status: ontology review and
 * scenario review both park a task deliberately and wait for a person. Driven
 * against the live stack it sits at progress 0.5 with status `awaiting_review`
 * and never moves again, so a poller that only knows "terminal or keep going"
 * polls that task until the tab is closed.
 */

const FAST = { interval: 1, maxInterval: 2 }

function stubFetch(responses) {
  const calls = []
  global.fetch = vi.fn(async (url) => {
    calls.push(url)
    const next = responses.shift()
    if (next instanceof Error) throw next
    if (typeof next === 'function') return next()
    return {
      ok: true,
      status: 200,
      headers: new Map([['content-type', 'application/json']]),
      json: async () => next,
    }
  })
  // The client reads headers via .get(), which Map provides.
  return calls
}

afterEach(() => {
  vi.restoreAllMocks()
  delete global.fetch
})

describe('the vocabulary', () => {
  it('treats succeeded and failed as terminal', () => {
    expect(isTerminal(TaskStatus.SUCCEEDED)).toBe(true)
    expect(isTerminal(TaskStatus.FAILED)).toBe(true)
  })

  it('does NOT treat awaiting_review as terminal', () => {
    expect(isTerminal(TaskStatus.AWAITING_REVIEW)).toBe(false)
    expect(isParked(TaskStatus.AWAITING_REVIEW)).toBe(true)
  })

  it('keeps going while pending or running', () => {
    expect(isSettled(TaskStatus.PENDING)).toBe(false)
    expect(isSettled(TaskStatus.RUNNING)).toBe(false)
  })

  it('settles on all three end states', () => {
    expect(isSettled(TaskStatus.SUCCEEDED)).toBe(true)
    expect(isSettled(TaskStatus.FAILED)).toBe(true)
    expect(isSettled(TaskStatus.AWAITING_REVIEW)).toBe(true)
  })

  it('does not settle on a status it has never heard of', () => {
    expect(isSettled('reticulating')).toBe(false)
  })
})

describe('watchTask', () => {
  it('STOPS ON awaiting_review RATHER THAN POLLING FOREVER', async () => {
    const calls = stubFetch([
      { status: TaskStatus.RUNNING, progress: 0.2 },
      { status: TaskStatus.RUNNING, progress: 0.4 },
      { status: TaskStatus.AWAITING_REVIEW, progress: 0.5, stage: 'ontology_review' },
      { status: TaskStatus.AWAITING_REVIEW, progress: 0.5 }, // must never be asked for
    ])

    const task = await watchTask('/graph/status', 't-1', FAST)

    expect(task.status).toBe(TaskStatus.AWAITING_REVIEW)
    expect(calls).toHaveLength(3)
  })

  it('reports a parked task as parked, not as finished', async () => {
    stubFetch([{ status: TaskStatus.AWAITING_REVIEW }])
    const task = await watchTask('/graph/status', 't-1', FAST)

    expect(isParked(task.status)).toBe(true)
    expect(isTerminal(task.status)).toBe(false)
  })

  it('resolves rather than throws when a task fails', async () => {
    stubFetch([{ status: TaskStatus.FAILED, error: 'the model produced nothing' }])
    const task = await watchTask('/graph/status', 't-1', FAST)

    // A failed task carries the message worth showing; throwing loses it.
    expect(task.status).toBe(TaskStatus.FAILED)
    expect(task.error).toContain('nothing')
  })

  it('runs to succeeded through intermediate states', async () => {
    const calls = stubFetch([
      { status: TaskStatus.PENDING },
      { status: TaskStatus.RUNNING },
      { status: TaskStatus.SUCCEEDED, result: { graph_id: 'g-1' } },
    ])
    const task = await watchTask('/graph/status', 't-1', FAST)

    expect(task.result.graph_id).toBe('g-1')
    expect(calls).toHaveLength(3)
  })

  it('reports progress on every answer', async () => {
    stubFetch([
      { status: TaskStatus.RUNNING, progress: 0.3 },
      { status: TaskStatus.SUCCEEDED, progress: 1 },
    ])
    const seen = []
    await watchTask('/graph/status', 't-1', { ...FAST, onUpdate: (t) => seen.push(t.progress) })

    expect(seen).toEqual([0.3, 1])
  })

  it('encodes the task id rather than pasting it into a path', async () => {
    const calls = stubFetch([{ status: TaskStatus.SUCCEEDED }])
    await watchTask('/graph/status', 'a/../b', FAST)

    expect(calls[0]).toContain('a%2F..%2Fb')
  })
})

describe('errors while polling', () => {
  it('survives a hiccup and carries on', async () => {
    stubFetch([
      { status: TaskStatus.RUNNING },
      new TypeError('connection reset'),
      { status: TaskStatus.SUCCEEDED },
    ])
    const seen = []
    const task = await pollUntil(
      () => import('../../src/api/client.js').then((m) => m.get('/graph/status/t-1')),
      { ...FAST, done: (t) => isSettled(t.status), onError: (e) => seen.push(e) },
    )

    expect(task.status).toBe(TaskStatus.SUCCEEDED)
    expect(seen).toHaveLength(1)
  })

  it('GIVES UP AT ONCE ON A 404 — a missing task will not appear later', async () => {
    let calls = 0
    await expect(
      pollUntil(
        async () => {
          calls += 1
          throw new ApiError('No task', { status: 404 })
        },
        { ...FAST, done: () => false },
      ),
    ).rejects.toThrow('No task')

    expect(calls, 'a 404 must not be retried').toBe(1)
  })

  it('gives up at once on a refusal', async () => {
    let calls = 0
    await expect(
      pollUntil(
        async () => {
          calls += 1
          throw new ApiError('Still running', { status: 409 })
        },
        { ...FAST, done: () => false },
      ),
    ).rejects.toThrow('Still running')

    expect(calls).toBe(1)
  })

  it('gives up after enough consecutive failures', async () => {
    let calls = 0
    await expect(
      pollUntil(
        async () => {
          calls += 1
          throw new TypeError('down')
        },
        { ...FAST, done: () => false, maxErrors: 3 },
      ),
    ).rejects.toThrow('down')

    expect(calls).toBe(3)
  })

  it('stops immediately when aborted', async () => {
    const controller = new AbortController()
    const promise = pollUntil(async () => ({ status: TaskStatus.RUNNING }), {
      ...FAST,
      done: () => false,
      signal: controller.signal,
    })
    controller.abort(new Error('left the page'))

    await expect(promise).rejects.toThrow('left the page')
  })
})

describe('defaults', () => {
  it('polls often enough to feel live without hammering a long run', () => {
    expect(DEFAULT_INTERVAL).toBeGreaterThanOrEqual(1000)
    expect(DEFAULT_INTERVAL).toBeLessThanOrEqual(3000)
  })
})
