/**
 * @vitest-environment happy-dom
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ConfigEditor from '../../src/components/ConfigEditor.vue'

/**
 * Config form validation, mounted.
 *
 * The module tests prove `validateConfig` and `pruneActions` are right. Only a
 * mounted test can show that the form asks them, that an invalid config cannot
 * be submitted, and that switching platform actually rewrites the action set
 * on screen rather than merely computing a new one.
 */

const CONFIG = () => ({
  event: 'The council published a draft housing density policy.',
  platform: 'twitter',
  rounds: 3,
  hours_per_round: 6,
  notes: '',
  broadcaster: { name: 'Riverbend City News', handle: 'rb_city_news', description: '' },
  action_space: {
    platform: 'twitter',
    actions: ['CREATE_POST', 'LIKE_POST', 'REPOST', 'DO_NOTHING'],
  },
  seed_posts: [
    { attribution: 'broadcaster', content: 'Council publishes a draft policy.',
      speaker: '', demoted_reason: '' },
  ],
  scheduled_events: [
    { round: 1, description: 'The mayor responds.', content: 'A statement.',
      counterfactual: true, enabled: false },
  ],
})

const mountEditor = (overrides = {}) =>
  mount(ConfigEditor, { props: { config: { ...CONFIG(), ...overrides } } })

const saveButton = (wrapper) =>
  wrapper.findAll('button').find((b) => /Save/.test(b.text()))

describe('the form', () => {
  it('offers only the platforms this build supports', () => {
    const options = mountEditor().findAll('select option').map((o) => o.text())
    expect(options).toContain('twitter')
    expect(options).toContain('reddit')
    expect(options).not.toContain('myspace')
  })

  it('shows the round ceiling rather than letting it be discovered', () => {
    expect(mountEditor().text()).toContain('max 10')
  })

  it('renders a checkbox per action the platform has', () => {
    const labels = mountEditor().findAll('fieldset label').map((l) => l.text())
    expect(labels).toContain('CREATE_POST')
    expect(labels).toContain('REPOST')
    expect(labels).not.toContain('CREATE_COMMENT')
  })
})

describe('validation blocks the save', () => {
  it('a sound config can be saved', () => {
    expect(saveButton(mountEditor()).attributes('disabled')).toBeUndefined()
  })

  it('AN INVALID CONFIG CANNOT BE SUBMITTED', async () => {
    const wrapper = mountEditor()
    await wrapper.findAll('input[type=number]')[0].setValue(0)

    expect(wrapper.get('.problems').text()).toContain('Rounds')
    expect(saveButton(wrapper).attributes('disabled')).toBeDefined()
  })

  it('an empty event is refused', async () => {
    const wrapper = mountEditor()
    await wrapper.get('textarea').setValue('   ')

    expect(wrapper.get('.problems').text()).toContain('event')
    expect(saveButton(wrapper).attributes('disabled')).toBeDefined()
  })

  it('AN AGENT WITH NO PERMITTED ACTIONS CANNOT DO ANYTHING', async () => {
    const wrapper = mountEditor()
    for (const box of wrapper.findAll('fieldset input[type=checkbox]')) {
      if (box.element.checked) await box.setValue(false)
    }

    expect(wrapper.get('.problems').text()).toContain('cannot do anything')
    expect(saveButton(wrapper).attributes('disabled')).toBeDefined()
  })

  it('says nothing at all when there is nothing wrong', () => {
    expect(mountEditor().find('.problems').exists()).toBe(false)
  })
})

describe('switching platform', () => {
  it('REWRITES THE ACTION SET ON SCREEN, NOT JUST IN THE DRAFT', async () => {
    const wrapper = mountEditor()
    await wrapper.get('select').setValue('reddit')

    const labels = wrapper.findAll('fieldset label').map((l) => l.text())
    expect(labels).toContain('CREATE_COMMENT')
    expect(labels).not.toContain('REPOST')
  })

  it('says which actions it dropped', async () => {
    const wrapper = mountEditor()
    await wrapper.get('select').setValue('reddit')

    expect(wrapper.get('.notice').text()).toContain('REPOST')
    expect(wrapper.get('.notice').text()).toContain('not available on reddit')
  })

  it('keeps the actions that exist on both', async () => {
    const wrapper = mountEditor()
    await wrapper.get('select').setValue('reddit')

    const checked = wrapper.findAll('fieldset input[type=checkbox]')
      .filter((b) => b.element.checked).length
    expect(checked).toBeGreaterThan(0)
  })

  it('says nothing when a switch drops nothing', async () => {
    const wrapper = mountEditor({
      action_space: { platform: 'twitter', actions: ['CREATE_POST', 'DO_NOTHING'] },
    })
    await wrapper.get('select').setValue('reddit')

    expect(wrapper.find('.notice').exists()).toBe(false)
  })
})

describe('seed posts', () => {
  it('EXPLAINS THAT A NAMED QUOTE IS CHECKED AGAINST THE DOCUMENT', async () => {
    const wrapper = mountEditor()
    const attribution = wrapper.findAll('select').find((s) =>
      s.findAll('option').some((o) => o.text() === 'broadcaster'))
    await attribution.setValue('named')

    expect(wrapper.text()).toContain('checked against the source document')
  })

  it('marks a post the server demoted, with the reason', () => {
    const wrapper = mountEditor({
      seed_posts: [{
        attribution: 'broadcaster', content: 'A quote.', speaker: 'Jane Doe',
        demoted_reason: 'not found in the source document',
      }],
    })

    expect(wrapper.get('.demoted').text()).toContain('not found in the source')
    expect(wrapper.get('.is-demoted').exists()).toBe(true)
  })

  it('refuses a named post that names nobody', async () => {
    const wrapper = mountEditor({
      seed_posts: [{ attribution: 'named', content: 'A quote.', speaker: '',
                     demoted_reason: '' }],
    })

    expect(wrapper.get('.problems').text()).toContain('names nobody')
  })
})

describe('a run that has already started', () => {
  it('SAYS SAVING WILL FORK RATHER THAN EDIT', () => {
    const wrapper = mount(ConfigEditor, {
      props: { config: CONFIG(), locked: true },
    })

    expect(wrapper.text()).toContain('saving creates a copy')
    expect(saveButton(wrapper).text()).toContain('Save as a new simulation')
  })

  it('an unlocked run just saves', () => {
    expect(saveButton(mountEditor()).text()).toContain('Save scenario')
  })
})

describe('saving', () => {
  it('emits the edited draft, not the original prop', async () => {
    const wrapper = mountEditor()
    await wrapper.get('textarea').setValue('A different event entirely.')
    await saveButton(wrapper).trigger('click')

    expect(wrapper.emitted('save')[0][0].event).toBe('A different event entirely.')
  })

  it('DOES NOT MUTATE THE PROP IT WAS GIVEN', async () => {
    // The parent holds the server's copy; editing it in place would make a
    // discarded edit look saved.
    const config = CONFIG()
    const wrapper = mount(ConfigEditor, { props: { config } })
    await wrapper.get('textarea').setValue('Changed.')

    expect(config.event).toBe(CONFIG().event)
  })
})
