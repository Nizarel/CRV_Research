import { citationNumbers, sourceMeta, sourceTitle } from '../evidence'
import type { Answer, Evidence } from '../types'
import { Icon } from './Icon'
import './CitationList.css'

interface CitationListProps {
  answer: Answer
  onOpenEvidence: (evidence: Evidence) => void
}

export function CitationList({ answer, onOpenEvidence }: CitationListProps) {
  if (answer.citations.length === 0) return null
  const numbers = citationNumbers(answer)
  const sources = [...answer.citations].sort(
    (a, b) => (numbers.get(a.id) ?? 0) - (numbers.get(b.id) ?? 0),
  )

  return (
    <section className="source-list" aria-label="Sources">
      <h3 className="source-list-title">
        Sources <span className="count-pill">{sources.length}</span>
      </h3>
      <ol>
        {sources.map((evidence) => {
          const title = sourceTitle(evidence)
          const meta = sourceMeta(evidence).join(' · ')
          return (
            <li key={evidence.id} data-ref={evidence.id}>
              <button
                type="button"
                className="source-item"
                onClick={() => onOpenEvidence(evidence)}
                title="Voir le passage cité"
              >
                <span className="ref-chip ref-chip-static">{numbers.get(evidence.id)}</span>
                <span className="source-item-text">
                  <span className="source-item-title" title={title}>
                    {title}
                  </span>
                  {meta && <span className="source-item-meta">{meta}</span>}
                </span>
                <span className="source-item-open">
                  <Icon name="expand" size={13} />
                </span>
              </button>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
