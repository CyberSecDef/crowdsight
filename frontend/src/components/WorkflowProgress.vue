<script setup>
/* The five stages, shown as a workflow even though the URLs are not numbered.
   A stage the user cannot reach yet renders as text rather than a link — the
   spec's ordering is real (you cannot review profiles before a simulation
   exists) and a link that 404s teaches nothing. */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useWorkflowStore } from '../stores/workflow.js'

const route = useRoute()
const workflow = useWorkflowStore()

const current = computed(() => route.meta?.stage ?? 0)
const stages = computed(() => workflow.reachable)
</script>

<template>
  <nav class="stages" aria-label="Workflow stages">
    <component
      v-for="entry in stages"
      :key="entry.stage"
      :is="entry.available ? 'RouterLink' : 'span'"
      :to="entry.available ? workflow.routeFor(entry.stage) : undefined"
      class="stages__item"
      :class="{
        'is-current': entry.stage === current,
        'is-locked': !entry.available,
      }"
      :aria-current="entry.stage === current ? 'step' : undefined"
    >
      <span class="stages__num">{{ entry.stage }}</span>
      <span>{{ entry.label }}</span>
    </component>
  </nav>
</template>

<style scoped>
.stages {
  display: flex;
  gap: 0.25rem;
  flex-wrap: wrap;
  padding: 0.5rem 0 0.75rem;
}

.stages__item {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.25rem 0.6rem 0.25rem 0.3rem;
  border-radius: 999px;
  font-size: 0.85rem;
  text-decoration: none;
  color: var(--text-dim);
  border: 1px solid transparent;
}

.stages__item:hover:not(.is-locked) {
  background: var(--surface-2);
}

.stages__num {
  display: grid;
  place-items: center;
  width: 1.4rem;
  height: 1.4rem;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  font-size: 0.75rem;
}

.is-current {
  color: var(--text);
  border-color: var(--border);
  background: var(--surface);
}

.is-current .stages__num {
  background: var(--accent);
  color: var(--accent-text);
  border-color: transparent;
}

.is-locked {
  opacity: 0.45;
  cursor: default;
}
</style>
