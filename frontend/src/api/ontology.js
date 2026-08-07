/**
 * Ontology editing rules, mirrored from the backend.
 *
 * Two things here are the server's rules restated in the browser, and both
 * exist so the operator sees a consequence *before* submitting rather than
 * discovering it afterwards:
 *
 * 1. `toIdentifier` — typing "Public Figure" stores `PublicFigure`. The editor
 *    shows that, because a name silently becoming something else is a nasty
 *    surprise to find in a finished graph.
 * 2. `orphanedRelationships` — the backend *drops* any relationship whose
 *    endpoint types are not in the ontology, and drops it quietly. So deleting
 *    an entity type deletes relationships too. The editor warns before the
 *    delete instead of letting them vanish on save.
 *
 * The identifier rule is implemented twice, in two languages, which is exactly
 * how implementations drift. `backend/tests/fixtures/identifier_cases.json` is
 * the contract, and both suites assert against it.
 */

const NON_ALNUM = /[^0-9A-Za-z]+/
const CAMEL_BOUNDARY = /(?<=[a-z0-9])(?=[A-Z])/

/**
 * Turn a human phrase into a bare identifier, exactly as the backend does.
 *
 * Returns "" when nothing usable survives — callers treat that as a rejection
 * rather than substituting a placeholder. A leading digit is one such case:
 * "3rd sector" yields nothing, because a label cannot start with a number.
 *
 * @param {string} name
 * @param {boolean} upperFirst  true for entity types (PascalCase),
 *                              false for relationships (UPPER_SNAKE_CASE)
 */
export function toIdentifier(name, upperFirst = true) {
  const words = String(name ?? '')
    .trim()
    .split(NON_ALNUM)
    .filter(Boolean)
  if (!words.length) return ''

  const parts = []
  for (const word of words) {
    // Split existing camelCase so "publicFigure" and "Public Figure" agree.
    parts.push(...word.split(CAMEL_BOUNDARY).filter(Boolean))
  }

  const identifier = upperFirst
    ? parts.map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join('')
    : parts.map((p) => p.toUpperCase()).join('_')

  if (!identifier) return ''
  const first = identifier.charAt(0)
  if (!/[A-Za-z_]/.test(first)) return ''
  return identifier.slice(0, 63)
}

export const entityIdentifier = (name) => toIdentifier(name, true)
export const relationshipIdentifier = (name) => toIdentifier(name, false)

/**
 * Relationships the backend would silently drop, given these entity types.
 *
 * Returns one entry per doomed relationship with the endpoints that are
 * missing, so the warning can name them.
 */
export function orphanedRelationships(ontology) {
  const known = new Set((ontology?.entity_types || []).map((t) => t.name))
  const out = []
  for (const relationship of ontology?.relationship_types || []) {
    const endpoints = [
      ...(relationship.source_types || []),
      ...(relationship.target_types || []),
    ]
    const missing = [...new Set(endpoints.filter((e) => !known.has(e)))]
    if (missing.length) out.push({ relationship, missing })
  }
  return out
}

/** What removing `typeName` would take with it. */
export function relationshipsLostByRemoving(ontology, typeName) {
  const remaining = (ontology?.entity_types || []).filter((t) => t.name !== typeName)
  return orphanedRelationships({ ...ontology, entity_types: remaining })
}

/**
 * Everything wrong with an ontology, as messages. Empty means submittable.
 *
 * This is not the authority — the server revalidates — but submitting a form
 * the server will certainly reject is a wasted round trip and a worse message.
 */
export function validateOntology(ontology) {
  const problems = []
  const entities = ontology?.entity_types || []

  if (!entities.length) {
    problems.push('An ontology needs at least one entity type.')
  }

  const seen = new Set()
  for (const entity of entities) {
    if (!entity.name) {
      problems.push(`"${entity.label || '(unnamed)'}" does not produce a usable type name.`)
      continue
    }
    if (seen.has(entity.name)) {
      problems.push(`${entity.name} appears more than once; the duplicate will be dropped.`)
    }
    seen.add(entity.name)
    if (!entity.description) {
      problems.push(`${entity.name} has no description; the extractor relies on it.`)
    }
  }

  for (const { relationship, missing } of orphanedRelationships(ontology)) {
    problems.push(
      `${relationship.name} points at ${missing.join(', ')}, which ` +
        `${missing.length === 1 ? 'is not' : 'are not'} in the ontology — ` +
        'it will be dropped.',
    )
  }

  return problems
}

/** A blank entity type, for the "add" button. */
export const blankEntityType = () => ({
  name: '',
  label: '',
  description: '',
  attributes: [],
})

export const blankRelationshipType = () => ({
  name: '',
  label: '',
  description: '',
  source_types: [],
  target_types: [],
  attributes: [],
})
