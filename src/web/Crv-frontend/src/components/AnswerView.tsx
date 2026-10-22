import type { ReactNode } from 'react'
import { citationNumbers, formatClaimValue, formatPages, formatQuantity, sourceTitle } from '../evidence'
import type { Answer, AnswerStatus, Claim, Evidence } from '../types'
import { Icon, type IconName } from './Icon'
import { Markdown, type RenderRef } from './Markdown'
import './AnswerView.css'

interface AnswerViewProps {
  answer: Answer
  onOpenEvidence: (evidence: Evidence) => void
}

const STATUS_NOTICE: Partial<
  Record<AnswerStatus, { tone: 'warn' | 'info' | 'neutral'; icon: IconName; label: string }>
> = {
  partial: { tone: 'warn', icon: 'warning', label: 'Réponse partielle' },
  insufficient_evidence: { tone: 'warn', icon: 'warning', label: 'Preuves insuffisantes' },
  needs_clarification: { tone: 'info', icon: 'info', label: 'Précision nécessaire' },
  out_of_scope: { tone: 'neutral', icon: 'info', label: 'Hors périmètre' },
}

const OPERATIONS: Record<string, string> = {
  convert: 'Conversion',
  ratio: 'Rapport',
  recovery: 'Récupération',
  sum: 'Somme',
  weighted_recovery: 'Récupération pondérée',
}

export function AnswerView({ answer, onOpenEvidence }: AnswerViewProps) {
  const numbers = citationNumbers(answer)
  const byId = new Map(answer.citations.map((citation) => [citation.id, citation]))
  const notice = STATUS_NOTICE[answer.status]
  const limits = [...new Set([...answer.coverage.gaps, ...answer.reasons])]

  const chip = (evidence: Evidence, key: string) => (
    <button
      key={key}
      type="button"
      className="ref-chip"
      data-ref={evidence.id}
      title={[sourceTitle(evidence), formatPages(evidence.pages)].filter(Boolean).join(' · ')}
      aria-label={`Ouvrir la source ${numbers.get(evidence.id)}`}
      onClick={() => onOpenEvidence(evidence)}
    >
      {numbers.get(evidence.id)}
    </button>
  )

  const renderRef: RenderRef = (ref, key) => {
    const evidence = byId.get(ref)
    return evidence ? chip(evidence, key) : `[${ref}]`
  }

  const claimText = (claim: Claim) => claim.text.trim() || claim.quote

  return (
    <div className="answer">
      {notice && (
        <div className={`answer-notice answer-notice-${notice.tone}`} role="status">
          <Icon name={notice.icon} />
          {notice.label}
        </div>
      )}

      <div className="answer-prose">
        <Markdown text={answer.content} renderRef={renderRef} />
      </div>

      {answer.conflicts.length > 0 && (
        <AnswerPanel icon="conflict" title="Sources en désaccord" tone="warn">
          {answer.conflicts.map((conflict, index) => (
            <div key={index} className="answer-conflict">
              <strong>
                {conflict.subject}
                {conflict.quantity ? ` — ${conflict.quantity}` : ''}
              </strong>
              <ul>
                {conflict.claim_indices.map((claimIndex) => {
                  const claim = answer.claims[claimIndex]
                  if (!claim) return null
                  return (
                    <li key={claimIndex}>
                      {claimText(claim)} {citeChips(claim, byId, chip, `c${index}-${claimIndex}`)}
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </AnswerPanel>
      )}

      {answer.calculations.length > 0 && (
        <AnswerPanel icon="calculator" title="Valeurs calculées" tone="info">
          <ul>
            {answer.calculations.map((calculation, index) => (
              <li key={index}>
                <strong>{OPERATIONS[calculation.operation] ?? calculation.operation}</strong> :{' '}
                {formatQuantity(calculation.value, calculation.unit)}
                <span className="answer-panel-note">
                  {' '}
                  d'après{' '}
                  {calculation.evidence_ids.map((id) => {
                    const evidence = byId.get(id)
                    return evidence ? chip(evidence, `calc${index}-${id}`) : null
                  })}
                </span>
              </li>
            ))}
          </ul>
        </AnswerPanel>
      )}

      {limits.length > 0 && (
        <AnswerPanel icon="warning" title="Limites de la réponse" tone="warn">
          <ul>
            {limits.map((limit, index) => (
              <li key={index}>{limit}</li>
            ))}
          </ul>
        </AnswerPanel>
      )}

      {answer.claims.length > 0 && (
        <details className="answer-claims">
          <summary>
            <Icon name="quote" />
            Faits sourcés <span className="count-pill">{answer.claims.length}</span>
          </summary>
          <ol>
            {answer.claims.map((claim, index) => {
              const value = formatClaimValue(claim)
              const context = [claim.subject, claim.conditions, claim.scope, claim.date].filter(Boolean)
              return (
                <li key={index} className="claim">
                  <div className="claim-head">
                    <span className="claim-text">{claimText(claim)}</span>
                    {value && <span className="claim-value">{value}</span>}
                  </div>
                  {context.length > 0 && <div className="claim-context">{context.join(' · ')}</div>}
                  <blockquote className="claim-quote">
                    {claim.quote} {citeChips(claim, byId, chip, `q${index}`)}
                  </blockquote>
                </li>
              )
            })}
          </ol>
        </details>
      )}
    </div>
  )
}

function citeChips(
  claim: Claim,
  byId: Map<string, Evidence>,
  chip: (evidence: Evidence, key: string) => ReactNode,
  prefix: string,
): ReactNode[] {
  return claim.evidence_ids.flatMap((id) => {
    const evidence = byId.get(id)
    return evidence ? [chip(evidence, `${prefix}-${id}`)] : []
  })
}

function AnswerPanel({
  icon,
  title,
  tone,
  children,
}: {
  icon: IconName
  title: string
  tone: 'warn' | 'info'
  children: ReactNode
}) {
  return (
    <aside className={`answer-panel answer-panel-${tone}`}>
      <h3 className="answer-panel-title">
        <Icon name={icon} />
        {title}
      </h3>
      {children}
    </aside>
  )
}
