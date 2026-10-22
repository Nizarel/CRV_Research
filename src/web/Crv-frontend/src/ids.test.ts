import { afterEach, describe, expect, it, vi } from 'vitest'
import { newId } from './ids'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

afterEach(() => vi.unstubAllGlobals())

describe('newId', () => {
  it('returns a v4 UUID without crypto.randomUUID (insecure origins)', () => {
    vi.stubGlobal('crypto', { getRandomValues: (array: Uint8Array) => array.fill(255) })
    expect(newId()).toMatch(UUID_V4)
  })

  it('returns distinct ids', () => {
    expect(newId()).not.toBe(newId())
  })
})
