<script setup>
/* Who did what.

   Silent agents are counted rather than hidden. An agent that never acted is a
   real outcome — the participation roll skips low-activity agents on purpose —
   and a table that only listed the busy ones would make a quiet population
   look like a small one. */
import { computed, ref } from 'vue'

const props = defineProps({
  agents: { type: Array, default: () => [] },
})

const showSilent = ref(false)

const silent = computed(() => props.agents.filter((a) => !a.actions).length)
const shown = computed(() =>
  showSilent.value ? props.agents : props.agents.filter((a) => a.actions > 0),
)
</script>

<template>
  <section class="card stack">
    <div class="row">
      <h3>Agent activity</h3>
      <span class="dim small">
        {{ agents.length }} agent(s), {{ silent }} never acted
      </span>
      <span class="spacer"></span>
      <label class="check small">
        <input v-model="showSilent" type="checkbox" />
        Show silent agents
      </label>
    </div>

    <p v-if="!agents.length" class="dim small">No activity recorded yet.</p>

    <div v-else class="scroll-x">
      <table>
        <thead>
          <tr>
            <th>Agent</th><th>Activity</th><th>Actions</th>
            <th>Posts</th><th>Comments</th><th>Engagement</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="agent in shown" :key="agent.user_id">
            <td>
              {{ agent.name }}
              <span class="dim mono small">@{{ agent.username }}</span>
            </td>
            <td><span class="tag">{{ agent.activity_level }}</span></td>
            <td>{{ agent.actions }}</td>
            <td>{{ agent.posts ?? 0 }}</td>
            <td>{{ agent.comments ?? 0 }}</td>
            <td>{{ agent.engagement_received ?? 0 }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.spacer { flex: 1; }
</style>
