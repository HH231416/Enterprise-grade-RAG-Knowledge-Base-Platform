"""图模块数据结构与受控枚举。

设计约束（与主项目既有语义对齐）：
- 图证据身份 = (批次, 文档, 分块, 关系)，批次与来源快照共同决定有效性。
- 每条可用于事实回答的关系必须携带原文证据与可核验位置。
- 条件未知时保持未知，不允许模型补写。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# 受控枚举：实体类型与关系类型
# 第一版围绕「事项—部门—材料—步骤—条款」，未知类型一律拒绝入库。
# ---------------------------------------------------------------------------

ENTITY_TYPES: frozenset[str] = frozenset({
    "procedure",     # 办事事项
    "department",    # 部门
    "material",      # 材料
    "step",          # 办理步骤
    "clause",        # 制度条款
})

RELATION_TYPES: frozenset[str] = frozenset({
    "handled_by",     # 事项由部门受理
    "requires_material",  # 事项需要材料
    "has_step",       # 事项包含步骤
    "has_prerequisite",    # 事项存在前置条件
    "governed_by",    # 事项依据条款
    "applies_to",     # 条款的适用范围（方向受限，见 ALLOWED_RELATION_DIRECTION）
})

# 关系方向白名单：key 为关系类型，value 为 (source_entity_type, target_entity_type)
ALLOWED_RELATION_SHAPE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "handled_by": (frozenset({"procedure"}), frozenset({"department"})),
    "requires_material": (frozenset({"procedure", "step"}), frozenset({"material"})),
    "has_step": (frozenset({"procedure"}), frozenset({"step"})),
    "has_prerequisite": (frozenset({"procedure"}), frozenset({"procedure", "material"})),
    "governed_by": (frozenset({"procedure", "step"}), frozenset({"clause"})),
    "applies_to": (frozenset({"clause"}), frozenset({"procedure"})),
}

# 字段长度上限，防止模型输出超长内容污染索引
MAX_NAME_LENGTH = 255
MAX_EVIDENCE_LENGTH = 2000
MAX_RELATION_PER_CHUNK = 20
MAX_ENTITIES_PER_BATCH = 200

# 访问范围：第一版图谱仅收录公开资料
ACCESS_SCOPE_PUBLIC = "public"

BatchStatus = Literal[
    "pending",
    "running",
    "pending_review",
    "approved",
    "searchable",
    "publish_failed",
    "superseded",
    "failed",
]

ExtractionResult = Literal[
    "candidates",            # 成功抽取到候选
    "empty",                 # 成功执行但无可用候选
    "llm_failed",            # 模型调用失败（超时/错误）
    "invalid_output",        # 输出格式非法
    "evidence_rejected",     # 候选未通过证据校验
]


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """一次构图所依据的来源快照。

    指纹变化意味着旧批次的候选不再对应当前内容，不应继续作为有效证据。
    """

    doc_uuid: str
    doc_version: str
    source_fingerprint: str
    chunk_count: int
    access_scope: str
    file_hash: str = ""

    def is_same_source(self, other: "SourceSnapshot") -> bool:
        return (
            self.doc_uuid == other.doc_uuid
            and self.doc_version == other.doc_version
            and self.source_fingerprint == other.source_fingerprint
            and self.access_scope == other.access_scope
        )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """关系所依据的原文证据定位。"""

    doc_uuid: str
    chunk_uuid: str
    chunk_index: int
    evidence_text: str
    section_title: str | None = None
    page_no: int | None = None


@dataclass(frozen=True, slots=True)
class GraphEntityRecord:
    key: str            # 归一化实体键，用于消歧
    entity_type: str
    name: str
    aliases: tuple[str, ...] = ()

    @property
    def is_valid_type(self) -> bool:
        return self.entity_type in ENTITY_TYPES


@dataclass(frozen=True, slots=True)
class GraphRelationRecord:
    """一条候选/已发布关系。

    每条关系必须绑定 evidence 与 batch_uuid；无原文证据的关系不允许发布。
    """

    relation_type: str
    source_key: str
    source_type: str
    source_name: str
    target_key: str
    target_type: str
    target_name: str
    evidence: EvidenceRef
    batch_uuid: str
    source_fingerprint: str
    applicability: str | None = None
    applicability_known: bool = False

    @property
    def relation_id(self) -> str:
        return f"{self.source_key}|{self.relation_type}|{self.target_key}"


@dataclass(slots=True)
class CandidateValidationResult:
    """程序校验结果。

    只能证明格式、锚点与受控枚举符合要求，不能证明关系语义正确。
    语义正确性由人工批次审核负责。
    """

    accepted: list[GraphRelationRecord] = field(default_factory=list)
    rejected: list[tuple[GraphRelationRecord, list[str]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_accepted(self) -> bool:
        return bool(self.accepted)


@dataclass(slots=True)
class GraphRetrievalResult:
    """图检索结果。

    用 status 区分「未启用 / 已启用但无结果 / 服务不可用」，
    不用 None 混淆这三种语义。

    validated 携带已通过批次与快照校验的结果（含权威文档与分块定位），
    调用方直接使用，不需要重复校验。
    """

    status: Literal["disabled", "empty", "ok", "unavailable"]
    relations: list[GraphRelationRecord] = field(default_factory=list)
    validated: list = field(default_factory=list)
    message: str | None = None

    @property
    def is_degraded(self) -> bool:
        return self.status == "unavailable"

    @property
    def has_relations(self) -> bool:
        return bool(self.relations)
