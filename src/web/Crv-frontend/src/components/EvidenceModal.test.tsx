import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { Answer, Evidence } from '../types'
import { EvidenceModal } from './EvidenceModal'

const DIGEST = 'a'.repeat(64)

function evidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    id: 'ev-a',
    release: 'crv-v1',
    chunk_id: 'c1',
    title: 'Traitement du zinc Nizi',
    content: 'Rdt (Ag) = 94 à 97 %.',
    source_uri: `crv-sources/crv/sources/${DIGEST}.pdf`,
    source_path: 'CTT Hydro/Traitement du zinc.pdf',
    document_code: null,
    pages: [7],
    locators: [],
    generated: false,
    document_class: 'research_report',
    extraction_route: 'di_layout',
    source_role: 'unclassified',
    ...overrides,
  }
}

const answer = { claims: [] } as unknown as Answer

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '')
  }
  HTMLDialogElement.prototype.close = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

describe('EvidenceModal source document', () => {
  it('keeps the extract view and opens the PDF on the cited page', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 206 }))
    vi.stubGlobal('fetch', fetchMock)
    render(<EvidenceModal evidence={evidence()} answer={answer} number={1} onClose={() => {}} />)

    expect(screen.getByText('Texte de la source')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Document (p. 7)' }))

    const frame = await screen.findByTitle(/PDF : Traitement du zinc Nizi/)
    expect(frame.getAttribute('src')).toBe(`/api/v1/sources/${DIGEST}.pdf#page=7`)
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ headers: { Range: 'bytes=0-0' } })
  })

  it('lets the reader switch between several cited pages', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 206 })))
    render(
      <EvidenceModal evidence={evidence({ pages: [6, 5] })} answer={answer} number={1} onClose={() => {}} />,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Document' }))
    await screen.findByTitle(/PDF/)
    fireEvent.click(screen.getByRole('button', { name: 'p. 6' }))

    await waitFor(() =>
      expect(screen.getByTitle(/PDF/).getAttribute('src')).toBe(`/api/v1/sources/${DIGEST}.pdf#page=6`),
    )
  })

  it('explains when the document cannot be served', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 404 })))
    render(<EvidenceModal evidence={evidence()} answer={answer} number={1} onClose={() => {}} />)

    fireEvent.click(screen.getByRole('tab', { name: 'Document (p. 7)' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/n'est pas disponible/)
  })

  it('offers no document tab for sources that are not stored PDFs', () => {
    render(
      <EvidenceModal
        evidence={evidence({ source_uri: 'DATAROOM/some/path.xlsx' })}
        answer={answer}
        number={1}
        onClose={() => {}}
      />,
    )

    expect(screen.queryByRole('tab')).toBeNull()
  })
})
