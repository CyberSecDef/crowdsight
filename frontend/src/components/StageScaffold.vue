<script setup>
/* A stage the shell routes to but a later step builds out.

   It says which step fills it in rather than showing a blank page, because
   "nothing here" and "broken" look identical otherwise, and this shell is
   going to be walked through before stages 2-6 exist. */
defineProps({
  title: { type: String, required: true },
  step: { type: String, required: true },
  covers: { type: Array, default: () => [] },
})
</script>

<template>
  <section class="card stack">
    <div>
      <h2>{{ title }}</h2>
      <p class="dim small">Built in {{ step }}. The shell routes here already.</p>
    </div>
    <ul v-if="covers.length" class="covers small">
      <li v-for="item in covers" :key="item">{{ item }}</li>
    </ul>
    <slot />
  </section>
</template>

<style scoped>
.covers {
  margin: 0;
  padding-left: 1.1rem;
  color: var(--text-dim);
}
</style>
