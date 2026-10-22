import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, parseSseChunk, postFeedback, streamChatTurn } from './client'

function sse(...frames: Array<[string, object]>): string {
  return frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
}

function streamResponse(text: string, chunkSize = text.length): Response {
  const bytes = new TextEncoder().encode(text)
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize))
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

afterEach(() => vi.unstubAllGlobals())

const idle = {
  onProgress: () => {},
  onPlan: () => {},
  onFacet: () => {},
  onAnswer: () => {},
  onError: () => {},
}

describe('parseSseChunk', () => {
  it('returns complete frames and keeps the partial tail', () => {
    const { events, rest } = parseSseChunk('id: 1\nevent: progress\ndata: {"step":"understand"}\n\nevent: ans')
    expect(events).toEqual([{ event: 'progress', data: '{"step":"understand"}' }])
    expect(rest).toBe('event: ans')
  })
})

describe('streamChatTurn', () => {
  const answerPayload = { status: 'answered', content: 'Réponse [ev-a]', citations: [], claims: [] }

  it('posts the turn and reports progress then the answer', async () => {
    const body = sse(
      ['progress', { step: 'started' }],
      ['progress', { step: 'understand' }],
      ['plan', { intent: 'lookup', facets: ['Q1'] }],
      ['facet', { facet: 'Q1', status: 'completed', claims: [], sources: [] }],
      ['answer', answerPayload],
      ['done', { status: 'completed' }],
    )
    const fetchMock = vi.fn().mockResolvedValue(streamResponse(body, 7))
    vi.stubGlobal('fetch', fetchMock)

    const steps: string[] = []
    const plans: unknown[] = []
    const facets: unknown[] = []
    const answers: unknown[] = []
    const errors: Error[] = []
    streamChatTurn('thread-1', 'run-1', 'Bonjour', {
      onProgress: (step) => steps.push(step),
      onPlan: (plan) => plans.push(plan),
      onFacet: (finding) => facets.push(finding),
      onAnswer: (answer) => answers.push(answer),
      onError: (error) => errors.push(error),
    })

    await vi.waitFor(() => expect(answers).toHaveLength(1))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(steps).toEqual(['started', 'understand'])
    expect(plans).toEqual([{ intent: 'lookup', facets: ['Q1'] }])
    expect(facets).toEqual([{ facet: 'Q1', status: 'completed', claims: [], sources: [] }])
    expect(answers[0]).toMatchObject({ content: 'Réponse [ev-a]' })
    expect(errors).toEqual([])
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/chat')
    expect(JSON.parse(init.body)).toEqual({ thread_id: 'thread-1', request_id: 'run-1', message: 'Bonjour' })
  })

  it('reports a research failure with its trace id', async () => {
    const body = sse(['error', { error: 'research_failed', trace_id: 'trace-9' }])
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse(body)))

    const errors: Error[] = []
    streamChatTurn('t', 'r', 'q', { ...idle, onError: (e) => errors.push(e) })

    await vi.waitFor(() => expect(errors).toHaveLength(1))
    expect(errors[0].message).toContain('trace-9')
  })

  it('reports a stream that ends without an answer', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse(sse(['progress', { step: 'started' }]))))

    const errors: Error[] = []
    streamChatTurn('t', 'r', 'q', { ...idle, onError: (e) => errors.push(e) })

    await vi.waitFor(() => expect(errors).toHaveLength(1))
    expect(errors[0].message).toContain('interrompue')
  })

  it('maps a conflicting turn to a clear message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 409 })))

    const errors: Error[] = []
    streamChatTurn('t', 'r', 'q', { ...idle, onError: (e) => errors.push(e) })

    await vi.waitFor(() => expect(errors).toHaveLength(1))
    expect(errors[0]).toBeInstanceOf(ApiError)
    expect(errors[0].message).toContain('déjà en cours')
  })
})

describe('base path', () => {
  it('prefixes API calls with the app base path behind the gateway', async () => {
    vi.stubEnv('BASE_URL', '/crv/')
    vi.resetModules()
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"id":"t"}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const { createConversation } = await import('./client')
    await createConversation()

    expect(fetchMock.mock.calls[0][0]).toBe('/crv/api/v1/conversations')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', body: '{}' })
    vi.unstubAllEnvs()
  })
})

describe('postFeedback', () => {
  it('sends the run id as request_id with a -1/1 rating', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await postFeedback('thread-1', 'run-1', -1, 'faux')

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/feedback')
    expect(JSON.parse(init.body)).toEqual({
      thread_id: 'thread-1',
      request_id: 'run-1',
      rating: -1,
      comment: 'faux',
    })
  })

  it('throws when the backend refuses the vote', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))
    await expect(postFeedback('t', 'r', 1)).rejects.toBeInstanceOf(ApiError)
  })
})
