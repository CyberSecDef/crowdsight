<script setup>
import { useRoute } from 'vue-router'
const route = useRoute()
</script>

<template>
  <div class="stack">
    <h1>Not found</h1>
    <p class="dim">
      Nothing is routed at <code>{{ route.fullPath }}</code>.
    </p>
    <p><RouterLink class="btn" to="/">Back to projects</RouterLink></p>
  </div>
</template>
