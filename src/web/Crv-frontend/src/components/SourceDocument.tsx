import { useEffect, useState } from 'react'
import { sourceDocumentAvailable, sourceDocumentUrl } from '../api/client'
import './SourceDocument.css'

interface SourceDocumentProps {
  digest: string
  pages: number[]
  title: string
}

type Availability = 'checking' | 'ready' | 'missing'

/** The cited source PDF in the browser's own viewer, opened on the cited page. */
export function SourceDocument({ digest, pages, title }: SourceDocumentProps) {
  const url = sourceDocumentUrl(digest)
  const [page, setPage] = useState<number | null>(pages[0] ?? null)
  const [availability, setAvailability] = useState<Availability>('checking')

  useEffect(() => {
    let cancelled = false
    void sourceDocumentAvailable(url).then((ok) => {
      if (!cancelled) setAvailability(ok ? 'ready' : 'missing')
    })
    return () => {
      cancelled = true
    }
  }, [url])

  const target = page === null ? url : `${url}#page=${page}`

  if (availability === 'checking') {
    return <p className="source-doc-state">Chargement du document…</p>
  }
  if (availability === 'missing') {
    return (
      <p className="source-doc-state source-doc-error" role="alert">
        Le document source n'est pas disponible. Les passages cités restent consultables dans
        l'onglet « Extrait ».
      </p>
    )
  }

  return (
    <div className="source-doc">
      <div className="source-doc-bar">
        {pages.length > 1 && (
          <div className="source-doc-pages" role="group" aria-label="Pages citées">
            {pages.map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={page === value}
                className={page === value ? 'is-active' : ''}
                onClick={() => setPage(value)}
              >
                p. {value}
              </button>
            ))}
          </div>
        )}
        <a className="source-doc-open" href={target} target="_blank" rel="noreferrer">
          Ouvrir dans un nouvel onglet
        </a>
      </div>
      <iframe key={target} className="source-doc-frame" src={target} title={`PDF : ${title}`} />
    </div>
  )
}
