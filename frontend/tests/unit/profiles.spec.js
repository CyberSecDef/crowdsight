import { describe, expect, it } from 'vitest'

import {
  ACTIVITY_LEVELS,
  IMMUTABLE_FIELDS,
  breakdown,
  canRename,
  describeChanges,
  editedProfiles,
  fieldIsLocked,
  isNamed,
  lockReason,
  matches,
} from '../../src/api/profiles.js'

/**
 * Population editing rules.
 *
 * The immutability rules are the ones worth testing hardest. The server
 * enforces them regardless — it overwrites those fields from what it has
 * stored rather than trusting the body — so these tests are about the UI not
 * *offering* an edit that will be silently discarded, which is its own kind of
 * lie.
 */

const NAMED = {
  user_id: 1,
  name: 'Jane Doe',
  username: 'jane_doe',
  occupation: 'councillor',
  provenance: 'named',
  source_entity_uuid: 'e-1',
  source_entity_type: 'Person',
  interests: ['housing'],
}

const SYNTHETIC = {
  user_id: 0,
  name: 'Dawn Mercer',
  username: 'dawn_mercer',
  occupation: 'carpenter',
  provenance: 'synthetic',
  source_entity_uuid: null,
  interests: ['woodwork', 'planning'],
  traits: ['blunt'],
}

describe('provenance', () => {
  it('recognises a named agent', () => {
    expect(isNamed(NAMED)).toBe(true)
    expect(isNamed(SYNTHETIC)).toBe(false)
  })

  it.each(IMMUTABLE_FIELDS)('%s is locked on every agent', (field) => {
    expect(fieldIsLocked(NAMED, field)).toBe(true)
    expect(fieldIsLocked(SYNTHETIC, field)).toBe(true)
  })

  it('A NAMED AGENT CANNOT BE RENAMED', () => {
    // The name ties the agent to a real entity in the graph.
    expect(canRename(NAMED)).toBe(false)
    expect(fieldIsLocked(NAMED, 'name')).toBe(true)
  })

  it('a synthetic agent can be renamed', () => {
    expect(canRename(SYNTHETIC)).toBe(true)
    expect(fieldIsLocked(SYNTHETIC, 'name')).toBe(false)
  })

  it('an ordinary field is not locked on either', () => {
    for (const profile of [NAMED, SYNTHETIC]) {
      expect(fieldIsLocked(profile, 'occupation')).toBe(false)
      expect(fieldIsLocked(profile, 'activity_level')).toBe(false)
    }
  })

  it('explains why provenance is fixed, in terms of what it means', () => {
    expect(lockReason(NAMED, 'provenance')).toMatch(/real named person|invented/)
  })

  it('explains why a named agent keeps its name', () => {
    expect(lockReason(NAMED, 'name')).toMatch(/real entity/)
  })

  it('gives no reason for a field that is not locked', () => {
    expect(lockReason(SYNTHETIC, 'occupation')).toBe('')
  })
})

describe('breakdown', () => {
  it('counts named and synthetic separately', () => {
    const stats = breakdown([NAMED, SYNTHETIC, SYNTHETIC])
    expect(stats).toMatchObject({ total: 3, named: 1, synthetic: 2 })
  })

  it('reports a percentage for the bar', () => {
    expect(breakdown([NAMED, SYNTHETIC]).namedPercent).toBe(50)
  })

  it('does not divide by zero on an empty population', () => {
    expect(breakdown([])).toMatchObject({ total: 0, named: 0, namedPercent: 0 })
  })

  it('handles nothing at all', () => {
    expect(breakdown(null).total).toBe(0)
  })
})

describe('editedProfiles', () => {
  it('finds nothing when nothing changed', () => {
    const original = [NAMED, SYNTHETIC]
    expect(editedProfiles(original, JSON.parse(JSON.stringify(original)))).toEqual([])
  })

  it('finds the agent that changed', () => {
    const current = [NAMED, { ...SYNTHETIC, occupation: 'joiner' }]
    const edited = editedProfiles([NAMED, SYNTHETIC], current)
    expect(edited).toHaveLength(1)
    expect(edited[0].user_id).toBe(0)
  })

  it('ignores an agent that is no longer present', () => {
    expect(editedProfiles([NAMED, SYNTHETIC], [NAMED])).toEqual([])
  })
})

describe('describeChanges', () => {
  const original = [NAMED, SYNTHETIC]

  it('reports a clean population as not dirty', () => {
    const result = describeChanges({ original, kept: original, edited: [] })
    expect(result.dirty).toBe(false)
    expect(result.renumbers).toBe(false)
  })

  it('REPORTS THAT A REMOVAL RENUMBERS THE REST', () => {
    // user_id is the list index, so everything after a removal shifts.
    const result = describeChanges({ original, kept: [NAMED], edited: [] })
    expect(result.removed).toHaveLength(1)
    expect(result.renumbers).toBe(true)
    expect(result.changes.join(' ')).toContain('removed')
  })

  it('an edit alone does not renumber anything', () => {
    const edited = [{ ...SYNTHETIC, occupation: 'joiner' }]
    const result = describeChanges({ original, kept: [NAMED, edited[0]], edited })
    expect(result.dirty).toBe(true)
    expect(result.renumbers).toBe(false)
  })

  it('describes edits and removals together', () => {
    const edited = [{ ...NAMED, occupation: 'chair' }]
    const result = describeChanges({ original, kept: edited, edited })
    expect(result.changes.join(' · ')).toMatch(/edited.*removed/)
  })
})

describe('matches', () => {
  it('matches everything on an empty query', () => {
    expect(matches(SYNTHETIC, '')).toBe(true)
    expect(matches(SYNTHETIC, '   ')).toBe(true)
  })

  it.each(['carpenter', 'CARPENTER', 'dawn', 'woodwork', 'blunt'])(
    'matches on %s',
    (query) => {
      expect(matches(SYNTHETIC, query)).toBe(true)
    },
  )

  it('does not match something absent', () => {
    expect(matches(SYNTHETIC, 'astronaut')).toBe(false)
  })

  it('survives a profile with missing fields', () => {
    expect(matches({ name: 'X' }, 'x')).toBe(true)
  })
})

describe('the activity levels offered', () => {
  it('are the three the backend accepts', () => {
    expect(ACTIVITY_LEVELS).toEqual(['low', 'moderate', 'high'])
  })
})
