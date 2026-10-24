import { describe, expect, it } from 'vitest'
import { localizeLimit } from './evidence'

describe('localizeLimit', () => {
  it('translates fixed backend notes', () => {
    expect(localizeLimit('Statements the verifier found unsupported were removed.')).toContain('retirées')
    expect(localizeLimit('Top-k evidence retrieval is not an exhaustive corpus review.')).toContain('exhaustive')
  })

  it('translates parameterised notes', () => {
    expect(localizeLimit('Candidate entity mapping: Tizert')).toBe(
      'Entité candidate retenue pour la navigation : Tizert.',
    )
    expect(localizeLimit('A research facet failed (TimeoutError); its evidence is missing.')).toContain('TimeoutError')
  })

  it('keeps notes the model already wrote', () => {
    expect(localizeLimit("Aucune teneur n'est rapportée.")).toBe("Aucune teneur n'est rapportée.")
  })
})
