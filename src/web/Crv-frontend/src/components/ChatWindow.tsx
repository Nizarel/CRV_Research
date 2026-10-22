import { useEffect, useRef } from 'react'
import type { ChatMessage } from '../types'
import { EmptyState } from './EmptyState'
import { MessageBubble } from './MessageBubble'
import './ChatWindow.css'

interface ChatWindowProps {
  messages: ChatMessage[]
  error: string | null
  onOpenEvidence: (citation: ChatMessage['citations'][number]) => void
  onPickExample: (question: string) => void
}

export function ChatWindow({ messages, error, onOpenEvidence, onPickExample }: ChatWindowProps) {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="chat-window">
        <EmptyState onPick={onPickExample} />
      </div>
    )
  }

  return (
    <div className="chat-window">
      <div className="chat-window-inner">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} onOpenEvidence={onOpenEvidence} />
        ))}
        {error && (
          <div className="chat-error-banner" role="alert">
            {error}
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  )
}
