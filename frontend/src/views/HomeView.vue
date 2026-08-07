<script setup>
/* The project list. A "project" is not a backend entity — the closest thing is
   a graph, which is what a set of uploaded documents becomes, and simulations
   hang off it. So this lists both rather than inventing a wrapper the API
   would then have to be taught about. */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { graph as graphApi, simulation as simulationApi } from '../api/index.js'
import { runStateClass } from '../api/states.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ErrorBanner from '../components/ErrorBanner.vue'

const router = useRouter()
const workflow = useWorkflowStore()

const graphs = ref([])
const simulations = ref([])
const loading = ref(true)
const error = ref(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    const [graphResult, simResult] = await Promise.all([
      graphApi.list(),
      simulationApi.list({ limit: 10 }),
    ])
    graphs.value = graphResult?.graphs ?? []
    simulations.value = simResult?.simulations ?? []
  } catch (err) {
    error.value = err
  } finally {
    loading.value = false
  }
}

function openGraph(entry) {
  workflow.selectGraph(entry.graph_id)
  router.push({ name: 'graph', params: { graphId: entry.graph_id } })
}

function openSimulation(entry) {
  workflow.selectSimulation(entry.sim_id, { graphId: entry.graph_id })
  router.push({ name: 'run', params: { simId: entry.sim_id } })
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <div class="row">
      <h1>Projects</h1>
      <RouterLink class="btn btn--primary" :to="{ name: 'graph-new' }">
        New graph
      </RouterLink>
    </div>

    <ErrorBanner :error="error" :retry="load" />
    <p v-if="loading" class="dim">Loading…</p>

    <section v-if="!loading">
      <h2>Graphs</h2>
      <p v-if="!graphs.length" class="dim small">
        Nothing yet. Upload documents to build the first graph.
      </p>
      <div v-else class="grid">
        <button
          v-for="entry in graphs"
          :key="entry.graph_id"
          class="card project"
          type="button"
          @click="openGraph(entry)"
        >
          <strong class="mono">{{ entry.graph_id }}</strong>
          <span class="dim small">{{ entry.domain || 'no domain recorded' }}</span>
          <span class="dim small">
            {{ entry.entity_count ?? 0 }} entities · {{ entry.filename || '—' }}
          </span>
        </button>
      </div>
    </section>

    <section v-if="!loading">
      <h2>Recent simulations</h2>
      <p v-if="!simulations.length" class="dim small">No simulations yet.</p>
      <div v-else class="scroll-x">
        <table>
          <thead>
            <tr>
              <th>Simulation</th>
              <th>State</th>
              <th>Platform</th>
              <th>Prepared</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in simulations" :key="entry.sim_id">
              <td class="mono">{{ entry.sim_id }}</td>
              <td>
                <span class="tag" :class="runStateClass(entry.state)">
                  {{ entry.state || 'unknown' }}
                </span>
              </td>
              <td>{{ entry.platform || '—' }}</td>
              <td>{{ entry.prepared ? 'yes' : 'no' }}</td>
              <td>
                <button class="btn" type="button" @click="openSimulation(entry)">
                  Open
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.project {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  text-align: left;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.project:hover { border-color: var(--accent); }
</style>
