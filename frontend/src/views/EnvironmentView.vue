<script setup>
/* Stage 2. Step 3 builds profile browsing and editing. */
import { onMounted, ref } from 'vue'
import { simulation as simulationApi } from '../api/index.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ErrorBanner from '../components/ErrorBanner.vue'
import StageScaffold from '../components/StageScaffold.vue'

const props = defineProps({ simId: { type: String, required: true } })

const workflow = useWorkflowStore()
const profiles = ref([])
const error = ref(null)

async function load() {
  error.value = null
  try {
    const result = await simulationApi.profiles(props.simId, { limit: 5 })
    profiles.value = result?.profiles ?? []
    workflow.selectSimulation(props.simId)
  } catch (err) {
    error.value = err
  }
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <h1>Environment</h1>
    <p class="dim small mono">{{ simId }}</p>
    <ErrorBanner :error="error" :retry="load" />

    <section v-if="profiles.length" class="card">
      <h2>{{ profiles.length }} profile(s) sampled</h2>
      <ul class="small dim">
        <li v-for="entry in profiles" :key="entry.user_id">
          {{ entry.name || entry.username }} — {{ entry.occupation || 'unknown' }}
          ({{ entry.provenance || 'synthetic' }})
        </li>
      </ul>
    </section>

    <StageScaffold
      title="Stage 2 — environment setup"
      step="Phase 9 Step 3"
      :covers="[
        'Browse generated agents and inspect personas',
        'Named-vs-synthetic breakdown shown plainly',
        'Edit or remove agents before the run',
      ]"
    />
  </div>
</template>
