<script setup>
/* One background task, while it runs.

   `awaiting_review` gets its own treatment: it is not finished and not
   failing, it is waiting for a person, and showing it as "in progress" would
   leave the operator watching a bar that will never move. */
import { computed } from 'vue'
import { TaskStatus, isParked } from '../api/polling.js'

const props = defineProps({
  task: { type: Object, default: null },
  label: { type: String, default: 'Working' },
})

const percent = computed(() => Math.round((props.task?.progress ?? 0) * 100))
const parked = computed(() => isParked(props.task?.status))
const failed = computed(() => props.task?.status === TaskStatus.FAILED)
</script>

<template>
  <div v-if="task" class="card stack" role="status" aria-live="polite">
    <div class="row">
      <strong>{{ parked ? 'Waiting for you' : failed ? 'Failed' : label }}</strong>
      <span class="tag" :class="{ 'tag--bad': failed, 'tag--warn': parked }">
        {{ task.status }}
      </span>
      <span v-if="task.stage" class="dim small">{{ task.stage }}</span>
      <span class="dim small">{{ percent }}%</span>
    </div>
    <div class="track" :aria-valuenow="percent" aria-valuemin="0" aria-valuemax="100" role="progressbar">
      <div class="fill" :class="{ 'is-failed': failed }" :style="{ width: `${percent}%` }"></div>
    </div>
    <p v-if="task.message" class="dim small">{{ task.message }}</p>
    <p v-if="task.error" class="failed small">{{ task.error }}</p>
  </div>
</template>

<style scoped>
.track {
  height: 6px;
  border-radius: 999px;
  background: var(--surface-2);
  overflow: hidden;
}

.fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

.is-failed { background: var(--bad); }
.failed { margin: 0; color: var(--bad); }
</style>
