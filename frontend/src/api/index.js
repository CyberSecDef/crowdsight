/**
 * The backend surface, named.
 *
 * One function per route, grouped the way the blueprints are. Views import
 * from here rather than writing paths, so a route that moves is a one-line
 * change and a route that does not exist is a missing import rather than a 404
 * discovered by a user.
 *
 * Note the two simulation groups. `/api/simulations` (plural) is the config
 * store from Phase 5; `/api/simulation` (singular) is the control plane from
 * Phase 6. Both are live and they are not the same thing.
 */

import { get, post, put, del, request } from './client.js'
import { watchTask } from './polling.js'

export * from './client.js'
export * from './polling.js'
export * from './states.js'

// --------------------------------------------------------------------------
// Stage 1 — documents and the graph
// --------------------------------------------------------------------------

export const graph = {
  /**
   * Upload one document and start building its graph.
   *
   * The part is named `file`, singular, and the endpoint takes exactly one per
   * request — sending `files` gets a 400 that says nothing arrived at all.
   *
   * `reviewOntology` stops the job after the ontology is proposed and parks the
   * task as `awaiting_review`, so it can be edited before extraction runs.
   * Extraction is the expensive stage and a wrong ontology wastes all of it.
   */
  upload(file, { graphId = '', reviewOntology = false } = {}) {
    const form = new FormData()
    form.append('file', file)
    if (graphId) form.append('graph_id', graphId)
    if (reviewOntology) form.append('review_ontology', 'true')
    return post('/graph/upload', form)
  },

  taskStatus: (taskId) => get(`/graph/status/${encodeURIComponent(taskId)}`),
  watch: (taskId, options) => watchTask('/graph/status', taskId, options),
  tasks: (query) => get('/graph/tasks', query),

  list: () => get('/graph/'),
  detail: (graphId) => get(`/graph/${encodeURIComponent(graphId)}`),
  remove: (graphId) => del(`/graph/${encodeURIComponent(graphId)}`),

  ontology: (graphId) => get(`/graph/${encodeURIComponent(graphId)}/ontology`),
  saveOntology: (graphId, body) =>
    post(`/graph/${encodeURIComponent(graphId)}/ontology`, body),

  entities: (graphId, query) =>
    get(`/graph/${encodeURIComponent(graphId)}/entities`, query),
  entity: (graphId, uuid) =>
    get(`/graph/${encodeURIComponent(graphId)}/entities/${encodeURIComponent(uuid)}`),
  entityTypes: (graphId) => get(`/graph/${encodeURIComponent(graphId)}/entity-types`),
  relationships: (graphId, query) =>
    get(`/graph/${encodeURIComponent(graphId)}/relationships`, query),
  subgraph: (graphId, query) => get(`/graph/${encodeURIComponent(graphId)}/subgraph`, query),
  search: (graphId, query) => get(`/graph/${encodeURIComponent(graphId)}/search`, query),
}

// --------------------------------------------------------------------------
// Stage 2/3 — simulation configuration and control
// --------------------------------------------------------------------------

export const simulation = {
  // The config store: /api/simulations
  create: (body) => post('/simulations/', body),
  listConfigs: (query) => get('/simulations/', query),
  config: (simId) => get(`/simulations/${encodeURIComponent(simId)}/config`),
  saveConfig: (simId, body) =>
    put(`/simulations/${encodeURIComponent(simId)}/config`, body),
  detail: (simId) => get(`/simulations/${encodeURIComponent(simId)}`),

  // The control plane: /api/simulation
  derive: (body) => post('/simulation/create', body),
  prepare: (body) => post('/simulation/prepare', body),
  prepareStatus: (query) => get('/simulation/prepare/status', query),
  list: (query) => get('/simulation/list', query),
  summary: (simId) => get(`/simulation/${encodeURIComponent(simId)}`),
  profiles: (simId, query) =>
    get(`/simulation/${encodeURIComponent(simId)}/profiles`, query),
  status: (simId) => get(`/simulation/${encodeURIComponent(simId)}/status`),
  budget: () => get('/simulation/budget'),

  start: (body) => post('/simulation/start', body),
  stop: (body) => post('/simulation/stop', body),

  runStatus: (simId) => get(`/simulation/${encodeURIComponent(simId)}/run-status`),
  runStatusDetail: (simId) =>
    get(`/simulation/${encodeURIComponent(simId)}/run-status/detail`),
  timeline: (simId, query) =>
    get(`/simulation/${encodeURIComponent(simId)}/timeline`, query),
  agentStats: (simId, query) =>
    get(`/simulation/${encodeURIComponent(simId)}/agent-stats`, query),
  actions: (simId, query) => get(`/simulation/${encodeURIComponent(simId)}/actions`, query),
  posts: (simId, query) => get(`/simulation/${encodeURIComponent(simId)}/posts`, query),
  comments: (simId, query) =>
    get(`/simulation/${encodeURIComponent(simId)}/comments`, query),

  // Stage 5
  interview: (body) => post('/simulation/interview', body),
  interviewBatch: (body) => post('/simulation/interview/batch', body),
  interviewAll: (body) => post('/simulation/interview/all', body),
  interviewHistory: (body) => post('/simulation/interview/history', body),

  envStatus: (body) => post('/simulation/env-status', body),
  closeEnv: (body) => post('/simulation/close-env', body),
}

// --------------------------------------------------------------------------
// Stage 4 — reports
// --------------------------------------------------------------------------

export const report = {
  generate: (body) => post('/report/generate', body),
  taskStatus: (taskId) => get(`/report/status/${encodeURIComponent(taskId)}`),
  watch: (taskId, options) => watchTask('/report/status', taskId, options),

  list: (query) => get('/report/', query),
  detail: (reportId) => get(`/report/${encodeURIComponent(reportId)}`),
  remove: (reportId) => del(`/report/${encodeURIComponent(reportId)}`),

  /** The export endpoint returns a document, not JSON. */
  exportUrl: (reportId, format = 'markdown', download = false) => {
    const params = new URLSearchParams({ format })
    if (download) params.set('download', 'true')
    return `/api/report/${encodeURIComponent(reportId)}/export?${params}`
  },
  exportText: (reportId, format = 'markdown') =>
    request(`/report/${encodeURIComponent(reportId)}/export`, {
      query: { format },
      raw: true,
    }).then((response) => response.text()),
}

export const health = {
  live: () => get('/health/live'),
  ready: () => get('/health/ready'),
}
