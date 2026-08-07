/**
 * Population editing rules, mirrored from the backend.
 *
 * Two of the server's refusals are restated here so the UI can show them as
 * facts about a field rather than as an error after saving:
 *
 * 1. `provenance` and the source links are immutable. They record whether an
 *    agent stands for someone the document actually named or is a plausible
 *    member of the crowd we invented. Relabelling a synthetic agent as `named`
 *    would put invented words in a real person's mouth in every report that
 *    followed, and nothing downstream could tell.
 * 2. A named agent's name is fixed, because it ties that agent to a real
 *    entity in the graph. Synthetic names are free.
 *
 * The server enforces all of this regardless — it overwrites these fields from
 * what it has stored rather than trusting the body. These functions exist so a
 * field that cannot change is not offered as though it could.
 */

/** Mirrors IMMUTABLE_FIELDS in backend/app/api/simulation.py. */
export const IMMUTABLE_FIELDS = ['provenance', 'source_entity_uuid', 'source_entity_type']

/** Fields an operator may edit, in the order they are worth reading. */
export const EDITABLE_TEXT_FIELDS = [
  { key: 'occupation', label: 'Occupation' },
  { key: 'sector', label: 'Sector' },
  { key: 'leanings', label: 'Leanings' },
  { key: 'writing_style', label: 'Writing style' },
  { key: 'gender', label: 'Gender' },
  { key: 'country', label: 'Country' },
]

export const ACTIVITY_LEVELS = ['low', 'moderate', 'high']

export const isNamed = (profile) => profile?.provenance === 'named'

/** A named agent's name is part of its link to the graph. */
export const canRename = (profile) => !isNamed(profile)

export function fieldIsLocked(profile, field) {
  if (IMMUTABLE_FIELDS.includes(field)) return true
  return field === 'name' && isNamed(profile)
}

/** Why a field cannot be edited, for the UI to show next to it. */
export function lockReason(profile, field) {
  if (field === 'provenance') {
    return 'Provenance records whether this agent stands for a real named person or is one we invented. It cannot be changed.'
  }
  if (IMMUTABLE_FIELDS.includes(field)) {
    return 'This links the agent to the entity it came from in the graph.'
  }
  if (field === 'name' && isNamed(profile)) {
    return 'This agent is named after a real entity in the document, so its name is fixed.'
  }
  return ''
}

export function breakdown(profiles) {
  const list = profiles || []
  const named = list.filter(isNamed).length
  return {
    total: list.length,
    named,
    synthetic: list.length - named,
    namedPercent: list.length ? Math.round((named / list.length) * 100) : 0,
  }
}

/**
 * What a save would do, in words, so it can be shown before it happens.
 *
 * Removal is the part worth spelling out: `user_id` is the list index, so
 * dropping an agent renumbers every agent after it.
 */
export function describeChanges({ original, kept, edited }) {
  const removed = (original || []).filter(
    (profile) => !kept.some((k) => k.user_id === profile.user_id),
  )
  const changes = []

  if (edited.length) {
    changes.push(`${edited.length} agent(s) edited`)
  }
  if (removed.length) {
    changes.push(`${removed.length} agent(s) removed`)
  }
  return {
    removed,
    edited,
    changes,
    dirty: Boolean(changes.length),
    renumbers: removed.length > 0,
  }
}

/** Which agents actually differ from what was loaded. */
export function editedProfiles(original, current) {
  const before = new Map((original || []).map((p) => [p.user_id, p]))
  return (current || []).filter((profile) => {
    const was = before.get(profile.user_id)
    if (!was) return false
    return JSON.stringify(was) !== JSON.stringify(profile)
  })
}

/** Match a profile against a free-text query, over the fields worth searching. */
export function matches(profile, query) {
  const needle = String(query || '').trim().toLowerCase()
  if (!needle) return true
  const haystack = [
    profile.name,
    profile.username,
    profile.occupation,
    profile.sector,
    profile.leanings,
    profile.background,
    ...(profile.interests || []),
    ...(profile.traits || []),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return haystack.includes(needle)
}
