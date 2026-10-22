import { formatPages, formatQuantity } from '../evidence'
import type { FacetFinding, FacetSource, ProgressStep, ResearchPlan } from '../types'
import './ResearchProgress.css'

const STAGES: Array<{ after: ProgressStep; label: string }> = [
  { after: 'started', label: 'Analyse de la question' },
  { after: 'understand', label: 'Recherche dans le corpus' },
  { after: 'research', label: 'Rapprochement des preuves' },
  { after: 'reconcile', label: 'Rédaction et vérification des citations' },
]
const ORDER: ProgressStep[] = ['started', 'understand', 'research', 'reconcile', 'answer']

const FACET_OUTCOME: Record<FacetFinding['status'], string> = {
  completed: '',
  budget_exhausted: 'budget de recherche épuisé',
  failed: 'échec de la recherche',
}

interface ResearchProgressProps {
  step: ProgressStep
  plan?: ResearchPlan
  findings: FacetFinding[]
}

export function ResearchProgress({ step, plan, findings }: ResearchProgressProps) {
  const reached = ORDER.indexOf(step)
  const byFacet = new Map(findings.map((finding) => [finding.facet, finding]))

  return (
    <div className="research-progress" role="status" aria-label="Recherche en cours">
      <ol className="progress-steps">
        {STAGES.map((stage, index) => {
          const state = index < reached ? 'done' : index === reached ? 'active' : 'todo'
          return (
            <li key={stage.after} className={`progress-step progress-step-${state}`}>
              <span className="progress-dot" aria-hidden="true" />
              {stage.label}
            </li>
          )
        })}
      </ol>

      {plan && plan.facets.length > 0 && (
        <section className="facet-list" aria-label="Sous-questions">
          <h3 className="facet-list-title">
            Sous-questions
            <span className="facet-list-note">résultats provisoires, non vérifiés</span>
          </h3>
          <ul>
            {plan.facets.map((facet) => (
              <FacetRow key={facet} facet={facet} finding={byFacet.get(facet)} />
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function FacetRow({ facet, finding }: { facet: string; finding?: FacetFinding }) {
  if (!finding) {
    return (
      <li className="facet facet-waiting">
        <span className="progress-dot" aria-hidden="true" />
        <span className="facet-question">{facet}</span>
      </li>
    )
  }

  const outcome = FACET_OUTCOME[finding.status]
  const count = finding.claims.length
  const summary = outcome || (count === 0 ? 'aucun fait trouvé' : `${count} fait${count > 1 ? 's' : ''} trouvé${count > 1 ? 's' : ''}`)
  const titles = new Map(finding.sources.map((source) => [source.id, source]))

  return (
    <li className={`facet facet-${finding.status}`}>
      <details>
        <summary>
          <span className="progress-dot" aria-hidden="true" />
          <span className="facet-question">{facet}</span>
          <span className="facet-summary">{summary}</span>
        </summary>
        {count > 0 && (
          <ul className="facet-claims">
            {finding.claims.map((claim, index) => (
              <li key={index}>
                <span>{claim.text.trim() || claim.quote}</span>
                {claim.value !== null && claim.value !== '' && (
                  <span className="claim-value">
                    {formatQuantity(claim.value, claim.unit, claim.qualifier)}
                  </span>
                )}
                <span className="facet-claim-source">
                  {claim.evidence_ids.map((id) => sourceLabel(titles.get(id))).filter(Boolean).join(' ; ')}
                </span>
              </li>
            ))}
          </ul>
        )}
      </details>
    </li>
  )
}

function sourceLabel(source?: FacetSource): string {
  if (!source) return ''
  const title = source.title.trim() || source.source.split(/[\\/]/).pop() || 'Document source'
  const pages = formatPages(source.pages)
  return pages ? `${title}, ${pages}` : title
}
