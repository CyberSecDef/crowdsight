<script setup>
/* Review and edit the proposed ontology before extraction runs.

   This screen exists because extraction is the expensive stage and a wrong
   ontology wastes all of it. Two backend behaviours are surfaced here rather
   than left to be discovered:

   * a typed name is normalised — "Public Figure" is stored as PublicFigure —
     so the identifier is shown as you type;
   * a relationship whose endpoint types are absent is dropped on save, and
     dropped silently, so removing an entity type warns about what goes with it. */
import { computed, ref, watch } from 'vue'
import {
  blankEntityType,
  blankRelationshipType,
  entityIdentifier,
  relationshipIdentifier,
  relationshipsLostByRemoving,
  validateOntology,
} from '../api/ontology.js'

const props = defineProps({
  /* The proposal to edit. Deliberately one-way: the draft is this component's
     own copy and only leaves on `approve`.

     It was a v-model first, and that was a bug. The child deep-watched its
     draft and emitted, the parent wrote the value back, the parent's watcher
     re-cloned it into the draft, which emitted again — an infinite update
     loop, so the whole subtree silently failed to render. Nothing upstream
     needs to see every keystroke, so nothing upstream is told about them. */
  ontology: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['approve'])

/* A JSON round-trip, not structuredClone.

   structuredClone throws DataCloneError on a Vue reactive Proxy, and a prop is
   exactly that. It fails during setup, so the component never renders at all —
   and because the failure is in setup rather than in a handler, the parent's
   subtree blanks with it. The page kept its heading and lost everything else,
   which reads as a routing problem rather than a crash. The ontology is pure
   JSON, so a round-trip is both sufficient and honest about the data. */
const copy = (value) => JSON.parse(JSON.stringify(value ?? null))

const draft = ref(copy(props.ontology))
const showJson = ref(false)
const jsonText = ref('')
const jsonError = ref('')

watch(
  () => props.ontology,
  (value) => {
    // A genuinely new proposal replaces the draft; edits to it do not.
    draft.value = copy(value)
  },
)

const problems = computed(() => validateOntology(draft.value))
const entityNames = computed(() => draft.value.entity_types.map((t) => t.name).filter(Boolean))

function renameEntity(entity, typed) {
  const previous = entity.name
  entity.label = typed
  entity.name = entityIdentifier(typed)
  // Keep relationships pointing at the renamed type rather than orphaning them.
  if (previous && entity.name && previous !== entity.name) {
    for (const relationship of draft.value.relationship_types) {
      relationship.source_types = relationship.source_types.map((t) =>
        t === previous ? entity.name : t,
      )
      relationship.target_types = relationship.target_types.map((t) =>
        t === previous ? entity.name : t,
      )
    }
  }
}

function renameRelationship(relationship, typed) {
  relationship.label = typed
  relationship.name = relationshipIdentifier(typed)
}

function removeEntity(index) {
  const entity = draft.value.entity_types[index]
  const lost = relationshipsLostByRemoving(draft.value, entity.name)
  if (lost.length) {
    const names = lost.map((l) => l.relationship.name).join(', ')
    const ok = window.confirm(
      `Removing ${entity.name} also removes ${lost.length} relationship(s) ` +
        `that point at it: ${names}.\n\nThe backend drops these silently on ` +
        'save, so they are removed here too. Continue?',
    )
    if (!ok) return
    const doomed = new Set(lost.map((l) => l.relationship))
    draft.value.relationship_types = draft.value.relationship_types.filter(
      (r) => !doomed.has(r),
    )
  }
  draft.value.entity_types.splice(index, 1)
}

function addEntity() {
  draft.value.entity_types.push(blankEntityType())
}

function addRelationship() {
  draft.value.relationship_types.push(blankRelationshipType())
}

function removeRelationship(index) {
  draft.value.relationship_types.splice(index, 1)
}

function toggleEndpoint(list, name) {
  const at = list.indexOf(name)
  if (at === -1) list.push(name)
  else list.splice(at, 1)
}

function attributesOf(entity) {
  return (entity.attributes || []).join(', ')
}

function setAttributes(entity, text) {
  entity.attributes = text
    .split(',')
    .map((a) => a.trim())
    .filter(Boolean)
}

function openJson() {
  jsonText.value = JSON.stringify(draft.value, null, 2)
  jsonError.value = ''
  showJson.value = true
}

function applyJson() {
  try {
    const parsed = JSON.parse(jsonText.value)
    if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.entity_types)) {
      throw new Error('needs an entity_types array')
    }
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
      <h2>Review the ontology</h2>
      <span class="dim small">
        Extraction is the expensive stage — a wrong schema wastes all of it.
      </span>
    </div>

    <label class="field">
      <span class="dim small">Domain</span>
      <input v-model="draft.domain" type="text" :disabled="busy" />
    </label>

    <!-- Entity types -->
    <div class="row">
      <h3>Entity types ({{ draft.entity_types.length }})</h3>
      <button class="btn" type="button" :disabled="busy" @click="addEntity">Add type</button>
    </div>

    <article
      v-for="(entity, index) in draft.entity_types"
      :key="`e-${index}`"
      class="card stack type"
    >
      <div class="row">
        <label class="field grow">
          <span class="dim small">Name</span>
          <input
            :value="entity.label || entity.name"
            type="text"
            :disabled="busy"
            @input="renameEntity(entity, $event.target.value)"
          />
        </label>
        <span class="identifier mono" :class="{ 'is-bad': !entity.name }">
          {{ entity.name || 'not a usable name' }}
        </span>
        <button class="btn" type="button" :disabled="busy" @click="removeEntity(index)">
          Remove
        </button>
      </div>
      <label class="field">
        <span class="dim small">Description — the extractor relies on this</span>
        <input v-model="entity.description" type="text" :disabled="busy" />
      </label>
      <label class="field">
        <span class="dim small">Attributes, comma separated</span>
        <input
          :value="attributesOf(entity)"
          type="text"
          :disabled="busy"
          @input="setAttributes(entity, $event.target.value)"
        />
      </label>
    </article>

    <!-- Relationship types -->
    <div class="row">
      <h3>Relationship types ({{ draft.relationship_types.length }})</h3>
      <button class="btn" type="button" :disabled="busy" @click="addRelationship">
        Add relationship
      </button>
    </div>

    <article
      v-for="(relationship, index) in draft.relationship_types"
      :key="`r-${index}`"
      class="card stack type"
    >
      <div class="row">
        <label class="field grow">
          <span class="dim small">Name</span>
          <input
            :value="relationship.label || relationship.name"
            type="text"
            :disabled="busy"
            @input="renameRelationship(relationship, $event.target.value)"
          />
        </label>
        <span class="identifier mono" :class="{ 'is-bad': !relationship.name }">
          {{ relationship.name || 'not a usable name' }}
        </span>
        <button class="btn" type="button" :disabled="busy" @click="removeRelationship(index)">
          Remove
        </button>
      </div>
      <label class="field">
        <span class="dim small">Description</span>
        <input v-model="relationship.description" type="text" :disabled="busy" />
      </label>
      <div class="endpoints">
        <fieldset>
          <legend class="dim small">From</legend>
          <label v-for="name in entityNames" :key="`s-${name}`" class="check small">
            <input
              type="checkbox"
              :checked="relationship.source_types.includes(name)"
              :disabled="busy"
              @change="toggleEndpoint(relationship.source_types, name)"
            />
            {{ name }}
          </label>
        </fieldset>
        <fieldset>
          <legend class="dim small">To</legend>
          <label v-for="name in entityNames" :key="`t-${name}`" class="check small">
            <input
              type="checkbox"
              :checked="relationship.target_types.includes(name)"
              :disabled="busy"
              @change="toggleEndpoint(relationship.target_types, name)"
            />
            {{ name }}
          </label>
        </fieldset>
      </div>
    </article>

    <!-- Problems -->
    <div v-if="problems.length" class="problems" role="alert">
      <strong class="small">These will change what gets saved:</strong>
      <ul class="small">
        <li v-for="problem in problems" :key="problem">{{ problem }}</li>
      </ul>
    </div>

    <!-- JSON escape hatch -->
    <div class="row">
      <button class="btn" type="button" @click="showJson ? (showJson = false) : openJson()">
        {{ showJson ? 'Close JSON' : 'Edit as JSON' }}
      </button>
      <button
        class="btn btn--primary"
        type="button"
        :disabled="busy || !draft.entity_types.length"
        @click="emit('approve', draft)"
      >
        {{ busy ? 'Starting extraction…' : 'Approve and extract' }}
      </button>
    </div>

    <div v-if="showJson" class="stack">
      <textarea v-model="jsonText" class="mono json" rows="18" spellcheck="false"></textarea>
      <p v-if="jsonError" class="rejected small">{{ jsonError }}</p>
      <p><button class="btn" type="button" @click="applyJson">Apply JSON</button></p>
    </div>
  </section>
</template>

<style scoped>
.field {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.field input,
.json {
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  width: 100%;
}

.json {
  font-family: var(--mono);
  font-size: 0.85rem;
}

.grow { flex: 1; min-width: 12rem; }

.identifier {
  font-size: 0.8rem;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text-dim);
  white-space: nowrap;
}

.is-bad { color: var(--bad); border-color: var(--bad); }

.type { border-left: 3px solid var(--border); }

.endpoints {
  display: flex;
  gap: var(--gap);
  flex-wrap: wrap;
}

fieldset {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.4rem 0.6rem;
  margin: 0;
  min-width: 10rem;
}

.check {
  display: block;
  white-space: nowrap;
}

.problems {
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--surface);
  padding: 0.6rem 0.9rem;
}

.problems ul { margin: 0.25rem 0 0; padding-left: 1.1rem; color: var(--text-dim); }

.rejected { margin: 0; color: var(--bad); }
</style>
