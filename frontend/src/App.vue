<script setup>
import { RouterLink, RouterView } from 'vue-router'
import WorkflowProgress from './components/WorkflowProgress.vue'
</script>

<template>
  <div class="app">
    <header class="masthead">
      <div class="container">
        <div class="masthead__inner">
          <RouterLink to="/" class="brand">
            CrowdSight
            <small>local-only swarm simulation</small>
          </RouterLink>
          <nav class="nav">
            <RouterLink to="/">Projects</RouterLink>
            <RouterLink to="/runs">Run history</RouterLink>
          </nav>
        </div>
        <WorkflowProgress />
      </div>
    </header>

    <main>
      <div class="container">
        <RouterView v-slot="{ Component }">
          <component :is="Component" />
        </RouterView>
      </div>
    </main>
  </div>
</template>
