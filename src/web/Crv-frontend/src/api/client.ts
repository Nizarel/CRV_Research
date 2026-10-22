import type { Answer, FacetFinding, ProgressStep, ResearchPlan, Run, Thread } from '../types'

/** Base URL for the backend API. Defaults to the app's own base path (`/crv` behind the gateway,
 * empty locally) so the browser stays same-origin with the reverse proxy. Override via
 * VITE_API_BASE_URL for a separately-hosted API. */
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? import.meta.env.BASE_URL.replace(/\/$/, '')
const API = `${API_BASE}/api/v1`

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function failureMessage(status: number): string {
  switch (status) {
    case 401:
      return 'Authentification requise : reconnectez-vous puis réessayez.'
    case 403:
      return 'Accès refusé : votre demande a été rejetée par le serveur.'
    case 404:
      return 'Conversation introuvable.'
    case 409:
      return 'Une autre question est déjà en cours dans cette conversation. Patientez ou ouvrez une nouvelle conversation.'
    case 422:
      return 'Question invalide : elle doit contenir entre 1 et 8 000 caractères.'
    default:
      return `Le serveur a répondu avec le statut ${status}.`
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init)
  if (!response.ok) throw new ApiError(response.status, failureMessage(response.status))
  return (await response.json()) as T
}

export function createConversation(): Promise<Thread> {
  return request<Thread>('/conversations', { method: 'POST' })
}

export function fetchHistory(threadId: string): Promise<Run[]> {
  return request<Run[]>(`/conversations/${encodeURIComponent(threadId)}`)
}

/** Record a vote on one answer; voting again on the same run replaces the earlier vote. */
export async function postFeedback(
  threadId: string,
  runId: string,
  rating: 1 | -1,
  comment?: string,
): Promise<void> {
  const response = await fetch(`${API}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, request_id: runId, rating, comment: comment ?? '' }),
  })
  if (!response.ok) {
    throw new ApiError(response.status, `Envoi de l'avis impossible (statut ${response.status}).`)
  }
}

export interface ChatStreamHandlers {
  onProgress: (step: ProgressStep) => void
  onPlan: (plan: ResearchPlan) => void
  onFacet: (finding: FacetFinding) => void
  onAnswer: (answer: Answer) => void
  onError: (error: Error) => void
}

/** One parsed Server-Sent Event frame. */
interface SseEvent {
  event: string
  data: string
}

/** Parse a buffer of raw SSE text into complete frames, returning the parsed events plus
 * whatever trailing (incomplete) text should be kept for the next chunk.
 *
 * Frames are separated by a blank line; each has `event: <name>` / `data: <json>` lines. Parsed
 * by hand (not `EventSource`) because `POST /api/v1/chat` needs a request body. */
export function parseSseChunk(buffer: string): { events: SseEvent[]; rest: string } {
  const frames = buffer.split('\n\n')
  const rest = frames.pop() ?? ''
  const events: SseEvent[] = []
  for (const frame of frames) {
    let event = 'message'
    const dataLines: string[] = []
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) {
        event = line.slice('event:'.length).trim()
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice('data:'.length).trim())
      }
    }
    if (dataLines.length > 0) events.push({ event, data: dataLines.join('\n') })
  }
  return { events, rest }
}

interface ErrorEvent {
  error?: string
  error_type?: string
  trace_id?: string
}

/** POST one chat turn and stream its progress. Exactly one of `onAnswer` / `onError` fires
 * (unless aborted). Returns an abort function. */
export function streamChatTurn(
  threadId: string,
  requestId: string,
  message: string,
  handlers: ChatStreamHandlers,
): () => void {
  const controller = new AbortController()

  const run = async () => {
    let settled = false
    const fail = (error: Error) => {
      if (settled) return
      settled = true
      handlers.onError(error)
    }
    try {
      const response = await fetch(`${API}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_id: threadId, request_id: requestId, message }),
        signal: controller.signal,
      })
      if (!response.ok || !response.body) {
        throw new ApiError(response.status, failureMessage(response.status))
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
        const { events, rest } = parseSseChunk(buffer)
        buffer = rest
        for (const evt of events) {
          if (evt.event === 'progress') {
            handlers.onProgress((JSON.parse(evt.data) as { step: ProgressStep }).step)
          } else if (evt.event === 'plan') {
            handlers.onPlan(JSON.parse(evt.data) as ResearchPlan)
          } else if (evt.event === 'facet') {
            handlers.onFacet(JSON.parse(evt.data) as FacetFinding)
          } else if (evt.event === 'answer') {
            settled = true
            handlers.onAnswer(JSON.parse(evt.data) as Answer)
          } else if (evt.event === 'error') {
            const payload = JSON.parse(evt.data) as ErrorEvent
            const trace = payload.trace_id ? ` (trace ${payload.trace_id})` : ''
            fail(new Error(`La recherche a échoué${trace}. Réessayez dans un instant.`))
          }
        }
      }
      fail(new Error('La connexion a été interrompue avant la fin de la réponse.'))
    } catch (error) {
      if ((error as { name?: string }).name === 'AbortError') return
      fail(error instanceof Error ? error : new Error(String(error)))
    }
  }

  void run()
  return () => controller.abort()
}
