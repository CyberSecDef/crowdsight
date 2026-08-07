<script setup>
/* Stage 4 — the report.

   Two things are load-bearing here and neither is the prose.

   The verification record is rendered always, including on a clean report. A
   document that quietly dropped three fabricated claims looks identical to one
   that never made any, and omitting the section when clean leaves "verified
   and sound" indistinguishable from "never verified".

   And every claim's citation is followable — the posts endpoint grew a
   post_ids filter for exactly this. A citation nobody can check is a citation
   nobody should trust. */
import { computed, onMounted, ref, watch } from 'vue'
import { report as reportApi, simulation as simulationApi } from '../api/index.js'
import { actionDistribution, buildInfluenceGraph } from '../api/influence.js'
import { isParked, TaskStatus } from '../api/polling.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ActionChart from '../components/ActionChart.vue'
import CitationLink from '../components/CitationLink.vue'
import ErrorBanner from '../components/ErrorBanner.vue'
import GraphCanvas from '../components/GraphCanvas.vue'
import SentimentChart from '../components/SentimentChart.vue'
import TaskProgressBar from '../components/TaskProgressBar.vue'

const props = defineProps({
  simId: { type: String, required: true },
  reportId: { type: String, default: '' },
})

const workflow = useWorkflowStore()

const reports = ref([])
const report = ref(null)
const task = ref(null)
const generating = ref(false)
const loading = ref(true)
const error = ref(null)
const influence = ref({ nodes: [], edges: [], unresolved: 0, isolatedClaims: [] })
const selectedAgent = ref('')

const grounding = computed(() => report.value?.grounding || {})
const distribution = computed(() => actionDistribution(report.value?.evidence?.timeline || []))

async function load() {
  loading.value = true
  error.value = null
  try {
    workflow.selectSimulation(props.simId)
    const listing = await reportApi.list({ sim_id: props.simId })
    reports.value = listing?.reports || []
    const wanted = props.reportId || reports.value[0]?.report_id
    report.value = wanted ? await reportApi.detail(wanted) : null
    if (report.value) await buildGraph()
  } catch (err) {
    error.value = err
  } finally {
    loading.value = false
  }
}

/** The influence graph comes from what agents did, not from what the report says. */
async function buildGraph() {
  try {
    const [posts, actions] = await Promise.all([
      simulationApi.posts(props.simId, { limit: 500, order: 'oldest' }),
      simulationApi.actions(props.simId, { limit: 1000, order: 'oldest' }),
    ])
    influence.value = buildInfluenceGraph({
      posts: posts.posts || [],
      actions: actions.actions || [],
      influential: report.value?.influential_agents || [],
    })
  } catch {
    influence.value = { nodes: [], edges: [], unresolved: 0, isolatedClaims: [] }
  }
}

async function generate() {
  generating.value = true
  error.value = null
  task.value = null
  try {
    const started = await reportApi.generate({ sim_id: props.simId })
    const finished = await reportApi.watch(started.task_id, {
      onUpdate: (value) => (task.value = value),
    })
    task.value = finished
    if (finished.status === TaskStatus.SUCCEEDED) await load()
  } catch (err) {
    error.value = err
  } finally {
    generating.value = false
  }
}

async function open(reportId) {
  report.value = await reportApi.detail(reportId)
  await buildGraph()
}

onMounted(load)
watch(() => [props.simId, props.reportId], load)
</script>

<template>
  <div class="stack">
    <div class="row">
      <h1>Report</h1>
      <span class="mono dim small">{{ simId }}</span>
      <span class="spacer"></span>
      <button class="btn btn--primary" type="button" :disabled="generating" @click="generate">
        {{ generating ? 'Generating…' : reports.length ? 'Generate another' : 'Generate a report' }}
      </button>
    </div>

    <ErrorBanner :error="error" :retry="load" />
    <TaskProgressBar v-if="task && !isParked(task.status)" :task="task" label="Writing the report" />
    <p v-if="generating" class="dim small">
      A report is written by the local model and takes minutes. This page can be left open.
    </p>

    <p v-if="loading" class="dim">Loading…</p>

    <p v-else-if="!report" class="dim">
      No report for this run yet.
    </p>

    <template v-else>
      <!-- Which report -->
      <div v-if="reports.length > 1" class="row">
        <span class="dim small">{{ reports.length }} reports on this run</span>
        <button
          v-for="entry in reports"
          :key="entry.report_id"
          class="btn"
          type="button"
          :disabled="entry.report_id === report.report_id"
          @click="open(entry.report_id)"
        >
          {{ entry.generated_at?.slice(0, 16) || entry.report_id }}
        </button>
      </div>

      <!-- Export -->
      <div class="row">
        <a class="btn" :href="reportApi.exportUrl(report.report_id, 'markdown', true)">
          Export Markdown
        </a>
        <a class="btn" :href="reportApi.exportUrl(report.report_id, 'html', true)">
          Export HTML
        </a>
        <a class="btn" :href="reportApi.exportUrl(report.report_id, 'html')" target="_blank">
          Open HTML
        </a>
        <span class="dim small">
          {{ report.tool_calls_used }} tool call(s) ·
          {{ report.reflection_rounds_used }} reflection round(s)
        </span>
      </div>

      <!-- Verification: always rendered, including when clean -->
      <section class="card stack verification">
        <div class="row">
          <h2>Verification</h2>
          <span class="tag" :class="grounding.dropped?.length ? 'tag--bad' : 'tag--ok'">
            {{ grounding.resolved ?? 0 }}/{{ grounding.checked ?? 0 }} citation(s) resolved
          </span>
        </div>
        <p v-if="grounding.empty_run" class="small">
          The run holds no data, so nothing could be verified.
        </p>
        <p v-else-if="!grounding.dropped?.length && !grounding.uncited_claims?.length"
           class="small">
          Every claim cited the run and every citation resolved.
        </p>
        <div v-if="grounding.dropped?.length">
          <strong class="small">
            {{ grounding.dropped.length }} claim(s) dropped as unsupported:
          </strong>
          <ul class="small">
            <li v-for="(dropped, index) in grounding.dropped" :key="index">
              {{ dropped.claim || dropped.text }} — {{ dropped.reason }}
            </li>
          </ul>
        </div>
        <p v-if="grounding.uncited_claims?.length" class="small dim">
          {{ grounding.uncited_claims.length }} claim(s) cited nothing. They were kept:
          showing no working is not the same as being wrong.
        </p>
        <p v-if="grounding.prose_unresolved?.length" class="small dim">
          {{ grounding.prose_unresolved.length }} reference(s) in the prose could not be
          resolved.
        </p>
      </section>

      <!-- Summary -->
      <section class="card stack">
        <h2>Executive summary</h2>
        <p>{{ report.executive_summary }}</p>
        <p v-if="report.event" class="dim small">{{ report.event }}</p>
      </section>

      <SentimentChart
        :trajectory="report.sentiment_trajectory"
        :reading="report.sentiment_reading"
      />

      <ActionChart :distribution="distribution" />

      <!-- Narratives -->
      <section v-if="report.dominant_narratives?.length" class="card stack">
        <h2>Dominant narratives</h2>
        <article v-for="(narrative, index) in report.dominant_narratives" :key="index">
          <h3>{{ narrative.label }}</h3>
          <p>{{ narrative.summary }}</p>
          <p v-if="narrative.support" class="dim small">{{ narrative.support }}</p>
          <CitationLink :citation="narrative.citation" :sim-id="simId" />
        </article>
      </section>

      <section v-if="report.counter_narratives?.length" class="card stack">
        <h2>Counter-narratives</h2>
        <article v-for="(narrative, index) in report.counter_narratives" :key="index">
          <h3>{{ narrative.label }}</h3>
          <p>{{ narrative.summary }}</p>
          <CitationLink :citation="narrative.citation" :sim-id="simId" />
        </article>
      </section>

      <!-- Influence -->
      <section class="card stack">
        <div class="row">
          <h2>Influence</h2>
          <span class="dim small">
            {{ influence.edges.length }} amplification(s) between
            {{ influence.nodes.length }} agent(s)
          </span>
        </div>
        <p class="dim small">
          Drawn from what agents did — every repost and quote is an edge from the
          amplifier to whoever wrote the original. Highlighted nodes are the
          agents the report singled out.
        </p>
        <p v-if="influence.isolatedClaims.length" class="warn small" role="alert">
          {{ influence.isolatedClaims.length }} agent(s) the report calls influential
          neither amplified anyone nor were amplified.
        </p>
        <p v-if="influence.unresolved" class="dim small">
          {{ influence.unresolved }} amplification(s) pointed at posts outside the
          page loaded here and are not drawn.
        </p>

        <GraphCanvas
          v-if="influence.nodes.length"
          :nodes="influence.nodes"
          :edges="influence.edges"
          :selected="selectedAgent"
          @select="selectedAgent = $event"
        />
        <p v-else class="dim small">Nothing was amplified in this run.</p>

        <article v-for="agent in report.influential_agents || []" :key="agent.user_id">
          <h3>{{ agent.username || `agent ${agent.user_id}` }}</h3>
          <p>{{ agent.why }}</p>
          <CitationLink :citation="agent.citation" :sim-id="simId" />
        </article>
        <p v-if="report.influence_propagation">{{ report.influence_propagation }}</p>
      </section>

      <!-- Emergent behaviour -->
      <section v-if="report.emergent_behaviour?.length" class="card stack">
        <h2>Emergent behaviour</h2>
        <article v-for="(finding, index) in report.emergent_behaviour" :key="index">
          <p><strong>{{ finding.claim }}</strong></p>
          <p v-if="finding.detail" class="small">{{ finding.detail }}</p>
          <CitationLink :citation="finding.citation" :sim-id="simId" />
        </article>
      </section>

      <!-- Caveats -->
      <section v-if="report.caveats?.length" class="card stack">
        <h2>Caveats</h2>
        <ul>
          <li v-for="(caveat, index) in report.caveats" :key="index">{{ caveat }}</li>
        </ul>
      </section>
    </template>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.verification { border-left: 3px solid var(--accent); }
.warn { color: var(--warn); margin: 0; }
article + article { margin-top: 0.9rem; }
h3 { margin-bottom: 0.15rem; }
</style>
