<script setup>
/* Stage 5 — asking the population questions.

   An interview needs a live worker: the agent answers in character from
   accumulated memory, and that memory lives in the running process. A run that
   has stopped has nobody to ask. So the ask controls are shown *disabled* on a
   finished run with the reason beside them, rather than hidden — history stays
   fully readable either way, and "why can't I ask?" deserves an answer on the
   page rather than a guess.

   Fresh answers appear where they were asked and history reloads underneath,
   because an interview that failed has an error worth showing and nothing
   written to the trace. Only refreshing history would make that failure vanish
   and leave fewer answers than agents asked, with no reason why. */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { simulation as simulationApi } from '../api/index.js'
import {
  canInterview,
  failedAnswers,
  fanOutWarning,
  groupByQuestion,
  isStartingUp,
  STARTING_UP_HINT,
  succeededAnswers,
  validateQuestion,
  whyNotInterviewable,
} from '../api/interview.js'
import { isSettled, pollUntil, TaskStatus } from '../api/polling.js'
import { runFinished } from '../api/states.js'
import { useWorkflowStore } from '../stores/workflow.js'
import ErrorBanner from '../components/ErrorBanner.vue'
import TaskProgressBar from '../components/TaskProgressBar.vue'

const props = defineProps({ simId: { type: String, required: true } })

const workflow = useWorkflowStore()

const runState = ref('')
const profiles = ref([])
const history = ref([])
const question = ref('')
const chosen = ref([])
const answers = ref([])
const askedQuestion = ref('')
const task = ref(null)
const asking = ref(false)
const loading = ref(true)
const error = ref(null)
const filterAgent = ref('')
const startingUp = ref(false)

const live = computed(() => canInterview(runState.value))
const blockedReason = computed(() => whyNotInterviewable(runState.value))
const problem = computed(() => validateQuestion(question.value))
const grouped = computed(() => groupByQuestion(history.value))
const failed = computed(() => failedAnswers(answers.value))
const answered = computed(() => succeededAnswers(answers.value))

async function load() {
  loading.value = true
  error.value = null
  try {
    workflow.selectSimulation(props.simId)
    const [status, population] = await Promise.all([
      simulationApi.runStatus(props.simId).catch(() => null),
      simulationApi.profiles(props.simId).catch(() => ({ profiles: [] })),
    ])
    runState.value = status?.state || ''
    workflow.runState = runState.value
    profiles.value = population?.profiles || []
    await loadHistory()
  } catch (err) {
    error.value = err
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  const body = { sim_id: props.simId, limit: 200 }
  if (filterAgent.value !== '') body.agent = Number(filterAgent.value)
  const result = await simulationApi.interviewHistory(body).catch(() => null)
  history.value = result?.interviews || []
}

function toggleAgent(userId) {
  const at = chosen.value.indexOf(userId)
  if (at === -1) chosen.value.push(userId)
  else chosen.value.splice(at, 1)
}

async function askOne() {
  await run(async () => {
    const result = await simulationApi.interview({
      sim_id: props.simId,
      question: question.value.trim(),
      agent: chosen.value[0],
    })
    answers.value = [result]
  })
}

async function askChosen() {
  await run(async () => {
    const started = await simulationApi.interviewBatch({
      sim_id: props.simId,
      question: question.value.trim(),
      agents: [...chosen.value],
    })
    await follow(started.task_id)
  })
}

async function askEveryone() {
  // A model call per agent, on the same GPU as anything else running.
  if (!window.confirm(fanOutWarning(profiles.value.length))) return
  await run(async () => {
    const started = await simulationApi.interviewAll({
      sim_id: props.simId,
      question: question.value.trim(),
    })
    await follow(started.task_id)
  })
}

async function follow(taskId) {
  const finished = await simulationApi.watchInterview(taskId, {
    onUpdate: (value) => (task.value = value),
  })
  task.value = finished
  if (finished.status === TaskStatus.SUCCEEDED) {
    answers.value = finished.result?.answers || []
  }
}

async function run(action) {
  asking.value = true
  error.value = null
  answers.value = []
  task.value = null
  askedQuestion.value = question.value.trim()
  try {
    await action()
    // The durable record lives in the run's database; reload it so the two
    // agree. Fresh answers stay on screen because a failed one never got there.
    await loadHistory()
  } catch (err) {
    error.value = err
    // A run can finish between loading this page and pressing the button, and
    // a short run finishes in under a minute. The failure then reads as a
    // generic fault when the truth is simply that the worker has gone — so
    // re-read the state and let the panel explain it.
    await refreshState()
    // The opposite case: the run is live but the worker has not opened its
    // socket yet. Same error message, opposite meaning.
    startingUp.value = isStartingUp(runState.value, err)
  } finally {
    asking.value = false
  }
}

async function refreshState() {
  const status = await simulationApi.runStatus(props.simId).catch(() => null)
  if (!status) return
  runState.value = status.state || ''
  workflow.runState = runState.value
}

/* Watch for the run ending while this page is open. A live run is exactly the
   case where the answer to "can I ask?" changes underneath the reader. */
let stateWatch = null

function watchState() {
  stateWatch?.abort(new Error('left the interview view'))
  if (!live.value) return
  stateWatch = new AbortController()
  pollUntil(() => simulationApi.runStatus(props.simId), {
    interval: 5000,
    signal: stateWatch.signal,
    onUpdate: (value) => {
      runState.value = value?.state || ''
      workflow.runState = runState.value
    },
    done: (value) => runFinished(value?.state),
  }).catch(() => {})
}

onMounted(load)
onBeforeUnmount(() => stateWatch?.abort(new Error('left the interview view')))
watch(() => props.simId, load)
watch(filterAgent, loadHistory)
watch(live, watchState)
</script>

<template>
  <div class="stack">
    <div class="row">
      <h1>Interaction</h1>
      <span class="mono dim small">{{ simId }}</span>
      <span v-if="runState" class="tag">{{ runState }}</span>
    </div>

    <ErrorBanner :error="error" :retry="load" />
    <p v-if="startingUp" class="starting small" role="status">{{ STARTING_UP_HINT }}</p>
    <p v-if="loading" class="dim">Loading…</p>

    <template v-else>
      <!-- Why asking may not be possible -->
      <div v-if="!live" class="blocked" role="status">
        <strong class="small">Interviews need a live run.</strong>
        <p class="small">{{ blockedReason }}</p>
        <p class="small">
          <RouterLink class="btn" :to="{ name: 'run', params: { simId } }">
            Go to stage 3
          </RouterLink>
        </p>
      </div>

      <!-- Ask -->
      <section class="card stack">
        <h2>Ask the population</h2>

        <label class="field">
          <span class="dim small">Question</span>
          <textarea
            v-model="question"
            rows="3"
            :disabled="!live || asking"
            placeholder="What do you think about the consultation period?"
          ></textarea>
        </label>

        <details class="picker">
          <summary class="small">
            Choose agents ({{ chosen.length }} selected of {{ profiles.length }})
          </summary>
          <div class="agents">
            <label
              v-for="profile in profiles"
              :key="profile.user_id"
              class="check small"
            >
              <input
                type="checkbox"
                :checked="chosen.includes(profile.user_id)"
                :disabled="!live || asking"
                @change="toggleAgent(profile.user_id)"
              />
              {{ profile.name }}
              <span class="dim">{{ profile.provenance }}</span>
            </label>
          </div>
        </details>

        <p v-if="problem && question" class="bad small">{{ problem }}</p>

        <div class="row">
          <button
            class="btn btn--primary"
            type="button"
            :disabled="!live || asking || Boolean(problem) || chosen.length !== 1"
            @click="askOne"
          >
            Ask the selected agent
          </button>
          <button
            class="btn"
            type="button"
            :disabled="!live || asking || Boolean(problem) || chosen.length < 2"
            @click="askChosen"
          >
            Ask {{ chosen.length || 'the' }} selected
          </button>
          <button
            class="btn"
            type="button"
            :disabled="!live || asking || Boolean(problem) || !profiles.length"
            @click="askEveryone"
          >
            Ask everyone ({{ profiles.length }})
          </button>
          <span v-if="asking" class="dim small">Asking…</span>
        </div>
      </section>

      <TaskProgressBar
        v-if="task && !isSettled(task.status)"
        :task="task"
        label="Interviewing"
      />

      <!-- Fresh answers -->
      <section v-if="answers.length" class="card stack">
        <div class="row">
          <h2>Answers</h2>
          <span class="dim small">{{ askedQuestion }}</span>
          <span v-if="failed.length" class="tag tag--bad">
            {{ failed.length }} failed
          </span>
        </div>

        <article v-for="(answer, index) in answered" :key="`a-${index}`" class="answer">
          <div class="row small">
            <strong>{{ answer.name || `agent ${answer.user_id}` }}</strong>
            <span class="dim mono">@{{ answer.username }}</span>
          </div>
          <p>{{ answer.response || answer.answer }}</p>
        </article>

        <article v-for="(answer, index) in failed" :key="`f-${index}`" class="answer is-failed">
          <div class="row small">
            <strong>{{ answer.name || `agent ${answer.user_id}` }}</strong>
            <span class="tag tag--bad">no answer</span>
          </div>
          <p class="bad small">{{ answer.error }}</p>
        </article>
      </section>

      <!-- History -->
      <section class="card stack">
        <div class="row">
          <h2>Interview history</h2>
          <span class="dim small">{{ history.length }} recorded</span>
          <span class="spacer"></span>
          <label class="field">
            <span class="dim small">Agent</span>
            <select v-model="filterAgent">
              <option value="">Everyone</option>
              <option
                v-for="profile in profiles"
                :key="profile.user_id"
                :value="profile.user_id"
              >
                {{ profile.name }}
              </option>
            </select>
          </label>
        </div>

        <p v-if="!history.length" class="dim small">
          Nothing has been asked of this run yet.
        </p>

        <article v-for="(group, index) in grouped" :key="index" class="group">
          <h3>{{ group.question }}</h3>
          <p class="dim small">
            {{ group.answers.length }} answer(s)
            <template v-if="group.rounds.length">
              · round{{ group.rounds.length > 1 ? 's' : '' }} {{ group.rounds.join(', ') }}
            </template>
          </p>
          <article v-for="entry in group.answers" :key="`${entry.user_id}-${entry.created_at}`"
                   class="answer">
            <div class="row small">
              <strong>{{ entry.name || `agent ${entry.user_id}` }}</strong>
              <span class="dim mono">@{{ entry.username }}</span>
              <span v-if="entry.round !== null" class="tag">round {{ entry.round }}</span>
            </div>
            <p>{{ entry.response }}</p>
          </article>
        </article>
      </section>
    </template>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.field { display: flex; flex-direction: column; gap: 0.15rem; }

textarea,
select {
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  width: 100%;
}

.blocked {
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--surface);
  padding: 0.6rem 0.9rem;
}

.blocked p { margin: 0.25rem 0 0; color: var(--text-dim); }

.starting {
  margin: 0;
  padding: 0.5rem 0.8rem;
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--surface);
}

.picker summary { cursor: pointer; color: var(--text-dim); }

.agents {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
  gap: 0.1rem 0.6rem;
  margin-top: 0.4rem;
  max-height: 220px;
  overflow-y: auto;
}

.answer {
  border-left: 2px solid var(--border);
  padding: 0.25rem 0 0.25rem 0.6rem;
  margin-top: 0.5rem;
}

.answer p { margin: 0.15rem 0 0; }
.is-failed { border-left-color: var(--bad); }
.bad { color: var(--bad); }
.group + .group { margin-top: 1.2rem; }
</style>
