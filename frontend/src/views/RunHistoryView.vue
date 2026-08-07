<script setup>
/* Every run the machine has on disk, newest first, with the reports filed
   against them. A run takes hours, so coming back to one you started
   yesterday is the normal case rather than the exception. */
import { computed, onMounted, ref } from 'vue'
import { report as reportApi, simulation as simulationApi } from '../api/index.js'
import { useWorkflowStore } from '../stores/workflow.js'
import { runStateClass } from '../api/states.js'
import ErrorBanner from '../components/ErrorBanner.vue'

const workflow = useWorkflowStore()
const runs = ref([])
const reports = ref([])
const loading = ref(true)
const error = ref(null)

const reportsBySim = computed(() => {
  const grouped = {}
  for (const entry of reports.value) {
    ;(grouped[entry.sim_id] ||= []).push(entry)
  }
  return grouped
})

async function load() {
  loading.value = true
  error.value = null
  try {
    const [runResult, reportResult] = await Promise.all([
      simulationApi.list({ limit: 100 }),
      reportApi.list({ limit: 100 }),
    ])
    runs.value = runResult?.simulations ?? []
    reports.value = reportResult?.reports ?? []
  } catch (err) {
    error.value = err
  } finally {
    loading.value = false
  }
}

function select(entry) {
  workflow.selectSimulation(entry.sim_id, { graphId: entry.graph_id })
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <h1>Run history</h1>
    <ErrorBanner :error="error" :retry="load" />
    <p v-if="loading" class="dim">Loading…</p>

    <p v-else-if="!runs.length" class="dim">
      No runs on disk yet.
    </p>

    <div v-else class="scroll-x">
      <table>
        <thead>
          <tr>
            <th>Simulation</th>
            <th>State</th>
            <th>Platform</th>
            <th>Finished</th>
            <th>Reports</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in runs" :key="entry.sim_id">
            <td class="mono">{{ entry.sim_id }}</td>
            <td>
              <span class="tag" :class="runStateClass(entry.state)">
                {{ entry.state || 'unknown' }}
              </span>
            </td>
            <td>{{ entry.platform || '—' }}</td>
            <td class="small dim">{{ (entry.finished_at || '').slice(0, 16) || '—' }}</td>
            <td>{{ (reportsBySim[entry.sim_id] || []).length }}</td>
            <td class="row">
              <RouterLink
                class="btn"
                :to="{ name: 'run', params: { simId: entry.sim_id } }"
                @click="select(entry)"
              >
                Run
              </RouterLink>
              <RouterLink
                class="btn"
                :to="{ name: 'report', params: { simId: entry.sim_id } }"
                @click="select(entry)"
              >
                Report
              </RouterLink>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
