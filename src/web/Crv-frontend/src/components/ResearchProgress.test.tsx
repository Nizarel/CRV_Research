import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { FacetFinding } from '../types'
import { ResearchProgress } from './ResearchProgress'

const finding: FacetFinding = {
  facet: 'Effet de l’eau de mer',
  status: 'completed',
  claims: [
    {
      text: 'Perte de 5 points de récupération Cu',
      evidence_ids: ['ev-a'],
      quote: 'perte de 5 points',
      subject: 'eau de mer',
      quantity: 'recovery',
      value: '5',
      unit: 'points',
      date: null,
      conditions: null,
      scope: null,
      calculation_index: null,
      qualifier: '~',
    },
  ],
  sources: [
    { id: 'ev-a', title: 'Réunion 2015', source: 'x\\y.pdf', pages: [3], document_code: null, source_role: 'unclassified' },
  ],
}

describe('ResearchProgress', () => {
  it('lists planned sub-questions as waiting until their findings arrive', () => {
    render(
      <ResearchProgress
        step="understand"
        plan={{ intent: 'lookup', facets: ['Effet de l’eau de mer', 'Autre question'] }}
        findings={[finding]}
      />,
    )

    expect(screen.getByText('1 fait trouvé')).toBeTruthy()
    expect(screen.getByText('Autre question').closest('li')?.className).toContain('facet-waiting')
    expect(screen.getByText(/non vérifiés/)).toBeTruthy()
  })

  it('shows a finding with its value and source when expanded', () => {
    render(
      <ResearchProgress step="research" plan={{ intent: 'lookup', facets: [finding.facet] }} findings={[finding]} />,
    )

    fireEvent.click(screen.getByText('1 fait trouvé'))

    expect(screen.getByText('Perte de 5 points de récupération Cu')).toBeTruthy()
    expect(screen.getByText('≈ 5 points')).toBeTruthy()
    expect(screen.getByText('Réunion 2015, p. 3')).toBeTruthy()
  })

  it('reports a failed or exhausted sub-question', () => {
    render(
      <ResearchProgress
        step="research"
        plan={{ intent: 'lookup', facets: ['A', 'B'] }}
        findings={[
          { facet: 'A', status: 'failed', claims: [], sources: [] },
          { facet: 'B', status: 'budget_exhausted', claims: [], sources: [] },
        ]}
      />,
    )

    expect(screen.getByText('échec de la recherche')).toBeTruthy()
    expect(screen.getByText('budget de recherche épuisé')).toBeTruthy()
  })
})
