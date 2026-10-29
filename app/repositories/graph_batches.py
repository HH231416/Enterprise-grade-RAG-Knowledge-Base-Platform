"""图批次、候选与生效指针的数据访问。

关键约束集中在这里，避免各入口自行拼装状态判断：
- 只有 searchable 批次可被设为生效。
- 生效指针每文档唯一，切换是覆盖写。
- 一份来源失效只影响该文档的批次，不删除其他来源支撑的关系。
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.graph import GraphActiveBatch, GraphBuildBatch, GraphCandidate

# 允许进入在线召回的批次状态
SEARCHABLE_STATUS = "searchable"
# 终态：不再被检索
TERMINAL_STATUSES = ("superseded",)


class GraphBatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- 批次 ---------------------------------------------------------------

    def get_by_uuid(self, batch_uuid: UUID) -> GraphBuildBatch | None:
        return self.session.scalar(
            select(GraphBuildBatch).where(GraphBuildBatch.batch_uuid == batch_uuid)
        )

    def get_by_snapshot(
        self, doc_uuid: str, source_fingerprint: str
    ) -> GraphBuildBatch | None:
        """按来源快照查批次。唯一约束保证同一快照只有一个批次，实现构建幂等。"""
        return self.session.scalar(
            select(GraphBuildBatch).where(
                GraphBuildBatch.doc_uuid == doc_uuid,
                GraphBuildBatch.source_fingerprint == source_fingerprint,
            )
        )

    def create_batch(self, **kwargs) -> GraphBuildBatch:
        batch = GraphBuildBatch(**kwargs)
        self.session.add(batch)
        self.session.flush()
        return batch

    def list_batches(
        self, doc_uuid: str | None = None, status: str | None = None
    ) -> list[GraphBuildBatch]:
        stmt = select(GraphBuildBatch)
        if doc_uuid is not None:
            stmt = stmt.where(GraphBuildBatch.doc_uuid == doc_uuid)
        if status is not None:
            stmt = stmt.where(GraphBuildBatch.status == status)
        stmt = stmt.order_by(GraphBuildBatch.created_at.desc())
        return list(self.session.scalars(stmt).all())

    def try_create_batch(self, **kwargs) -> tuple[GraphBuildBatch | None, bool]:
        """幂等创建。命中唯一约束时返回已存在批次，created=False。"""
        try:
            return self.create_batch(**kwargs), True
        except IntegrityError:
            self.session.rollback()
            existing = self.get_by_snapshot(
                kwargs["doc_uuid"], kwargs["source_fingerprint"]
            )
            return existing, False

    def set_status(
        self,
        batch_uuid: UUID,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        extraction_result: str | None = None,
        increment_retry: bool = False,
    ) -> None:
        values: dict = {"status": status, "updated_at": datetime.now(UTC)}
        if error_code is not None:
            values["error_code"] = error_code
        if error_message is not None:
            values["error_message"] = error_message[:2000]
        if extraction_result is not None:
            values["extraction_result"] = extraction_result
        if increment_retry:
            values["retry_count"] = GraphBuildBatch.retry_count + 1
        self.session.execute(
            update(GraphBuildBatch).where(GraphBuildBatch.batch_uuid == batch_uuid).values(**values)
        )

    def mark_superseded(self, doc_uuid: str, *, keep_batch: UUID | None = None) -> int:
        """来源变化后使旧批次失效。

        除 keep_batch 指定的新批次外，该文档其他批次一律置 superseded。
        """
        stmt = update(GraphBuildBatch).where(
            GraphBuildBatch.doc_uuid == doc_uuid,
            GraphBuildBatch.status.notin_(TERMINAL_STATUSES),
        )
        if keep_batch is not None:
            stmt = stmt.where(GraphBuildBatch.batch_uuid != keep_batch)
        result = self.session.execute(stmt.values(status="superseded", updated_at=datetime.now(UTC)))
        return int(result.rowcount or 0)

    def mark_reviewed(self, batch_uuid: UUID, reviewer: str) -> None:
        self.session.execute(
            update(GraphBuildBatch)
            .where(GraphBuildBatch.batch_uuid == batch_uuid)
            .values(
                reviewed_by=reviewer,
                reviewed_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    def mark_published(self, batch_uuid: UUID) -> None:
        self.session.execute(
            update(GraphBuildBatch)
            .where(GraphBuildBatch.batch_uuid == batch_uuid)
            .values(
                status=SEARCHABLE_STATUS,
                published_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    # -- 候选 ---------------------------------------------------------------

    def add_candidates(self, candidates: list[GraphCandidate]) -> None:
        if candidates:
            self.session.add_all(candidates)
            self.session.flush()

    def delete_candidates(self, batch_uuid: UUID) -> None:
        """删除批次全部候选。重建同批次时调用，保证候选不重复累积。"""
        for candidate in self.session.scalars(
            select(GraphCandidate).where(GraphCandidate.batch_uuid == batch_uuid)
        ):
            self.session.delete(candidate)
        self.session.flush()

    def list_candidates(
        self, batch_uuid: UUID, review_status: str | None = None
    ) -> list[GraphCandidate]:
        stmt = select(GraphCandidate).where(GraphCandidate.batch_uuid == batch_uuid)
        if review_status is not None:
            stmt = stmt.where(GraphCandidate.review_status == review_status)
        stmt = stmt.order_by(GraphCandidate.chunk_index, GraphCandidate.id)
        return list(self.session.scalars(stmt).all())

    def count_candidates(self, batch_uuid: UUID, review_status: str | None = None) -> int:
        stmt = select(GraphCandidate).where(GraphCandidate.batch_uuid == batch_uuid)
        if review_status is not None:
            stmt = stmt.where(GraphCandidate.review_status == review_status)
        from sqlalchemy import func

        return int(self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)

    def set_candidate_review_status(
        self, candidate_uuids: list[UUID], review_status: str
    ) -> int:
        if not candidate_uuids:
            return 0
        result = self.session.execute(
            update(GraphCandidate)
            .where(GraphCandidate.candidate_uuid.in_(candidate_uuids))
            .values(review_status=review_status)
        )
        return int(result.rowcount or 0)

    def refresh_candidate_counts(self, batch_uuid: UUID) -> None:
        approved = self.count_candidates(batch_uuid, "approved")
        rejected = self.count_candidates(batch_uuid, "rejected")
        self.session.execute(
            update(GraphBuildBatch)
            .where(GraphBuildBatch.batch_uuid == batch_uuid)
            .values(approved_count=approved, rejected_count=rejected)
        )

    # -- 生效指针 -----------------------------------------------------------

    def get_active(self, doc_uuid: str) -> GraphActiveBatch | None:
        return self.session.scalar(
            select(GraphActiveBatch).where(GraphActiveBatch.doc_uuid == doc_uuid)
        )

    def list_active(self, doc_uuids: list[str] | None = None) -> list[GraphActiveBatch]:
        stmt = select(GraphActiveBatch)
        if doc_uuids is not None:
            stmt = stmt.where(GraphActiveBatch.doc_uuid.in_(doc_uuids))
        return list(self.session.scalars(stmt).all())

    def list_searchable_batch_uuids(self) -> dict:
        """返回 doc_uuid → batch_uuid 的生效映射，只有 searchable 批次算数。

        键统一为 uuid.UUID：图侧字符串 doc_uuid 需归一化后才能与此对齐。
        """
        stmt = (
            select(GraphActiveBatch.doc_uuid, GraphActiveBatch.batch_uuid)
            .join(
                GraphBuildBatch,
                GraphBuildBatch.batch_uuid == GraphActiveBatch.batch_uuid,
            )
            .where(GraphBuildBatch.status == SEARCHABLE_STATUS)
        )
        return {doc_uuid: batch_uuid for doc_uuid, batch_uuid in self.session.execute(stmt)}

    def activate(self, doc_uuid: str, batch_uuid: UUID, source_fingerprint: str, actor: str | None) -> GraphActiveBatch:
        """切换生效指针。每文档唯一，重复调用为覆盖写（幂等）。"""
        existing = self.get_active(doc_uuid)
        if existing is not None:
            existing.batch_uuid = batch_uuid
            existing.source_fingerprint = source_fingerprint
            existing.activated_by = actor
            existing.activated_at = datetime.now(UTC)
            self.session.flush()
            return existing
        pointer = GraphActiveBatch(
            doc_uuid=doc_uuid,
            batch_uuid=batch_uuid,
            source_fingerprint=source_fingerprint,
            activated_by=actor,
        )
        self.session.add(pointer)
        self.session.flush()
        return pointer

    def deactivate(self, doc_uuid: str) -> None:
        """撤回指针。不删除其他来源支撑的关系。"""
        pointer = self.get_active(doc_uuid)
        if pointer is not None:
            self.session.delete(pointer)
            self.session.flush()
