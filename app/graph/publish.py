"""图索引受控发布。

发布语义：
- 只有 approved 批次可发布；未审核候选不参与在线检索。
- 发布前重新校验来源快照：来源已变化则阻止发布，要求重新构建与审核。
- 图写入成功后才切换生效指针并置 searchable。
- 写入失败置 publish_failed，可重试；重复执行不产生重复发布。

发布可见性由业务库的有效指针决定，不由图存储自证：
即使 Neo4j 已写入，若指针切换失败，该批次仍不会进入在线回答。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.graph.builder import GraphBuildService, is_public_document
from app.graph.errors import (
    GraphAdminRequiredError,
    GraphPublishFailedError,
    GraphScopeError,
    GraphSourceChangedError,
)
from app.graph.types import SourceSnapshot
from app.repositories.graph_batches import GraphBatchRepository

logger = logging.getLogger(__name__)

APPROVABLE_STATUSES = ("pending_review", "publish_failed")


@dataclass(slots=True)
class PublishOutcome:
    batch_uuid: str
    published: bool
    status: str
    approved_count: int = 0
    rejected_count: int = 0
    message: str | None = None


class GraphPublishService:
    def __init__(self, session: Session, store=None) -> None:
        self.session = session
        self.batches = GraphBatchRepository(session)
        self.builder = GraphBuildService(session)
        self._store = store

    def _resolve_store(self):
        if self._store is not None:
            return self._store
        from app.graph.factory import get_graph_store

        return get_graph_store()

    # -- 审批 ---------------------------------------------------------------

    def approve_batch(
        self,
        batch_uuid: UUID,
        *,
        rejected_candidate_uuids: list[UUID],
        admin_actor: str | None,
    ) -> PublishOutcome:
        """管理员剔除候选后批准其余候选。

        admin_actor 必须是非空可信管理员标识。require_admin_actor 在配置关闭时
        返回 None，此时不得继续审批。
        """
        if not admin_actor:
            raise GraphAdminRequiredError()

        batch = self.batches.get_by_uuid(batch_uuid)
        if batch is None:
            raise GraphScopeError(f"graph batch {batch_uuid} not found")
        if batch.status not in APPROVABLE_STATUSES:
            raise GraphScopeError(
                f"batch {batch_uuid} is not awaiting review (status={batch.status})"
            )

        # 校验公开范围与权威可检索状态：非公开或已撤回的资料不能被批准发布
        document = self.builder.load_eligible_document(batch.doc_uuid)
        self.builder.assert_public_scope(document)

        # 批准时来源已变化 → 阻止发布陈旧候选
        self._assert_snapshot_current(batch)

        candidates = self.batches.list_candidates(batch.batch_uuid)
        # 统一按 UUID 对象比较：candidate_uuid 是 UUID 类型，
        # 若与字符串集合比较会恒不相等，导致剔除失效。
        rejected_ids = {UUID(str(item)) for item in rejected_candidate_uuids}

        to_reject = [c.candidate_uuid for c in candidates if c.candidate_uuid in rejected_ids]
        to_approve = [
            c.candidate_uuid
            for c in candidates
            if c.review_status == "pending" and c.candidate_uuid not in rejected_ids
        ]

        # 幂等：重复批准同一批次不产生额外副作用
        self.batches.set_candidate_review_status(to_reject, "rejected")
        self.batches.set_candidate_review_status(to_approve, "approved")
        self.batches.refresh_candidate_counts(batch.batch_uuid)
        self.batches.mark_reviewed(batch.batch_uuid, admin_actor)
        self.batches.set_status(batch.batch_uuid, "approved")
        self.session.commit()

        approved_count = self.batches.count_candidates(batch.batch_uuid, "approved")
        rejected_count = self.batches.count_candidates(batch.batch_uuid, "rejected")
        return PublishOutcome(
            batch_uuid=str(batch.batch_uuid),
            published=False,
            status="approved",
            approved_count=approved_count,
            rejected_count=rejected_count,
        )

    # -- 发布 ---------------------------------------------------------------

    def publish_batch(self, batch_uuid: UUID, *, admin_actor: str | None) -> PublishOutcome:
        """把已批准批次写入图索引并切换生效指针。

        步骤顺序刻意为「先校验 → 再写图 → 最后切指针」，
        保证指针只指向确实写入成功且快照仍然有效的批次。
        """
        if not admin_actor:
            raise GraphAdminRequiredError()

        batch = self.batches.get_by_uuid(batch_uuid)
        if batch is None:
            raise GraphScopeError(f"graph batch {batch_uuid} not found")
        # searchable 也允许重发：发布是幂等操作，重复调用用于修复图存储侧不一致，
        # 不应因状态已是 searchable 而被拒。
        if batch.status not in ("approved", "publish_failed", "searchable"):
            raise GraphScopeError(
                f"batch {batch_uuid} is not approved for publish (status={batch.status})"
            )

        document = self.builder.load_eligible_document(batch.doc_uuid)
        self.builder.assert_public_scope(document)
        self._assert_snapshot_current(batch)

        approved = self.batches.list_candidates(batch.batch_uuid, "approved")
        relations = [c.to_relation_record(batch.source_fingerprint) for c in approved]

        store = self._resolve_store()
        # 存储不可用时必须发布失败，绝不能把数据写进进程内存并标记 searchable。
        # 那样会造成「配置了持久化但实际数据随进程消失」的假发布。
        try:
            usable = store.probe()
        except Exception as exc:  # noqa: BLE001
            usable = False
            store_reason = str(exc)
        else:
            store_reason = "graph store probe failed"
        if not usable:
            self.batches.set_status(
                batch.batch_uuid,
                "publish_failed",
                error_code="graph_store_unavailable",
                error_message=store_reason,
            )
            self.session.commit()
            raise GraphPublishFailedError(
                f"graph store is unavailable, publish aborted: {store_reason}"
            )

        try:
            # 必须用 replace 而非 upsert：管理员可能剔除了部分候选，
            # upsert 不会清除已剔除候选在图存储中的既有关系。
            store.replace_batch(str(batch.batch_uuid), relations)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph publish write failed: %s", exc)
            self.batches.set_status(
                batch.batch_uuid,
                "publish_failed",
                error_code="graph_write_failed",
                error_message=str(exc)[:2000],
                increment_retry=True,
            )
            self.session.commit()
            raise GraphPublishFailedError(f"graph index write failed: {exc}") from exc

        # 写图成功后再切指针。指针切换失败时批次仍非 searchable，不进入在线回答。
        self.batches.activate(
            doc_uuid=batch.doc_uuid,
            batch_uuid=batch.batch_uuid,
            source_fingerprint=batch.source_fingerprint,
            actor=admin_actor,
        )
        self.batches.mark_published(batch.batch_uuid)
        self.session.commit()

        return PublishOutcome(
            batch_uuid=str(batch.batch_uuid),
            published=True,
            status="searchable",
            approved_count=len(approved),
            rejected_count=self.batches.count_candidates(batch.batch_uuid, "rejected"),
        )

    def approve_and_publish(
        self,
        batch_uuid: UUID,
        *,
        rejected_candidate_uuids: list[UUID] | None = None,
        admin_actor: str | None,
    ) -> PublishOutcome:
        approve = self.approve_batch(
            batch_uuid,
            rejected_candidate_uuids=rejected_candidate_uuids or [],
            admin_actor=admin_actor,
        )
        if approve.approved_count == 0:
            # 全部候选被剔除：不发布图索引，批次保持 approved 且不生效
            return approve
        return self.publish_batch(batch_uuid, admin_actor=admin_actor)

    # -- 失效 ---------------------------------------------------------------

    def invalidate_document(self, doc_uuid: str, *, actor: str | None = None, physical: bool = False) -> None:
        """资料不可检索时立即阻止其图证据继续使用。

        默认只撤指针与失效批次（逻辑阻断立即生效）。
        physical=True 时额外清理图存储中的关系数据。
        共享实体可能由其他有效文档支撑，因此不删除其他文档的批次。
        """
        pointer = self.batches.get_active(doc_uuid)
        batch_uuid = str(pointer.batch_uuid) if pointer is not None else None

        self.batches.deactivate(doc_uuid)
        self.batches.mark_superseded(doc_uuid)

        if physical and batch_uuid:
            try:
                self._resolve_store().remove_batch(batch_uuid)
            except Exception as exc:  # noqa: BLE001
                # 物理清理失败不影响逻辑阻断
                logger.warning("graph physical cleanup failed for %s: %s", batch_uuid, exc)
        self.session.commit()

    # -- 内部 ---------------------------------------------------------------

    def _assert_snapshot_current(self, batch) -> None:
        """发布前校验来源快照未变化。"""
        snapshot, _ = self.builder.build_snapshot(batch_document(self.session, batch.doc_uuid))
        expected = SourceSnapshot(
            doc_uuid=batch.doc_uuid,
            doc_version=batch.doc_version,
            source_fingerprint=batch.source_fingerprint,
            chunk_count=batch.chunk_count,
            access_scope=batch.access_scope,
        )
        if not snapshot.is_same_source(expected):
            self.batches.set_status(
                batch.batch_uuid,
                "failed",
                error_code="source_snapshot_changed",
                error_message="document changed after batch creation; rebuild and review required",
            )
            self.session.commit()
            raise GraphSourceChangedError()


def batch_document(session: Session, doc_uuid: str):
    from sqlalchemy import select

    from app.models.document import Document

    document = session.scalar(select(Document).where(Document.doc_uuid == doc_uuid))
    if document is None:
        raise GraphScopeError(f"document {doc_uuid} not found")
    return document


__all__ = ["GraphPublishService", "PublishOutcome", "is_public_document"]
