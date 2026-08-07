<script setup>
/* Drag-and-drop upload with client-side validation.

   The validation is a courtesy — the server refuses the same things — but
   refusing a 40 MB PDF before it goes over the wire, and saying which rule it
   broke, beats a round trip that ends in a 400. */
import { computed, ref } from 'vue'
import {
  ACCEPT_ATTRIBUTE,
  ALLOWED_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  formatBytes,
  validateDrop,
  validateFile,
} from '../api/limits.js'

const props = defineProps({
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['selected'])

const input = ref(null)
const dragging = ref(false)
const rejected = ref('')
const chosen = ref(null)

const hint = computed(
  () =>
    `${ALLOWED_EXTENSIONS.map((e) => `.${e}`).join(', ')} · up to ${formatBytes(MAX_UPLOAD_BYTES)}`,
)

function accept(result, file) {
  if (!result.ok) {
    rejected.value = result.reason
    chosen.value = null
    return
  }
  rejected.value = ''
  chosen.value = file
  emit('selected', file)
}

function onDrop(event) {
  dragging.value = false
  const files = event.dataTransfer?.files
  const result = validateDrop(files)
  accept(result, result.ok ? files[0] : null)
}

function onPick(event) {
  const file = event.target.files?.[0]
  accept(validateFile(file), file)
  // Let the same file be chosen again after a rejection.
  event.target.value = ''
}

function clear() {
  chosen.value = null
  rejected.value = ''
  emit('selected', null)
}
</script>

<template>
  <div class="stack">
    <div
      class="drop"
      :class="{ 'is-dragging': dragging, 'is-busy': busy }"
      role="button"
      tabindex="0"
      :aria-label="`Choose a document. ${hint}`"
      @click="!busy && input.click()"
      @keydown.enter.prevent="!busy && input.click()"
      @keydown.space.prevent="!busy && input.click()"
      @dragover.prevent="dragging = true"
      @dragenter.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <input
        ref="input"
        class="visually-hidden"
        type="file"
        :accept="ACCEPT_ATTRIBUTE"
        :disabled="busy"
        @change="onPick"
      />
      <template v-if="chosen">
        <strong>{{ chosen.name }}</strong>
        <span class="dim small">{{ formatBytes(chosen.size) }}</span>
      </template>
      <template v-else>
        <strong>Drop a document here</strong>
        <span class="dim small">or click to choose · {{ hint }}</span>
        <span class="dim small">One document per graph.</span>
      </template>
    </div>

    <p v-if="rejected" class="rejected small" role="alert">{{ rejected }}</p>
    <p v-if="chosen && !busy">
      <button class="btn" type="button" @click.stop="clear">Choose a different file</button>
    </p>
  </div>
</template>

<style scoped>
.drop {
  border: 2px dashed var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  padding: 2.5rem 1rem;
  text-align: center;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  transition: border-color 0.12s, background 0.12s;
}

.drop:hover,
.drop:focus-visible {
  border-color: var(--accent);
}

.is-dragging {
  border-color: var(--accent);
  background: var(--surface-2);
}

.is-busy {
  opacity: 0.6;
  cursor: progress;
}

.rejected {
  margin: 0;
  color: var(--bad);
}

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
</style>
