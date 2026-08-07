<script setup>
/* The action feed, newest last.

   Only ever appended to — the monitor walks the feed forward from the last
   offset it saw rather than re-reading it, so this component never has to
   reconcile a list that changed underneath it.

   Engine actions are excluded by the reader's default, which matters: a
   300-agent run opens with 300 sign_up rows, and a feed that led with those
   would bury the first thing an agent actually did. */
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps({
  actions: { type: Array, default: () => [] },
  live: { type: Boolean, default: false },
})

const container = ref(null)
const follow = ref(true)
const filter = ref('')

const shown = computed(() => {
  const needle = filter.value.trim().toLowerCase()
  if (!needle) return props.actions
  return props.actions.filter((entry) =>
    `${entry.username} ${entry.name} ${entry.action}`.toLowerCase().includes(needle),
  )
})

function onScroll() {
  const element = container.value
  if (!element) return
  // Following means "pinned to the bottom". Scrolling up stops the feed
  // yanking you back every few seconds while you are reading.
  follow.value = element.scrollHeight - element.scrollTop - element.clientHeight < 40
}

watch(
  () => props.actions.length,
  async () => {
    if (!follow.value) return
    await nextTick()
    if (container.value) container.value.scrollTop = container.value.scrollHeight
  },
)
</script>

<template>
  <section class="card stack">
    <div class="row">
      <h3>Action feed</h3>
      <span class="dim small">{{ actions.length }} shown</span>
      <span v-if="live" class="tag tag--warn">live</span>
      <span class="spacer"></span>
      <input v-model="filter" type="search" placeholder="filter by agent or action" />
    </div>

    <div ref="container" class="feed" @scroll="onScroll">
      <p v-if="!shown.length" class="dim small">
        Nothing yet. Actions appear as agents take them.
      </p>
      <article v-for="(entry, index) in shown" :key="`${entry.user_id}-${index}`" class="entry">
        <span class="round mono">r{{ entry.round }}</span>
        <span class="who">
          {{ entry.name }}
          <span class="dim mono">@{{ entry.username }}</span>
        </span>
        <span class="what tag">{{ entry.action }}</span>
        <span v-if="!entry.population" class="tag tag--warn">broadcaster</span>
      </article>
    </div>

    <p v-if="!follow" class="dim small">
      Scrolled up — the feed will not jump.
      <button class="btn" type="button" @click="follow = true">Follow again</button>
    </p>
  </section>
</template>

<style scoped>
.feed {
  max-height: 340px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
}

.entry {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  padding: 0.25rem 0.5rem;
  border-bottom: 1px solid var(--border);
  font-size: 0.85rem;
}

.entry:last-child { border-bottom: none; }
.round { color: var(--text-dim); min-width: 2rem; }
.who { flex: 1; }
.spacer { flex: 1; }

input[type='search'] {
  font: inherit;
  font-size: 0.85rem;
  padding: 0.25rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
}
</style>
