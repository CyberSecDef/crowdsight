<script setup>
/* Stage 2 — the population, before it runs.

   Edits and removals are staged locally and saved in one call, because that is
   what the endpoint does: the server rewrites profiles.json and both OASIS
   files together and renumbers user_id, which is the list index. Staging also
   means the consequence of a removal can be shown before it happens rather
   than discovered afterwards.

   The named-versus-synthetic split is the headline rather than a detail. It is
   the difference between an agent standing for someone the document actually
   named and one we invented to fill out the crowd, and every claim a report
   later makes about "the population" rests on knowing which is which. */
import { computed, onMounted, ref, watch } from 'vue'
import { simulation as simulationApi } from '../api/index.js'
import {
  breakdown,
  describeChanges,
  editedProfiles,
  isNamed,
  matches,
} from '../api/profiles.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ErrorBanner from '../components/ErrorBanner.vue'
import ProfileCard from '../components/ProfileCard.vue'

const props = defineProps({ simId: { type: String, required: true } })

const workflow = useWorkflowStore()

const original = ref([])
const working = ref([])
const removedIds = ref([])
const openIds = ref([])
const provenanceFilter = ref('all')
const query = ref('')
const loading = ref(true)
const saving = ref(false)
const error = ref(null)
const saved = ref('')

const kept = computed(() =>
  working.value.filter((profile) => !removedIds.value.includes(profile.user_id)),
)

const stats = computed(() => breakdown(kept.value))
const originalStats = computed(() => breakdown(original.value))

const changes = computed(() =>
  describeChanges({
    original: original.value,
    kept: kept.value,
    edited: editedProfiles(original.value, kept.value),
  }),
)

const visible = computed(() =>
  working.value.filter((profile) => {
    if (provenanceFilter.value === 'named' && !isNamed(profile)) return false
    if (provenanceFilter.value === 'synthetic' && isNamed(profile)) return false
    return matches(profile, query.value)
  }),
)

async function load() {
  loading.value = true
  error.value = null
  saved.value = ''
  try {
    workflow.selectSimulation(props.simId)
    const result = await simulationApi.profiles(props.simId)
    original.value = result?.profiles ?? []
    working.value = JSON.parse(JSON.stringify(original.value))
    removedIds.value = []
    openIds.value = []
  } catch (err) {
    error.value = err
  } finally {
    loading.value = false
  }
}

function update(profile) {
  const at = working.value.findIndex((p) => p.user_id === profile.user_id)
  if (at !== -1) working.value[at] = profile
}

function toggle(userId) {
  const at = openIds.value.indexOf(userId)
  if (at === -1) openIds.value.push(userId)
  else openIds.value.splice(at, 1)
}

function remove(userId) {
  if (!removedIds.value.includes(userId)) removedIds.value.push(userId)
}

function restore(userId) {
  removedIds.value = removedIds.value.filter((id) => id !== userId)
}

function discard() {
  working.value = JSON.parse(JSON.stringify(original.value))
  removedIds.value = []
  saved.value = ''
}

async function save() {
  saving.value = true
  error.value = null
  saved.value = ''
  try {
    const result = await simulationApi.replaceProfiles(props.simId, kept.value)
    const message =
      `Saved: ${result.count} agent(s) — ${result.named} named, ` +
      `${result.synthetic} synthetic.` +
      (result.renumbered ? ' Agent ids were renumbered.' : '')
    // Reload rather than trusting the local copy: the server renumbers
    // user_ids and regenerates usernames, so what is on disk is not what was
    // sent, and editing on top of a stale copy would target the wrong agents.
    //
    // The confirmation is set *after* the reload, because load() clears it —
    // setting it first meant the message was wiped before it ever rendered.
    await load()
    saved.value = message
  } catch (err) {
    error.value = err
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(() => props.simId, load)
</script>

<template>
  <div class="stack">
    <div class="row">
      <h1>Environment</h1>
      <span class="mono dim small">{{ simId }}</span>
    </div>

    <ErrorBanner :error="error" :retry="load" />
    <p v-if="loading" class="dim">Loading the population…</p>

    <template v-if="!loading && original.length">
      <!-- The breakdown, as the headline -->
      <section class="card stack">
        <div class="row">
          <h2>{{ stats.total }} agent(s)</h2>
          <span v-if="stats.total !== originalStats.total" class="tag tag--warn">
            was {{ originalStats.total }}
          </span>
        </div>
        <div class="split-bar" role="img"
             :aria-label="`${stats.named} named, ${stats.synthetic} synthetic`">
          <span class="named" :style="{ width: `${stats.namedPercent}%` }"></span>
        </div>
        <p class="small dim">
          <strong>{{ stats.named }} named</strong> — agents standing for people and
          organisations the document actually names ·
          <strong>{{ stats.synthetic }} synthetic</strong> — plausible members of
          the crowd, invented to fill out the population
        </p>
      </section>

      <!-- Filters -->
      <div class="row">
        <label class="field">
          <span class="dim small">Show</span>
          <select v-model="provenanceFilter">
            <option value="all">All ({{ working.length }})</option>
            <option value="named">Named only</option>
            <option value="synthetic">Synthetic only</option>
          </select>
        </label>
        <label class="field grow">
          <span class="dim small">Search personas</span>
          <input v-model="query" type="search" placeholder="occupation, interest, leaning…" />
        </label>
      </div>

      <!-- Pending changes -->
      <div v-if="changes.dirty" class="pending" role="status">
        <div class="row">
          <strong class="small">{{ changes.changes.join(' · ') }}</strong>
          <span class="spacer"></span>
          <button class="btn" type="button" :disabled="saving" @click="discard">
            Discard
          </button>
          <button class="btn btn--primary" type="button" :disabled="saving" @click="save">
            {{ saving ? 'Saving…' : 'Save population' }}
          </button>
        </div>
        <p v-if="changes.renumbers" class="dim small">
          Agent ids are positions in the list, so removing
          {{ changes.removed.length }} agent(s) renumbers the ones after them.
          Nothing refers to those ids until the run starts.
        </p>
      </div>

      <p v-if="saved" class="saved small" role="status">{{ saved }}</p>

      <p v-if="!visible.length" class="dim">
        No agents match that filter.
      </p>

      <ProfileCard
        v-for="profile in visible"
        :key="profile.user_id"
        :profile="profile"
        :open="openIds.includes(profile.user_id)"
        :removed="removedIds.includes(profile.user_id)"
        :busy="saving"
        @toggle="toggle(profile.user_id)"
        @update="update"
        @remove="remove(profile.user_id)"
        @restore="restore(profile.user_id)"
      />
    </template>

    <p v-else-if="!loading && !error" class="dim">
      This simulation has no population yet. Prepare it first.
    </p>
  </div>
</template>

<style scoped>
.split-bar {
  height: 10px;
  border-radius: 999px;
  background: var(--surface-2);
  overflow: hidden;
  border: 1px solid var(--border);
}

.named {
  display: block;
  height: 100%;
  background: var(--warn);
}

.field { display: flex; flex-direction: column; gap: 0.15rem; }
.grow { flex: 1; min-width: 12rem; }

.field input,
.field select {
  font: inherit;
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  width: 100%;
}

.pending {
  position: sticky;
  top: 0.5rem;
  z-index: 2;
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  background: var(--surface);
  padding: 0.6rem 0.9rem;
}

.pending p { margin: 0.3rem 0 0; }
.spacer { flex: 1; }
.saved { color: var(--ok); margin: 0; }
</style>
