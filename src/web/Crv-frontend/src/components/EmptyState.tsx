import reminexLogo from '../assets/reminex-logo.jpg'
import './EmptyState.css'

const EXAMPLE_QUESTIONS = [
  "Quel est l'effet de la substitution de l'eau de process par l'eau de mer sur la flottation de Tizert ?",
  'Quelles sont les conditions de lixiviation retenues pour le minerai de la zone de cémentation de Hajar, et pourquoi la flottation a-t-elle été écartée ?',
  "Retrace l'historique des campagnes de pilotage de la flottation de Tizert de 2015 à 2024 et l'évolution des performances.",
  'Quelles voies de valorisation aval du cuivre (cathode/blister) ont été étudiées par Managem et quels sont les chiffres clés ?',
]

interface EmptyStateProps {
  onPick: (question: string) => void
}

export function EmptyState({ onPick }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <img src={reminexLogo} alt="Reminex — Managem Group" className="empty-state-logo" />
      <h1>CRV Research</h1>
      <p className="empty-state-tagline">
        Interrogez le corpus de recherche du Centre de Recherche : rapports de laboratoire et de
        pilotage, études de faisabilité, articles. Chaque réponse cite les passages exacts qui la
        fondent ; quand les sources ne suffisent pas, l'assistant le dit.
      </p>

      <div className="empty-state-examples">
        {EXAMPLE_QUESTIONS.map((q) => (
          <button key={q} type="button" className="example-chip" onClick={() => onPick(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  )
}
