import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { fetchConversation, sendMessage } from '../api'
import type { Citation, ConversationMessage } from '../types'

export function QaChatPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [title, setTitle] = useState('')
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!id) return
    fetchConversation(id).then((data) => {
      setMessages(data.messages)
      setTitle(data.title)
    }).catch(() => navigate('/qa'))
  }, [id, navigate])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function handleSend(e: FormEvent) {
    e.preventDefault()
    if (!input.trim() || sending || !id) return
    const question = input.trim()
    setInput('')
    setSending(true)
    // 乐观更新用户消息
    setMessages((prev) => [
      ...prev,
      {
        message_id: 'temp-' + Date.now(),
        role: 'user',
        content: question,
        citations: null,
        metadata: null,
        created_at: new Date().toISOString(),
      },
    ])
    try {
      const data = await sendMessage(id, { question })
      setMessages((prev) => [
        ...prev,
        {
          message_id: data.message_id,
          role: 'assistant',
          content: data.answer,
          citations: data.citations,
          metadata: data.latency_ms as unknown as Record<string, unknown>,
          created_at: new Date().toISOString(),
        },
      ])
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          message_id: 'err-' + Date.now(),
          role: 'assistant',
          content: '抱歉，请求失败，请重试。',
          citations: null,
          metadata: null,
          created_at: new Date().toISOString(),
        },
      ])
    } finally { setSending(false) }
  }

  return (
    <div className="qa-chat-container">
      <header className="qa-chat-header">
        <button className="text-button" onClick={() => navigate('/qa')}>
          ← 返回列表
        </button>
        <h3>{title}</h3>
        <span style={{ width: 80 }} />
      </header>

      <div className="qa-chat-messages">
        {messages.length === 0 ? (
          <div className="empty-debug-state">
            <strong>开始对话</strong>
            <p>输入问题，基于知识库获取智能回答。</p>
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.message_id}
              className={`qa-message ${msg.role === 'user' ? 'qa-message-user' : 'qa-message-assistant'}`}
            >
              <div className="qa-message-bubble">
                <div className="qa-message-content">{msg.content}</div>
                {msg.citations && msg.citations.length > 0 && (
                  <details className="qa-citations">
                    <summary>引用来源 ({msg.citations.length})</summary>
                    {msg.citations.map((c: Citation, i: number) => (
                      <div key={i} className="qa-citation-item">
                        <strong>[{i + 1}] {c.title}</strong>
                        <span className="muted">模块: {c.source_module} | 分数: {c.score.toFixed(3)}</span>
                        <p>{c.snippet}</p>
                      </div>
                    ))}
                  </details>
                )}
                {msg.metadata && (
                  <div className="qa-latency muted">
                    {(msg.metadata as Record<string, number>).total != null
                      ? `耗时 ${(msg.metadata as Record<string, number>).total}ms`
                      : ''}
                  </div>
                )}
              </div>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      <form className="qa-chat-input" onSubmit={handleSend}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入你的问题..."
          disabled={sending}
        />
        <button className="primary-button" type="submit" disabled={sending || !input.trim()}>
          {sending ? '发送中...' : '发送'}
        </button>
      </form>
    </div>
  )
}
