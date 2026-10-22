import type { Answer, Claim, Evidence } from './types'

function fileName(path: string): string | null {
  const name = path.split(/[\\/]/).pop()?.trim()
  return name || null
}

/** Human title of a source: its document title, else the original file name, else its code. */
export function sourceTitle(evidence: Evidence): string {
  return (
    evidence.title.trim() ||
    fileName(evidence.source_path) ||
    evidence.document_code ||
    'Document source'
  )
}

/** `p. 3`, `pp. 3–5` or `pp. 3, 7, 9` for a sorted, de-duplicated page list. */
export function formatPages(pages: number[]): string | null {
  const sorted = [...new Set(pages)].sort((a, b) => a - b)
  if (sorted.length === 0) return null
  if (sorted.length === 1) return `p. ${sorted[0]}`
  const contiguous = sorted.every((page, index) => index === 0 || page === sorted[index - 1] + 1)
  return contiguous
    ? `pp. ${sorted[0]}–${sorted[sorted.length - 1]}`
    : `pp. ${sorted.join(', ')}`
}

/** Short qualifiers shown next to a source title. */
export function sourceMeta(evidence: Evidence): string[] {
  const parts = [
    evidence.document_code && evidence.document_code !== evidence.title.trim()
      ? evidence.document_code
      : null,
    formatPages(evidence.pages),
    evidence.generated ? 'description générée' : null,
    evidence.source_role === 'secondary_summary' ? 'synthèse secondaire' : null,
  ]
  return parts.filter((part): part is string => Boolean(part))
}

const MARKER = /\[([^[\]\n]+)\]/g

/** Display number (1, 2, …) of each cited source, in order of first appearance in the answer
 * text; sources the text never mentions are numbered after the others. */
export function citationNumbers(answer: Answer): Map<string, number> {
  const known = new Set(answer.citations.map((citation) => citation.id))
  const numbers = new Map<string, number>()
  for (const match of answer.content.matchAll(MARKER)) {
    const id = match[1]
    if (known.has(id) && !numbers.has(id)) numbers.set(id, numbers.size + 1)
  }
  for (const citation of answer.citations) {
    if (!numbers.has(citation.id)) numbers.set(citation.id, numbers.size + 1)
  }
  return numbers
}

/** Exact quotations the answer relies on from one source. */
export function quotesFor(answer: Answer, evidenceId: string): string[] {
  const quotes = answer.claims
    .filter((claim) => claim.evidence_ids.includes(evidenceId))
    .map((claim) => claim.quote.trim())
    .filter(Boolean)
  return [...new Set(quotes)]
}

export interface Segment {
  text: string
  marked: boolean
}

/** Splits `content` so that every occurrence of a quote is its own marked segment. */
export function highlightSegments(content: string, quotes: string[]): Segment[] {
  const ranges: Array<[number, number]> = []
  for (const quote of quotes) {
    if (!quote) continue
    let from = 0
    for (;;) {
      const start = content.indexOf(quote, from)
      if (start < 0) break
      ranges.push([start, start + quote.length])
      from = start + quote.length
    }
  }
  ranges.sort((a, b) => a[0] - b[0])
  const merged: Array<[number, number]> = []
  for (const range of ranges) {
    const last = merged[merged.length - 1]
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1])
    else merged.push([...range])
  }
  const segments: Segment[] = []
  let cursor = 0
  for (const [start, end] of merged) {
    if (start > cursor) segments.push({ text: content.slice(cursor, start), marked: false })
    segments.push({ text: content.slice(start, end), marked: true })
    cursor = end
  }
  if (cursor < content.length) segments.push({ text: content.slice(cursor), marked: false })
  return segments
}

/** `≥ 71,5 %` style rendering of a claim's numeric value, or null for qualitative claims. */
export function formatClaimValue(claim: Claim): string | null {
  if (claim.value === null || claim.value === '') return null
  return formatQuantity(claim.value, claim.unit, claim.qualifier)
}

const QUALIFIERS: Record<string, string> = { '<': '<', '<=': '≤', '>': '>', '>=': '≥', '~': '≈' }

export function formatQuantity(
  value: string | number,
  unit: string | null,
  qualifier: string | null = null,
): string {
  const number = String(value).replace('.', ',')
  const prefix = qualifier ? `${QUALIFIERS[qualifier] ?? qualifier} ` : ''
  return `${prefix}${number}${unit ? ` ${unit}` : ''}`
}
