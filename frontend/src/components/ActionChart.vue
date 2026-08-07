<script setup>
/* What the population actually did, as a bar per action type.

   Counted from the timeline, which is our own per-round record, so the totals
   here and the per-round table cannot disagree. */
import { computed } from 'vue'

const props = defineProps({
  distribution: { type: Array, default: () => [] },
})

const max = computed(() =>
  Math.max(1, ...props.distribution.map((entry) => entry.count)),
)
const total = computed(() =>
  props.distribution.reduce((sum, entry) => sum + entry.count, 0),
)
</script>

<template>
  <figure class="card">
    <figcaption><h3>Action distribution</h3></figcaption>

    <p v-if="!distribution.length" class="dim small">No actions were recorded.</p>

    <table v-else class="bars">
      <tbody>
        <tr v-for="entry in distribution" :key="entry.action">
          <th scope="row">{{ entry.action }}</th>
          <td class="bar-cell">
            <span class="bar" :style="{ width: `${(entry.count / max) * 100}%` }"></span>
          </td>
          <td class="count mono">{{ entry.count }}</td>
          <td class="dim small">{{ Math.round((entry.count / total) * 100) }}%</td>
        </tr>
      </tbody>
    </table>
  </figure>
</template>

<style scoped>
figure { margin: 0; }
figcaption { margin-bottom: 0.4rem; }
.bars { width: 100%; }
.bars th { font-weight: 400; white-space: nowrap; width: 1%; }
.bar-cell { width: 100%; }

.bar {
  display: block;
  height: 12px;
  border-radius: 3px;
  background: var(--accent);
  min-width: 2px;
}

.count { text-align: right; width: 1%; }
</style>
