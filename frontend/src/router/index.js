/**
 * Routes are named after the thing they operate on, not after a step number.
 *
 * The five stages are a workflow, but they are not one resource: stages 1 and 2
 * happen before a simulation exists, and stage 1's output (a graph) can feed
 * several simulations. A /workflow/:step scheme would have to invent an id for
 * stage 1 and would lose your place on refresh. A run takes hours, so every
 * stage being a real, bookmarkable URL is worth more than the numbering.
 *
 * `meta.stage` is what the progress indicator reads, so the numbering still
 * shows up in the UI — it just is not what the address bar is built from.
 */

import { createRouter, createWebHistory } from 'vue-router'

export const STAGES = [
  { stage: 1, key: 'graph', label: 'Graph build' },
  { stage: 2, key: 'profiles', label: 'Environment' },
  { stage: 3, key: 'run', label: 'Simulation' },
  { stage: 4, key: 'report', label: 'Report' },
  { stage: 5, key: 'interview', label: 'Interaction' },
]

const routes = [
  {
    path: '/',
    name: 'home',
    component: () => import('../views/HomeView.vue'),
    meta: { title: 'Projects' },
  },
  {
    path: '/graphs/new',
    name: 'graph-new',
    component: () => import('../views/GraphBuildView.vue'),
    meta: { title: 'New graph', stage: 1 },
  },
  {
    path: '/graphs/:graphId',
    name: 'graph',
    component: () => import('../views/GraphBuildView.vue'),
    props: true,
    meta: { title: 'Graph', stage: 1 },
  },
  {
    path: '/simulations/:simId/profiles',
    name: 'profiles',
    component: () => import('../views/EnvironmentView.vue'),
    props: true,
    meta: { title: 'Environment', stage: 2 },
  },
  {
    path: '/simulations/:simId/run',
    name: 'run',
    component: () => import('../views/SimulationView.vue'),
    props: true,
    meta: { title: 'Simulation', stage: 3 },
  },
  {
    path: '/simulations/:simId/report/:reportId?',
    name: 'report',
    component: () => import('../views/ReportView.vue'),
    props: true,
    meta: { title: 'Report', stage: 4 },
  },
  {
    path: '/simulations/:simId/interview',
    name: 'interview',
    component: () => import('../views/InteractionView.vue'),
    props: true,
    meta: { title: 'Interaction', stage: 5 },
  },
  {
    path: '/runs',
    name: 'runs',
    component: () => import('../views/RunHistoryView.vue'),
    meta: { title: 'Run history' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('../views/NotFoundView.vue'),
    meta: { title: 'Not found' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: (to, from, saved) => saved ?? { top: 0 },
})

router.afterEach((to) => {
  const title = to.meta?.title
  document.title = title ? `${title} · CrowdSight` : 'CrowdSight'
})

export default router
