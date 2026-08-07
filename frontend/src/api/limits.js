/**
 * What the backend will accept, mirrored for client-side validation.
 *
 * Validating in the browser is a courtesy, not a control — the server rejects
 * the same things regardless, and it is the one that decides. The point is to
 * refuse a 40 MB PDF before it is uploaded over a slow link, and to say why
 * immediately instead of after the round trip.
 *
 * The risk of mirroring is drift: a UI that accepts what the server refuses is
 * worse than no validation at all, because the failure arrives late and looks
 * like a bug. So these mirror `ALLOWED_EXTENSIONS` and `MAX_CONTENT_LENGTH`
 * exactly, and `scripts/verify_frontend.sh` checks them against the running
 * config rather than trusting this comment.
 */

/** Mirrors Config.ALLOWED_EXTENSIONS. */
export const ALLOWED_EXTENSIONS = ['markdown', 'md', 'pdf', 'txt']

/** Mirrors Config.MAX_CONTENT_LENGTH — 50 MiB. */
export const MAX_UPLOAD_BYTES = 52_428_800

/** For the file picker's `accept`, which wants dotted extensions. */
export const ACCEPT_ATTRIBUTE = ALLOWED_EXTENSIONS.map((e) => `.${e}`).join(',')

export function extensionOf(filename) {
  const name = String(filename || '')
  const dot = name.lastIndexOf('.')
  // A leading dot is a hidden file, not an extension: ".txt" has no extension.
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : ''
}

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * Check one file. Returns `{ ok: true }` or `{ ok: false, reason }`.
 *
 * The reason is written to be shown to a person, so it names the file and what
 * was wrong with it rather than restating the rule in the abstract.
 */
export function validateFile(file) {
  if (!file) return { ok: false, reason: 'No file was chosen.' }

  const extension = extensionOf(file.name)
  if (!extension) {
    return { ok: false, reason: `${file.name} has no file extension.` }
  }
  if (!ALLOWED_EXTENSIONS.includes(extension)) {
    return {
      ok: false,
      reason:
        `${file.name} is a .${extension} file. ` +
        `Accepted: ${ALLOWED_EXTENSIONS.map((e) => `.${e}`).join(', ')}.`,
    }
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return {
      ok: false,
      reason:
        `${file.name} is ${formatBytes(file.size)}, over the ` +
        `${formatBytes(MAX_UPLOAD_BYTES)} limit.`,
    }
  }
  // A zero-byte file parses to nothing and produces an empty ontology, which
  // is a confusing way to find out the file was empty.
  if (file.size === 0) {
    return { ok: false, reason: `${file.name} is empty.` }
  }
  return { ok: true }
}

/**
 * Check a drop. A graph is built from exactly one document, so more than one
 * file is refused here rather than silently using the first — which is what
 * the endpoint does too, and for the same reason.
 */
export function validateDrop(files) {
  const list = Array.from(files || [])
  if (list.length === 0) return { ok: false, reason: 'No file was dropped.' }
  if (list.length > 1) {
    return {
      ok: false,
      reason:
        `${list.length} files were dropped. A graph is built from one ` +
        'document — drop a single file, or build a graph per document.',
    }
  }
  return validateFile(list[0])
}
