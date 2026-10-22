import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as client from '../api/client'
import type { Answer, Evidence, Run } from '../types'
import { messagesFromHistory } from '../hooks/useChat'
import { AnswerView } from './AnswerView'
import { FeedbackBar } from './FeedbackBar'

function evidence(id: string, overrides: Partial<Evidence> = {}): Evidence {
  return {
    id,
    release: 'crv-v1',
    chunk_id: id,
    title: `Document ${id}`,
    content: 'Le rendement atteint 78 %.',
    source_uri: '',
    source_path: '',
    document_code: null,
    pages: [2],
    locators: [],
    generated: false,
    document_class: 'report',
    extraction_route: 'di',
    source_role: 'unclassified',
    ...overrides,
  }
}

function answer(overrides: Partial<Answer> = {}): Answer {
  return {
    status: 'answered',
    content: 'Le rendement est de 78 % [ev-a]. Autre valeur [ev-b].',
    claims: [
      {
        text: 'Rendement de 78 %',
        evidence_ids: ['ev-a'],
        quote: 'rendement atteint 78 %',
        subject: 'Tizert',
        quantity: 'recovery',
        value: '78',
        unit: '%',
        date: null,
        conditions: null,
        scope: null,
        calculation_index: null,
        qualifier: null,
      },
      {
        text: 'Rendement de 80 %',
        evidence_ids: ['ev-b'],
        quote: 'rendement 80 %',
        subject: 'Tizert',
        quantity: 'recovery',
        value: '80',
        unit: '%',
        date: null,
        conditions: null,
        scope: null,
        calculation_index: null,
        qualifier: null,
      },
    ],
    citations: [evidence('ev-a'), evidence('ev-b')],
    conflicts: [],
    calculations: [],
    coverage: { matched: 0, visited: 0, enumerated: 0, catalogue_complete: false, gaps: [] },
    reasons: [],
    search_release: 'crv-v1',
    kg_release: 'kg-1',
    trace_id: 't',
    usage: {},
    retrieval_mode: 'kg',
    ...overrides,
  }
}

describe('AnswerView', () => {
  it('renders numbered citation chips that open the matching source', () => {
    const onOpen = vi.fn()
    render(<AnswerView answer={answer()} onOpenEvidence={onOpen} />)

    fireEvent.click(screen.getAllByRole('button', { name: 'Ouvrir la source 2' })[0])
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'ev-b' }))
  })

  it('keeps unknown markers as plain text', () => {
    render(<AnswerView answer={answer({ content: 'Texte [inconnu].' })} onOpenEvidence={vi.fn()} />)
    expect(screen.getByText(/\[inconnu\]/)).toBeTruthy()
  })

  it('flags a partial answer and lists its limits and conflicts', () => {
    render(
      <AnswerView
        answer={answer({
          status: 'partial',
          reasons: ['Statements the verifier found unsupported were removed.'],
          coverage: { matched: 1, visited: 2, enumerated: 0, catalogue_complete: false, gaps: ['Aucun rapport 2019'] },
          conflicts: [{ subject: 'Tizert', quantity: 'recovery', claim_indices: [0, 1] }],
        })}
        onOpenEvidence={vi.fn()}
      />,
    )

    expect(screen.getByText('Réponse partielle')).toBeTruthy()
    expect(screen.getByText('Aucun rapport 2019')).toBeTruthy()
    expect(screen.getByText('Sources en désaccord')).toBeTruthy()
    expect(screen.getAllByText(/Rendement de 80 %/).length).toBeGreaterThan(0)
  })
})

describe('messagesFromHistory', () => {
  const run = (overrides: Partial<Run>): Run => ({
    id: 'run-1',
    thread_id: 't',
    question: 'Question ?',
    status: 'completed',
    result: null,
    ...overrides,
  })

  it('restores completed answers and explains failed or interrupted turns', () => {
    const done = answer()
    const messages = messagesFromHistory('t', [
      run({ result: done }),
      run({ id: 'run-2', status: 'failed', result: { error: 'RuntimeError' } }),
      run({ id: 'run-3', status: 'running' }),
    ])

    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant', 'user', 'assistant'])
    expect(messages[1]).toMatchObject({ runId: 'run-1', threadId: 't', answer: done })
    expect(messages[3].failure).toContain('échoué')
    expect(messages[5].failure).toContain('en cours')
  })
})

describe('FeedbackBar', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('records a positive vote against the run', async () => {
    const post = vi.spyOn(client, 'postFeedback').mockResolvedValue()
    render(<FeedbackBar threadId="t" runId="run-1" />)

    fireEvent.click(screen.getByRole('button', { name: 'Réponse utile' }))

    await waitFor(() => expect(post).toHaveBeenCalledWith('t', 'run-1', 1, undefined))
  })

  it('asks for an optional comment after a negative vote', async () => {
    const post = vi.spyOn(client, 'postFeedback').mockResolvedValue()
    render(<FeedbackBar threadId="t" runId="run-1" />)

    fireEvent.click(screen.getByRole('button', { name: 'Réponse incorrecte ou incomplète' }))
    const box = await screen.findByLabelText('Commentaire (facultatif)')
    fireEvent.change(box, { target: { value: 'valeur erronée' } })
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer le commentaire' }))

    await waitFor(() => expect(post).toHaveBeenLastCalledWith('t', 'run-1', -1, 'valeur erronée'))
  })
})
