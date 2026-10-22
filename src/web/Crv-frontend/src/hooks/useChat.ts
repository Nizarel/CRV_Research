import { useCallback, useRef, useState } from 'react'
import { createConversation, fetchHistory, streamChatTurn } from '../api/client'
import type { Answer, ChatMessage, Run } from '../types'

function newId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function isAnswer(result: Run['result']): result is Answer {
  return result !== null && 'status' in result && 'content' in result
}

/** Turns persisted runs back into the question/answer messages shown in the chat. */
export function messagesFromHistory(threadId: string, runs: Run[]): ChatMessage[] {
  return runs.flatMap((run): ChatMessage[] => {
    const question: ChatMessage = { id: `${run.id}-q`, role: 'user', content: run.question }
    const base = { id: run.id, role: 'assistant' as const, threadId, runId: run.id }
    if (run.status === 'completed' && isAnswer(run.result)) {
      return [question, { ...base, content: run.result.content, answer: run.result }]
    }
    const failure =
      run.status === 'running'
        ? 'Cette question est encore en cours de traitement ou a été interrompue.'
        : run.status === 'cancelled'
          ? 'Cette question a été annulée avant la fin de la réponse.'
          : 'La recherche a échoué pour cette question.'
    return [question, { ...base, content: '', failure }]
  })
}

/** Drives one conversation: creates the backend thread on the first question, streams each
 * turn's progress, and swaps in the verified, sourced answer when it arrives. */
export function useChat(onThreadPersisted: (threadId: string, title: string) => void) {
  const [threadId, setThreadId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<(() => void) | null>(null)

  const reset = useCallback(() => {
    abortRef.current?.()
    abortRef.current = null
    setThreadId(null)
    setMessages([])
    setError(null)
    setIsStreaming(false)
  }, [])

  const loadThread = useCallback(async (id: string) => {
    abortRef.current?.()
    abortRef.current = null
    setIsStreaming(false)
    setError(null)
    try {
      const runs = await fetchHistory(id)
      setThreadId(id)
      setMessages(messagesFromHistory(id, runs))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const sendMessage = useCallback(
    async (text: string) => {
      const question = text.trim()
      if (!question || isStreaming) return

      setError(null)
      setIsStreaming(true)

      let activeThread = threadId
      if (!activeThread) {
        try {
          activeThread = (await createConversation()).id
        } catch (err) {
          setIsStreaming(false)
          setError(err instanceof Error ? err.message : String(err))
          return
        }
        setThreadId(activeThread)
      }
      const thread = activeThread
      onThreadPersisted(thread, question.slice(0, 80))

      const runId = newId()
      const assistantId = `${runId}-a`
      setMessages((prev) => [
        ...prev,
        { id: `${runId}-q`, role: 'user', content: question },
        { id: assistantId, role: 'assistant', content: '', pending: true, step: 'started' },
      ])
      const update = (patch: Partial<ChatMessage>) =>
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, ...patch } : m)))

      abortRef.current = streamChatTurn(thread, runId, question, {
        onProgress: (step) => update({ step }),
        onPlan: (plan) => update({ plan }),
        onFacet: (finding) =>
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId ? { ...m, findings: [...(m.findings ?? []), finding] } : m,
            ),
          ),
        onAnswer: (answer) => {
          update({
            content: answer.content,
            answer,
            pending: false,
            step: undefined,
            plan: undefined,
            findings: undefined,
            threadId: thread,
            runId,
          })
          setIsStreaming(false)
        },
        onError: (err) => {
          update({ pending: false, step: undefined, plan: undefined, findings: undefined, failure: err.message })
          setIsStreaming(false)
        },
      })
    },
    [threadId, isStreaming, onThreadPersisted],
  )

  return { threadId, messages, isStreaming, error, sendMessage, reset, loadThread }
}
