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
          metadata: {
            ...(data.latency_ms as unknown as Record<string, unknown>),
            graph_status: data.graph_status,
            graph_completeness: data.graph_completeness,
          },
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
                <GraphNotice metadata={msg.metadata} />
                {msg.citations && msg.citations.length > 0 && (
                  <details className="qa-citations">
                    <summary>引用来源 ({msg.citations.length})</summary>
                    {msg.citations.map((c: Citation, i: number) => (
                      <CitationItem key={i} citation={c} index={i} />
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

/** 图证据只作为原文引用的辅助解释，事实以引用原文为准。 */
function CitationItem({ citation, index }: { citation: Citation; index: number }) {
  const isGraphOnly = citation.evidence_type === 'graph_relation'
  const relations = citation.graph_relations ?? []
  const sources = citation.recall_sources ?? []
  return (
    <div className="qa-citation-item">
      <strong>[{index + 1}] {citation.title}</strong>
      <span className="muted">
        模块: {citation.source_module} | 分数: {citation.score.toFixed(3)}
        {citation.chunk_index != null ? ` | 分块: ${citation.chunk_index}` : ''}
      </span>
      {sources.length > 0 && (
        <span className="muted"> | 召回来源: {sources.join(' + ')}</span>
      )}
      {isGraphOnly && (
        <span className="muted"> | 来源: 图关系命中的原文分块</span>
      )}
      <p>{citation.snippet}</p>
      {relations.length > 0 && (
        <details className="qa-graph-relations">
          <summary>关系路径 ({relations.length})</summary>
          {relations.map((r, i) => (
            <div key={i} className="qa-graph-relation-item">
              <code>{r.relation_type}</code>
              <span>
                {r.source_entity_name}
                <span className="muted">（{r.source_entity_type}）</span>
                {' → '}
                {r.target_entity_name}
                <span className="muted">（{r.target_entity_type}）</span>
              </span>
              {r.applicability_known ? (
                <span className="muted"> | 适用条件: {r.applicability}</span>
              ) : (
                <span className="muted"> | 适用条件: 原文未说明</span>
              )}
            </div>
          ))}
        </details>
      )}
    </div>
  )
}

/** 图检索状态与证据完整性提示。降级与不完整都如实展示，不静默。 */
function GraphNotice({ metadata }: { metadata: Record<string, unknown> | null }) {
  if (!metadata) return null
  const status = metadata.graph_status as string | null | undefined
  const completeness = metadata.graph_completeness as
    | { missing_relations?: string[]; unknown_applicability?: boolean }
    | null
    | undefined
  if (!status || status === 'disabled') return null

  if (status === 'unavailable') {
    return (
      <div className="qa-graph-notice muted">
        图关系检索暂不可用，本次回答仅基于文档检索证据。
      </div>
    )
  }

  const missing = completeness?.missing_relations ?? []
  if (missing.length > 0) {
    return (
      <div className="qa-graph-notice warn">
        证据不完整，缺少关系：{missing.join('、')}。当前不作确定结论。
        {completeness?.unknown_applicability ? '部分依据的适用条件未知。' : ''}
      </div>
    )
  }
  if (status === 'ok') {
    return (
      <div className="qa-graph-notice">
        已结合图关系证据（仅公开校园制度与办事指南）。
        {completeness?.unknown_applicability ? '部分依据的适用条件未知，请以原文条款为准。' : ''}
      </div>
    )
  }
  return null
}
