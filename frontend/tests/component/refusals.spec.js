/**
 * @vitest-environment happy-dom
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import CitationLink from '../../src/components/CitationLink.vue'
import OntologyEditor from '../../src/components/OntologyEditor.vue'
import ProfileCard from '../../src/components/ProfileCard.vue'

/**
 * The components that render a refusal.
 *
 * Each of these shows the operator something the server would otherwise
 * enforce silently: a field that cannot be edited, a name that will become
 * something else, a citation that resolves to nothing. A template change could
 * drop any of them and every module test would still pass — the rule would
 * still be *computed* correctly, just never shown. Mounting is the only way to
 * catch that.
 */

// --------------------------------------------------------------------------
// OntologyEditor — a typed name becomes an identifier
// --------------------------------------------------------------------------

const ONTOLOGY = () => ({
  domain: 'Housing',
  entity_types: [
    { name: 'Person', label: 'Person', description: 'A person', attributes: [] },
    { name: 'Council', label: 'Council', description: 'A council', attributes: [] },
  ],
  relationship_types: [
    { name: 'WORKS_FOR', label: 'WORKS_FOR', description: 'employment',
      source_types: ['Person'], target_types: ['Council'], attributes: [] },
  ],
})

describe('OntologyEditor', () => {
  it('SHOWS WHAT A TYPED NAME WILL BECOME, BEFORE IT IS SUBMITTED', async () => {
    const wrapper = mount(OntologyEditor, { props: { ontology: ONTOLOGY() } })
    await wrapper.findAll('.type input[type=text]')[0].setValue('Public Figure')

    expect(wrapper.findAll('.identifier')[0].text()).toBe('PublicFigure')
  })

  it('says plainly when a name produces nothing usable', async () => {
    const wrapper = mount(OntologyEditor, { props: { ontology: ONTOLOGY() } })
    await wrapper.findAll('.type input[type=text]')[0].setValue('3rd sector')

    const identifier = wrapper.findAll('.identifier')[0]
    expect(identifier.text()).toBe('not a usable name')
    expect(identifier.classes()).toContain('is-bad')
  })

  it('WARNS BEFORE A REMOVAL SILENTLY TAKES RELATIONSHIPS WITH IT', async () => {
    // The backend drops relationships whose endpoints are gone, without
    // saying so. Removing Council orphans WORKS_FOR.
    const confirm = vi.fn(() => false)
    vi.stubGlobal('confirm', confirm)

    const wrapper = mount(OntologyEditor, { props: { ontology: ONTOLOGY() } })
    const remove = wrapper.findAll('.type button').filter((b) => b.text() === 'Remove')
    await remove[1].trigger('click')

    expect(confirm).toHaveBeenCalled()
    expect(confirm.mock.calls[0][0]).toContain('WORKS_FOR')
    // Declining leaves everything alone.
    expect(wrapper.text()).toContain('Council')
    vi.unstubAllGlobals()
  })

  it('does not ask when a removal costs nothing', async () => {
    const confirm = vi.fn(() => true)
    vi.stubGlobal('confirm', confirm)

    const ontology = ONTOLOGY()
    ontology.entity_types.push({ name: 'Ward', label: 'Ward', description: 'a ward',
                                 attributes: [] })
    const wrapper = mount(OntologyEditor, { props: { ontology } })
    const remove = wrapper.findAll('.type button').filter((b) => b.text() === 'Remove')
    await remove[2].trigger('click')

    expect(confirm).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('renaming a type carries its relationships along', async () => {
    const wrapper = mount(OntologyEditor, { props: { ontology: ONTOLOGY() } })
    await wrapper.findAll('.type input[type=text]')[0].setValue('Individual')

    // No orphan warning appears, because the endpoint followed the rename.
    expect(wrapper.find('.problems').exists()).toBe(false)
  })
})

// --------------------------------------------------------------------------
// ProfileCard — provenance cannot be edited
// --------------------------------------------------------------------------

const NAMED = {
  user_id: 1, name: 'Jane Doe', username: 'jane_doe', age: 47,
  occupation: 'councillor', background: 'Chairs the committee.',
  provenance: 'named', activity_level: 'moderate',
  source_entity_uuid: 'e-1', source_entity_type: 'Person',
  interests: ['housing'], traits: [],
}

const SYNTHETIC = { ...NAMED, user_id: 0, name: 'Dawn Mercer', username: 'dawn_mercer',
                    provenance: 'synthetic', source_entity_uuid: null,
                    source_entity_type: null }

describe('ProfileCard', () => {
  it('shows provenance without the card being opened', () => {
    const wrapper = mount(ProfileCard, { props: { profile: NAMED } })
    expect(wrapper.get('.tag').text()).toBe('named')
  })

  it('PROVENANCE IS RENDERED AS A FACT, NEVER AS AN INPUT', () => {
    // The server overwrites it from what it has stored, so an input here would
    // offer an edit that is silently discarded.
    const wrapper = mount(ProfileCard, { props: { profile: SYNTHETIC, open: true } })

    expect(wrapper.get('.locked').text()).toContain('synthetic')
    expect(wrapper.find('.locked input').exists()).toBe(false)
  })

  it('A NAMED AGENT OFFERS NO NAME INPUT, AND SAYS WHY', () => {
    const wrapper = mount(ProfileCard, { props: { profile: NAMED, open: true } })

    expect(wrapper.text()).toContain('named after a real entity')
    const labels = wrapper.findAll('label').filter((l) => l.text().startsWith('Name'))
    expect(labels[0].find('input').exists()).toBe(false)
  })

  it('a synthetic agent can be renamed', () => {
    const wrapper = mount(ProfileCard, { props: { profile: SYNTHETIC, open: true } })
    const labels = wrapper.findAll('label').filter((l) => l.text().startsWith('Name'))

    expect(labels[0].find('input').exists()).toBe(true)
  })

  it('emits the whole edited profile, so the parent can match it by id', async () => {
    const wrapper = mount(ProfileCard, { props: { profile: SYNTHETIC, open: true } })
    const occupation = wrapper.findAll('label')
      .find((l) => l.text().startsWith('Occupation')).get('input')
    await occupation.setValue('boat builder')

    const emitted = wrapper.emitted('update')[0][0]
    expect(emitted.user_id).toBe(0)
    expect(emitted.occupation).toBe('boat builder')
  })

  it('a removed agent says what will happen and offers a way back', () => {
    const wrapper = mount(ProfileCard, { props: { profile: SYNTHETIC, removed: true } })

    expect(wrapper.text()).toContain('Will be removed when you save')
    expect(wrapper.findAll('button').some((b) => /Keep after all/.test(b.text()))).toBe(true)
  })
})

// --------------------------------------------------------------------------
// CitationLink — a citation that resolves to nothing
// --------------------------------------------------------------------------

describe('CitationLink', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  const mountCitation = (citation) =>
    mount(CitationLink, { props: { citation, simId: 'sim-1' } })

  it('AN UNCITED CLAIM SAYS SO RATHER THAN SHOWING NOTHING', () => {
    // Grounding keeps uncited claims deliberately: showing no working is not
    // the same as being wrong, and a blank space says neither.
    const wrapper = mountCitation({ post_ids: [], agent_ids: [], rounds: [] })

    expect(wrapper.get('.uncited').text()).toContain('No evidence cited')
  })

  it('lists what a claim rests on before it is opened', () => {
    const wrapper = mountCitation({ post_ids: [4, 12], agent_ids: [0], rounds: [2] })
    const summary = wrapper.get('.evidence').text()

    expect(summary).toContain('posts 4, 12')
    expect(summary).toContain('agent 0')
    expect(summary).toContain('round 2')
  })

  it('fetches the cited posts by id when opened', async () => {
    const fetched = []
    global.fetch = vi.fn(async (url) => {
      fetched.push(url)
      return {
        ok: true, status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ posts: [{ post_id: 4, name: 'Dawn', username: 'dawn',
                                       round: 1, kind: 'original', engagement: 3,
                                       content: 'This will ruin the corridor.' }] }),
      }
    })

    const wrapper = mountCitation({ post_ids: [4] })
    await wrapper.get('.evidence').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(fetched[0]).toContain('post_ids=4')
    expect(wrapper.get('.post .content').text()).toContain('ruin the corridor')
    delete global.fetch
  })

  it('SAYS SO WHEN A CITED POST CANNOT BE FOUND', async () => {
    // Silence here would hide exactly the failure verification exists to catch.
    global.fetch = vi.fn(async () => ({
      ok: true, status: 200,
      headers: new Map([['content-type', 'application/json']]),
      json: async () => ({ posts: [] }),
    }))

    const wrapper = mountCitation({ post_ids: [999] })
    await wrapper.get('.evidence').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(wrapper.text()).toContain('could not be found in this run')
    delete global.fetch
  })
})
