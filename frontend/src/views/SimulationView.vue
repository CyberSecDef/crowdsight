<script setup>
/* Stage 3 — configure it, launch it, watch it.

   The screen shows both the scenario and the run, because they are the same
   simulation at different moments and a run takes hours: coming back to a live
   run and wanting to check what was configured is the normal case.

   Editing a started run forks it. The server returns a different sim_id, and
   this view follows the edit there — staying put would show a config that does
   not match what was just saved, and a second edit would fork again. */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { simulation as simulationApi } from '../api/index.js'
import { RunState, runFinished } from '../api/states.js'
import { useRunMonitor } from '../composables/useRunMonitor.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ActionFeed from '../components/ActionFeed.vue'
import AgentActivity from '../components/AgentActivity.vue'
import ConfigEditor from '../components/ConfigEditor.vue'
import ErrorBanner from '../components/ErrorBanner.vue'
import RunProgress from '../components/RunProgress.vue'

const props = defineProps({ simId: { type: String, required: true } })

const router = useRouter()
const workflow = useWorkflowStore()

const config = ref(null)
const meta = ref(null)
const budget = ref(null)
const loading = ref(true)
const saving = ref(false)
const launching = ref(false)
const error = ref(null)
const forkNotice = ref(null)
const changes = ref([])
const showConfig = ref(false)

let monitor = useRunMonitor(props.simId)

const state = computed(() => monitor.status.value?.state || meta.value?.state || '')
const running = computed(() => state.value === RunState.RUNNING)
const finished = computed(() => runFinished(state.value))
const failed = computed(() => state.value === RunState.FAILED)
/* `prepared` on the summary means "has a scenario", not "has a population" —
   a fork is prepared the moment it is created and has no agents at all. Using
   it here enabled Start on every fork, which the server then refused. The
   population is asked about separately. */
const hasScenario = computed(() => Boolean(meta.value?.prepared))
const hasPopulation = ref(false)

async function load() {
  loading.value = true
  error.value = null
  try {
    workflow.selectSimulation(props.simId)
    const [summary, scenario] = await Promise.all([
      simulationApi.summary(props.simId),
      simulationApi.config(props.simId).catch(() => null),
    ])
    meta.value = { ...(summary.meta || {}), prepared: summary.prepared }
    config.value = scenario
    // 409 means the population has not been generated; that is a state.
    hasPopulation.value = await simulationApi
      .profiles(props.simId, { limit: 1 })
      .then((result) => (result?.count ?? 0) > 0)
      .catch(() => false)
    // A run that has never started opens on its scenario; one that has opens
    // on the run, which is what you came back for.
    showConfig.value = !summary.meta?.started_at
    await refreshBudget()
    await attach()
  } catch (err) {
    error.value = err
  } finally {
    loading.value = false
  }
}

async function refreshBudget() {
  budget.value = await simulationApi.budget().catch(() => null)
}

/** Watch a live run; read a finished one once. */
async function attach() {
  monitor.reset()
  const status = await simulationApi.runStatus(props.simId).catch(() => null)
  monitor.status.value = status
  // The stage indicator reads this to decide whether report and interview are
  // reachable. Nothing else sets it, so forgetting it here silently locks the
  // last two stages for the whole session.
  workflow.runState = status?.state || ''
  if (status?.state === RunState.RUNNING) monitor.start()
  else await monitor.loadStatic()
}

async function save(edited) {
  saving.value = true
  error.value = null
  forkNotice.value = null
  changes.value = []
  try {
    const result = await simulationApi.saveConfig(props.simId, edited)
    changes.value = result.changes || []
    if (result.forked && result.sim_id !== props.simId) {
      const notice = { from: props.simId, to: result.sim_id }
      workflow.selectSimulation(result.sim_id)
      // Set the notice *after* navigating. The simId watcher clears it as part
      // of switching simulations, so setting it first meant the one message
      // explaining why the id changed was wiped by the change itself.
      await router.push({ name: 'run', params: { simId: result.sim_id } })
      forkNotice.value = notice
      return
    }
    config.value = result.config
    meta.value = { ...(result.meta || {}), prepared: meta.value?.prepared }
  } catch (err) {
    error.value = err
  } finally {
    saving.value = false
  }
}

async function launch() {
  launching.value = true
  error.value = null
  try {
    const result = await simulationApi.start({ sim_id: props.simId })
    meta.value = { ...meta.value, state: RunState.RUNNING }
    showConfig.value = false
    if (result.resumed) {
      changes.value = [`Resumed from the last checkpoint (pid ${result.pid}).`]
    }
    await attach()
  } catch (err) {
    error.value = err
    await refreshBudget()
  } finally {
    launching.value = false
  }
}

async function halt() {
  launching.value = true
  error.value = null
  try {
    await simulationApi.stop({ sim_id: props.simId })
    monitor.stop()
    await attach()
    await load()
  } catch (err) {
    error.value = err
  } finally {
    launching.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => monitor.stop())

// The run finishes while the page is open, which is when stages 4 and 5
// become reachable.
watch(() => monitor.status.value?.state, (value) => {
  workflow.runState = value || ''
})

watch(() => props.simId, () => {
  monitor.stop()
  monitor = useRunMonitor(props.simId)
  forkNotice.value = null
  load()
})
</script>

<template>
  <div class="stack">
    <div class="row">
      <h1>Simulation</h1>
      <span class="mono dim small">{{ simId }}</span>
      <span v-if="state" class="tag">{{ state }}</span>
      <span class="spacer"></span>
      <!-- Gated on !loading: load() decides which side to open on, so a click
           made while it is still running is silently undone a moment later. -->
      <button
        v-if="config && !loading"
        class="btn"
        type="button"
        @click="showConfig = !showConfig"
      >
        {{ showConfig ? 'Show the run' : 'Show the scenario' }}
      </button>
    </div>

    <ErrorBanner :error="error" :retry="load" />
    <ErrorBanner :error="monitor.error.value" />

    <!-- The edit landed somewhere else -->
    <div v-if="forkNotice" class="fork" role="alert">
      <strong>This run had already started, so your edit created a new simulation.</strong>
      <p class="small">
        <span class="mono">{{ forkNotice.from }}</span> is preserved untouched.
        You are now looking at <span class="mono">{{ forkNotice.to }}</span>.
      </p>
    </div>

    <p v-if="changes.length" class="changes small" role="status">
      {{ changes.join(' · ') }}
    </p>

    <p v-if="loading" class="dim">Loading…</p>

    <!-- Launch controls -->
    <section v-if="!loading" class="card stack">
      <div class="row">
        <template v-if="running">
          <button class="btn" type="button" :disabled="launching" @click="halt">
            {{ launching ? 'Stopping…' : 'Stop the run' }}
          </button>
          <span class="dim small">
            Stopping is graceful — the current round finishes or is rolled back.
          </span>
        </template>
        <template v-else>
          <button
            class="btn btn--primary"
            type="button"
            :disabled="launching || !hasPopulation || !hasScenario"
            @click="launch"
          >
            {{ launching ? 'Starting…' : failed ? 'Resume from checkpoint' : 'Start the run' }}
          </button>
          <span v-if="!hasScenario" class="dim small">
            This simulation has no scenario yet — derive one first.
          </span>
          <span v-else-if="!hasPopulation" class="dim small">
            This simulation has no population yet. A fork carries the scenario
            but not the agents — prepare it in stage 2.
          </span>
          <span v-else-if="finished && !failed" class="dim small">
            This run is complete. Starting it again would resume from its last
            checkpoint.
          </span>
        </template>
        <span class="spacer"></span>
        <span v-if="budget" class="dim small">
          {{ budget.running }}/{{ budget.capacity }} run(s) in flight
        </span>
      </div>
      <p v-if="running" class="dim small">
        A real run takes hours. This page can be closed and reopened; the run
        does not depend on it.
      </p>
    </section>

    <!-- The scenario -->
    <ConfigEditor
      v-if="!loading && showConfig && config"
      :config="config"
      :busy="saving"
      :locked="Boolean(meta?.started_at)"
      @save="save"
    />

    <!-- The run -->
    <template v-if="!loading && !showConfig">
      <RunProgress :status="monitor.status.value" :timeline="monitor.timeline.value" />
      <ActionFeed :actions="monitor.actions.value" :live="running" />
      <AgentActivity :agents="monitor.agents.value" />
    </template>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }

.fork {
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--surface);
  padding: 0.6rem 0.9rem;
}

.fork p { margin: 0.2rem 0 0; color: var(--text-dim); }
.changes { color: var(--warn); margin: 0; }
</style>
