/**
 * The one place the UI talks to the backend.
 *
 * Every view goes through here rather than calling fetch itself, so error
 * handling is the same everywhere and there is a single place to look when a
 * response shape changes. The backend reports failures as `{"error": "..."}`
 * with a real status code, so an ApiError carries both — a view that wants to
 * treat 404 differently from 500 can, without parsing strings.
 *
 * The base URL is relative on purpose. The gateway serves the UI and proxies
 * /api on the same origin, so there is no host to configure and no way to
 * point this at somewhere off the machine by editing a setting.
 */

export const API_BASE = '/api'

/** A request that reached the backend and came back as a failure. */
export class ApiError extends Error {
  constructor(message, { status = 0, body = null, url = '' } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.url = url
  }

  get notFound() {
    return this.status === 404
  }

  /** The run is busy, or the control plane is full. Worth retrying. */
  get transient() {
    return this.status === 429 || this.status === 503 || this.status === 0
  }

  /** A refusal the user can act on: bad input, or the wrong state. */
  get refusal() {
    return this.status === 400 || this.status === 409 || this.status === 422
  }
}

/** A request that never got an answer: the backend is down, or aborted. */
export class NetworkError extends ApiError {
  constructor(message, { url = '', cause = null } = {}) {
    super(message, { status: 0, url })
    this.name = 'NetworkError'
    this.cause = cause
  }
}

function buildUrl(path, query) {
  const url = path.startsWith('/api') ? path : `${API_BASE}${path}`
  if (!query) return url

  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    // A null or undefined filter means "not filtering", not "filter by empty".
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((v) => params.append(key, v))
    else params.append(key, value)
  }
  const search = params.toString()
  return search ? `${url}?${search}` : url
}

async function readBody(response) {
  const type = response.headers.get('content-type') || ''
  try {
    if (type.includes('application/json')) return await response.json()
    const text = await response.text()
    return text || null
  } catch {
    return null
  }
}

function messageFrom(body, response, url) {
  if (body && typeof body === 'object' && typeof body.error === 'string') {
    return body.error
  }
  if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 400)
  return `${response.status} ${response.statusText || 'error'} from ${url}`
}

/**
 * Make a request. Resolves with the parsed body, or throws an ApiError.
 *
 * @param {string} path    Either "/graph/upload" or a full "/api/..." path.
 * @param {object} options fetch options plus `query`, and `raw` to get the
 *                         Response itself (exports are files, not JSON).
 */
export async function request(path, options = {}) {
  const { query, raw = false, body, headers, ...rest } = options
  const url = buildUrl(path, query)

  const init = { ...rest, headers: { Accept: 'application/json', ...headers } }
  if (body !== undefined) {
    if (body instanceof FormData) {
      // Let the browser set the multipart boundary; setting it by hand is the
      // classic way to make an upload fail with no useful message.
      init.body = body
    } else {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
  }

  let response
  try {
    response = await fetch(url, init)
  } catch (cause) {
    if (cause?.name === 'AbortError') throw cause
    throw new NetworkError(`Could not reach the backend (${url})`, { url, cause })
  }

  if (raw) {
    if (!response.ok) {
      throw new ApiError(messageFrom(await readBody(response), response, url), {
        status: response.status,
        url,
      })
    }
    return response
  }

  const parsed = await readBody(response)
  if (!response.ok) {
    throw new ApiError(messageFrom(parsed, response, url), {
      status: response.status,
      body: parsed,
      url,
    })
  }
  return parsed
}

export const get = (path, query, options) => request(path, { ...options, query })
export const post = (path, body, options) => request(path, { ...options, method: 'POST', body })
export const put = (path, body, options) => request(path, { ...options, method: 'PUT', body })
export const del = (path, options) => request(path, { ...options, method: 'DELETE' })
