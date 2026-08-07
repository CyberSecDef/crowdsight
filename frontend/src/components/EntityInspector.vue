<script setup>
/* One entity, in full, when a node is tapped.

   `inferred` is called out because it is the difference between something the
   document named and something the extractor concluded — and a reader deciding
   whether to trust a graph needs to see which is which. */
defineProps({
  entity: { type: Object, default: null },
})
defineEmits(['close'])
</script>

<template>
  <aside v-if="entity" class="card stack" aria-label="Entity detail">
    <div class="row">
      <h3>{{ entity.name }}</h3>
      <span class="tag">{{ entity.type }}</span>
      <span v-if="entity.inferred" class="tag tag--warn">inferred</span>
      <button class="btn" type="button" @click="$emit('close')">Close</button>
    </div>

    <p class="dim small">
      Mentioned {{ entity.mention_count ?? 0 }} time(s)
      <template v-if="entity.normalised"> · normalised as
        <code>{{ entity.normalised }}</code>
      </template>
    </p>

    <div v-if="entity.aliases?.length">
      <span class="dim small">Also written as</span>
      <ul class="small">
        <li v-for="alias in entity.aliases" :key="alias">{{ alias }}</li>
      </ul>
    </div>

    <div v-if="Object.keys(entity.attributes || {}).length">
      <span class="dim small">Attributes</span>
      <table>
        <tbody>
          <tr v-for="(value, key) in entity.attributes" :key="key">
            <th>{{ key }}</th>
            <td>{{ value }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="entity.inferred" class="dim small">
      This entity was inferred rather than named outright in the document.
    </p>
  </aside>
</template>
