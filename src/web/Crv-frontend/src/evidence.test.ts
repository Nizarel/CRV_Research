import { describe, expect, it } from 'vitest'
import {
  citationNumbers,
  formatClaimValue,
  formatPages,
  highlightSegments,
  quotesFor,
  sourceMeta,
  sourceTitle,
} from './evidence'
import type { Answer, Claim, Evidence } from './types'

function evidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    id: 'ev-a',
    release: 'crv-v1',
    chunk_id: 'chunk-a',
    title: 'Rapport de pilotage Tizert',
    content: 'Le rendement atteint 78 % avec 21,5 % Cu au concentré.',
    source_uri: 'https://example.invalid/a',
    source_path: 'Data\\Tizert\\pilotage.pdf',
    document_code: null,
    pages: [4],
    locators: [],
    generated: false,
    document_class: 'report',
    extraction_route: 'di',
    source_role: 'unclassified',
    ...overrides,
  }
}

function claim(overrides: Partial<Claim> = {}): Claim {
  return {
    text: 'Rendement de 78 %',
    evidence_ids: ['ev-a'],
    quote: 'rendement atteint 78 %',
    subject: 'Tizert',
    quantity: 'recovery',
    value: '78',
    unit: '%',
    date: null,
    conditions: null,
    scope: null,
    calculation_index: null,
    qualifier: null,
    ...overrides,
  }
}

function answer(overrides: Partial<Answer> = {}): Answer {
  return {
    status: 'answered',
    content: 'Premier fait [ev-b]. Second fait [ev-a]. Repris [ev-b]. Inconnu [zzz].',
    claims: [claim()],
    citations: [evidence(), evidence({ id: 'ev-b' }), evidence({ id: 'ev-c' })],
    conflicts: [],
    calculations: [],
    coverage: { matched: 0, visited: 0, enumerated: 0, catalogue_complete: false, gaps: [] },
    reasons: [],
    search_release: 'crv-v1',
    kg_release: 'kg-1',
    trace_id: 't',
    usage: {},
    retrieval_mode: 'kg',
    ...overrides,
  }
}

describe('citationNumbers', () => {
  it('numbers sources by first appearance, then the unmentioned ones, ignoring unknown markers', () => {
    const numbers = citationNumbers(answer())
    expect([...numbers.entries()]).toEqual([
      ['ev-b', 1],
      ['ev-a', 2],
      ['ev-c', 3],
    ])
  })
})

describe('sourceTitle', () => {
  it('prefers the title, then the file name, then the code', () => {
    expect(sourceTitle(evidence())).toBe('Rapport de pilotage Tizert')
    expect(sourceTitle(evidence({ title: ' ' }))).toBe('pilotage.pdf')
    expect(sourceTitle(evidence({ title: '', source_path: '', document_code: 'CR-AR-01' }))).toBe('CR-AR-01')
    expect(sourceTitle(evidence({ title: '', source_path: '' }))).toBe('Document source')
  })
})

describe('formatPages', () => {
  it('formats single, contiguous and scattered pages', () => {
    expect(formatPages([])).toBeNull()
    expect(formatPages([4])).toBe('p. 4')
    expect(formatPages([5, 3, 4, 4])).toBe('pp. 3–5')
    expect(formatPages([2, 7])).toBe('pp. 2, 7')
  })
})

describe('sourceMeta', () => {
  it('lists code, pages and warnings', () => {
    expect(sourceMeta(evidence({ document_code: 'CR-01', generated: true }))).toEqual([
      'CR-01',
      'p. 4',
      'description générée',
    ])
  })
})

describe('quotesFor', () => {
  it('returns the distinct quotes of the claims citing a source', () => {
    const result = answer({
      claims: [claim(), claim(), claim({ evidence_ids: ['ev-b'], quote: 'autre' })],
    })
    expect(quotesFor(result, 'ev-a')).toEqual(['rendement atteint 78 %'])
  })
})

describe('highlightSegments', () => {
  it('marks every occurrence of a quote and merges overlaps', () => {
    expect(highlightSegments('a bc d bc', ['bc'])).toEqual([
      { text: 'a ', marked: false },
      { text: 'bc', marked: true },
      { text: ' d ', marked: false },
      { text: 'bc', marked: true },
    ])
    expect(highlightSegments('abcdef', ['abcd', 'cdef'])).toEqual([{ text: 'abcdef', marked: true }])
  })

  it('returns the whole text when no quote matches', () => {
    expect(highlightSegments('texte', ['absent'])).toEqual([{ text: 'texte', marked: false }])
  })
})

describe('formatClaimValue', () => {
  it('renders qualifier, decimal comma and unit', () => {
    expect(formatClaimValue(claim({ value: '71.5', qualifier: '>=' }))).toBe('≥ 71,5 %')
    expect(formatClaimValue(claim({ value: null }))).toBeNull()
  })
})
