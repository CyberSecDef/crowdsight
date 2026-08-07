<script setup>
/* Show and hide entity types.

   Counts are shown next to each type because "hide Organisation" means
   something different when it is 2 nodes than when it is 40. */
defineProps({
  types: { type: Array, default: () => [] },
  hidden: { type: Array, default: () => [] },
})
defineEmits(['toggle', 'all', 'none'])
</script>

<template>
  <div class="row filter">
    <span class="dim small">Types</span>
    <button
      v-for="entry in types"
      :key="entry.type"
      class="chip"
      type="button"
      :class="{ 'is-hidden': hidden.includes(entry.type) }"
      :aria-pressed="!hidden.includes(entry.type)"
      @click="$emit('toggle', entry.type)"
    >
      {{ entry.type }} <span class="dim">{{ entry.count }}</span>
    </button>
    <button class="btn" type="button" @click="$emit('all')">Show all</button>
  </div>
</template>

<style scoped>
.filter { gap: 0.3rem; }

.chip {
  font: inherit;
  font-size: 0.8rem;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
}

.chip:hover { border-color: var(--accent); }

.is-hidden {
  opacity: 0.45;
  text-decoration: line-through;
}
</style>
