<script setup>
/* Sentiment over rounds, as inline SVG.

   Two things this has to be honest about, both from Phase 8:

   * a round with nothing scored has no mean at all — plotting it as zero would
     read as "balanced" when it means "we do not know", so the line breaks;
   * a mean over two posts and a mean over two hundred look identical, so each
     point carries how many posts it rests on. */
import { computed } from 'vue'

const props = defineProps({
  trajectory: { type: Array, default: () => [] },
  reading: { type: String, default: '' },
})

const W = 640
const H = 200
const PAD = { left: 38, right: 14, top: 14, bottom: 26 }

const scored = computed(() =>
  props.trajectory.filter((entry) => entry.mean !== null && entry.mean !== undefined),
)

const unscored = computed(() => props.trajectory.length - scored.value.length)

const xFor = (index) => {
  const span = Math.max(1, props.trajectory.length - 1)
  return PAD.left + (index / span) * (W - PAD.left - PAD.right)
}
/* Fixed -1..1 domain: auto-scaling would make a run that moved from -0.05 to
   0.05 look like a collapse in opinion. */
const yFor = (mean) => PAD.top + ((1 - mean) / 2) * (H - PAD.top - PAD.bottom)

const points = computed(() =>
  props.trajectory
    .map((entry, index) => ({ ...entry, index }))
    .filter((entry) => entry.mean !== null && entry.mean !== undefined),
)

/* Segments rather than one path: a gap where a round could not be scored must
   stay a gap. */
const segments = computed(() => {
  const out = []
  let run = []
  for (const entry of props.trajectory.map((e, index) => ({ ...e, index }))) {
    if (entry.mean === null || entry.mean === undefined) {
      if (run.length > 1) out.push(run)
      run = []
    } else {
      run.push(entry)
    }
  }
  if (run.length > 1) out.push(run)
  return out.map((run) => run.map((e) => `${xFor(e.index)},${yFor(e.mean)}`).join(' '))
})
</script>

<template>
  <figure class="card">
    <figcaption class="row">
      <h3>Sentiment across rounds</h3>
      <span v-if="unscored" class="tag tag--warn">
        {{ unscored }} round(s) unscored
      </span>
    </figcaption>

    <p v-if="!trajectory.length" class="dim small">
      No sentiment was scored for this run.
    </p>

    <svg v-else :viewBox="`0 0 ${W} ${H}`" class="chart" role="img"
         :aria-label="`Sentiment by round: ${points.map((p) => `round ${p.round} ${p.mean.toFixed(2)}`).join(', ')}`">
      <!-- zero line: the difference between opposed and supportive -->
      <line :x1="PAD.left" :x2="W - PAD.right" :y1="yFor(0)" :y2="yFor(0)" class="axis" />
      <line :x1="PAD.left" :x2="PAD.left" :y1="PAD.top" :y2="H - PAD.bottom" class="axis" />

      <text :x="PAD.left - 6" :y="yFor(1) + 4" class="tick">+1</text>
      <text :x="PAD.left - 6" :y="yFor(0) + 4" class="tick">0</text>
      <text :x="PAD.left - 6" :y="yFor(-1) + 4" class="tick">−1</text>

      <polyline v-for="(segment, index) in segments" :key="index"
                :points="segment" class="line" />

      <g v-for="point in points" :key="point.round">
        <circle :cx="xFor(point.index)" :cy="yFor(point.mean)"
                :r="4" class="point"
                :class="{ thin: (point.scored ?? 0) < 3 }" />
        <text :x="xFor(point.index)" :y="H - 8" class="tick mid">r{{ point.round }}</text>
        <title>
          Round {{ point.round }}: {{ point.mean.toFixed(2) }} from
          {{ point.scored ?? 0 }} of {{ point.posts ?? 0 }} post(s)
        </title>
      </g>
    </svg>

    <p v-if="reading" class="small">{{ reading }}</p>
    <p v-if="points.some((p) => (p.scored ?? 0) < 3)" class="dim small">
      Hollow points rest on fewer than three scored posts.
    </p>
  </figure>
</template>

<style scoped>
.chart { width: 100%; height: auto; }
.axis { stroke: var(--border); stroke-width: 1; }
.line { fill: none; stroke: var(--accent); stroke-width: 2; }
.point { fill: var(--accent); }
.point.thin { fill: var(--surface); stroke: var(--accent); stroke-width: 2; }
.tick { fill: var(--text-dim); font-size: 10px; text-anchor: end; }
.tick.mid { text-anchor: middle; }
figure { margin: 0; }
figcaption { margin-bottom: 0.4rem; }
</style>
