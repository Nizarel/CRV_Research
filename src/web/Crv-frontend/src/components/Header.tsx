import reminexLogo from '../assets/reminex-logo.jpg'
import { Icon } from './Icon'
import './Header.css'

interface HeaderProps {
  onToggleSidebar: () => void
}

export function Header({ onToggleSidebar }: HeaderProps) {
  return (
    <header className="app-header">
      <button
        className="header-menu-btn"
        onClick={onToggleSidebar}
        aria-label="Afficher/masquer l'historique des conversations"
        type="button"
      >
        <Icon name="menu" size={20} />
      </button>

      <div className="header-brand">
        <img src={reminexLogo} alt="Reminex — Managem Group" className="header-logo" />
        <span className="header-divider" aria-hidden="true" />
        <div className="header-titles">
          <span className="header-title">CRV Research</span>
          <span className="header-subtitle">Assistant de recherche documentaire</span>
        </div>
      </div>

      <div className="header-badge" title="Chaque affirmation est liée à une citation exacte du corpus">
        <Icon name="shield" size={14} />
        Réponses sourcées
      </div>
    </header>
  )
}
