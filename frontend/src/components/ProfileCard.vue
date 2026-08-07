<script setup>
/* One agent: a summary row that expands into an editor.

   Provenance is shown on the collapsed row rather than buried in the detail,
   because "is this a real person from the document or someone we invented" is
   the single most important thing about an agent and the reason this screen
   exists. Fields that cannot be changed are rendered as text with the reason
   beside them, rather than as inputs that reject on save. */
import { computed } from 'vue'
import {
  ACTIVITY_LEVELS,
  EDITABLE_TEXT_FIELDS,
  canRename,
  isNamed,
  lockReason,
} from '../api/profiles.js'

const props = defineProps({
  profile: { type: Object, required: true },
  open: { type: Boolean, default: false },
  removed: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['toggle', 'update', 'remove', 'restore'])

const named = computed(() => isNamed(props.profile))

function set(field, value) {
  emit('update', { ...props.profile, [field]: value })
}

function setList(field, text) {
  set(
    field,
    text
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean),
  )
}
</script>

<template>
  <article class="card agent" :class="{ 'is-removed': removed, 'is-named': named }">
    <div class="row head">
      <button class="disclose" type="button" :aria-expanded="open" @click="emit('toggle')">
        {{ open ? '▾' : '▸' }}
      </button>

      <strong>{{ profile.name }}</strong>
      <span class="mono dim small">@{{ profile.username }}</span>

      <span class="tag" :class="named ? 'tag--warn' : ''">
        {{ named ? 'named' : 'synthetic' }}
      </span>
      <span class="dim small">{{ profile.occupation || 'no occupation' }}</span>
      <span class="tag">{{ profile.activity_level }}</span>

      <span class="spacer"></span>

      <button v-if="!removed" class="btn" type="button" :disabled="busy" @click="emit('remove')">
        Remove
      </button>
      <button v-else class="btn" type="button" :disabled="busy" @click="emit('restore')">
        Keep after all
      </button>
    </div>

    <p v-if="removed" class="dim small removed-note">
      Will be removed when you save.
    </p>

    <div v-else-if="open" class="detail stack">
      <!-- Locked fields, shown as facts rather than offered as inputs. -->
      <p class="locked small">
        <span class="dim">Provenance</span>
        <strong>{{ profile.provenance }}</strong>
        <span class="dim">— {{ lockReason(profile, 'provenance') }}</span>
      </p>

      <label class="field">
        <span class="dim small">
          Name
          <template v-if="!canRename(profile)">
            — {{ lockReason(profile, 'name') }}
          </template>
        </span>
        <input
          v-if="canRename(profile)"
          :value="profile.name"
          type="text"
          :disabled="busy"
          @input="set('name', $event.target.value)"
        />
        <strong v-else>{{ profile.name }}</strong>
      </label>

      <div class="pair">
        <label class="field">
          <span class="dim small">Age</span>
          <input
            :value="profile.age"
            type="number"
            min="1"
            :disabled="busy"
            @input="set('age', Number($event.target.value))"
          />
        </label>
        <label class="field">
          <span class="dim small">Activity level</span>
          <select
            :value="profile.activity_level"
            :disabled="busy"
            @change="set('activity_level', $event.target.value)"
          >
            <option v-for="level in ACTIVITY_LEVELS" :key="level" :value="level">
              {{ level }}
            </option>
          </select>
        </label>
      </div>

      <label v-for="field in EDITABLE_TEXT_FIELDS" :key="field.key" class="field">
        <span class="dim small">{{ field.label }}</span>
        <input
          :value="profile[field.key] || ''"
          type="text"
          :disabled="busy"
          @input="set(field.key, $event.target.value)"
        />
      </label>

      <label class="field">
        <span class="dim small">Background</span>
        <textarea
          :value="profile.background || ''"
          rows="3"
          :disabled="busy"
          @input="set('background', $event.target.value)"
        ></textarea>
      </label>

      <label class="field">
        <span class="dim small">Interests, comma separated</span>
        <input
          :value="(profile.interests || []).join(', ')"
          type="text"
          :disabled="busy"
          @input="setList('interests', $event.target.value)"
        />
      </label>

      <label class="field">
        <span class="dim small">Traits, comma separated</span>
        <input
          :value="(profile.traits || []).join(', ')"
          type="text"
          :disabled="busy"
          @input="setList('traits', $event.target.value)"
        />
      </label>

      <p v-if="named && profile.source_entity_uuid" class="dim small">
        From graph entity
        <code>{{ profile.source_entity_type }}</code>
        <code>{{ profile.source_entity_uuid }}</code>
      </p>
    </div>
  </article>
</template>

<style scoped>
.agent { padding: 0.6rem 0.8rem; }
.is-named { border-left: 3px solid var(--warn); }
.is-removed { opacity: 0.55; }

.head { gap: 0.5rem; }
.spacer { flex: 1; }

.disclose {
  font: inherit;
  border: none;
  background: none;
  color: var(--text-dim);
  cursor: pointer;
  padding: 0 0.2rem;
}

.detail { margin-top: 0.6rem; }

.locked {
  margin: 0;
  padding: 0.4rem 0.6rem;
  border-radius: var(--radius);
  background: var(--surface-2);
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}

.removed-note { margin: 0.3rem 0 0; }

.field { display: flex; flex-direction: column; gap: 0.15rem; }

.field input,
.field select,
.field textarea {
  font: inherit;
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  width: 100%;
}

.pair {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: var(--gap);
}
</style>
