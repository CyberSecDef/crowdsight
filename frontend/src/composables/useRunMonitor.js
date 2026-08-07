/**
 * Watching a run that takes hours.
 *
 * Four endpoints move at three different speeds, so polling them all on one
 * timer would spend most of its requests re-fetching identical answers:
 *
 * * `run-status` is small and drives the progress bar, so it ticks fastest.
 * * The action feed only ever grows, so it is walked forward from the last
 *   offset seen. Re-fetching the whole feed every few seconds for a run that
 *   produces thousands of actions is a lot of work to learn one new row.
 * * `timeline` and `agent-stats` do not change within a round, so they are
 *   refreshed when the round counter moves rather than on a clock.
 *
 * Polling stops when the run finishes, and pauses while the tab is hidden —
 * the shared helper already does the second, which matters here more than
 * anywhere else in the app.
 */

import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'

import { ApiError, simulation as simulationApi } from '../api/index.js'
import { pollUntil } from '../api/polling.js'
import { runFinished } from '../api/states.js'

export const STATUS_INTERVAL = 2000
export const FEED_INTERVAL = 4000
/** Enough to watch a feed scroll; not so much that it holds a run in memory. */
export const FEED_CAP = 400

export function useRunMonitor(simId) {
  const status = ref(null)
  const actions = ref([])
  const timeline = ref([])
  const agents = ref([])
  const error = ref(null)
  const watching = ref(false)

  /** The offset to fetch the feed forward from; only ever moves up. */
  let feedOffset = 0
  let lastRound = -1
  let controller = null
  const tasks = shallowRef([])

  const state = computed(() => status.value?.state || '')
  const finished = computed(() => runFinished(state.value))
  const percent = computed(() => Math.round(status.value?.percent ?? 0))

  async function loadRoundScopedData() {
    const [timelineResult, agentResult] = await Promise.all([
      simulationApi.timeline(simId).catch(() => null),
      simulationApi.agentStats(simId, { limit: 200, sort: 'actions' }).catch(() => null),
    ])
    if (timelineResult) timeline.value = timelineResult.rounds || []
    if (agentResult) agents.value = agentResult.agents || []
  }

  /**
   * Fetch only what is new, walking `next_offset` forward.
   *
   * A run that has not started has no database, and the reader says so with a
   * 409. That is a state, not a fault — the scenario screen would otherwise
   * carry an error banner about a run nobody has launched yet.
   */
  async function drainFeed() {
    for (let guard = 0; guard < 20; guard += 1) {
      let page
      try {
        page = await simulationApi.actions(simId, {
          limit: 100,
          offset: feedOffset,
          order: 'oldest',
        })
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) return
        throw err
      }
      const fresh = page.actions || []
      if (!fresh.length) return

      actions.value = [...actions.value, ...fresh].slice(-FEED_CAP)
      feedOffset = page.next_offset ?? feedOffset + fresh.length
      if (!page.has_more) return
    }
  }

  async function refreshOnce() {
    status.value = await simulationApi.runStatus(simId)
    const round = status.value?.rounds_completed ?? -1
    if (round !== lastRound) {
      lastRound = round
      await loadRoundScopedData()
    }
  }

  function start() {
    if (watching.value) return
    watching.value = true
    error.value = null
    controller = new AbortController()
    const signal = controller.signal

    tasks.value = [
      // Status: fast, cheap, drives the bar. Stops when the run settles.
      pollUntil(() => simulationApi.runStatus(simId), {
        interval: STATUS_INTERVAL,
        signal,
        onUpdate: async (value) => {
          status.value = value
          const round = value?.rounds_completed ?? -1
          if (round !== lastRound) {
            lastRound = round
            await loadRoundScopedData()
          }
        },
        onError: (err) => (error.value = err),
        done: (value) => runFinished(value?.state),
      }).catch((err) => {
        if (err?.name !== 'AbortError') error.value = err
      }),

      // The feed, walked forward rather than re-read.
      pollUntil(async () => {
        await drainFeed()
        return status.value
      }, {
        interval: FEED_INTERVAL,
        signal,
        onError: (err) => (error.value = err),
        done: () => finished.value,
      }).catch((err) => {
        if (err?.name !== 'AbortError') error.value = err
      }),
    ]

    Promise.allSettled(tasks.value).then(() => {
      watching.value = false
    })
  }

  function stop() {
    controller?.abort(new Error('left the run view'))
    controller = null
    watching.value = false
  }

  /** Read everything once, for a run that is already over. */
  async function loadStatic() {
    error.value = null
    try {
      await refreshOnce()
      await drainFeed()
    } catch (err) {
      error.value = err
    }
  }

  function reset() {
    stop()
    status.value = null
    actions.value = []
    timeline.value = []
    agents.value = []
    feedOffset = 0
    lastRound = -1
  }

  onBeforeUnmount(stop)

  return {
    status, actions, timeline, agents, error, watching,
    state, finished, percent,
    start, stop, loadStatic, reset, refreshOnce,
  }
}
