import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  entityIdentifier,
  orphanedRelationships,
  relationshipIdentifier,
  relationshipsLostByRemoving,
  toIdentifier,
  validateOntology,
} from '../../src/api/ontology.js'

/**
 * The identifier rule is implemented twice — Python for storage, JavaScript so
 * the editor can show what a typed name will become. Two implementations of one
 * rule drift, so both suites assert against the same fixture. If this file and
 * backend/tests/test_ontology_generator.py disagree, one of them is wrong and
 * the graph gets type names the operator did not choose.
 */
const CASES = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('../../../backend/tests/fixtures/identifier_cases.json', import.meta.url)),
    'utf8',
  ),
)

describe('toIdentifier matches the backend', () => {
  it.each(CASES.entity)('entity %j -> %j', (input, expected) => {
    expect(entityIdentifier(input)).toBe(expected)
  })

  it.each(CASES.relationship)('relationship %j -> %j', (input, expected) => {
    expect(relationshipIdentifier(input)).toBe(expected)
  })

  it('caps a name at the length the backend caps it at', () => {
    expect(toIdentifier('a'.repeat(200)).length).toBe(63)
  })

  it('REFUSES A NAME THAT WOULD START WITH A DIGIT', () => {
    // A label cannot begin with a number, so the backend returns nothing at
    // all rather than mangling it into something storable.
    expect(entityIdentifier('3rd sector')).toBe('')
  })

  it('handles null and undefined without throwing', () => {
    expect(entityIdentifier(null)).toBe('')
    expect(entityIdentifier(undefined)).toBe('')
  })
})

const ONTOLOGY = () => ({
  domain: 'Housing',
  entity_types: [
    { name: 'Person', label: 'Person', description: 'A person', attributes: [] },
    { name: 'Council', label: 'Council', description: 'A council', attributes: [] },
  ],
  relationship_types: [
    {
      name: 'WORKS_FOR',
      description: 'employment',
      source_types: ['Person'],
      target_types: ['Council'],
      attributes: [],
    },
  ],
})

describe('relationships the backend would silently drop', () => {
  it('finds none when every endpoint exists', () => {
    expect(orphanedRelationships(ONTOLOGY())).toEqual([])
  })

  it('NAMES THE MISSING ENDPOINT RATHER THAN JUST FLAGGING THE RELATIONSHIP', () => {
    const ontology = ONTOLOGY()
    ontology.relationship_types[0].target_types = ['Committee']
    const [orphan] = orphanedRelationships(ontology)

    expect(orphan.relationship.name).toBe('WORKS_FOR')
    expect(orphan.missing).toEqual(['Committee'])
  })

  it('reports what removing an entity type would take with it', () => {
    const lost = relationshipsLostByRemoving(ONTOLOGY(), 'Council')
    expect(lost).toHaveLength(1)
    expect(lost[0].relationship.name).toBe('WORKS_FOR')
  })

  it('reports nothing lost when the type is unused', () => {
    const ontology = ONTOLOGY()
    ontology.entity_types.push({ name: 'Ward', description: 'a ward', attributes: [] })
    expect(relationshipsLostByRemoving(ontology, 'Ward')).toEqual([])
  })
})

describe('validateOntology', () => {
  it('passes a sound ontology', () => {
    expect(validateOntology(ONTOLOGY())).toEqual([])
  })

  it('refuses an ontology with no entity types', () => {
    const problems = validateOntology({ entity_types: [], relationship_types: [] })
    expect(problems.join(' ')).toContain('at least one entity type')
  })

  it('flags a name that produced no usable identifier', () => {
    const ontology = ONTOLOGY()
    ontology.entity_types[0] = { name: '', label: '!!!', description: 'x', attributes: [] }
    expect(validateOntology(ontology).join(' ')).toContain('usable type name')
  })

  it('flags a duplicate, because the backend keeps only the first', () => {
    const ontology = ONTOLOGY()
    ontology.entity_types.push({ name: 'Person', description: 'again', attributes: [] })
    expect(validateOntology(ontology).join(' ')).toContain('more than once')
  })

  it('flags a missing description, which the extractor relies on', () => {
    const ontology = ONTOLOGY()
    ontology.entity_types[0].description = ''
    expect(validateOntology(ontology).join(' ')).toContain('no description')
  })

  it('warns that an orphaned relationship will be dropped', () => {
    const ontology = ONTOLOGY()
    ontology.relationship_types[0].source_types = ['Ghost']
    expect(validateOntology(ontology).join(' ')).toContain('will be dropped')
  })
})
