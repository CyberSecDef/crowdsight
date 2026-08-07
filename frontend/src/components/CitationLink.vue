<script setup>
/* A claim's evidence, expandable into the posts it rests on.

   The whole point of Phase 8 was that a report claim cites the run rather than
   the model's prior assumptions. That guarantee is only worth something if a
   reader can actually follow the citation, so these fetch the cited posts by
   id — which is why the posts endpoint grew a `post_ids` filter.

   A citation that resolves to nothing is shown as such rather than silently
   collapsing. Grounding drops fabricated claims before a report is returned,
   so an unresolvable id here means something else is wrong, and hiding it
   would hide exactly the failure the verification exists to catch. */
import { ref } from 'vue'
import { simulation as simulationApi } from '../api/index.js'

const props = defineProps({
  citation: { type: Object, default: () => ({}) },
  simId: { type: String, required: true },
})

const open = ref(false)
const posts = ref([])
const loading = ref(false)
const failed = ref('')

async function toggle() {
  open.value = !open.value
  if (!open.value || posts.value.length || loading.value) return

  const ids = props.citation?.post_ids || []
  if (!ids.length) return

  loading.value = true
  failed.value = ''
  try {
    const result = await simulationApi.posts(props.simId, {
      post_ids: ids.join(','),
      limit: ids.length,
    })
    posts.value = result.posts || []
  } catch (error) {
    failed.value = error.message
  } finally {
    loading.value = false
  }
}

const empty = () =>
  !(props.citation?.post_ids?.length ||
    props.citation?.agent_ids?.length ||
    props.citation?.rounds?.length)
</script>

<template>
  <div class="citation">
    <p v-if="empty()" class="dim small uncited">
      No evidence cited for this claim.
    </p>

    <template v-else>
      <button class="evidence small" type="button" :aria-expanded="open" @click="toggle">
        <span>{{ open ? '▾' : '▸' }}</span>
        <span v-if="citation.post_ids?.length">
          post{{ citation.post_ids.length > 1 ? 's' : '' }}
          {{ citation.post_ids.join(', ') }}
        </span>
        <span v-if="citation.agent_ids?.length">
          · agent{{ citation.agent_ids.length > 1 ? 's' : '' }}
          {{ citation.agent_ids.join(', ') }}
        </span>
        <span v-if="citation.rounds?.length">
          · round{{ citation.rounds.length > 1 ? 's' : '' }}
          {{ citation.rounds.join(', ') }}
        </span>
      </button>

      <div v-if="open" class="cited">
        <p v-if="loading" class="dim small">Fetching the cited post(s)…</p>
        <p v-else-if="failed" class="bad small">{{ failed }}</p>

        <article v-for="post in posts" :key="post.post_id" class="post">
          <div class="row small">
            <strong>{{ post.name }}</strong>
            <span class="dim mono">@{{ post.username }}</span>
            <span class="tag">round {{ post.round }}</span>
            <span class="tag">{{ post.kind }}</span>
            <span class="dim">{{ post.engagement }} engagement</span>
          </div>
          <p class="content">{{ post.content || post.quote_content || '(no text)' }}</p>
        </article>

        <p v-if="!loading && !failed && citation.post_ids?.length && !posts.length"
           class="bad small">
          The cited post(s) could not be found in this run.
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.citation { margin-top: 0.35rem; }

.evidence {
  font: inherit;
  font-size: 0.8rem;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-dim);
  padding: 0.15rem 0.5rem;
  cursor: pointer;
  text-align: left;
}

.evidence:hover { border-color: var(--accent); color: var(--text); }

.uncited { margin: 0; font-style: italic; }

.cited { margin-top: 0.4rem; }

.post {
  border-left: 2px solid var(--border);
  padding: 0.3rem 0 0.3rem 0.6rem;
  margin-bottom: 0.4rem;
}

.content { margin: 0.2rem 0 0; font-size: 0.9rem; }
.bad { color: var(--bad); margin: 0; }
</style>
