import { useCallback, useEffect, useState } from 'react'
import type { ConversationSummary } from '../types'

const STORAGE_KEY = 'crv.conversations.v1'
const MAX_HISTORY = 30

function readStore(): ConversationSummary[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as ConversationSummary[]) : []
  } catch {
    return []
  }
}

function writeStore(items: ConversationSummary[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items))
  } catch {
    // localStorage unavailable (private browsing, quota) — history just won't persist.
  }
}

/** Client-side conversation history: the backend lists thread ids but keeps no titles, so the
 * browser stores each conversation's first question as its title on this device. */
export function useConversations() {
  const [conversations, setConversations] = useState<ConversationSummary[]>(() => readStore())

  useEffect(() => writeStore(conversations), [conversations])

  const upsert = useCallback((threadId: string, title: string) => {
    setConversations((prev) => {
      const existing = prev.find((c) => c.threadId === threadId)
      const next = prev.filter((c) => c.threadId !== threadId)
      // Keep the conversation's original title (its first question) stable across
      // turns — only the "last updated" ordering should change, like most chat apps.
      next.unshift({ threadId, title: existing?.title ?? title, updatedAt: new Date().toISOString() })
      return next.slice(0, MAX_HISTORY)
    })
  }, [])

  const remove = useCallback((threadId: string) => {
    setConversations((prev) => prev.filter((c) => c.threadId !== threadId))
  }, [])

  return { conversations, upsert, remove }
}
