import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createConversation, deleteConversation, fetchConversations } from '../api'
import type { ConversationItem } from '../types'

export function QaListPage() {
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<ConversationItem[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    fetchConversations()
      .then((data) => setConversations(data.conversations))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  async function handleCreate() {
    setCreating(true)
    try {
      const data = await createConversation({})
      navigate(`/qa/${data.conversation_id}`)
    } catch { setCreating(false) }
  }

  async function handleDelete(conversationId: string, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      await deleteConversation(conversationId)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== conversationId))
    } catch (error) {
      console.error('删除会话失败', error)
    }
  }

  return (
    <>
      <section className="panel hero-panel compact-hero">
        <div className="hero-copy">
          <p className="eyebrow">智能问答</p>
          <h2>多轮对话</h2>
          <p className="muted">基于知识库的智能问答，支持多轮上下文对话。</p>
        </div>
        <div className="hero-actions">
          <button className="primary-button" onClick={handleCreate} disabled={creating}>
            {creating ? '创建中...' : '新建对话'}
          </button>
        </div>
      </section>

      <section className="panel">
        {loading ? (
          <div className="empty-debug-state"><strong>加载中...</strong></div>
        ) : conversations.length === 0 ? (
          <div className="empty-debug-state">
            <strong>暂无对话</strong>
            <p>点击「新建对话」开始基于知识库的智能问答。</p>
          </div>
        ) : (
          <div className="conversation-list">
            {conversations.map((conv) => (
              <article
                key={conv.conversation_id}
                className="conversation-card"
                onClick={() => navigate(`/qa/${conv.conversation_id}`)}
                style={{ cursor: 'pointer' }}
              >
                <div className="conversation-card-main">
                  <h4>{conv.title}</h4>
                  <p className="muted">{conv.last_message || '暂无消息'}</p>
                </div>
                <div className="conversation-card-meta">
                  <span className="badge">{conv.message_count} 条消息</span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {new Date(conv.updated_at).toLocaleString('zh-CN')}
                  </span>
                  <button
                    className="danger-button"
                    style={{ marginTop: 4, fontSize: 12 }}
                    onClick={(e) => handleDelete(conv.conversation_id, e)}
                  >
                    删除
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  )
}
