<script setup>
/* Stage 4. Step 5 builds the rendered report, charts and citation links. */
import { onMounted, ref } from 'vue'
import { report as reportApi } from '../api/index.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ErrorBanner from '../components/ErrorBanner.vue'
import StageScaffold from '../components/StageScaffold.vue'

const props = defineProps({
  simId: { type: String, required: true },
  reportId: { type: String, default: '' },
})

const workflow = useWorkflowStore()
const reports = ref([])
const error = ref(null)

async function load() {
  error.value = null
  try {
    workflow.selectSimulation(props.simId)
    const result = await reportApi.list({ sim_id: props.simId })
    reports.value = result?.reports ?? []
  } catch (err) {
    error.value = err
  }
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <h1>Report</h1>
    <p class="dim small mono">{{ simId }}</p>
    <ErrorBanner :error="error" :retry="load" />

    <section v-if="reports.length" class="card">
      <h2>{{ reports.length }} report(s) on this run</h2>
      <ul class="small">
        <li v-for="entry in reports" :key="entry.report_id">
          <span class="mono">{{ entry.report_id }}</span>
          <span class="dim"> — {{ entry.citations_resolved ?? 0 }} citation(s) resolved</span>
          <p class="dim small">{{ entry.summary }}</p>
          <a :href="reportApi.exportUrl(entry.report_id, 'markdown')">markdown</a>
        </li>
      </ul>
    </section>
    <p v-else class="dim small">No reports generated for this run yet.</p>

    <StageScaffold
      title="Stage 4 — report"
      step="Phase 9 Step 5"
      :covers="[
        'Rendered report with sentiment, action and influence charts',
        'Citation links that jump to the underlying post',
        'Markdown and HTML export buttons',
      ]"
    />
  </div>
</template>
