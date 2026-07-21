"""文档分块策略模块：实现知识库文档的多种分块算法。

支持策略：
- fixed：固定大小分块（按 max_chars 切分，overlap_chars 重叠）
- structural：结构化分块（按标题层级识别文档结构）
- table-aware：表格感知分块（按行数/分组/字符数切分表格）
- parent-child：父子分块（父块保留上下文，子块用于检索）
- semantic：语义分块（按句子 + 相似度阈值合并）
- chat：聊天记录分块

核心入口：build_chunks(parsed_blocks, options) → list[ChunkPayload]
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from collections import Counter  # 统计行出现频次，用于去除重复噪声行
from dataclasses import dataclass  # 轻量级数据类
import math  # ceil 向上取整，用于 Token 估算
import re  # 正则：标题识别、页码过滤、句子分割
from collections.abc import Callable
from uuid import uuid4  # 为父子分块生成唯一标识符

from app.ingestion.types import ChunkPayload, ChunkingOptions, ParsedBlock  # 核心数据结构

# ============================================================
# 预编译正则表达式（模块级常量，避免每次调用重复编译）
# ============================================================
# 页码标记：匹配 " 3 / 25 " 格式的页码行
_PAGE_MARKER_RE = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")
# Markdown 标题：匹配 # ~ ###### 开头的行
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 数字编号标题：匹配 "1."、"1.2."、"3．" 等
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:[．.]|\s+)\s*(\S.*)$")
# 中文数字编号标题：匹配 "一、"、"十二．" 等
_CHINESE_NUMBERED_HEADING_RE = re.compile(r"^([一二三四五六七八九十百]+)[、．.]\s*(\S.*)$")
# 句子分割：按中英文句末标点拆分为句子列表
_SENTENCE_RE = re.compile(r"[^。！？.!?]+[。！？.!?]?")


# ---- 内部数据类：表示结构化分块中的一个章节 ----
@dataclass(slots=True)  # slots=True：禁用 __dict__，节省内存（每个实例约省 1KB），适合大量 Section 对象场景
class _Section:
    """结构化分块中的章节数据类。
    
    面试点：为什么用 __slots__？
    - 减少内存占用，因为分块过程中会创建大量 Section 对象
    - 禁止动态添加属性，提高属性访问速度
    """
    text: str                # 章节文本内容
    heading: str | None      # 章节标题（可能为 None，表示无标题段落）
    page_no: int | None      # 所在页码
    source: ParsedBlock      # 来源块引用
    start_line: int          # 起始行号（用于追溯原始位置）
    split_reason: str = "heading"  # 分割原因：heading/tail/table_of_contents/size


# ---- 分块策略注册表 ----
class ChunkingStrategyRegistry:
    """分块策略注册表：管理所有支持的分块策略及其参数 schema。
    
    面试点：为什么用注册表模式（Registry Pattern）？
    - 策略与参数解耦：每种策略声明自己需要的参数类型
    - 前端可根据 schema 动态生成配置表单
    - 新增策略只需在 _SCHEMAS 中添加一项即可
    """
    # 策略名 → 参数名到类型的映射字典
    # 前端的策略配置表单由此 schema 动态渲染
    _SCHEMAS = {
        "fixed": {"max_chars": "int", "overlap_chars": "int"},
        "structural": {"max_chars": "int", "overlap_chars": "int"},
        "table-aware": {"table_rows_per_chunk": "int", "max_chars": "int"},
        "parent-child": {
            "parent_max_chars": "int",      # 父块最大字符数（上下文窗口）
            "child_max_chars": "int",       # 子块最大字符数（检索粒度）
            "overlap_chars": "int",         # 子块之间的重叠字符数
        },
        "semantic": {
            "min_chunk_sentences": "int",       # 最小句子数
            "max_chunk_sentences": "int",       # 最大句子数（硬上限）
            "similarity_threshold": "float",    # 语义相似度阈值
        },
        "chat": {"max_chars": "int", "topic_gap_minutes": "int"},
    }

    def list_strategies(self) -> list[dict]:
        """列出所有可用策略及其参数 schema，供前端 API 调用。"""
        return [
            {"name": name, "params_schema": schema}
            for name, schema in self._SCHEMAS.items()
        ]

    def require(self, strategy: str) -> str:
        """验证并规范化策略名称。
        
        Args:
            strategy: 用户传入的策略名称（不区分大小写）
        Returns:
            规范化的小写策略名称
        Raises:
            ValueError: 不支持的策略
        """
        normalized = strategy.strip().lower()  # 去空格 + 小写化
        if normalized not in self._SCHEMAS:
            raise ValueError(f"unsupported chunking strategy: {strategy}")
        return normalized


# ---- 核心入口：分块调度器 ----
def build_chunks(
    parsed_blocks: list[ParsedBlock],
    *,
    options: ChunkingOptions | None = None,  # * 强制 keyword-only，防止传参顺序错误
    semantic_embedder: Callable[[list[str]], list[list[float]]] | None = None,
) -> list[ChunkPayload]:
    """根据解析后的块和策略选项生成分块列表（所有分块策略的统一入口）。
    
    面试点：为什么使用 keyword-only 参数（* 号）？
    - 提高可读性：调用者必须写 build_chunks(blocks, options=opts)
    - 防止参数位置错误：避免把 options 误当成第二个位置参数
    """
    # 使用默认选项或传入的选项
    opts = options or ChunkingOptions()
    # 验证并规范化策略名（不区分大小写）
    strategy = ChunkingStrategyRegistry().require(opts.strategy)
    # 空列表快速返回，避免后续空遍历
    if not parsed_blocks:
        return []

    # ---- 策略分发：根据策略名调用对应的构建函数 ----
    # 面试点：策略模式（Strategy Pattern）
    # 每种分块策略封装为独立的 _build_* 函数，build_chunks 只负责路由
    if strategy == "structural":
        payloads = _build_structural(parsed_blocks, opts)      # 结构化分块
    elif strategy == "table-aware":
        payloads = _build_table_aware(parsed_blocks, opts)     # 表格感知分块
    elif strategy == "parent-child":
        payloads = _build_parent_child(parsed_blocks, opts)    # 父子分块
    elif strategy == "semantic":
        payloads = _build_semantic(parsed_blocks, opts, semantic_embedder=semantic_embedder)
    else:
        # 兜底：fixed 和 chat 都走固定大小分块（chat 的 topic 切分在其他地方处理）
        payloads = _build_fixed(parsed_blocks, opts, strategy=strategy)

    # ---- 后处理：为每个分块统一设置索引和策略标记 ----
    for index, payload in enumerate(payloads):
        payload.chunk_index = index                             # 设置全局序号
        payload.metadata_json["chunking_strategy"] = strategy   # 记录使用的分块策略
    return payloads


# ---- 固定大小分块 ----
def _build_fixed(
    blocks: list[ParsedBlock],
    options: ChunkingOptions,
    *,
    strategy: str = "fixed",
) -> list[ChunkPayload]:
    """固定大小分块：按 max_chars 切分文本，overlap_chars 控制重叠。
    
    面试点：为什么要 overlap（重叠）？
    - 防止关键信息落在分块边界上导致检索遗漏
    - 例如"项目经理负责……"被切在"经理"和"负责"之间，overlap 保证上下文连贯
    - 典型值：max_chars=500, overlap=50（10% 重叠率）
    """
    payloads: list[ChunkPayload] = []
    for block in blocks:
        text = block.text.strip()  # 去除首尾空白
        if not text:
            continue                # 跳过空块
        # 按固定大小切分，带重叠
        parts = _split_by_size(text, options.max_chars, options.overlap_chars)
        for part in parts:
            # 为每个分块创建 payload，标记 split_reason="size"
            payloads.append(_payload_from_block(block, part, metadata={"split_reason": "size"}))
    return payloads


# ---- 结构化分块 ----
def _build_structural(blocks: list[ParsedBlock], options: ChunkingOptions) -> list[ChunkPayload]:
    """结构化分块：按标题层级识别文档结构，保持章节完整性。
    
    面试点：结构化分块 vs 固定大小分块的优势
    - 保持语义完整性：不会在段落中间切断
    - 携带标题上下文：每个 chunk 的 metadata 记录所属章节标题
    - 检索时可利用标题层级做相关性加权
    """
    # 第一步：从原始块中提取结构化章节（按标题分割）
    sections = _extract_structural_sections(blocks, max_chars=options.max_chars)
    payloads: list[ChunkPayload] = []
    for section in sections:
        # 第二步：对过长的章节再次按固定大小切分
        parts = _split_by_size(section.text, options.max_chars, options.overlap_chars)
        for part_index, part in enumerate(parts):
            # 元数据包含章节信息 + 分段信息
            metadata = {
                "section_heading": section.heading,        # 所属章节标题
                "section_start_line": section.start_line,  # 章节起始行号
                # 若章节被切分为多段，则 split_reason 改为 "size"
                "split_reason": section.split_reason if len(parts) == 1 else "size",
                "section_part": part_index + 1,            # 当前段序号（从 1 开始）
                "section_part_total": len(parts),          # 总段数
            }
            payloads.append(
                _payload_from_block(
                    section.source,
                    part,
                    # 优先使用章节标题，fallback 到源块的 section_title
                    section_title=section.heading or section.source.section_title,
                    page_no=section.page_no,
                    metadata=metadata,
                )
            )
    return payloads


# ---- 结构化章节提取（核心算法） ----
def _extract_structural_sections(blocks: list[ParsedBlock], *, max_chars: int) -> list[_Section]:
    """从解析块中按标题层级提取结构化章节。
    
    面试点：这个函数是本模块最复杂的算法，核心在于：
    1. 标题层级识别（Markdown #、数字编号、中文编号）
    2. 父子标题合并策略（may_merge_child）
    3. 页码标记过滤（_PAGE_MARKER_RE）
    4. 目录检测与隔离（_looks_like_table_of_contents）
    5. 重复噪声行去除（_remove_repeated_noise）
    """
    # 预处理：去除重复噪声行（如页眉页脚）
    cleaned_blocks = _remove_repeated_noise(blocks)
    sections: list[_Section] = []
    
    # ---- 状态变量：维护当前正在构建的章节 ----
    current_lines: list[str] = []        # 当前章节的所有行
    current_heading: str | None = None   # 当前章节标题
    current_number: str | None = None    # 当前章节的数字编号（如 "1.2"）
    current_page: int | None = None      # 当前页码
    current_source: ParsedBlock | None = None  # 来源块
    current_start_line = 1               # 章节起始行号
    current_heading_only = False         # 标记：当前行是否仅为标题行（后面还没跟内容）
    line_number = 0                      # 全局行号计数器

    # ---- 内嵌函数：将当前累积的章节刷入结果列表 ----
    def flush(reason: str = "heading") -> None:
        """将当前章节写入 sections 列表，并重置所有状态变量。
        
        Args:
            reason: 触发 flush 的原因（heading/tail/table_of_contents/size）
        """
        nonlocal current_lines, current_heading, current_number, current_page
        nonlocal current_source, current_start_line, current_heading_only
        # 拼接文本时过滤空行
        text = "\n".join(line for line in current_lines if line.strip()).strip()
        if text and current_source is not None:
            sections.append(
                _Section(
                    text=text,
                    heading=current_heading,
                    page_no=current_page,
                    source=current_source,
                    start_line=current_start_line,
                    split_reason=reason,
                )
            )
        # 重置所有状态
        current_lines = []
        current_heading = None
        current_number = None
        current_page = None
        current_source = None
        current_heading_only = False

    # ---- 主循环：逐块逐行处理 ----
    for block in cleaned_blocks:
        # 按行拆分并去除空行
        raw_lines = [line.strip() for line in block.text.splitlines() if line.strip()]
        if not raw_lines:
            continue
        
        # 目录检测：如果看起来像目录，整块作为一个独立 section
        if _looks_like_table_of_contents(raw_lines):
            flush()  # 先刷出之前累积的内容
            heading_index = raw_lines.index("目录")  # 找到"目录"行的位置
            sections.append(
                _Section(
                    text="\n".join(raw_lines),
                    heading="目录",
                    page_no=block.page_no,
                    source=block,
                    start_line=line_number + heading_index + 1,
                    split_reason="table_of_contents",  # 标记为目录类型
                )
            )
            line_number += len(raw_lines)
            continue

        # 逐行处理
        for line in raw_lines:
            line_number += 1
            heading = _heading_info(line)  # 判断当前行是否为标题
            
            # ---- 非标题行处理 ----
            if heading is None:
                # 如果还没有来源块，初始化
                if current_source is None:
                    current_source = block
                    current_page = block.page_no
                    current_start_line = line_number
                # 如果当前只有标题行且不是页码标记，更新来源信息
                elif current_heading_only and not _PAGE_MARKER_RE.match(line):
                    current_page = block.page_no or current_page
                    current_source = block
                current_lines.append(line)  # 追加行内容
                # 如果不是页码标记，说明标题后面跟了正文，清除 heading_only 标记
                if not _PAGE_MARKER_RE.match(line):
                    current_heading_only = False
                continue

            # ---- 标题行处理 ----
            heading_text, heading_number, heading_kind = heading
            
            # 判断当前标题是否为上一标题的子标题
            # 条件：编号型标题 + 存在父编号 + 当前编号以"父编号."开头
            # 例如：父编号 "1"，子编号 "1.1" → is_child=True
            is_child = bool(
                heading_kind == "numbered"
                and current_number
                and heading_number
                and heading_number.startswith(f"{current_number}.")
            )
            # 如果合并后长度不超过 max_chars，允许将子标题合并到父章节
            may_merge_child = is_child and len("\n".join([*current_lines, line])) <= max_chars

            # 情况1：当前有内容但没有标题 → 给之前的内容补上标题
            if current_lines and current_heading is None:
                current_lines.append(line)
                current_heading = heading_text
                current_number = heading_number
                current_heading_only = True
                continue

            # 情况2：遇到新标题且不可合并 → flush 旧章节
            if current_lines and not may_merge_child:
                flush()

            # 情况3：还没有来源 → 初始化新章节
            if current_source is None:
                current_source = block
                current_page = block.page_no
                current_start_line = line_number
                current_lines = [line]
                current_heading = heading_text
                current_number = heading_number
                current_heading_only = True
                continue

            # 情况4：子标题可合并 → 合并到当前章节
            if may_merge_child:
                current_lines.append(line)
                if current_heading_only:
                    # 用 " > " 连接父子标题形成层级路径
                    current_heading = f"{current_heading} > {heading_text}" if current_heading else heading_text
                    current_source = block
                    current_page = block.page_no or current_page
                # 更新 heading_only 标记
                current_heading_only = True if len(current_lines) == 1 else current_heading_only
                continue

            # 情况5：其他情况 → 更新当前章节标题
            current_lines.append(line)
            current_heading = heading_text
            current_number = heading_number
            current_heading_only = True

    # 循环结束：刷出最后一个章节
    # 如果最后没有标题（纯文本结尾），reason 为 "tail"
    flush("tail" if current_heading is None else "heading")
    return sections


# ---- 表格感知分块 ----
def _build_table_aware(blocks: list[ParsedBlock], options: ChunkingOptions) -> list[ChunkPayload]:
    """表格感知分块：按表格结构切分，支持行数限制、分组键和字符数限制。
    
    面试点：表格感知分块的三个切分条件（OR 关系）：
    1. 行数达到 table_rows_per_chunk → 切分
    2. 候选文本长度超过 max_chars → 切分
    3. 分组键变化 → 切分（保持同组数据在一起）
    
    表格渲染模式：
    - FAQ 模式（≥3 列）：分类+问题+答案格式
    - Records 模式（<3 列）：普通表头+数据行格式
    """
    payloads: list[ChunkPayload] = []
    for block in blocks:
        # 从块元数据中提取表格行数据（由解析器预填充）
        rows = list(block.metadata.get("_table_rows") or [])
        headers = list(block.metadata.get("headers") or [])
        
        # 如果没有结构化表格数据，退化为按纯文本切分
        if not rows:
            payloads.extend(_table_chunks_from_text(block, options))
            continue

        # ---- 表格行分组：按行数/字符数/分组键切分 ----
        batches: list[list[dict]] = []
        current: list[dict] = []
        current_group: list[str] | None = None  # 当前分组键（如按一级分类分组）
        
        for row in rows:
            group = list(row.get("group_key") or [])  # 当前行的分组键
            rendered = _render_table_row(row, headers)  # 渲染当前行为文本
            # 拼接候选文本（用于判断字符数是否超限）
            candidate = "\n\n".join([*[_render_table_row(item, headers) for item in current], rendered])
            # 判断分组键是否变化
            group_changed = bool(current and group and current_group and group != current_group)
            
            # 三个切分条件（满足任一即切分）：
            if current and (
                len(current) >= options.table_rows_per_chunk   # 条件1：行数超限
                or len(candidate) > options.max_chars           # 条件2：字符数超限
                or group_changed                                # 条件3：分组键变化
            ):
                batches.append(current)
                current = []
            current.append(row)
            current_group = group or current_group  # 更新当前分组键
        
        # 处理最后一批
        if current:
            batches.append(current)

        # ---- 为每批生成 ChunkPayload ----
        for batch in batches:
            row_numbers = [int(item.get("row_number") or 0) for item in batch]
            group_key = list(batch[0].get("group_key") or [])
            text = "\n\n".join(_render_table_row(item, headers) for item in batch)
            metadata = {
                "table_header": " | ".join(headers),                # 表头字符串
                # 渲染模式：≥3 列用 FAQ 格式，否则用普通格式
                "table_render_mode": "faq" if len(headers) >= 3 else "records",
                "table_group_key": group_key,                       # 分组键
                "table_row_count": len(batch),                      # 当前批的行数
            }
            payloads.append(
                _payload_from_block(
                    block,
                    text,
                    row_start=min(row_numbers) if row_numbers else block.row_start,
                    row_end=max(row_numbers) if row_numbers else block.row_end,
                    metadata=metadata,
                )
            )
    return payloads


# ---- 纯文本表格切分（无结构化表格数据时的 fallback） ----
def _table_chunks_from_text(block: ParsedBlock, options: ChunkingOptions) -> list[ChunkPayload]:
    """将纯文本表格按行切分（当解析器没有提取出结构化表格数据时使用）。
    
    策略：第一行视为表头，后续行按 table_rows_per_chunk 分组，
    每组都携带表头以保证上下文完整。
    """
    lines = [line.strip() for line in block.text.splitlines() if line.strip()]
    if not lines:
        return []
    # 第一行作为表头，其余为数据行
    header, rows = lines[0], lines[1:] or lines
    payloads: list[ChunkPayload] = []
    base_row = block.row_start or 1  # 基准行号
    
    # 按 table_rows_per_chunk 步长分组
    for offset in range(0, len(rows), options.table_rows_per_chunk):
        batch = rows[offset : offset + options.table_rows_per_chunk]
        # 每组都拼接表头，保证检索时能理解列含义
        text = "\n".join([header, *batch])
        payloads.append(
            _payload_from_block(
                block,
                text,
                row_start=base_row + offset,
                row_end=base_row + offset + len(batch) - 1,
                metadata={
                    "table_header": header,
                    "table_render_mode": "records",
                    "table_row_count": len(batch),
                    "table_group_key": [],
                },
            )
        )
    return payloads


# ---- 表格行渲染 ----
def _render_table_row(row: dict, headers: list[str]) -> str:
    """将表格行渲染为自然语言文本，方便向量检索。
    
    面试点：为什么要把表格渲染成自然语言而不是保留表格格式？
    - Embedding 模型对自然语言的理解远好于表格格式
    - "分类：技术支持 | 问题：如何重置密码 | 答案：请访问..." 比 Markdown 表格更容易匹配用户查询
    - 相似问题字段（similar_questions）的加入增强了 FAQ 场景的召回率
    
    两种渲染模式：
    - FAQ 模式（≥4 列）：分类层级 + 问题 + 相似问题 + 答案
    - 简化模式（3 列）：问题 + 补充信息 + 答案
    """
    fields = dict(row.get("fields") or {})               # 行字段字典
    similar = list(row.get("similar_questions") or [])   # 相似问题列表（FAQ 场景）
    # 按表头顺序提取值，排除"相似问题"列（单独处理）
    values = [fields.get(header, "") for header in headers if not header.startswith("相似问题")]
    
    # ---- FAQ 模式：≥4 列 ----
    if len(headers) >= 4:
        categories = values[:-2]  # 前面的都是分类列
        question = values[-2] if len(values) >= 2 else (values[0] if values else "")
        answer = values[-1] if values else ""
        lines = []
        category_labels = ["一级分类", "二级分类", "三级分类"]
        for index, category in enumerate(categories):
            if category:
                label = category_labels[index] if index < len(category_labels) else f"分类{index + 1}"
                lines.append(f"{label}：{category}")
        if question:
            lines.append(f"问题：{question}")
        if similar:
            # 相似问题用中文分号连接，增强语义覆盖
            lines.append(f"相似问题：{'；'.join(similar)}")
        if answer:
            lines.append(f"答案：{answer}")
        return "\n".join(lines)

    # ---- 简化模式：3 列（问题 + 补充信息 + 答案） ----
    if len(values) >= 3:
        return f"问题：{values[0]}\n补充信息：{values[1]}\n答案：{values[2]}"
    # 兜底：直接返回原始文本
    return str(row.get("text") or "")


# ---- 父子分块 ----
def _build_parent_child(blocks: list[ParsedBlock], options: ChunkingOptions) -> list[ChunkPayload]:
    """父子分块：父块保留完整上下文，子块用于精确检索。
    
    面试点：父子分块的核心思想（Parent-Child Chunking）
    - 子块（child）：按 child_max_chars 切分的小粒度块 → 用于向量检索（精确匹配）
    - 父块（parent）：按 parent_max_chars 切分的大粒度块 → 作为上下文注入 LLM（完整语义）
    - 检索流程：用子块匹配查询 → 返回对应父块的完整上下文
    - 优势：兼顾检索精度和上下文完整性
    
    元数据设计：
    - parent_chunk_uuid：同一父块的所有子块共享，用于回溯父块
    - chunk_group_uuid：同一分组的所有块共享
    - context_text：子块携带完整父文本，方便检索后直接使用
    """
    # 第一步：构建父块文本列表
    parents = _build_parent_texts(blocks, options)
    payloads: list[ChunkPayload] = []
    parent_total = len(parents)
    
    for parent_index, (text, source, heading, block_count, page_start, page_end, reason) in enumerate(parents):
        parent_uuid = str(uuid4())   # 父块唯一标识（同一父块的所有子块共享）
        group_uuid = str(uuid4())    # 分组标识
        # 第二步：将父块按 child_max_chars 切分为子块
        children = _split_by_size(text, options.child_max_chars, options.overlap_chars)
        
        for child_index, child in enumerate(children):
            metadata = {
                "parent_index": parent_index,           # 父块序号
                "parent_total": parent_total,           # 父块总数
                "child_index": child_index,             # 子块在父块中的序号
                "child_total": len(children),           # 该父块的子块总数
                "parent_char_count": len(text),         # 父块字符数
                "child_char_count": len(child),         # 子块字符数
                "parent_block_count": block_count,      # 父块包含的原始块数
                "parent_page_start": page_start,        # 父块起始页
                "parent_page_end": page_end,            # 父块结束页
                "parent_section_heading": heading,      # 父块章节标题
                "parent_split_reason": reason,          # 父块分割原因
            }
            payloads.append(
                _payload_from_block(
                    source,
                    child,
                    section_title=heading or source.section_title,
                    page_no=page_start,
                    metadata=metadata,
                    parent_chunk_uuid=parent_uuid,      # 关联父块 UUID
                    chunk_group_uuid=group_uuid,        # 关联分组 UUID
                    chunk_level="child",                 # 标记为子块级别
                    context_text=text,                   # 携带完整父文本作为上下文
                )
            )
    return payloads


# ---- 父块文本构建 ----
def _build_parent_texts(blocks: list[ParsedBlock], options: ChunkingOptions) -> list[tuple]:
    """构建父块文本列表，返回 (文本, 来源块, 标题, 块数, 起始页, 结束页, 分割原因) 元组列表。
    
    面试点：父块构建的三个分支：
    1. 多块合并且不超限 → 直接合并为一个父块（structural_group）
    2. 单块超限 → 提取结构化章节后再切分（递归使用 structural 策略）
    3. 兜底 → 按 parent_max_chars 硬切分（oversized_section）
    """
    # 将所有块拼接为一个完整文本
    combined = "\n\n".join(block.text.strip() for block in blocks if block.text.strip())
    if not combined:
        return []

    # 提取每个块的第一个标题，用 " > " 连接形成层级标题路径
    headings = [_first_heading(block.text) for block in blocks]
    combined_heading = " > ".join(heading for heading in headings if heading) or None
    
    # 计算页码范围
    page_values = [block.page_no for block in blocks if block.page_no is not None]
    page_start = min(page_values) if page_values else blocks[0].page_no
    page_end = max(page_values) if page_values else blocks[-1].page_no

    # ---- 分支1：多块合并且总长度不超限 → 直接作为一个父块 ----
    if len(blocks) > 1 and len(combined) <= options.parent_max_chars:
        return [(combined, blocks[0], combined_heading, len(blocks), page_start, page_end, "structural_group")]

    source = blocks[0]
    candidates: list[tuple[str, str | None, str]] = []
    
    # ---- 分支2：单块超限 → 尝试用结构化分块提取章节 ----
    if len(blocks) == 1 and len(combined) > options.parent_max_chars:
        # 递归使用结构化分块提取章节
        sections = _extract_structural_sections(blocks, max_chars=options.parent_max_chars)
        if len(sections) > 1:
            # 提取成功，将每个章节作为候选父块
            candidates = [(section.text, section.heading, "structural_group") for section in sections]
    
    # ---- 分支3：兜底 → 按 parent_max_chars 硬切分（overlap=0） ----
    if not candidates:
        candidates = [(combined, combined_heading, "oversized_section")]

    # 最终切分：对每个候选文本按 parent_max_chars 切分
    parents: list[tuple] = []
    for candidate_text, heading, reason in candidates:
        parts = _split_by_size(candidate_text, options.parent_max_chars, 0)  # 父块不重叠
        for part in parts:
            parents.append((part, source, heading, len(blocks), page_start, page_end, reason))
    return parents


# ---- 语义分块 ----
def _build_semantic(
    blocks: list[ParsedBlock],
    options: ChunkingOptions,
    *,
    semantic_embedder: Callable[[list[str]], list[list[float]]] | None,
) -> list[ChunkPayload]:
    """基于 Embedding 余弦相似度的句子级语义分块。

    每个解析块内的句子会批量向量化。新句与当前块尾部 ``merge_window``
    句的平均向量相似度低于阈值时，在句子边界处切分；最大句数仍是硬上限。
    没有可用向量提供者时保留可预览的句子数兜底，并在元数据中明确标记，
    摄入链路会始终传入生产 Embedding 提供者。
    """
    payloads: list[ChunkPayload] = []
    for block in blocks:
        sentences = [match.group(0).strip() for match in _SENTENCE_RE.finditer(block.text) if match.group(0).strip()]
        if not sentences:
            continue

        vectors = semantic_embedder(sentences) if semantic_embedder is not None else None
        if vectors is not None and len(vectors) != len(sentences):
            raise ValueError("semantic embedder must return one vector for each sentence")

        segments: list[tuple[int, int, str, float | None]] = []
        start = 0
        max_sentences = max(1, options.max_chunk_sentences)
        min_sentences = max(1, min(options.min_chunk_sentences, max_sentences))
        window_size = max(1, options.merge_window)
        for sentence_index in range(1, len(sentences)):
            current_size = sentence_index - start
            split_reason: str | None = None
            similarity: float | None = None
            if current_size >= max_sentences:
                split_reason = "max_sentence_limit"
            elif vectors is not None and current_size >= min_sentences:
                context_start = max(start, sentence_index - window_size)
                similarity = _cosine_similarity(_mean_vector(vectors[context_start:sentence_index]), vectors[sentence_index])
                if similarity < options.similarity_threshold:
                    split_reason = "similarity_threshold"

            if split_reason is not None:
                segments.append((start, sentence_index, split_reason, similarity))
                start = sentence_index

        segments.append((start, len(sentences), "tail", None))
        total = len(segments)
        for start, end, reason, similarity in segments:
            metadata = {
                "sentence_start": start + 1,
                "sentence_end": end,
                "semantic_split_reason": reason,
                "semantic_segment_total": total,
                "semantic_similarity_threshold": options.similarity_threshold,
                "semantic_merge_window": window_size,
                "semantic_embedding_used": vectors is not None,
            }
            if similarity is not None:
                metadata["semantic_previous_similarity"] = round(similarity, 6)
            if vectors is None:
                metadata["semantic_fallback_reason"] = "embedding_provider_unavailable"
            payloads.append(
                _payload_from_block(
                    block,
                    "".join(sentences[start:end]),
                    metadata=metadata,
                )
            )
    return payloads


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dimensions = len(vectors[0])
    if dimensions == 0 or any(len(vector) != dimensions for vector in vectors):
        raise ValueError("semantic embedding vectors must have a consistent non-zero dimension")
    return [sum(vector[index] for vector in vectors) / len(vectors) for index in range(dimensions)]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("semantic embedding vectors must have a consistent non-zero dimension")
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    if denominator == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / denominator


# ---- 统一的分块构建辅助函数 ----
def _payload_from_block(
    block: ParsedBlock,
    text: str,
    *,
    section_title: str | None = None,    # 覆盖源块的 section_title
    page_no: int | None = None,          # 覆盖源块的页码
    row_start: int | None = None,        # 覆盖源块的起始行
    row_end: int | None = None,          # 覆盖源块的结束行
    metadata: dict | None = None,        # 附加元数据
    parent_chunk_uuid: str | None = None, # 父子分块的父块 UUID
    chunk_group_uuid: str | None = None,  # 分块组 UUID
    chunk_level: str | None = None,       # 分块级别（如 "child"）
    context_text: str | None = None,      # 上下文文本（父子分块的父文本）
) -> ChunkPayload:
    """从 ParsedBlock 构建 ChunkPayload，统一处理元数据清理和默认值回退。
    
    面试点：为什么要把这个逻辑抽成独立函数？
    - DRY 原则：6 种分块策略共用同一套 payload 构建逻辑
    - 元数据清理：过滤掉以 "_" 开头的内部字段（如 _table_rows）
    - 默认值回退：传入的参数优先，否则 fallback 到源块的值
    """
    # 清理元数据：移除内部字段（以 "_" 开头），合并外部传入的元数据
    clean_metadata = {key: value for key, value in block.metadata.items() if not key.startswith("_")}
    clean_metadata.update(metadata or {})
    
    return ChunkPayload(
        chunk_index=0,  # 后续由 build_chunks 统一设置
        chunk_text=text,
        chunk_type=block.chunk_type,
        # 优先使用传入值，否则 fallback 到源块值
        section_title=section_title if section_title is not None else block.section_title,
        page_no=page_no if page_no is not None else block.page_no,
        sheet_name=block.sheet_name,
        row_start=row_start if row_start is not None else block.row_start,
        row_end=row_end if row_end is not None else block.row_end,
        # 估算 token 数和字符数
        token_count=_estimate_tokens(text),
        char_count=len(text),
        metadata_json=clean_metadata,
        # 父子分块相关字段
        parent_chunk_uuid=parent_chunk_uuid,
        chunk_group_uuid=chunk_group_uuid,
        chunk_level=chunk_level,
        context_text=context_text,
    )


# ---- 通用文本切分工具 ----
def _split_by_size(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """按固定字符数切分文本，支持重叠。
    
    面试点：滑动窗口切分的实现细节
    - step = size - overlap：滑动步长
    - 例如 size=500, overlap=50 → step=450
    - 每个 chunk 和下一个 chunk 有 50 个字符的重叠
    - 边界保护：size 至少为 1，overlap 不能超过 size-1（防止无限循环）
    
    Args:
        text: 待切分文本
        max_chars: 每个分块的最大字符数
        overlap_chars: 相邻分块之间的重叠字符数
    Returns:
        切分后的文本列表
    """
    normalized = text.strip()
    if not normalized:
        return []
    # 边界保护
    size = max(1, max_chars)                           # size 至少为 1
    overlap = max(0, min(overlap_chars, size - 1))     # overlap 在 [0, size-1] 范围内
    # 短文本直接返回
    if len(normalized) <= size:
        return [normalized]
    # 滑动窗口切分
    step = size - overlap  # 步长 = 窗口大小 - 重叠大小
    return [
        normalized[start : start + size].strip()
        for start in range(0, len(normalized), step)
        if normalized[start : start + size].strip()  # 过滤空字符串
    ]


# ---- Token 数估算 ----
def _estimate_tokens(text: str) -> int:
    """估算文本的 Token 数量（近似算法，不依赖具体 tokenizer）。
    
    面试点：为什么需要估算 Token 数而不是精确计算？
    - 精确计算需要加载 tokenizer（如 tiktoken），增加启动开销和依赖
    - 分块阶段的 Token 数只需近似值，用于后续的存储分配和预算控制
    - 估算规则：英文单词 ≈ 1 token，中文字符 ≈ 0.5 token（大多数中文 tokenizer 的近似比例）
    
    算法：
    - ASCII 单词：每个视为 1 token
    - 非 ASCII 字符（中文等）：每 2 个视为 1 token（ceil 向上取整）
    - 结果至少为 1
    """
    # 统计英文单词数（连续的字母数字下划线）
    ascii_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    # 统计非 ASCII 字符数（中文、日文、emoji 等）
    non_ascii = sum(1 for char in text if ord(char) > 127)
    # 中文 ≈ 0.5 token/字，向上取整
    return max(1, ascii_words + math.ceil(non_ascii / 2))


# ---- 标题行识别 ----
def _heading_info(line: str) -> tuple[str, str | None, str] | None:
    """识别一行文本是否为标题，返回 (标题文本, 编号, 类型) 或 None。
    
    面试点：标题识别的优先级和排除规则
    优先级：页码标记排除 → Markdown 标题 → 目录 → 数字编号 → 中文编号
    关键排除规则：
    - 页码标记（如 "3 / 25"）不视为标题
    - 4 位及以上不含点的数字（如 "2024"）不视为编号标题（可能是年份）
    
    Returns:
        (标题文本, 编号字符串或 None, 标题类型) 或 None（非标题）
        标题类型: "markdown" | "toc" | "numbered"
    """
    # 页码标记不视为标题（如 "3 / 25"）
    if _PAGE_MARKER_RE.match(line):
        return None
    
    # Markdown 标题：匹配 # ~ ###### 开头
    markdown = _MARKDOWN_HEADING_RE.match(line)
    if markdown:
        return markdown.group(2).strip(), None, "markdown"
    
    # 中文"目录"行
    if line == "目录":
        return "目录", None, "toc"
    
    # 数字编号标题：如 "1."、"1.2."、"3．"
    numbered = _NUMBERED_HEADING_RE.match(line)
    if numbered:
        number, title = numbered.groups()
        # 排除纯数字长串（≥4 位且不含 "."）—— 可能是年份或普通数字
        if len(number) >= 4 and not "." in number:
            return None
        return line.strip(), number.rstrip("."), "numbered"
    
    # 中文数字编号标题：如 "一、"、"十二．"
    chinese = _CHINESE_NUMBERED_HEADING_RE.match(line)
    if chinese:
        return line.strip(), None, "numbered"
    
    return None


# ---- 提取文本中的第一个标题 ----
def _first_heading(text: str) -> str | None:
    """提取文本中第一个标题的文本内容，用于父块标题拼接。"""
    for line in text.splitlines():
        info = _heading_info(line.strip())
        if info is not None:
            return info[0]  # 返回标题文本（info 元组的第一个元素）
    return None


# ---- 目录检测 ----
def _looks_like_table_of_contents(lines: list[str]) -> bool:
    """启发式判断文本块是否像目录页。
    
    判断依据：
    1. 包含"目录"关键词
    2. 至少有 2 行符合目录格式（如 "1.1 概述.............3"）
    
    面试点：为什么需要目录检测？
    - 目录页的内容不适合作为知识检索的 chunk（全是标题+页码，无语义信息）
    - 将其独立为一个 section（split_reason="table_of_contents"），后续可选择性过滤
    """
    if "目录" not in lines:
        return False
    # 统计符合"......数字"格式的行数（目录中的引导点 + 页码）
    leaders = sum(1 for line in lines if re.search(r"\.{5,}\s*\d+\s*$", line))
    return leaders >= 2  # 至少 2 行符合格式才判定为目录


# ---- 重复噪声行去除 ----
def _remove_repeated_noise(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    """去除跨块重复的噪声行（页眉、页脚、水印等）。
    
    面试点：为什么需要去噪声？
    - PDF 解析后，页眉页脚会在每个 ParsedBlock 中重复出现
    - 这些重复行不是有效知识，会污染向量检索结果
    - 如果不去除，用户搜索时可能匹配到页眉文本而非正文
    
    去噪策略：
    1. 统计每个唯一行在多少不同块中出现（用 set 去重同一块内的重复）
    2. 出现次数 ≥ 2 且（长度 ≤ 40 或包含"知识产权归属"）→ 标记为噪声
    3. 移除页码标记行前的"上一页残留"内容
    
    注意：知识产权归属条款在文档中反复出现，属于合法去噪目标。
    """
    # 统计每个唯一行在多少个不同块中出现
    line_counts = Counter(
        line.strip()
        for block in blocks
        for line in set(block.text.splitlines())  # set() 去重：同一块内多次出现只算 1 次
        if line.strip() and not _PAGE_MARKER_RE.match(line.strip())  # 排除空行和页码
    )
    # 筛选重复噪声行：出现在 ≥2 个块中，且满足短行或特殊关键词
    repeated = {
        line
        for line, count in line_counts.items()
        if count >= 2 and (len(line) <= 40 or "知识产权归属" in line)
    }
    
    cleaned: list[ParsedBlock] = []
    for block in blocks:
        # 过滤噪声行
        lines = [
            line
            for line in block.text.splitlines()
            if line.strip() not in repeated and "知识产权归属" not in line
        ]
        # 处理页码标记：如果前 4 行中有页码标记，截断之前的内容
        # 这是因为上一页的尾部内容可能混入当前块
        page_marker_index = next(
            (index for index, line in enumerate(lines[:4]) if _PAGE_MARKER_RE.match(line.strip())),
            None,
        )
        # 如果页码标记不在第 0 行且当前不是第 1 页，说明前面有上一页的残留内容
        if page_marker_index is not None and page_marker_index > 0 and block.page_no and block.page_no > 1:
            lines = lines[page_marker_index:]  # 从页码标记行开始截取
        
        text = "\n".join(lines)
        # 重建 ParsedBlock（保持其他字段不变）
        cleaned.append(
            ParsedBlock(
                text=text,
                chunk_type=block.chunk_type,
                section_title=block.section_title,
                page_no=block.page_no,
                sheet_name=block.sheet_name,
                row_start=block.row_start,
                row_end=block.row_end,
                metadata=dict(block.metadata),  # 浅拷贝元数据
            )
        )
    return cleaned
