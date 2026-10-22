import type { FocusEvent, MouseEvent } from 'react'
import type { ChatMessage, Evidence } from '../types'
import { AnswerView } from './AnswerView'
import { CitationList } from './CitationList'
import { FeedbackBar } from './FeedbackBar'
import { Icon } from './Icon'
import { ResearchProgress } from './ResearchProgress'
import './MessageBubble.css'

interface MessageBubbleProps {
  message: ChatMessage
  onOpenEvidence: (evidence: Evidence, answer: NonNullable<ChatMessage['answer']>) => void
}

/** Highlights every element of the answer tied to the hovered citation (marker, source card).
 * Done on the DOM, not in React state: re-rendering the answer between hover and click would
 * replace the node under the pointer and swallow the click. */
function linkCitations(event: MouseEvent<HTMLElement> | FocusEvent<HTMLElement>, on: boolean) {
  const ref = (event.target as HTMLElement).closest<HTMLElement>('[data-ref]')?.dataset.ref
  if (!ref) return
  event.currentTarget
    .querySelectorAll<HTMLElement>('[data-ref]')
    .forEach((element) => element.classList.toggle('is-linked', on && element.dataset.ref === ref))
}

export function MessageBubble({ message, onOpenEvidence }: MessageBubbleProps) {
  if (message.role === 'user') {
    return (
      <div className="message-row message-row-user">
        <div className="message-bubble-wrap">
          <div className="message-bubble bubble-user">
            <p className="answer-plain">{message.content}</p>
          </div>
        </div>
      </div>
    )
  }

  const { answer } = message

  return (
    <div className="message-row message-row-assistant">
      <div className="message-avatar" aria-hidden="true">
        <img src={`${import.meta.env.BASE_URL}favicon.svg`} alt="" />
      </div>

      <div className="message-bubble-wrap">
        <article
          className="message-bubble bubble-assistant"
          aria-busy={message.pending || undefined}
          onMouseOver={(event) => linkCitations(event, true)}
          onMouseOut={(event) => linkCitations(event, false)}
          onFocus={(event) => linkCitations(event, true)}
          onBlur={(event) => linkCitations(event, false)}
        >
          {message.pending && (
            <ResearchProgress
              step={message.step ?? 'started'}
              plan={message.plan}
              findings={message.findings ?? []}
            />
          )}

          {message.failure && (
            <div className="answer-failure" role="alert">
              <Icon name="warning" size={16} />
              {message.failure}
            </div>
          )}

          {answer && (
            <>
              <AnswerView
                answer={answer}
                onOpenEvidence={(evidence) => onOpenEvidence(evidence, answer)}
              />
              <CitationList
                answer={answer}
                onOpenEvidence={(evidence) => onOpenEvidence(evidence, answer)}
              />
            </>
          )}
        </article>

        {answer && message.threadId && message.runId && (
          <FeedbackBar threadId={message.threadId} runId={message.runId} />
        )}
      </div>
    </div>
  )
}
