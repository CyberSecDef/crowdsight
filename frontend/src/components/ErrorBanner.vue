<script setup>
/* One way of showing a failure, so every view shows it the same way.

   The distinction that matters is between a refusal and an outage. "This run
   is still going, so it cannot be reported on" is something the user can act
   on; "the backend is not answering" is not, and dressing the second up as the
   first sends people looking for a mistake they did not make. */
import { computed } from 'vue'
import { ApiError, NetworkError } from '../api/index.js'

const props = defineProps({
  error: { type: [Error, String, null], default: null },
  retry: { type: Function, default: null },
})

const message = computed(() => {
  if (!props.error) return ''
  return typeof props.error === 'string' ? props.error : props.error.message
})

const kind = computed(() => {
  const error = props.error
  if (!error || typeof error === 'string') return 'error'
  if (error instanceof NetworkError) return 'offline'
  if (error instanceof ApiError && error.refusal) return 'refused'
  if (error instanceof ApiError && error.notFound) return 'missing'
  return 'error'
})

const heading = computed(
  () =>
    ({
      offline: 'The backend is not answering',
      refused: 'That cannot be done yet',
      missing: 'Not found',
      error: 'Something went wrong',
    })[kind.value],
)
</script>

<template>
  <div v-if="message" class="banner" :class="`banner--${kind}`" role="alert">
    <div>
      <strong>{{ heading }}</strong>
      <p class="small">{{ message }}</p>
    </div>
    <button v-if="retry" class="btn" type="button" @click="retry">Try again</button>
  </div>
</template>

<style scoped>
.banner {
  display: flex;
  gap: var(--gap);
  align-items: flex-start;
  justify-content: space-between;
  border: 1px solid var(--border);
  border-left: 3px solid var(--bad);
  background: var(--surface);
  border-radius: var(--radius);
  padding: 0.75rem 1rem;
}

.banner p { margin: 0.15rem 0 0; color: var(--text-dim); }
.banner--refused { border-left-color: var(--warn); }
.banner--offline { border-left-color: var(--bad); }
.banner--missing { border-left-color: var(--text-dim); }
</style>
