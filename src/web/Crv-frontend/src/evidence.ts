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

const LIMITS: Record<string, string> = {
  'Statements the verifier found unsupported were removed.':
    'Des affirmations que le vérificateur jugeait non étayées ont été retirées.',
  'The synthesis failed verification; only verified findings are listed.':
    "La synthèse n'a pas passé la vérification ; seuls les constats vérifiés sont listés.",
  'The turn budget ran out before an answer could be verified.':
    "Le budget de la recherche a été épuisé avant qu'une réponse puisse être vérifiée.",
  'Top-k evidence retrieval is not an exhaustive corpus review.':
    "La recherche porte sur les meilleurs passages, pas sur une revue exhaustive du corpus.",
  'Top-50 candidate search per query; not an exhaustive document review.':
    "Recherche limitée aux 50 meilleurs candidats par requête, sans revue exhaustive des documents.",
  'A worker claim failed source/locator/value validation.':
    "Un constat extrait n'a pas passé la validation de la source, de l'emplacement ou de la valeur.",
  'A research facet exhausted the shared execution budget.':
    "Une sous-question a épuisé le budget d'exécution partagé.",
  'Evidence retrieval did not inspect every enumerated document.':
    "La recherche n'a pas examiné tous les documents recensés.",
  'KG enumeration excludes collections not represented in the pinned graph.':
    "Le recensement exclut les collections absentes du graphe de connaissances utilisé.",
  'KG-2 studies chose the documents of one search; cited text only.':
    "Une recherche a été restreinte aux documents choisis via le graphe de connaissances ; seul le texte cité fait foi.",
  'A rewritten search returned no new candidates.':
    "Une recherche reformulée n'a retourné aucun nouveau candidat.",
  'An extraction batch failed; some retrieved passages were not read.':
    "Un lot d'extraction a échoué ; certains passages retrouvés n'ont pas été lus.",
  'Some KG documents have no Search-compatible content hash.':
    "Certains documents du graphe n'ont pas d'empreinte compatible avec la recherche.",
  'Catalogue details limited to 12 documents; narrow the request.':
    'Détail du catalogue limité à 12 documents ; précisez la demande.',
  'Search-only baseline; no KG catalogue enumeration or navigation.':
    'Recherche seule : sans recensement ni navigation dans le graphe de connaissances.',
}

/** French text for the backend's fixed English coverage notes; other notes pass through. */
export function localizeLimit(text: string): string {
  const known = LIMITS[text]
  if (known) return known
  const candidate = /^Candidate entity mapping: (.+)$/.exec(text)
  if (candidate) return `Entité candidate retenue pour la navigation : ${candidate[1]}.`
  const failed = /^A research facet failed \((.+)\); its evidence is missing\.$/.exec(text)
  if (failed) return `Une sous-question a échoué (${failed[1]}) ; ses preuves manquent.`
  return text
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
