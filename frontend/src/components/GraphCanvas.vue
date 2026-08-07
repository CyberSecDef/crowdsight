<script setup>
/* The graph, drawn with Cytoscape.

   The subgraph endpoint already returns what a graph library wants — nodes
   with uuid/name/type and edges with source/target/type — so this is mostly
   styling, a layout, and getting the lifecycle right.

   Two things worth noting. Type filtering uses Cytoscape's own selectors and
   `.style('display')` rather than rebuilding the graph, so positions survive a
   filter toggle and the picture does not jump every time a box is unticked.
   And the instance is destroyed on unmount: Cytoscape attaches listeners to
   the window, so a view that mounts and unmounts repeatedly leaks without it. */
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import cytoscape from 'cytoscape'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  hiddenTypes: { type: Array, default: () => [] },
  selected: { type: String, default: '' },
})
const emit = defineEmits(['select'])

const container = ref(null)
const cy = shallowRef(null)

/* Colour by type, stably: the same type gets the same colour every time the
   graph is drawn, rather than depending on iteration order. */
const PALETTE = [
  '#7a4d2b', '#2f6f4f', '#3b5b8c', '#8a6a1f',
  '#6b3f6e', '#1f6f74', '#8c4a4a', '#4a5a2f',
]

function colourFor(type) {
  let hash = 0
  for (let i = 0; i < String(type).length; i += 1) {
    hash = (hash * 31 + String(type).charCodeAt(i)) >>> 0
  }
  return PALETTE[hash % PALETTE.length]
}

function elements() {
  const nodes = props.nodes.map((node) => ({
    data: {
      id: node.uuid,
      label: node.name,
      type: node.type,
      colour: colourFor(node.type),
      mentions: node.mention_count ?? 0,
      inferred: Boolean(node.inferred),
    },
  }))
  // An edge whose endpoints are not both present would make Cytoscape throw.
  const present = new Set(nodes.map((n) => n.data.id))
  const edges = props.edges
    .filter((edge) => present.has(edge.source) && present.has(edge.target))
    .map((edge) => ({
      data: {
        id: edge.uuid,
        source: edge.source,
        target: edge.target,
        label: edge.type,
      },
    }))
  return [...nodes, ...edges]
}

const STYLE = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(colour)',
      label: 'data(label)',
      color: '#ffffff',
      'text-outline-color': 'data(colour)',
      'text-outline-width': 2,
      'font-size': 10,
      width: 'mapData(mentions, 0, 10, 22, 52)',
      height: 'mapData(mentions, 0, 10, 22, 52)',
      'text-valign': 'center',
      'text-wrap': 'wrap',
      'text-max-width': '90px',
    },
  },
  {
    // An inferred entity was never named outright in the document.
    selector: 'node[?inferred]',
    style: { 'border-width': 2, 'border-style': 'dashed', 'border-color': '#ffffff' },
  },
  {
    selector: 'node:selected',
    style: { 'border-width': 4, 'border-style': 'solid', 'border-color': '#ffffff' },
  },
  {
    selector: 'edge',
    style: {
      width: 1.5,
      'line-color': '#9a9691',
      'target-arrow-color': '#9a9691',
      'target-arrow-shape': 'triangle',
      'curve-style': 'bezier',
      label: 'data(label)',
      'font-size': 8,
      color: '#9a9691',
      'text-rotation': 'autorotate',
      'text-background-color': '#000000',
      'text-background-opacity': 0.35,
      'text-background-padding': 2,
    },
  },
]

function layout() {
  if (!cy.value) return
  cy.value
    .layout({
      name: 'cose',
      animate: false,
      padding: 30,
      nodeRepulsion: 9000,
      idealEdgeLength: 110,
    })
    .run()
}

function build() {
  if (!container.value) return
  cy.value?.destroy()
  cy.value = cytoscape({
    container: container.value,
    elements: elements(),
    style: STYLE,
    wheelSensitivity: 0.2,
    maxZoom: 3,
    minZoom: 0.2,
  })
  cy.value.on('tap', 'node', (event) => emit('select', event.target.id()))
  cy.value.on('tap', (event) => {
    if (event.target === cy.value) emit('select', '')
  })
  applyFilter()
  layout()
}

function applyFilter() {
  if (!cy.value) return
  const hidden = new Set(props.hiddenTypes)
  cy.value.batch(() => {
    cy.value.nodes().forEach((node) => {
      node.style('display', hidden.has(node.data('type')) ? 'none' : 'element')
    })
    // An edge with a hidden endpoint has nothing to attach to.
    cy.value.edges().forEach((edge) => {
      const visible =
        edge.source().style('display') !== 'none' &&
        edge.target().style('display') !== 'none'
      edge.style('display', visible ? 'element' : 'none')
    })
  })
}

function focus(uuid) {
  if (!cy.value || !uuid) return
  const node = cy.value.getElementById(uuid)
  if (node.nonempty()) {
    cy.value.elements().unselect()
    node.select()
    cy.value.animate({ center: { eles: node }, duration: 200 })
  }
}

defineExpose({ fit: () => cy.value?.fit(undefined, 30), relayout: layout })

onMounted(build)
onBeforeUnmount(() => {
  cy.value?.destroy()
  cy.value = null
})

watch(() => [props.nodes, props.edges], build, { deep: true })
watch(() => props.hiddenTypes, applyFilter, { deep: true })
watch(() => props.selected, focus)
</script>

<template>
  <div class="canvas-wrap">
    <div ref="container" class="canvas" data-testid="graph-canvas"></div>
    <p v-if="!nodes.length" class="empty dim small">
      This graph has no entities to draw.
    </p>
    <div class="controls">
      <button class="btn" type="button" @click="cy?.fit(undefined, 30)">Fit</button>
      <button class="btn" type="button" @click="layout">Re-layout</button>
    </div>
  </div>
</template>

<style scoped>
.canvas-wrap {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  overflow: hidden;
}

.canvas {
  width: 100%;
  height: 480px;
}

.empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  pointer-events: none;
}

.controls {
  position: absolute;
  right: 0.5rem;
  bottom: 0.5rem;
  display: flex;
  gap: 0.25rem;
}
</style>
