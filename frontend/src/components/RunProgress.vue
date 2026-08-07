<script setup>
/* Where the run has got to.

   `percent` comes from the server rather than being computed here, so the bar
   and the round counter cannot disagree. A run that ends early — stopped, or
   killed at a time limit — leaves the bar short of full, which is honest. */
import { computed } from 'vue'
import { runStateClass } from '../api/states.js'

const props = defineProps({
  status: { type: Object, default: null },
  timeline: { type: Array, default: () => [] },
})

const percent = computed(() => Math.round(props.status?.percent ?? 0))
const agents = computed(() => props.status?.agents || {})
const counts = computed(() => Object.entries(props.status?.action_counts || {}))
</script>

<template>
  <section v-if="status" class="card stack">
    <div class="row">
      <h3>
        Round {{ status.round ?? 0 }} of {{ status.total_rounds ?? '—' }}
      </h3>
      <span class="tag" :class="runStateClass(status.state)">{{ status.state }}</span>
      <span class="dim small">{{ percent }}%</span>
      <span v-if="status.live === null" class="dim small">from disk</span>
    </div>

    <div class="track" role="progressbar" :aria-valuenow="percent"
         aria-valuemin="0" aria-valuemax="100">
      <div class="fill" :style="{ width: `${percent}%` }"></div>
    </div>

    <p class="dim small">
      {{ status.rounds_completed ?? 0 }} round(s) complete ·
      {{ agents.active_last_round ?? 0 }} agent(s) acted last round ·
      {{ agents.skipped_last_round ?? 0 }} skipped ·
      {{ agents.failed_last_round ?? 0 }} failed
    </p>

    <div v-if="counts.length" class="row counts">
      <span v-for="[action, count] in counts" :key="action" class="tag">
        {{ action }} {{ count }}
      </span>
    </div>

    <details v-if="timeline.length">
      <summary class="small">Per-round detail</summary>
      <div class="scroll-x">
        <table>
          <thead>
            <tr>
              <th>Round</th><th>Invoked</th><th>Acted</th><th>Skipped</th>
              <th>Failed</th><th>Posts</th><th>Comments</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="round in timeline" :key="round.round">
              <td>{{ round.round }}<span v-if="round.seed" class="dim"> seed</span></td>
              <td>{{ round.invoked }}</td>
              <td>{{ round.acted }}</td>
              <td>{{ round.skipped ?? 0 }}</td>
              <td :class="{ bad: round.failed }">{{ round.failed ?? 0 }}</td>
              <td>{{ round.posts ?? 0 }}</td>
              <td>{{ round.comments ?? 0 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
  </section>
</template>

<style scoped>
.track {
  height: 8px;
  border-radius: 999px;
  background: var(--surface-2);
  overflow: hidden;
}

.fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.4s ease;
}

.counts { gap: 0.3rem; }
.bad { color: var(--bad); }
summary { cursor: pointer; color: var(--text-dim); }
</style>
