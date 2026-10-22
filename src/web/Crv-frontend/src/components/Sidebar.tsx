import type { ConversationSummary } from '../types'
import { Icon } from './Icon'
import './Sidebar.css'

interface SidebarProps {
  conversations: ConversationSummary[]
  activeThreadId: string | null
  onSelect: (threadId: string) => void
  onNewConversation: () => void
  onDelete: (threadId: string) => void
}

function formatRelativeDate(iso: string): string {
  const date = new Date(iso)
  const diffMs = Date.now() - date.getTime()
  const diffMin = Math.round(diffMs / 60000)
  if (diffMin < 1) return "à l'instant"
  if (diffMin < 60) return `il y a ${diffMin} min`
  const diffH = Math.round(diffMin / 60)
  if (diffH < 24) return `il y a ${diffH} h`
  const diffD = Math.round(diffH / 24)
  return `il y a ${diffD} j`
}

export function Sidebar({
  conversations,
  activeThreadId,
  onSelect,
  onNewConversation,
  onDelete,
}: SidebarProps) {
  return (
    <aside className="app-sidebar">
      <button className="sidebar-new-btn" type="button" onClick={onNewConversation}>
        <Icon name="plus" size={16} />
        Nouvelle conversation
      </button>

      <div className="sidebar-list" role="list">
        {conversations.length === 0 && (
          <p className="sidebar-empty">Vos conversations récentes apparaîtront ici.</p>
        )}
        {conversations.map((c) => (
          <div
            key={c.threadId}
            role="listitem"
            className={`sidebar-item${c.threadId === activeThreadId ? ' active' : ''}`}
          >
            <button className="sidebar-item-btn" type="button" onClick={() => onSelect(c.threadId)}>
              <span className="sidebar-item-title">{c.title || 'Conversation'}</span>
              <span className="sidebar-item-date">{formatRelativeDate(c.updatedAt)}</span>
            </button>
            <button
              className="sidebar-item-delete"
              type="button"
              aria-label="Supprimer cette conversation de l'historique"
              onClick={() => onDelete(c.threadId)}
            >
              ×
            </button>
          </div>
        ))}
      </div>

      <div className="sidebar-footer">Reminex · Centre de Recherche</div>
    </aside>
  )
}
