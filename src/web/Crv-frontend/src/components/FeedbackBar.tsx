import { useState } from 'react'
import { postFeedback } from '../api/client'

interface FeedbackBarProps {
  threadId: string
  runId: string
}

type Rating = 1 | -1
type Status = 'idle' | 'sending' | 'voted' | 'commented' | 'error'

const MAX_COMMENT = 2000

/** Thumbs up/down on one answer; a down vote invites an optional comment. */
export function FeedbackBar({ threadId, runId }: FeedbackBarProps) {
  const [rating, setRating] = useState<Rating | null>(null)
  const [comment, setComment] = useState('')
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState('')

  const send = async (value: Rating, text?: string) => {
    setStatus('sending')
    try {
      await postFeedback(threadId, runId, value, text)
      setRating(value)
      setStatus(text ? 'commented' : 'voted')
    } catch (err) {
      setError(err instanceof Error ? err.message : '')
      setStatus('error')
    }
  }

  return (
    <div className="feedback-bar">
      <span className="feedback-label">Cette réponse vous aide-t-elle ?</span>
      <button
        type="button"
        className={`feedback-button ${rating === 1 ? 'feedback-selected' : ''}`}
        aria-label="Réponse utile"
        aria-pressed={rating === 1}
        disabled={status === 'sending'}
        onClick={() => void send(1)}
      >
        <ThumbIcon />
      </button>
      <button
        type="button"
        className={`feedback-button feedback-down ${rating === -1 ? 'feedback-selected' : ''}`}
        aria-label="Réponse incorrecte ou incomplète"
        aria-pressed={rating === -1}
        disabled={status === 'sending'}
        onClick={() => void send(-1)}
      >
        <ThumbIcon />
      </button>
      {status === 'error' && (
        <span className="feedback-note">{error || 'Envoi impossible, réessayez.'}</span>
      )}
      {status === 'commented' && <span className="feedback-note">Merci pour votre retour.</span>}
      {status === 'voted' && rating === 1 && <span className="feedback-note">Merci.</span>}
      {status === 'voted' && rating === -1 && (
        <form
          className="feedback-comment"
          onSubmit={(event) => {
            event.preventDefault()
            void send(-1, comment.trim())
          }}
        >
          <textarea
            aria-label="Commentaire (facultatif)"
            placeholder="Qu'est-ce qui est faux ou manquant ? (facultatif)"
            maxLength={MAX_COMMENT}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
          />
          <button type="submit" disabled={!comment.trim()}>
            Envoyer le commentaire
          </button>
        </form>
      )}
    </div>
  )
}

function ThumbIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M7 10v11H3V10h4Zm0 0 4-8a2.5 2.5 0 0 1 3 2.5V9h5.5a2 2 0 0 1 2 2.3l-1.4 8A2 2 0 0 1 18.1 21H7" />
    </svg>
  )
}
