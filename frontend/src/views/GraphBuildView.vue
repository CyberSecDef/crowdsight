<script setup>
/* Stage 1 — document in, graph out.

   Four phases in one view, because they are one task: choose a document,
   review the ontology it proposes, watch extraction, then explore the result.
   Which phase is showing is derived from what exists, not from a wizard step,
   so opening /graphs/<id> for a finished graph lands straight on the graph. */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { graph as graphApi, isParked, TaskStatus } from '../api/index.js'
import { useWorkflowStore } from '../stores/workflow.js'
import DropZone from '../components/DropZone.vue'
import EntityInspector from '../components/EntityInspector.vue'
import ErrorBanner from '../components/ErrorBanner.vue'
import GraphCanvas from '../components/GraphCanvas.vue'
import OntologyEditor from '../components/OntologyEditor.vue'
import TaskProgressBar from '../components/TaskProgressBar.vue'
import TypeFilter from '../components/TypeFilter.vue'

const props = defineProps({ graphId: { type: String, default: '' } })

const router = useRouter()
const workflow = useWorkflowStore()

const file = ref(null)
const reviewOntology = ref(true)
const uploading = ref(false)
const task = ref(null)
const ontology = ref(null)
const approving = ref(false)
const error = ref(null)

const detail = ref(null)
const nodes = ref([])
const edges = ref([])
const truncated = ref(false)
const entityTypes = ref([])
const hiddenTypes = ref([])
const selectedUuid = ref('')
const selectedEntity = ref(null)
const loadingGraph = ref(false)

/** Which of the four phases to show, derived rather than stepped. */
const phase = computed(() => {
  if (detail.value) return 'graph'
  if (ontology.value) return 'review'
  if (task.value && !isParked(task.value.status)) return 'working'
  return 'upload'
})

let abort = null

function watchOptions() {
  abort?.abort(new Error('superseded'))
  abort = new AbortController()
  return { signal: abort.signal, onUpdate: (t) => (task.value = t) }
}

async function upload() {
  if (!file.value) return
  uploading.value = true
  error.value = null
  ontology.value = null
  try {
    const started = await graphApi.upload(file.value, {
      reviewOntology: reviewOntology.value,
    })
    workflow.selectGraph(started.graph_id)
    // Keep the URL honest: the graph exists from here on, even if it is empty.
    if (started.graph_id !== props.graphId) {
      router.replace({ name: 'graph', params: { graphId: started.graph_id } })
    }
    await follow(started.task_id)
  } catch (err) {
    error.value = err
  } finally {
    uploading.value = false
  }
}

async function follow(taskId) {
  const finished = await graphApi.watch(taskId, watchOptions())
  task.value = finished

  if (finished.status === TaskStatus.FAILED) return
  if (isParked(finished.status)) {
    // Parked for ontology review. The proposal is in the task result, but read
    // it back from the API so what is edited is what was stored.
    ontology.value = await graphApi.ontology(workflow.graphId || props.graphId)
    return
  }
  await loadGraph()
}

async function approve(edited) {
  approving.value = true
  error.value = null
  try {
    const started = await graphApi.saveOntology(props.graphId || workflow.graphId, edited)
    ontology.value = null
    await follow(started.task_id)
  } catch (err) {
    error.value = err
  } finally {
    approving.value = false
  }
}

async function loadGraph() {
  const id = props.graphId || workflow.graphId
  if (!id) return
  loadingGraph.value = true
  error.value = null
  try {
    const [info, view, types] = await Promise.all([
      graphApi.detail(id),
      graphApi.subgraph(id, { limit: 500 }),
      graphApi.entityTypes(id),
    ])
    detail.value = info
    nodes.value = view.nodes || []
    edges.value = view.edges || []
    truncated.value = Boolean(view.truncated)
    entityTypes.value = types || []
    workflow.selectGraph(id)
  } catch (err) {
    // A graph that has not finished extracting is a 404, not a failure.
    if (!err.notFound) error.value = err
    detail.value = null
    // It may instead be parked waiting for its ontology to be reviewed. That
    // is a normal state to come back to — the whole point of parking is that
    // a person leaves and returns — so resume there rather than showing an
    // upload form that would start the document again.
    await resumeReviewIfParked(id)
  } finally {
    loadingGraph.value = false
  }
}

async function resumeReviewIfParked(id) {
  try {
    ontology.value = await graphApi.ontology(id)
  } catch {
    ontology.value = null
  }
}

async function select(uuid) {
  selectedUuid.value = uuid
  if (!uuid) {
    selectedEntity.value = null
    return
  }
  const known = nodes.value.find((n) => n.uuid === uuid)
  selectedEntity.value = known || null
  try {
    selectedEntity.value = await graphApi.entity(props.graphId || workflow.graphId, uuid)
  } catch {
    // The node data already on screen is enough; the detail call is a bonus.
  }
}

function toggleType(type) {
  const at = hiddenTypes.value.indexOf(type)
  if (at === -1) hiddenTypes.value.push(type)
  else hiddenTypes.value.splice(at, 1)
}

function startOver() {
  abort?.abort(new Error('starting over'))
  file.value = null
  task.value = null
  ontology.value = null
  detail.value = null
  router.push({ name: 'graph-new' })
}

onMounted(() => {
  if (props.graphId) loadGraph()
})

watch(() => props.graphId, (id) => {
  detail.value = null
  if (id) loadGraph()
})
</script>

<template>
  <div class="stack">
    <div class="row">
      <h1>{{ phase === 'graph' ? 'Graph' : 'Build a graph' }}</h1>
      <span v-if="graphId" class="mono dim small">{{ graphId }}</span>
      <button v-if="phase === 'graph'" class="btn" type="button" @click="startOver">
        New graph
      </button>
    </div>

    <ErrorBanner :error="error" :retry="phase === 'graph' ? loadGraph : null" />

    <!-- 1. Upload -->
    <section v-if="phase === 'upload'" class="stack">
      <DropZone :busy="uploading" @selected="file = $event" />
      <label class="check">
        <input v-model="reviewOntology" type="checkbox" :disabled="uploading" />
        Review the ontology before extracting
        <span class="dim small">
          — extraction is the expensive stage, and a wrong schema wastes all of it
        </span>
      </label>
      <p>
        <button
          class="btn btn--primary"
          type="button"
          :disabled="!file || uploading"
          @click="upload"
        >
          {{ uploading ? 'Uploading…' : 'Build graph' }}
        </button>
      </p>
    </section>

    <!-- 2. Extraction in progress -->
    <TaskProgressBar
      v-if="task && phase !== 'graph'"
      :task="task"
      :label="ontology ? 'Proposing an ontology' : 'Building the graph'"
    />

    <!-- 3. Ontology review -->
    <OntologyEditor
      v-if="phase === 'review'"
      :ontology="ontology"
      :busy="approving"
      @approve="approve"
    />

    <!-- 4. The graph -->
    <section v-if="phase === 'graph'" class="stack">
      <div class="card">
        <p class="dim small">{{ detail.domain || 'no domain recorded' }}</p>
        <p class="dim small">
          {{ detail.entity_count ?? 0 }} entities ·
          {{ detail.chunk_count ?? 0 }} chunks ·
          {{ detail.page_count ?? 0 }} page(s) ·
          <span class="mono">{{ detail.filename }}</span>
        </p>
      </div>

      <p v-if="truncated" class="truncated small" role="alert">
        This view is capped at 500 nodes and the graph is larger, so what you
        see is part of it.
      </p>

      <TypeFilter
        :types="entityTypes"
        :hidden="hiddenTypes"
        @toggle="toggleType"
        @all="hiddenTypes = []"
      />

      <div class="split">
        <GraphCanvas
          :nodes="nodes"
          :edges="edges"
          :hidden-types="hiddenTypes"
          :selected="selectedUuid"
          @select="select"
        />
        <EntityInspector :entity="selectedEntity" @close="select('')" />
      </div>
    </section>

    <p v-if="loadingGraph" class="dim">Loading the graph…</p>
  </div>
</template>

<style scoped>
.split {
  display: grid;
  gap: var(--gap);
  grid-template-columns: 1fr;
}

@media (min-width: 900px) {
  .split:has(aside) {
    grid-template-columns: 2fr 1fr;
  }
}

.check { display: block; }

.truncated {
  margin: 0;
  padding: 0.5rem 0.8rem;
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--surface);
}
</style>
