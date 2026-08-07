import { describe, expect, it } from 'vitest'

import {
  ACCEPT_ATTRIBUTE,
  ALLOWED_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  extensionOf,
  formatBytes,
  validateDrop,
  validateFile,
} from '../../src/api/limits.js'

/**
 * Client-side upload validation.
 *
 * This mirrors the server's rules, and a mirror that has drifted is worse than
 * no mirror at all: the UI accepts a file, the upload runs, and the refusal
 * arrives late looking like a bug. scripts/verify_frontend.sh checks these
 * constants against the running config; these tests cover the logic around them.
 */

const file = (name, size = 1024) => ({ name, size })

describe('extensionOf', () => {
  it.each([
    ['council.txt', 'txt'],
    ['NOTES.TXT', 'txt'],
    ['report.final.pdf', 'pdf'],
    ['readme.md', 'md'],
    ['no-extension', ''],
    ['.hidden', ''],
    ['', ''],
  ])('%j -> %j', (name, expected) => {
    expect(extensionOf(name)).toBe(expected)
  })
})

describe('validateFile', () => {
  it.each(ALLOWED_EXTENSIONS)('accepts .%s', (extension) => {
    expect(validateFile(file(`doc.${extension}`)).ok).toBe(true)
  })

  it.each(['exe', 'zip', 'docx', 'csv', 'js'])('refuses .%s', (extension) => {
    const result = validateFile(file(`doc.${extension}`))
    expect(result.ok).toBe(false)
    expect(result.reason).toContain(extension)
  })

  it('names the file and the accepted list when refusing a type', () => {
    const { reason } = validateFile(file('payload.exe'))
    expect(reason).toContain('payload.exe')
    expect(reason).toContain('.txt')
  })

  it('accepts a file at exactly the limit', () => {
    expect(validateFile(file('big.pdf', MAX_UPLOAD_BYTES)).ok).toBe(true)
  })

  it('REFUSES A FILE ONE BYTE OVER THE LIMIT', () => {
    const result = validateFile(file('big.pdf', MAX_UPLOAD_BYTES + 1))
    expect(result.ok).toBe(false)
    expect(result.reason).toContain('limit')
  })

  it('refuses an empty file rather than building an empty graph from it', () => {
    const result = validateFile(file('empty.txt', 0))
    expect(result.ok).toBe(false)
    expect(result.reason).toContain('empty')
  })

  it('refuses a file with no extension at all', () => {
    expect(validateFile(file('README')).ok).toBe(false)
  })

  it('refuses nothing at all', () => {
    expect(validateFile(null).ok).toBe(false)
  })
})

describe('validateDrop', () => {
  it('accepts exactly one good file', () => {
    expect(validateDrop([file('a.txt')]).ok).toBe(true)
  })

  it('REFUSES SEVERAL FILES RATHER THAN SILENTLY TAKING THE FIRST', () => {
    // A graph is built from one document. Taking the first would quietly
    // discard the rest, which is what the endpoint refuses to do too.
    const result = validateDrop([file('a.txt'), file('b.txt')])
    expect(result.ok).toBe(false)
    expect(result.reason).toContain('2 files')
  })

  it('refuses an empty drop', () => {
    expect(validateDrop([]).ok).toBe(false)
  })

  it('applies the same file rules to a drop', () => {
    expect(validateDrop([file('a.exe')]).ok).toBe(false)
  })
})

describe('the accept attribute offers exactly the allowed types', () => {
  it('is dotted and complete', () => {
    for (const extension of ALLOWED_EXTENSIONS) {
      expect(ACCEPT_ATTRIBUTE).toContain(`.${extension}`)
    }
  })
})

describe('formatBytes', () => {
  it.each([
    [512, '512 B'],
    [2048, '2.0 KB'],
    [52_428_800, '50.0 MB'],
  ])('%i -> %s', (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected)
  })
})
