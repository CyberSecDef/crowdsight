<script setup>
/* The scenario, before it runs.

   Two backend behaviours are surfaced here rather than left to be discovered:

   * the action set is per platform, so switching prunes actions the new
     platform has never heard of instead of sending them and being refused;
   * a seed post quoting a named person is checked against the source document,
     and one that cannot be found is demoted to the broadcaster. That is a
     correction to what the operator wrote, so a demoted post says so. */
import { computed, ref, watch } from 'vue'
import {
  ATTRIBUTIONS,
  MAX_ROUNDS,
  PLATFORMS,
  actionsFor,
  actionsLostBySwitching,
  blankScheduledEvent,
  blankSeedPost,
  pruneActions,
  validateConfig,
  wasDemoted,
} from '../api/scenario.js'

const props = defineProps({
  config: { type: Object, required: true },
  busy: { type: Boolean, default: false },
  locked: { type: Boolean, default: false },
})
const emit = defineEmits(['save'])

/* A JSON round-trip rather than structuredClone: a prop is a Vue reactive
   Proxy and structuredClone throws DataCloneError on one, in setup, which
   blanks the whole subtree without an error on screen. */
const copy = (value) => JSON.parse(JSON.stringify(value ?? null))

const draft = ref(copy(props.config))
const showJson = ref(false)
const jsonText = ref('')
const jsonError = ref('')
const platformNotice = ref('')

watch(() => props.config, (value) => {
  draft.value = copy(value)
  platformNotice.value = ''
})

const problems = computed(() => validateConfig(draft.value))
const available = computed(() => actionsFor(draft.value.platform))

function switchPlatform(platform) {
  const dropped = actionsLostBySwitching(draft.value.action_space?.actions, platform)
  draft.value.platform = platform
  draft.value.action_space = {
    platform,
    actions: pruneActions(draft.value.action_space?.actions, platform),
  }
  platformNotice.value = dropped.length
    ? `${dropped.join(', ')} removed — not available on ${platform}.`
    : ''
}

function toggleAction(action) {
  const actions = draft.value.action_space.actions
  const at = actions.indexOf(action)
  if (at === -1) actions.push(action)
  else actions.splice(at, 1)
}

function openJson() {
  jsonText.value = JSON.stringify(draft.value, null, 2)
  jsonError.value = ''
  showJson.value = true
}

function applyJson() {
  try {
    const parsed = JSON.parse(jsonText.value)
    if (!parsed || typeof parsed !== 'object') throw new Error('needs an object')
    draft.value = parsed
    showJson.value = false
    jsonError.value = ''
  } catch (error) {
    jsonError.value = String(error.message || error)
  }
}
</script>

<template>
  <section class="stack">
    <div class="row">
      <h2>Scenario</h2>
      <span v-if="locked" class="tag tag--warn">
        this run has started — saving creates a copy
      </span>
    </div>

    <label class="field">
      <span class="dim small">The event the population is reacting to</span>
      <textarea v-model="draft.event" rows="3" :disabled="busy"></textarea>
    </label>

    <div class="pair">
      <label class="field">
        <span class="dim small">Platform</span>
        <select
          :value="draft.platform"
          :disabled="busy"
          @change="switchPlatform($event.target.value)"
        >
          <option v-for="name in PLATFORMS" :key="name" :value="name">{{ name }}</option>
        </select>
      </label>
      <label class="field">
        <span class="dim small">Rounds (max {{ MAX_ROUNDS }})</span>
        <input
          v-model.number="draft.rounds"
          type="number"
          min="1"
          :max="MAX_ROUNDS"
          :disabled="busy"
        />
      </label>
      <label class="field">
        <span class="dim small">Hours per round</span>
        <input
          v-model.number="draft.hours_per_round"
          type="number"
          min="0.5"
          step="0.5"
          :disabled="busy"
        />
      </label>
    </div>

    <p v-if="platformNotice" class="notice small" role="status">{{ platformNotice }}</p>

    <!-- Action space -->
    <fieldset class="card">
      <legend class="dim small">Permitted actions on {{ draft.platform }}</legend>
      <label v-for="action in available" :key="action" class="check small">
        <input
          type="checkbox"
          :checked="draft.action_space.actions.includes(action)"
          :disabled="busy"
          @change="toggleAction(action)"
        />
        {{ action }}
      </label>
    </fieldset>

    <!-- Broadcaster -->
    <div class="pair">
      <label class="field">
        <span class="dim small">Broadcaster name</span>
        <input v-model="draft.broadcaster.name" type="text" :disabled="busy" />
      </label>
      <label class="field">
        <span class="dim small">Handle</span>
        <input v-model="draft.broadcaster.handle" type="text" :disabled="busy" />
      </label>
    </div>

    <!-- Seed posts -->
    <div class="row">
      <h3>Seed posts ({{ draft.seed_posts.length }})</h3>
      <button
        class="btn"
        type="button"
        :disabled="busy"
        @click="draft.seed_posts.push(blankSeedPost())"
      >
        Add seed post
      </button>
    </div>

    <article
      v-for="(post, index) in draft.seed_posts"
      :key="`s-${index}`"
      class="card stack item"
      :class="{ 'is-demoted': wasDemoted(post) }"
    >
      <div class="row">
        <label class="field">
          <span class="dim small">Attributed to</span>
          <select v-model="post.attribution" :disabled="busy">
            <option v-for="option in ATTRIBUTIONS" :key="option" :value="option">
              {{ option }}
            </option>
          </select>
        </label>
        <label v-if="post.attribution === 'named'" class="field grow">
          <span class="dim small">Speaker</span>
          <input v-model="post.speaker" type="text" :disabled="busy" />
        </label>
        <span class="spacer"></span>
        <button
          class="btn"
          type="button"
          :disabled="busy"
          @click="draft.seed_posts.splice(index, 1)"
        >
          Remove
        </button>
      </div>
      <textarea v-model="post.content" rows="2" :disabled="busy"></textarea>
      <p v-if="wasDemoted(post)" class="demoted small">
        Demoted to the broadcaster: {{ post.demoted_reason }}
      </p>
      <p v-else-if="post.attribution === 'named'" class="dim small">
        This quote is checked against the source document. If it is not there, it
        is demoted to the broadcaster rather than believed.
      </p>
    </article>

    <!-- Scheduled events -->
    <div class="row">
      <h3>Scheduled events ({{ draft.scheduled_events.length }})</h3>
      <button
        class="btn"
        type="button"
        :disabled="busy"
        @click="draft.scheduled_events.push(blankScheduledEvent())"
      >
        Add event
      </button>
    </div>

    <article
      v-for="(event, index) in draft.scheduled_events"
      :key="`e-${index}`"
      class="card stack item"
    >
      <div class="row">
        <label class="field">
          <span class="dim small">Fires in round</span>
          <input v-model.number="event.round" type="number" min="0" :disabled="busy" />
        </label>
        <label class="check small">
          <input v-model="event.enabled" type="checkbox" :disabled="busy" />
          Enabled
        </label>
        <label class="check small">
          <input v-model="event.counterfactual" type="checkbox" :disabled="busy" />
          Counterfactual
        </label>
        <span class="spacer"></span>
        <button
          class="btn"
          type="button"
          :disabled="busy"
          @click="draft.scheduled_events.splice(index, 1)"
        >
          Remove
        </button>
      </div>
      <input
        v-model="event.description"
        type="text"
        placeholder="What happens"
        :disabled="busy"
      />
      <textarea v-model="event.content" rows="2" :disabled="busy"></textarea>
    </article>

    <label class="field">
      <span class="dim small">Notes</span>
      <textarea v-model="draft.notes" rows="2" :disabled="busy"></textarea>
    </label>

    <div v-if="problems.length" class="problems" role="alert">
      <strong class="small">Fix these before saving:</strong>
      <ul class="small">
        <li v-for="problem in problems" :key="problem">{{ problem }}</li>
      </ul>
    </div>

    <div class="row">
      <button class="btn" type="button" @click="showJson ? (showJson = false) : openJson()">
        {{ showJson ? 'Close JSON' : 'Edit as JSON' }}
      </button>
      <button
        class="btn btn--primary"
        type="button"
        :disabled="busy || problems.length > 0"
        @click="emit('save', draft)"
      >
        {{ busy ? 'Saving…' : locked ? 'Save as a new simulation' : 'Save scenario' }}
      </button>
    </div>

    <div v-if="showJson" class="stack">
      <textarea v-model="jsonText" class="mono json" rows="18" spellcheck="false"></textarea>
      <p v-if="jsonError" class="demoted small">{{ jsonError }}</p>
      <p><button class="btn" type="button" @click="applyJson">Apply JSON</button></p>
    </div>
  </section>
</template>

<style scoped>
.field { display: flex; flex-direction: column; gap: 0.15rem; }
.grow { flex: 1; min-width: 10rem; }
.spacer { flex: 1; }

.field input,
.field select,
textarea,
.json {
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  width: 100%;
}

.json { font-family: var(--mono); font-size: 0.85rem; }

.pair {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
  gap: var(--gap);
}

fieldset {
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.check { display: inline-block; margin-right: 0.9rem; white-space: nowrap; }

.item { border-left: 3px solid var(--border); }
.is-demoted { border-left-color: var(--warn); }
.demoted { margin: 0; color: var(--warn); }

.notice {
  margin: 0;
  padding: 0.4rem 0.7rem;
  border-radius: var(--radius);
  background: var(--surface-2);
}

.problems {
  border: 1px solid var(--border);
  border-left: 3px solid var(--bad);
  border-radius: var(--radius);
  background: var(--surface);
  padding: 0.6rem 0.9rem;
}

.problems ul { margin: 0.25rem 0 0; padding-left: 1.1rem; color: var(--text-dim); }
</style>
