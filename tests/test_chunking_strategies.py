"""分块策略单元测试：验证 structural、parent-child、semantic 三种策略的正确性。

测试覆盖：
- 结构化分块：按标题切分、目录识别、页码过滤、跨页合并、小标题合并、数值表保留
- 父子分块：结构分组、跨页聚合、上下文关联
- 语义分块：最大句数限制、断点检测
- 预览端点：parent-child 的上下文输出
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from app.ingestion.chunking import build_chunks  # 核心分块函数
from app.ingestion.types import ChunkingOptions, ParsedBlock  # 分块选项和解析块类型


def test_structural_chunking_keeps_heading_with_body_and_explains_boundary():
    """测试结构化分块：按 Markdown 标题切分，保留标题和正文的关联。

    验证点：
    - 两个标题各生成一个 chunk
    - chunk 文本以标题开头
    - 元数据记录 section_heading 和 split_reason
    """
    text = "\n".join(
        [
            "# 总则",
            "平台用于统一管理企业知识库。",
            "支持上传、解析、切分和检索。",
            "",
            "## 适用范围",
            "适用于 PDF、Word、Excel 和导出的沟通记录。",
        ]
    )

    chunks = build_chunks(
        [ParsedBlock(text=text, metadata={"parser": "manual"})],
        options=ChunkingOptions(strategy="structural", max_chars=500, overlap_chars=0),
    )

    assert len(chunks) == 2  # 两个标题 → 两个 chunk
    assert chunks[0].chunk_text.startswith("# 总则\n平台用于")  # 第一个 chunk 包含标题
    assert chunks[1].chunk_text.startswith("## 适用范围\n适用于")  # 第二个 chunk
    assert [chunk.metadata_json["section_heading"] for chunk in chunks] == ["总则", "适用范围"]
    assert all(chunk.metadata_json["split_reason"] == "heading" for chunk in chunks)  # 切分原因
    assert all(chunk.metadata_json["section_start_line"] for chunk in chunks)  # 有起始行号


def test_structural_chunking_keeps_table_of_contents_as_one_section():
    """测试结构化分块：目录页被识别为整体，不按行切分。

    面试点：为什么要将目录作为一个整体 chunk？
    答：目录中的条目很短，单独切分会导致每个 chunk 信息量不足。
    整体保留目录有助于后续检索时定位章节结构。
    """
    text = "\n".join(
        [
            "2 / 14",
            "出差管理办法",
            "文件编码：BHJY-XZ-ZD01",
            "版本/更改：第三版",
            "页码：共14页",
            "目录",
            "1．目的............................................................................. 3",
            "2．适用范围......................................................................... 3",
            "3．基本规定......................................................................... 3",
            "3.1 出差管理办法总体规定............................................................. 3",
            "3.2 出差管理办法具体规定............................................................. 3",
            "3.2.1 出差申请审批................................................................... 4",
        ]
    )

    chunks = build_chunks(
        [ParsedBlock(text=text, page_no=2, metadata={"parser": "pymupdf"})],
        options=ChunkingOptions(strategy="structural", max_chars=1200, overlap_chars=0),
    )

    assert len(chunks) == 1  # 目录页作为整体 → 一个 chunk
    assert chunks[0].metadata_json["split_reason"] == "table_of_contents"  # 识别为目录
    assert chunks[0].metadata_json["section_heading"] == "目录"
    assert "3.2.1 出差申请审批" in chunks[0].chunk_text  # 目录内容完整保留


def test_structural_chunking_ignores_page_markers_as_headings():
    """测试结构化分块：页码标记（如 "3 / 14"）不会被误识别为标题。

    面试点：如何区分页码和标题？
    答：通过预编译的正则 _PAGE_NUMBER_RE 匹配 "数字 / 数字" 模式，
    将其从标题候选列表中排除，避免误切分。
    """
    text = "\n".join(
        [
            "3 / 14",
            "1．目的",
            "为了适应公司业务发展需要，规范出差申请、审批、报销全流程。",
            "2．适用范围",
            "适用于集团全体员工。",
        ]
    )

    chunks = build_chunks(
        [ParsedBlock(text=text, page_no=3, metadata={"parser": "pymupdf"})],
        options=ChunkingOptions(strategy="structural", max_chars=500, overlap_chars=0),
    )

    headings = [chunk.metadata_json.get("section_heading") for chunk in chunks]
    assert "3 / 14" not in headings  # 页码未被识别为标题
    assert headings == ["1．目的", "2．适用范围"]  # 正确的标题
    assert chunks[0].chunk_text.startswith("3 / 14\n1．目的")  # 页码保留在正文中


def test_structural_chunking_attaches_trailing_heading_to_next_page_body():
    """测试结构化分块：跨页标题处理——当前页末尾的标题应与下一页正文合并。

    面试点：为什么需要跨页标题合并？
    答：PDF 解析时，标题可能出现在第 N 页末尾，正文从第 N+1 页开始。
    如果不合并，标题会成为一个孤立的短 chunk，失去上下文意义。
    """
    blocks = [
        ParsedBlock(
            text="\n".join(
                [
                    "3 / 14",
                    "3.1 出差管理办法总体规定",
                    "适用于差旅申请、审批和报销流程。",
                    "3.2 出差管理办法具体规定",  # 末尾标题，正文在下一页
                ]
            ),
            page_no=3,
            metadata={"parser": "pymupdf"},
        ),
        ParsedBlock(
            text="\n".join(
                [
                    "出差管理办法",
                    "本页内容知识产权归属百合佳缘所有，未经授权许可，不得复制抄袭",
                    "4 / 14",
                    "3.2.1 出差申请审批",  # 上一页标题的正文
                    "公司员工出差应事先提交出差申请。",
                ]
            ),
            page_no=4,
            metadata={"parser": "pymupdf"},
        ),
    ]

    chunks = build_chunks(
        blocks,
        options=ChunkingOptions(strategy="structural", max_chars=500, overlap_chars=0),
    )

    # 验证没有孤立的 "3.2 出差管理办法具体规定" chunk
    assert all(chunk.chunk_text != "3.2 出差管理办法具体规定" for chunk in chunks)
    # 验证标题与正文合并了
    attached = [chunk for chunk in chunks if chunk.chunk_text.startswith("3.2 出差管理办法具体规定\n4 / 14")]
    assert len(attached) == 1  # 合并成功
    assert attached[0].page_no == 4  # 合并后页码取正文所在页
    assert attached[0].metadata_json["section_heading"] == "3.2 出差管理办法具体规定 > 3.2.1 出差申请审批"


def test_structural_chunking_filters_repeated_page_header_and_footer_noise():
    """测试结构化分块：过滤页眉页脚等重复噪声文本。

    面试点：如何识别和过滤页眉页脚？
    答：通过 _HEADER_FOOTER_NOISE_RE 正则匹配已知噪声模式（如公司版权声明），
    在多页中出现 2 次以上的相同行被判定为噪声并过滤。
    """
    blocks = [
        ParsedBlock(
            text="\n".join([
                "员工手册",
                "1 / 3",
                "1．总则",
                "平台用于统一管理企业知识库。",
                "本页内容知识产权归属百合佳缘所有，未经授权许可，不得复制抄袭",
            ]),
            page_no=1, metadata={"parser": "pymupdf"},
        ),
        ParsedBlock(
            text="\n".join([
                "员工手册",
                "2 / 3",
                "2．审批流程",
                "员工应先提交申请，再由主管审批。",
                "本页内容知识产权归属百合佳缘所有，未经授权许可，不得复制抄袭",
            ]),
            page_no=2, metadata={"parser": "pymupdf"},
        ),
        ParsedBlock(
            text="\n".join([
                "员工手册",
                "3 / 3",
                "3．归档要求",
                "审批记录需要完整保留。",
                "本页内容知识产权归属百合佳缘所有，未经授权许可，不得复制抄袭",
            ]),
            page_no=3, metadata={"parser": "pymupdf"},
        ),
    ]

    chunks = build_chunks(
        blocks,
        options=ChunkingOptions(strategy="structural", max_chars=500, overlap_chars=0),
    )

    chunk_text = "\n".join(chunk.chunk_text for chunk in chunks)
    assert "员工手册" not in chunk_text  # 页眉被过滤
    assert "知识产权归属" not in chunk_text  # 页脚被过滤
    assert "1 / 3\n1．总则" in chunk_text  # 页码保留在正文中
    assert [chunk.metadata_json.get("section_heading") for chunk in chunks] == ["1．总则", "2．审批流程", "3．归档要求"]


def test_structural_chunking_merges_short_cross_page_section_continuation():
    """测试结构化分块：短小节的内容跨页合并到同一 chunk 中。

    场景：章节 7.1.3.2 内容跨第 29、30 页，如果两部分都很短，
    应合并为一个 chunk，避免过度切分。
    """
    blocks = [
        ParsedBlock(
            text="\n".join([
                "7.1.3.2 应用内部权限管理",
                "应用内部分为两模块",
                "1、实际应用模块",
                "2、应用内部权限管理模块",
                "应用内部权限管理用户可执行操作",
                "例如应用有如下操作",
                "列表展示、添加、编辑、删除",
            ]),
            page_no=29, metadata={"parser": "pymupdf"},
        ),
        ParsedBlock(
            text="\n".join([
                "场景一：",
                "用户 A 属于数据研发部",
                "a、如果拥有查看权限，进入此应用，根据表1，可查看",
                "场景二",
                "用户 B 属于客服部",
                "场景三",
                "应用管理员 张三 权限操作",
                "7.2 应用外部权限管理",
                "可在测试环境进行操作，相关数据表如下：",
            ]),
            page_no=30, metadata={"parser": "pymupdf"},
        ),
    ]

    chunks = build_chunks(
        blocks,
        options=ChunkingOptions(strategy="structural", max_chars=1200, overlap_chars=0),
    )

    # 验证 7.1.3.2 的内容合并为一个 chunk
    section_chunks = [chunk for chunk in chunks if chunk.metadata_json.get("section_heading") == "7.1.3.2 应用内部权限管理"]
    assert len(section_chunks) == 1  # 只有一个 chunk
    assert "1、实际应用模块" in section_chunks[0].chunk_text  # 第 29 页内容
    assert "场景一：" in section_chunks[0].chunk_text  # 第 30 页内容
    assert "场景三" in section_chunks[0].chunk_text
    assert "7.2 应用外部权限管理" not in section_chunks[0].chunk_text  # 不同章节不合并
    assert any(chunk.metadata_json.get("section_heading") == "7.2 应用外部权限管理" for chunk in chunks)


def test_structural_chunking_merges_tiny_numbered_sibling_sections_under_parent():
    """测试结构化分块：极短的编号子章节（如 9.4.1/9.4.2/9.4.3）合并到父章节下。

    面试点：为什么要合并小子章节？
    答：子章节内容只有一两行时，独立切分会导致检索粒度太细，
    合并到父章节可以保持语义完整性，提高检索效果。
    """
    blocks = [
        ParsedBlock(
            text="\n".join(["9.4 人事管理", "人事流程审批通过后进入相关登记菜单。"]),
            page_no=35, metadata={"parser": "pymupdf"},
        ),
        ParsedBlock(
            text="\n".join([
                "9.4.1 员工管理", "员工信息查看。",
                "9.4.2 入职管理", "入职登记。",
                "9.4.3 转正管理", "转正登记。",
                "9.5 人事统计", "生成统计信息。",
            ]),
            page_no=36, metadata={"parser": "pymupdf"},
        ),
    ]

    chunks = build_chunks(
        blocks,
        options=ChunkingOptions(strategy="structural", max_chars=500, overlap_chars=0),
    )

    # 验证子章节合并到 9.4 下
    management_chunks = [chunk for chunk in chunks if chunk.metadata_json.get("section_heading") == "9.4 人事管理"]
    assert len(management_chunks) == 1  # 合并成功
    assert "9.4.1 员工管理" in management_chunks[0].chunk_text
    assert "9.4.3 转正管理" in management_chunks[0].chunk_text
    assert "9.5 人事统计" not in management_chunks[0].chunk_text  # 不同父章节不合并
    assert management_chunks[0].char_count <= 500  # 不超过 max_chars
    assert any(chunk.metadata_json.get("section_heading") == "9.5 人事统计" for chunk in chunks)


def test_structural_chunking_keeps_numeric_table_values_with_previous_section():
    """测试结构化分块：数值型表格数据保留在所属章节中，不被误切分。

    场景：补贴标准表格中的数字（5000、4000、3000）不应被识别为新章节。
    """
    text = "\n".join([
        "4.3.2 补贴标准",
        "地区/人员",
        "一线城市",
        "第二级M9-M8",
        "5000 元/月",
        "4000 元/月",
        "3000 元/月",
    ])

    chunks = build_chunks(
        [ParsedBlock(text=text, page_no=10, metadata={"parser": "pymupdf"})],
        options=ChunkingOptions(strategy="structural", max_chars=500, overlap_chars=0),
    )

    assert len(chunks) == 1  # 整体保留
    assert "5000 元/月\n4000 元/月\n3000 元/月" in chunks[0].chunk_text  # 数值保留


def test_parent_child_chunking_groups_by_structure_and_returns_parent_context():
    """测试父子分块：按结构分组，子块携带父块上下文。

    面试点：父子分块 vs 普通分块的区别？
    答：父子分块生成两层结构——
    - 父块（parent）：大粒度，保持完整的章节上下文
    - 子块（child）：小粒度，便于精确检索
    子块通过 parent_chunk_uuid 和 context_text 关联父块，
    检索时返回子块但可附带父块上下文。

    验证点：
    - 两个父块（总则、审批要求）各生成子块
    - 子块携带 context_text（父块完整文本）
    - 元数据记录 parent_section_heading、parent_split_reason 等
    """
    text = "\n\n".join([
        "# 总则\n制度适用所有员工。请按统一流程提交申请。审批记录需要完整保留。",
        "# 审批要求\n主管审批后进入部门复核。特殊情况需要补充说明和附件。",
    ])

    chunks = build_chunks(
        [ParsedBlock(text=text, metadata={"parser": "manual"})],
        options=ChunkingOptions(
            strategy="parent-child",
            parent_max_chars=70,   # 父块最大字符数
            child_max_chars=36,    # 子块最大字符数
            overlap_chars=0,
        ),
    )

    parent_indexes = [chunk.metadata_json["parent_index"] for chunk in chunks]
    assert set(parent_indexes) == {0, 1}  # 两个父块
    assert all(chunk.chunk_level == "child" for chunk in chunks)  # 所有块都是子块
    assert all(chunk.context_text for chunk in chunks)  # 都有上下文
    assert chunks[0].metadata_json["parent_section_heading"] == "总则"
    assert chunks[-1].metadata_json["parent_section_heading"] == "审批要求"
    assert all(chunk.metadata_json["parent_split_reason"] in {"structural_group", "oversized_section"} for chunk in chunks)
    assert all(chunk.metadata_json["parent_char_count"] >= chunk.metadata_json["child_char_count"] for chunk in chunks)


def test_parent_child_chunking_aggregates_text_blocks_into_document_level_parent():
    """测试父子分块：多个解析块聚合为文档级父块。

    场景：三个 PDF 页面分别解析为三个 ParsedBlock，
    如果总大小不超过 parent_max_chars，应合并为一个父块。
    """
    parsed_blocks = [
        ParsedBlock(text="# 第一页\n第一页制度内容。", page_no=1, metadata={"parser": "pymupdf"}),
        ParsedBlock(text="# 第二页\n第二页审批内容。", page_no=2, metadata={"parser": "pymupdf"}),
        ParsedBlock(text="# 第三页\n第三页归档内容。", page_no=3, metadata={"parser": "pymupdf"}),
    ]

    chunks = build_chunks(
        parsed_blocks,
        options=ChunkingOptions(
            strategy="parent-child",
            parent_max_chars=1000,  # 足够大，可以容纳三页
            child_max_chars=200,
            overlap_chars=0,
        ),
    )

    assert len(chunks) == 1  # 只有一个子块（因为父块只有一个）
    assert chunks[0].page_no == 1  # 起始页
    assert chunks[0].metadata_json["parent_index"] == 0
    assert chunks[0].metadata_json["parent_total"] == 1
    assert chunks[0].metadata_json["parent_block_count"] == 3  # 三个解析块合并
    assert chunks[0].metadata_json["parent_page_start"] == 1
    assert chunks[0].metadata_json["parent_page_end"] == 3
    assert chunks[0].metadata_json["parent_section_heading"] == "第一页 > 第二页 > 第三页"
    assert len({chunk.parent_chunk_uuid for chunk in chunks}) == 1  # 共享同一父块 UUID
    assert all(chunk.chunk_level == "child" for chunk in chunks)
    assert all(chunk.context_text for chunk in chunks)


def test_semantic_chunking_obeys_max_sentences_without_detected_breakpoints():
    """测试语义分块：无断点时按 max_chunk_sentences 强制切分。

    面试点：语义分块的工作原理？
    答：通过计算相邻句子的语义相似度，在相似度低于阈值的位置切分。
    但如果所有句子相似度都很高（或阈值为 0），则按 max_chunk_sentences
    强制切分，避免产生过大的 chunk。
    """
    text = "第一句。第二句。第三句。第四句。第五句。"

    chunks = build_chunks(
        [ParsedBlock(text=text, metadata={"parser": "manual"})],
        options=ChunkingOptions(
            strategy="semantic",
            min_chunk_sentences=1,
            max_chunk_sentences=2,  # 每 2 句切一个 chunk
            similarity_threshold=0.0,  # 阈值为 0（不检测语义断点）
            merge_window=2,
        ),
    )

    assert len(chunks) == 3  # 5 句 / 2 = 3 个 chunk
    assert [chunk.metadata_json["sentence_start"] for chunk in chunks] == [1, 3, 5]
    assert [chunk.metadata_json["sentence_end"] for chunk in chunks] == [2, 4, 5]
    assert [chunk.metadata_json["semantic_split_reason"] for chunk in chunks] == [
        "max_sentence_limit", "max_sentence_limit", "tail"  # 前两个超限，最后一个是尾部
    ]
    assert all(chunk.metadata_json["semantic_segment_total"] == 3 for chunk in chunks)


def test_semantic_chunking_splits_at_low_embedding_similarity():
    text = "报销需提交发票。报销单据由财务审核。服务器需要定期补丁。生产发布需要审批。"

    def fake_embedder(sentences):
        assert len(sentences) == 4
        return [
            [1.0, 0.0],
            [0.98, 0.02],
            [0.0, 1.0],
            [0.02, 0.98],
        ]

    chunks = build_chunks(
        [ParsedBlock(text=text)],
        options=ChunkingOptions(
            strategy="semantic",
            min_chunk_sentences=1,
            max_chunk_sentences=10,
            similarity_threshold=0.8,
            merge_window=2,
        ),
        semantic_embedder=fake_embedder,
    )

    assert [chunk.metadata_json["sentence_start"] for chunk in chunks] == [1, 3]
    assert [chunk.metadata_json["sentence_end"] for chunk in chunks] == [2, 4]
    assert chunks[0].metadata_json["semantic_split_reason"] == "similarity_threshold"
    assert chunks[0].metadata_json["semantic_previous_similarity"] < 0.8
    assert all(chunk.metadata_json["semantic_embedding_used"] is True for chunk in chunks)


def test_parent_child_preview_response_includes_explainable_context(client):
    """测试父子分块预览端点：返回可解释的上下文信息。

    面试点：为什么预览端点很重要？
    答：预览让用户在正式分块前看到分块效果，有助于选择合适的参数。
    这是典型的"可解释性"设计——让非技术人员也能理解分块策略的影响。
    """
    text = "ABCDEFGHIJ" * 12  # 120 字符

    resp = client.post(
        "/api/v1/chunking/preview",
        json={
            "text": text,
            "strategy": "parent-child",
            "options": {
                "parent_max_chars": 100,
                "child_max_chars": 40,
                "overlap_chars": 10,
            },
        },
    )

    assert resp.status_code == 200
    result_data = resp.json()["data"]
    first_chunk = result_data["chunks"][0]
    assert first_chunk["chunk_level"] == "child"  # 子块
    assert first_chunk["chunk_type"] == "text"     # 文本类型
    assert first_chunk["context_text"]              # 有父块上下文
    assert first_chunk["parent_chunk_uuid"]         # 有父块 UUID
    assert first_chunk["chunk_group_uuid"]          # 有分组 UUID
    assert first_chunk["metadata_json"]["parent_char_count"] == len(first_chunk["context_text"])
