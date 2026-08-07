<script setup>
/* Stage 1. Step 2 builds the upload UI, ontology review and the graph
   visualisation; the shell shows what the graph is so the wiring is visible. */
import { onMounted, ref, watch } from 'vue'
import { graph as graphApi } from '../api/index.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ErrorBanner from '../components/ErrorBanner.vue'
import StageScaffold from '../components/StageScaffold.vue'

const props = defineProps({ graphId: { type: String, default: '' } })

const workflow = useWorkflowStore()
const detail = ref(null)
const error = ref(null)
const loading = ref(false)

async function load() {
  if (!props.graphId) {
    detail.value = null
    return
  }
  loading.value = true
  error.value = null
  try {
    detail.value = await graphApi.detail(props.graphId)
    workflow.selectGraph(props.graphId)
  } catch (err) {
    error.value = err
  } finally {
    loading.value = false
  }
}

watch(() => props.graphId, load)
onMounted(load)
</script>

<template>
  <div class="stack">
    <h1>{{ graphId ? 'Graph' : 'New graph' }}</h1>
    <ErrorBanner :error="error" :retry="load" />

    <section v-if="graphId && detail" class="card">
      <h2 class="mono">{{ graphId }}</h2>
      <p class="dim small">{{ detail.domain || 'no domain recorded' }}</p>
      <p class="dim small">
        {{ detail.entity_count ?? 0 }} entities ·
        {{ detail.chunk_count ?? 0 }} chunks ·
        {{ detail.page_count ?? 0 }} page(s) ·
        {{ (detail.entity_types || []).length }} entity type(s)
      </p>
    </section>
    <p v-else-if="loading" class="dim">Loading…</p>

    <StageScaffold
      title="Stage 1 — graph build"
      step="Phase 9 Step 2"
      :covers="[
        'Drag-and-drop upload with client-side type and size validation',
        'Ontology review and edit before extraction',
        'Extraction progress, driven by the polling helper',
        'Interactive graph with type filtering and node inspection',
      ]"
    />
  </div>
</template>
