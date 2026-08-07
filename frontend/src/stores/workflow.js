/**
 * What the user is currently working on, and how far through they are.
 *
 * This exists because the workflow spans resources: a graph, then a simulation
 * built from it, then a report about that simulation. The URL carries whichever
 * id the current view needs, but the progress indicator has to know about the
 * others to say which stages are reachable — you cannot review profiles for a
 * simulation that has not been created.
 *
 * It holds ids and the last known status, not the data itself. Views fetch
 * their own data; a store that caches everything is a store that shows stale
 * counts after a run advances.
 */

import { defineStore } from 'pinia'
import { STAGES } from '../router/index.js'
import { simulation as simulationApi } from '../api/index.js'
import { runFinished } from '../api/states.js'

const STORAGE_KEY = 'crowdsight.workflow'

function restore() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}
  } catch {
    return {}
  }
}

export const useWorkflowStore = defineStore('workflow', {
  state: () => ({
    graphId: restore().graphId || '',
    simId: restore().simId || '',
    /** The last run state seen, so the nav can show it without refetching. */
    runState: '',
    error: null,
  }),

  getters: {
    hasGraph: (state) => Boolean(state.graphId),
    hasSimulation: (state) => Boolean(state.simId),

    /** A run that has finished can be reported on and interviewed against. */
    runFinished: (state) => runFinished(state.runState),

    /** Which stages the user can actually open, in order. */
    reachable(state) {
      return STAGES.map((entry) => ({
        ...entry,
        available:
          entry.stage === 1 ||
          (entry.stage === 2 && Boolean(state.simId)) ||
          (entry.stage === 3 && Boolean(state.simId)) ||
          // Reporting and interviewing need a run with data behind them.
          (entry.stage >= 4 && Boolean(state.simId) && this.runFinished),
      }))
    },

    /** Where a stage number actually points, given what exists. */
    routeFor(state) {
      return (stage) => {
        if (stage === 1) {
          return state.graphId
            ? { name: 'graph', params: { graphId: state.graphId } }
            : { name: 'graph-new' }
        }
        if (!state.simId) return { name: 'home' }
        const params = { simId: state.simId }
        if (stage === 2) return { name: 'profiles', params }
        if (stage === 3) return { name: 'run', params }
        if (stage === 4) return { name: 'report', params }
        return { name: 'interview', params }
      }
    },
  },

  actions: {
    selectGraph(graphId) {
      this.graphId = graphId || ''
      this.persist()
    },

    selectSimulation(simId, { graphId } = {}) {
      this.simId = simId || ''
      if (graphId) this.graphId = graphId
      this.runState = ''
      this.persist()
    },

    clear() {
      this.graphId = ''
      this.simId = ''
      this.runState = ''
      this.persist()
    },

    /** Read the run's state once, so the nav knows which stages are open. */
    async refreshRunStatus() {
      if (!this.simId) return null
      try {
        const status = await simulationApi.runStatus(this.simId)
        // run-status reports `state`, not `status`. They are different words.
        this.runState = status?.state || ''
        this.error = null
        return status
      } catch (error) {
        // Not fatal: the nav degrades to "stage not reachable yet".
        this.error = error.message
        return null
      }
    },

    persist() {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({ graphId: this.graphId, simId: this.simId }),
        )
      } catch {
        // Private browsing, or a full quota. Losing the breadcrumb is survivable.
      }
    },
  },
})
