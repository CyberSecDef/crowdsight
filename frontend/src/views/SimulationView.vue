<script setup>
/* Stage 3. Step 4 builds the launch controls and the live run view. */
import { onMounted, ref } from 'vue'
import { simulation as simulationApi } from '../api/index.js'
import { useWorkflowStore } from '../stores/workflow.js'
import { runStateClass } from '../api/states.js'
import ErrorBanner from '../components/ErrorBanner.vue'
import StageScaffold from '../components/StageScaffold.vue'

const props = defineProps({ simId: { type: String, required: true } })

const workflow = useWorkflowStore()
const status = ref(null)
const error = ref(null)

async function load() {
  error.value = null
  try {
    workflow.selectSimulation(props.simId)
    status.value = await simulationApi.runStatus(props.simId)
    workflow.runState = status.value?.state || ''
  } catch (err) {
    error.value = err
  }
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <h1>Simulation</h1>
    <p class="dim small mono">{{ simId }}</p>
    <ErrorBanner :error="error" :retry="load" />

    <section v-if="status" class="card">
      <div class="row">
        <span class="tag" :class="runStateClass(status.state)">
          {{ status.state || 'unknown' }}
        </span>
        <span class="dim small">
          round {{ status.round ?? 0 }} of {{ status.total_rounds ?? '—' }}
          ({{ Math.round(status.percent ?? 0) }}%)
        </span>
        <span v-if="!status.has_data" class="dim small">no run data yet</span>
      </div>
    </section>

    <StageScaffold
      title="Stage 3 — simulation"
      step="Phase 9 Step 4"
      :covers="[
        'Config review and edit, platform and round count',
        'Launch, stop and resume controls',
        'Live progress bar and round counter',
        'Streaming action feed and per-agent activity',
      ]"
    />
  </div>
</template>
