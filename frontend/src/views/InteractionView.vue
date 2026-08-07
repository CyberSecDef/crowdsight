<script setup>
/* Stage 5. Step 6 builds the interview UI. */
import { useWorkflowStore } from '../stores/workflow.js'
import StageScaffold from '../components/StageScaffold.vue'
import { onMounted } from 'vue'

const props = defineProps({ simId: { type: String, required: true } })
const workflow = useWorkflowStore()

onMounted(() => workflow.selectSimulation(props.simId))
</script>

<template>
  <div class="stack">
    <h1>Interaction</h1>
    <p class="dim small mono">{{ simId }}</p>

    <StageScaffold
      title="Stage 5 — interaction"
      step="Phase 9 Step 6"
      :covers="[
        'Pick an agent, or ask every agent at once',
        'Ask a question and view the responses',
        'Browse interview history for a finished run',
      ]"
    />
  </div>
</template>
