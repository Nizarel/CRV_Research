import { useRef, useState, type KeyboardEvent } from 'react'
import { Icon } from './Icon'
import './ChatInput.css'

const MAX_LENGTH = 8000

interface ChatInputProps {
  disabled: boolean
  onSend: (text: string) => void
}

export function ChatInput({ disabled, onSend }: ChatInputProps) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const submit = () => {
    if (!value.trim() || disabled) return
    onSend(value)
    setValue('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  const autoResize = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  return (
    <div className="chat-input-bar">
      <div className="chat-input-shell">
        <textarea
          ref={textareaRef}
          className="chat-input-textarea"
          placeholder="Posez une question sur le corpus (site, essai, rapport, procédé)…"
          rows={1}
          maxLength={MAX_LENGTH}
          value={value}
          disabled={disabled}
          onChange={(e) => {
            setValue(e.target.value)
            autoResize(e.target)
          }}
          onKeyDown={handleKeyDown}
        />
        <button
          type="button"
          className="chat-input-send"
          disabled={disabled || !value.trim()}
          onClick={submit}
          aria-label="Envoyer"
        >
          {disabled ? <span className="send-spinner" aria-hidden="true" /> : <Icon name="send" size={18} />}
        </button>
      </div>
      <p className="chat-input-hint">
        Entrée pour envoyer · Maj+Entrée pour une nouvelle ligne. Vérifiez les valeurs critiques
        dans le document source avant toute décision.
      </p>
    </div>
  )
}
