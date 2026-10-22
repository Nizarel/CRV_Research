import { useCallback, useState } from 'react'
import './App.css'
import { ChatInput } from './components/ChatInput'
import { ChatWindow } from './components/ChatWindow'
import { EvidenceModal } from './components/EvidenceModal'
import { Header } from './components/Header'
import { Sidebar } from './components/Sidebar'
import { citationNumbers } from './evidence'
import { useChat } from './hooks/useChat'
import { useConversations } from './hooks/useConversations'
import type { Answer, Evidence } from './types'

interface OpenedEvidence {
  evidence: Evidence
  answer: Answer
}

function App() {
  const { conversations, upsert, remove } = useConversations()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [opened, setOpened] = useState<OpenedEvidence | null>(null)

  const { threadId, messages, isStreaming, error, sendMessage, reset, loadThread } = useChat(upsert)

  const handleSelectConversation = useCallback(
    (id: string) => {
      void loadThread(id)
      setSidebarOpen(false)
    },
    [loadThread],
  )

  const handleNewConversation = useCallback(() => {
    reset()
    setSidebarOpen(false)
  }, [reset])

  const handleDeleteConversation = useCallback(
    (id: string) => {
      remove(id)
      if (id === threadId) reset()
    },
    [remove, reset, threadId],
  )

  const handleOpenEvidence = useCallback(
    (evidence: Evidence, answer: Answer) => setOpened({ evidence, answer }),
    [],
  )
  const closeEvidence = useCallback(() => setOpened(null), [])

  return (
    <div className={`app-shell${sidebarOpen ? ' sidebar-open' : ''}`}>
      <Header onToggleSidebar={() => setSidebarOpen((v) => !v)} />

      <Sidebar
        conversations={conversations}
        activeThreadId={threadId}
        onSelect={handleSelectConversation}
        onNewConversation={handleNewConversation}
        onDelete={handleDeleteConversation}
      />

      <main className="app-main">
        <ChatWindow
          messages={messages}
          error={error}
          onOpenEvidence={handleOpenEvidence}
          onPickExample={(question) => void sendMessage(question)}
        />
        <ChatInput disabled={isStreaming} onSend={(text) => void sendMessage(text)} />
      </main>

      {sidebarOpen && <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />}

      {opened && (
        <EvidenceModal
          evidence={opened.evidence}
          answer={opened.answer}
          number={citationNumbers(opened.answer).get(opened.evidence.id) ?? 0}
          onClose={closeEvidence}
        />
      )}
    </div>
  )
}

export default App
