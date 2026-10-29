import { useCallback, useEffect, useState } from 'react'
import {
  approveGraphBatch,
  createGraphBatch,
  fetchDocumentGraphStatus,
  fetchGraphBatch,
  publishGraphBatch,
  retryGraphBatch,
  runGraphExtraction,
} from '../api'
import type { GraphBatchDetail, GraphDocumentStatus } from '../types'

/**
 * 文档图谱构建与批次候选审核。
 *
 * 只做「能解释答案来源、能看见构建状态与失败原因」：
 * 展示候选关系的实体、方向、原文证据与已知适用条件，支持剔除后批准与发布。
 * 交互式全图浏览不在范围内。
 */
export function GraphPanel({ docUuid, accessLevel }: { docUuid: string; accessLevel?: string }) {
  const [status, setStatus] = useState<GraphDocumentStatus | null>(null)
  const [detail, setDetail] = useState<GraphBatchDetail | null>(null)
  const [rejected, setRejected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const isPublic = ['public', 'open', '公开'].includes((accessLevel ?? '').trim().toLowerCase())

  const reload = useCallback(async () => {
    try {
      const data = await fetchDocumentGraphStatus(docUuid)
      setStatus(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载图构建状态失败')
    }
  }, [docUuid])

  useEffect(() => {
    setDetail(null)
    setRejected(new Set())
    setMessage(null)
    setError(null)
    reload()
  }, [reload])

  async function openBatch(batchUuid: string) {
    try {
      const data = await fetchGraphBatch(batchUuid)
      setDetail(data)
      setRejected(new Set(data.candidates.filter((c) => c.review_status === 'rejected').map((c) => c.candidate_uuid)))
      setMessage(null)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载批次详情失败')
    }
  }

  async function run(action: () => Promise<unknown>, okText: string) {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      await action()
      setMessage(okText)
      await reload()
      if (detail) await openBatch(detail.batch?.batch_uuid ?? '')
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败')
      await reload()
    } finally {
      setBusy(false)
    }
  }

  if (!isPublic) {
    return (
      <div className="graph-panel">
        <p className="eyebrow">图谱</p>
        <div className="graph-notice muted">
          第一版图谱仅收录公开校园制度与办事指南。当前文档访问级别为
          <code> {accessLevel || '未设置'} </code>
          ，不纳入图构建与图检索；原有文档检索权限不受影响。
        </div>
      </div>
    )
  }

  const latest = status?.batches?.[0]

  return (
    <div className="graph-panel">
      <p className="eyebrow">图谱</p>

      <div className="graph-actions">
        <button
          className="secondary-button compact-button"
          type="button"
          disabled={busy}
          onClick={() =>
            run(async () => {
              const created = await createGraphBatch(docUuid)
              await runGraphExtraction(created.batch_uuid)
              await openBatch(created.batch_uuid)
            }, '已创建构建批次并执行抽取')
          }
        >
          构建图谱
        </button>
        {latest ? (
          <button
            className="secondary-button compact-button"
            type="button"
            disabled={busy}
            onClick={() => run(() => runGraphExtraction(latest.batch_uuid), '已重新抽取')}
          >
            重新抽取
          </button>
        ) : null}
        {latest && latest.status === 'failed' ? (
          <button
            className="secondary-button compact-button"
            type="button"
            disabled={busy}
            onClick={() => run(() => retryGraphBatch(latest.batch_uuid), '已提交重试')}
          >
            重试
          </button>
        ) : null}
        {status?.active_batch_uuid ? (
          <span className="status-chip status-chip-success">图谱已生效</span>
        ) : (
          <span className="status-chip">未生效</span>
        )}
      </div>

      {message ? <div className="graph-notice">{message}</div> : null}
      {error ? <div className="graph-notice warn">{error}</div> : null}

      {status && status.batches.length === 0 ? (
        <p className="muted">尚未构建图谱。</p>
      ) : null}

      {status?.batches?.length ? (
        <table className="graph-batch-table">
          <thead>
            <tr>
              <th>批次</th>
              <th>状态</th>
              <th>抽取结果</th>
              <th>候选/已批准/已剔除</th>
              <th>失败原因</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {status.batches.map((b) => (
              <tr key={b.batch_uuid}>
                <td><code>{b.batch_uuid.slice(0, 8)}</code></td>
                <td>{b.status}</td>
                <td>{b.extraction_result ?? '-'}</td>
                <td>
                  {b.candidate_count} / {b.approved_count} / {b.rejected_count}
                </td>
                <td className="muted">
                  {b.error_code ? `${b.error_code}: ${b.error_message ?? ''}` : '-'}
                </td>
                <td>
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => openBatch(b.batch_uuid)}
                  >
                    查看候选
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {detail?.batch ? (
        <div className="graph-batch-detail">
          <div className="graph-detail-head">
            <strong>候选关系审核</strong>
            <span className="muted">
              抽取器 {detail.batch.extractor_name ?? '-'} / 提示模板{' '}
              {detail.batch.prompt_template_version ?? '-'}
            </span>
          </div>
          <p className="muted">
            程序校验只保证格式、关系类型与原文锚点符合要求，关系语义是否正确需人工确认。
          </p>

          {detail.candidates.length === 0 ? (
            <p className="muted">该批次没有候选关系。</p>
          ) : (
            <ul className="graph-candidate-list">
              {detail.candidates.map((c) => {
                const isRejected = rejected.has(c.candidate_uuid)
                return (
                  <li key={c.candidate_uuid} className={isRejected ? 'graph-candidate rejected' : 'graph-candidate'}>
                    <label className="graph-candidate-head">
                      <input
                        type="checkbox"
                        checked={!isRejected}
                        onChange={(e) => {
                          const next = new Set(rejected)
                          if (e.target.checked) next.delete(c.candidate_uuid)
                          else next.add(c.candidate_uuid)
                          setRejected(next)
                        }}
                      />
                      <code>{c.relation_type}</code>
                      <span>
                        {c.source_entity_name}
                        <span className="muted">（{c.source_entity_type}）</span>
                        {' → '}
                        {c.target_entity_name}
                        <span className="muted">（{c.target_entity_type}）</span>
                      </span>
                      <span className="muted">
                        {isRejected ? '将剔除' : '将批准'} · 分块 {c.chunk_index}
                      </span>
                    </label>
                    <blockquote className="graph-evidence">
                      {c.evidence_text}
                    </blockquote>
                    <div className="muted">
                      适用条件：
                      {c.applicability_known ? (c.applicability ?? '') : '原文未说明（保持未知）'}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}

          <div className="graph-actions">
            <button
              className="primary-button compact-button"
              type="button"
              disabled={busy || detail.batch.status !== 'pending_review'}
              onClick={() =>
                run(
                  () => approveGraphBatch(detail.batch!.batch_uuid, Array.from(rejected)),
                  '已批准所选候选关系',
                )
              }
            >
              批准所选候选
            </button>
            <button
              className="secondary-button compact-button"
              type="button"
              disabled={busy || detail.batch.status !== 'approved'}
              onClick={() => run(() => publishGraphBatch(detail.batch!.batch_uuid), '已发布图索引并生效')}
            >
              发布图索引
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
