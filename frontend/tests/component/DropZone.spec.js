/**
 * @vitest-environment happy-dom
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import DropZone from '../../src/components/DropZone.vue'
import { MAX_UPLOAD_BYTES } from '../../src/api/limits.js'

/**
 * Upload validation, mounted.
 *
 * The module tests prove `validateFile` returns the right verdict. They cannot
 * prove the component asks it, shows the answer, or — the part that actually
 * matters — declines to emit a file it just rejected. A template that dropped
 * the check would pass every one of them.
 */

function fileOf(name, size = 1024) {
  // happy-dom's File does not let `size` be set from content cheaply, so it is
  // defined directly: the component only ever reads `name` and `size`.
  const file = new File(['x'], name, { type: 'text/plain' })
  Object.defineProperty(file, 'size', { value: size })
  return file
}

function dropEvent(files) {
  return { dataTransfer: { files } }
}

describe('what it offers', () => {
  it('names every accepted extension, so the rule is visible before trying', () => {
    const text = mount(DropZone).text()
    for (const extension of ['.txt', '.md', '.markdown', '.pdf']) {
      expect(text).toContain(extension)
    }
  })

  it('names the size limit', () => {
    expect(mount(DropZone).text()).toContain('50.0 MB')
  })

  it('says one document per graph, because that is what the endpoint takes', () => {
    expect(mount(DropZone).text()).toContain('One document per graph')
  })

  it('offers the accepted types to the file picker', () => {
    const accept = mount(DropZone).get('input[type=file]').attributes('accept')
    expect(accept).toContain('.pdf')
    expect(accept).not.toContain('.exe')
  })
})

describe('accepting a file', () => {
  it('emits it and shows its name and size', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('council.txt', 2048)]))

    expect(wrapper.emitted('selected')[0][0].name).toBe('council.txt')
    expect(wrapper.text()).toContain('council.txt')
    expect(wrapper.text()).toContain('2.0 KB')
  })

  it('accepts a file exactly at the limit', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('big.pdf', MAX_UPLOAD_BYTES)]))

    expect(wrapper.emitted('selected')).toBeTruthy()
    expect(wrapper.find('[role=alert]').exists()).toBe(false)
  })
})

describe('refusing a file', () => {
  /* The guarantee is that the parent never *holds* a rejected file — not that
     no event fires. Emitting null is how it is told, and asserting "no event"
     instead would forbid the very mechanism that clears a previous file. */
  const heldByParent = (wrapper) => {
    const emissions = wrapper.emitted('selected')
    return emissions ? emissions.at(-1)[0] : null
  }

  it('A REFUSED FILE NEVER REACHES THE PARENT', async () => {
    // The parent uploads whatever it is handed, so handing over a rejected
    // file would send it regardless of the verdict.
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('payload.exe')]))

    expect(heldByParent(wrapper)).toBeNull()
  })

  it('says which file and why, not just that something was wrong', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('payload.exe')]))

    const alert = wrapper.get('[role=alert]').text()
    expect(alert).toContain('payload.exe')
    expect(alert).toContain('.txt')
  })

  it('refuses one byte over the limit', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop',
      dropEvent([fileOf('big.pdf', MAX_UPLOAD_BYTES + 1)]))

    expect(heldByParent(wrapper)).toBeNull()
    expect(wrapper.get('[role=alert]').text()).toContain('limit')
  })

  it('refuses an empty file rather than building an empty graph', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('empty.txt', 0)]))

    expect(wrapper.get('[role=alert]').text()).toContain('empty')
  })

  it('REFUSES SEVERAL FILES RATHER THAN QUIETLY TAKING THE FIRST', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop',
      dropEvent([fileOf('a.txt'), fileOf('b.txt')]))

    expect(heldByParent(wrapper)).toBeNull()
    expect(wrapper.get('[role=alert]').text()).toContain('2 files')
  })

  it('clears a previous acceptance when a later file is refused', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('good.txt')]))
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('bad.exe')]))

    // The last emission must be null, or the parent still holds the good file
    // while the screen shows a refusal.
    const emissions = wrapper.emitted('selected')
    expect(emissions.at(-1)[0]).toBeNull()
    expect(wrapper.text()).not.toContain('good.txt')
  })
})

describe('changing your mind', () => {
  it('clearing emits nothing selected', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('council.txt')]))
    await wrapper.get('button').trigger('click')

    expect(wrapper.emitted('selected').at(-1)[0]).toBeNull()
  })
})

describe('while busy', () => {
  it('does not offer to change the file mid-upload', () => {
    const wrapper = mount(DropZone, { props: { busy: true } })
    expect(wrapper.get('input[type=file]').attributes('disabled')).toBeDefined()
  })
})

describe('drag feedback', () => {
  it('marks the zone while a file is over it, and unmarks on leave', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('dragover')
    expect(wrapper.get('.drop').classes()).toContain('is-dragging')

    await wrapper.get('.drop').trigger('dragleave')
    expect(wrapper.get('.drop').classes()).not.toContain('is-dragging')
  })

  it('stops dragging once something is dropped', async () => {
    const wrapper = mount(DropZone)
    await wrapper.get('.drop').trigger('dragover')
    await wrapper.get('.drop').trigger('drop', dropEvent([fileOf('a.txt')]))

    expect(wrapper.get('.drop').classes()).not.toContain('is-dragging')
  })
})
