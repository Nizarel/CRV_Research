import { useEffect, useRef, useState } from 'react'
import { formatPages, highlightSegments, quotesFor, sourceMeta, sourceTitle } from '../evidence'
import type { Answer, Evidence } from '../types'
import { Icon } from './Icon'
import './EvidenceModal.css'

interface EvidenceModalProps {
  evidence: Evidence
  answer: Answer
  number: number
  onClose: () => void
}

export function EvidenceModal({ evidence, answer, number, onClose }: EvidenceModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [copied, setCopied] = useState(false)
  const quotes = quotesFor(answer, evidence.id)
  const segments = highlightSegments(evidence.content, quotes)
  const title = sourceTitle(evidence)
  const meta = sourceMeta(evidence).join(' · ')
  const location = evidence.locators.join(' · ')

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    dialog.showModal()
    const handleCancel = (event: Event) => {
      event.preventDefault()
      onClose()
    }
    dialog.addEventListener('cancel', handleCancel)
    return () => {
      dialog.removeEventListener('cancel', handleCancel)
      if (dialog.open) dialog.close()
    }
  }, [onClose])

  const copyPath = async () => {
    try {
      await navigator.clipboard.writeText(evidence.source_path)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard unavailable (insecure context): the path stays selectable on screen.
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="evidence-modal"
      aria-labelledby="evidence-modal-title"
      onClick={(event) => {
        if (event.target === dialogRef.current) onClose()
      }}
    >
      <div className="evidence-frame">
        <header className="evidence-header">
          <span className="ref-chip ref-chip-static">{number}</span>
          <div className="evidence-heading">
            <h2 id="evidence-modal-title" title={title}>
              {title}
            </h2>
            {meta && <p>{meta}</p>}
          </div>
          <button type="button" className="evidence-close" onClick={onClose} aria-label="Fermer" autoFocus>
            <Icon name="close" size={16} />
          </button>
        </header>

        <div className="evidence-body">
          {evidence.generated && (
            <p className="evidence-warning" role="note">
              <Icon name="warning" size={15} />
              Description générée automatiquement : elle aide à retrouver l'information mais n'est
              pas un extrait du document d'origine.
            </p>
          )}

          <dl className="evidence-facts">
            {evidence.document_code && (
              <>
                <dt>Référence</dt>
                <dd>{evidence.document_code}</dd>
              </>
            )}
            {formatPages(evidence.pages) && (
              <>
                <dt>Pages</dt>
                <dd>{formatPages(evidence.pages)}</dd>
              </>
            )}
            {location && (
              <>
                <dt>Emplacement</dt>
                <dd>{location}</dd>
              </>
            )}
            {evidence.source_path && (
              <>
                <dt>Fichier d'origine</dt>
                <dd className="evidence-path">
                  <span>{evidence.source_path}</span>
                  <button type="button" onClick={() => void copyPath()} title="Copier le chemin">
                    <Icon name="copy" size={13} />
                    {copied ? 'Copié' : 'Copier'}
                  </button>
                </dd>
              </>
            )}
          </dl>

          {quotes.length > 0 && (
            <section>
              <h3>Passages cités dans la réponse</h3>
              {quotes.map((quote, index) => (
                <blockquote key={index} className="evidence-quote">
                  {quote}
                </blockquote>
              ))}
            </section>
          )}

          <section>
            <h3>Texte de la source</h3>
            <div className="evidence-text" tabIndex={0}>
              {segments.map((segment, index) =>
                segment.marked ? <mark key={index}>{segment.text}</mark> : segment.text,
              )}
            </div>
          </section>
        </div>
      </div>
    </dialog>
  )
}
