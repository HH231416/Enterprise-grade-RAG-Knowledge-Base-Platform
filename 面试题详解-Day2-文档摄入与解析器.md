# 面试题详解 - Day 2：文档摄入与解析器

> 基于项目源码 `e:\xm\rag-knowledge-system-main\` 的深度分析
> 面试题来源：AI Agent面试完整题库

---

## 文件名：app/api/documents.py — 文档管理 REST API

### 一、文件内容概述

`documents.py` 是 RAG 系统文档管理模块的 FastAPI 接入层，提供了从文档上传、查询、更新、下载到删除的完整 RESTful CRUD 接口，共计 9 个端点。核心端点包括：`POST /documents/upload`（上传文档并触发摄入流程——解析、分块、向量化、入库）、`GET /documents`（分页查询文档列表，支持按来源类型、模块、解析状态、索引状态、关键词等多维度筛选）、`GET /documents/{doc_uuid}`（获取单个文档详情）、`PATCH /documents/{doc_uuid}`（部分更新文档元数据）、`POST /documents/{doc_uuid}/reindex`（对单个文档重新向量化索引，支持同步/异步两种模式）、`POST /documents/batch/reindex`（批量重索引）、`GET /documents/{doc_uuid}/download`（下载原始文件，根据 MIME 类型智能选择 inline 预览或 attachment 下载）、`DELETE /documents/{doc_uuid}`（删除单个文档及关联数据）、`POST /documents/batch/delete`（批量删除）。所有端点都通过 FastAPI 的依赖注入机制获取数据库会话、链路追踪 ID 和全局配置，业务逻辑委托给 `DocumentService`、`DocumentManagementService` 和 `IngestionService` 三个服务类处理，路由层本身保持极简。

### 二、详细技术点解析

**技术点1：上传端点的全流程编排**

`upload_document` 端点（第154-191行）是整个 RAG 冷启动的第一入口。该端点接收一个混合类型的请求体——`file` 通过 `UploadFile` 接收二进制文件流，`title`、`source_type`、`source_module`、`version`、`access_level` 等元数据通过 `Form(...)` 从 multipart/form-data 中提取。调用 `DocumentService(settings, session).upload_document()` 执行的完整流程为：文件格式校验 -> 保存到磁盘 -> 创建数据库 `Document` 记录 -> 创建 `IngestionJob` 任务记录 -> 调用 `IngestionService.process_upload()` 执行解析+分块+向量化。返回的 `DocumentUploadResponse` 包含了 `doc_uuid`（后续操作的句柄）、`job_uuid`（异步模式下轮询进度的句柄）、`chunk_count`（同步模式下的分块数量）、`execution_mode`（告知调用方当前是 sync 还是 async 模式）。

**技术点2：同步/异步双模式策略分流**

`reindex_document` 端点（第197-229行）展示了配置驱动的策略分流模式。通过 `settings.ingestion_mode == "async"` 做分支判断：异步模式下，创建重索引任务记录后通过 `BackgroundJobRunner.submit_ingest_job()` 提交到后台任务队列，`chunk_count` 返回 0（实际值在任务完成后更新）；同步模式下，直接在请求线程内调用 `IngestionService(session, settings).reprocess_document(doc_uuid)` 完成所有处理并返回实际分块数。响应体中的 `execution_mode` 字段让客户端明确知道当前模式，以便决定是否需要轮询任务状态。

**技术点3：文件下载的 MIME 智能判断**

`download_document` 端点（第116-148行）使用了三层防御逻辑确保文件下载安全和用户体验。第一层，通过 `DocumentManagementService.get_document_detail()` 查询数据库记录，然后交叉校验 `data.file_path`（数据库记录中的路径）和 `data.file_exists`（磁盘文件实际是否存在），任一不满足都抛出 404 错误。第二层，使用标准库 `mimetypes.guess_type()` 根据文件扩展名推断 MIME 类型，未知后缀回退到 `application/octet-stream`（触发浏览器下载而非预览）。第三层，通过 `mime_type.startswith("image/")` 判断是否图片文件——图片使用 `content_disposition_type="inline"` 让浏览器直接显示，非图片使用 `"attachment"` 触发下载对话框。

**技术点4：Prometheus 指标埋点**

第190行 `documents_uploaded_total.labels(source_type=source_type, status="success").inc()` 在每次文档上传成功后递增 Prometheus Counter 指标。`.labels()` 调用为指标附加了 `source_type`（pdf/docx/txt 等）和 `status`（success/failed）两个维度，使得运维团队可以在 Grafana 中按文档类型或成功/失败状态分组查看上传量趋势。这种在业务关键路径上埋点的方式是微服务可观测性标准实践。

**技术点5：批量操作的 POST 语义选择**

`batch_delete_documents` 端点（第250-258行）使用 `POST /documents/batch/delete` 而非 `DELETE`，注释明确说明了原因（第250行）：`DELETE` 方法通常不携带请求体（虽然在 HTTP 规范中不禁止，但很多 HTTP 客户端和代理服务器忽略 DELETE 请求的 body）。使用 POST 携带 `BatchDocumentOperationRequest`（包含 `doc_uuids` 列表）是更可靠的选择。这是 API 设计中对 HTTP 语义和实际工程约束的务实权衡。

### 三、涉及到的面试题

- 第16题：传统 RAG 完整执行链路是什么？
- 第24题：RAG 冷启动问题是什么？如何解决？
- 第28题：RAG 如何处理文档更新、删除？
- 第75题：如何利用 Batch 请求降低成本和提升吞吐？
- 第89题：如何实现可审计性（Auditability），满足合规要求？

### 四、面试题详解（结合本项目）

---

#### 面试题16：传统 RAG 完整执行链路是什么？

- **原有答案**：完整传统 RAG 分为文档预处理入库和用户问答查询两条链路。文档侧：加载原始文件、文本分块、调用嵌入模型转为向量、存储至向量数据库；用户提问侧：问题向量化、向量相似度检索、检索结果重排过滤、将优质文档片段拼接进提示词，最后送入大模型生成结合私有知识的回答。

- **结合本项目，我会这样回答**：

  在本项目中，传统 RAG 被拆成离线摄入和在线问答两条链路。摄入侧完成文件解析、结构化分块、向量化和元数据入库；在线侧先带着用户上下文进行权限范围收敛，再执行向量或混合检索、阈值过滤、重排和引用组装。

  这两个阶段通过统一的 chunk 数据契约连接。分块时保留的页码、章节、文档 ID 和父子关系会在检索结果中回填，因此生成模块不仅拿到文本，也能拿到可追溯的证据。

- **关键代码点（供理解）**：
  - RAG 文档入库入口：`POST /documents/upload` 端点 (documents.py 第154-191行)
  - 文件保存与记录创建：`DocumentService(settings, session).upload_document(file=file, ...)` (第170行)
  - 摄入流程调用：`IngestionService.process_upload()` (ingestion.py 第37行)
  - 任务状态跟踪：`IngestionJob.current_step` 字段 (如 ingestion.py 第50-51行 `"parsing"`)
  - 同步/异步分流：`settings.ingestion_mode == "async"` vs `else` (第205行和第218行)
  - 响应体结构：`DocumentUploadResponse(doc_uuid=..., job_uuid=..., chunk_count=..., execution_mode=...)` (第182-188行)

---

#### 面试题24：RAG 冷启动问题是什么？如何解决？

- **原有答案**：RAG 冷启动指知识库初始化阶段，内部业务文档数量稀少，用户提问无法检索到匹配资料，回答质量低下。解决冷启动可以批量导入全部存量业务文档，开放文件上传接口持续补充资料，启用混合检索兜底大模型内置通用知识，同时优先上传业务高频问题对应的文档，快速填充知识库。

- **结合本项目，我会这样回答**：

  在本项目中，我从“数据还没进索引”和“服务刚启动”两个角度处理冷启动。数据侧提供文档解析、分块、向量化和索引构建的标准摄入流程，并用同步模式或小样本快速验证首批知识；服务侧通过健康检查、依赖启动顺序和连接池预热减少启动后的失败重试。

  冷启动阶段更重要的是让结果可验证，而不是急于追求吞吐。项目先运行冒烟问题集，检查索引是否可查、引用是否正确、外部 Provider 是否连通，再逐步扩大数据与并发。

- **关键代码点（供理解）**：
  - 单文档上传：`POST /documents/upload` 端点 (documents.py 第154-191行)
  - 批量重索引：`POST /documents/batch/reindex` 端点 (第102-110行)
  - 异步任务提交：`BackgroundJobRunner.submit_ingest_job(doc_uuid=..., job_uuid=..., settings=settings)` (第211-215行)
  - 进度轮询基础：`DocumentUploadResponse` 中的 `job_uuid` 和 `execution_mode` 字段 (第183-188行)
  - 列表筛选：`list_documents` 端点支持 `parse_status`、`index_status` 过滤 (第50-51行)
  - 元数据采集：`title`、`source_type`、`source_module`、`version`、`access_level` 等 Form 字段 (第157-164行)

---

#### 面试题28：RAG 如何处理文档更新、删除？

- **原有答案**：RAG 文档更新删除分为两套方案：小型知识库采用全量更新，删除旧文档全部向量后重新上传生成；百万级海量知识库使用增量更新，单独新增修改后的文档向量，精准删除废弃文档对应的向量，避免全量重建耗时；同时可以为每份文档标记版本号，检索时过滤过期版本向量，保证信息时效性。

- **结合本项目，我会这样回答**：

  在本项目中，文档更新和删除不能只改业务表记录，还要同步处理分块、向量索引和检索缓存。更新时重新解析、分块和写入新向量；删除时先限制其不再参与检索，再清理关联 chunk 和向量，避免出现“文档已删但还能被召回”。

  这类操作需要以文档 ID 为主线管理关联资源，并通过后台任务或事务边界降低半完成状态的风险。完成后还应保留审计记录，便于定位索引与业务数据不一致的问题。

- **关键代码点（供理解）**：
  - 元数据更新：`PATCH /documents/{doc_uuid}` (documents.py 第87-96行)
  - 单文档重索引：`POST /documents/{doc_uuid}/reindex` (第197-229行)
  - 批量重索引：`POST /documents/batch/reindex` (第102-110行)
  - 单文档删除：`DELETE /documents/{doc_uuid}` (第235-244行)
  - 批量删除：`POST /documents/batch/delete` (第250-258行)
  - 版本号字段：`version: str = Form(default="v1")` (第159行)
  - 旧块清理机制：`stale_chunk_ids` 参数 (ingestion.py 第169行)
  - 原子替换：`chunks.replace_for_document()` 调用 (ingestion.py 第106行)

---

#### 面试题75：如何利用 Batch 请求降低成本和提升吞吐？

- **原有答案**：利用 Batch 请求降本提吞吐，核心是批量处理独立任务：离线场景下使用 LLM 批量 API 提升 GPU 利用率，工具调用场景下批量查询减少网络开销，同时需保证任务间相对独立，从而在降低单任务成本的同时，提升系统整体吞吐。

- **结合本项目，我会这样回答**：

  在本项目中，我会在摄入和评测这类大量重复调用的链路使用批量处理。多个 chunk 聚合后再调用 Embedding 或向量写入，可以减少网络往返和连接开销；评测索引构建也按批次执行，避免单条请求把吞吐限制在 I/O 等待上。

  批量大小不是越大越好，需要结合模型接口限制、内存、超时和失败重试策略确定。实践中要保留单批失败定位与重试能力，避免一个异常批次让整次入库不可恢复。

- **关键代码点（供理解）**：
  - 批量重索引入口：`POST /documents/batch/reindex` (documents.py 第102-110行)
  - 批量删除入口：`POST /documents/batch/delete` (第250-258行)
  - 共享请求模型：`BatchDocumentOperationRequest` 含 `doc_uuids` 列表 (第24行)
  - 后台任务提交：`BackgroundJobRunner.submit_ingest_job()` (第211-215行)
  - Service 层委托：`DocumentManagementService(session).batch_reindex_documents(...)` (第109行)

---

#### 面试题89：如何实现可审计性（Auditability），满足合规要求？

- **原有答案**：（同 Day1，略）

- **结合本项目，我会这样回答**：

  在本项目中，我从请求入口开始建立审计链路。应用中间件为每个请求生成或透传 trace_id，API、服务层、异常处理和模型调用日志都使用同一个标识；响应也回传该标识，方便将客户端问题、服务日志和数据库记录关联起来。

  我的设计重点不是只多写一条日志，而是让一次请求从进入系统到最终回答可追溯。对异常和高风险操作还要保留标准化结果，同时控制日志中的敏感字段，避免审计能力本身成为数据泄露入口。

- **关键代码点（供理解）**：
  - 全链路 trace_id：`trace_id: str = Depends(get_trace_id)` (documents.py 第53行等多处)
  - 统一响应封装：`success_response(data.model_dump(mode="json"), trace_id)` (第67行等多处)
  - 任务审计记录：`IngestionJob` 的 `job_type`、`status`、`current_step`、`started_at`、`finished_at` 字段
  - 软删除：`deleted_at` 非空标记而非物理删除
  - Prometheus 指标：`documents_uploaded_total.labels(source_type=..., status=...).inc()` (第190行)
  - 批量操作的可追溯性：每个批量操作生成独立的 Job 记录

---

---

## 文件名：app/services/ingestion.py — 文档摄入核心服务

### 一、文件内容概述

`IngestionService` 是本项目 RAG 文档摄入管道的核心编排服务，负责将原始文档文件转化为可供检索的向量化 Chunk。该服务封装了完整的摄入流程——解析（通过 `ParserRegistry` 根据文件扩展名选择解析器，将 PDF/Word/Excel/HTML 等格式转换为统一的 `ParsedBlock` 列表）、分块（通过 `build_chunks()` 根据可配置的策略参数将文本块切割为 `DocumentChunk` 列表）、稀疏索引（通过 `SparseIndexingService` 进行 BM25 关键词索引的更新）、向量化入库（通过 `IndexingService` 调用 Embedding Provider 生成向量并写入向量数据库）。过程中每个步骤的状态更新都通过 `IngestionJob.current_step` 持久化，使得客户端可以实时轮询摄入进度。服务还通过 `reprocess_document()` 和 `resume_job()` 方法支持文档重索引和失败任务恢复。`_mark_failed()` 方法实现了失败时的完整回滚——包括事务回滚、状态标记、错误原因记录、以及已上传文件的清理。此外，`_build_chunking_options()` 方法从文档的 `extra_meta` 字段中读取 11 个分块参数，实现了基于文档级别的个性化分块策略配置。

### 二、详细技术点解析

**技术点1：多阶段的流水线编排**

`process_upload_with_cleanup()` 方法（第40-149行）将摄入流程划分为明确的阶段，每个阶段通过修改 `IngestionJob.current_step` 和 `Document.status`/`parse_status` 来驱动状态流转。初始状态为 `document.status="processing"`、`parse_status="running"`、`job.current_step="parsing"`。解析完成后进入 `"chunking"` 阶段，分块完成后文档状态更新为 `status="active"`、`parse_status="success"`、`index_status="pending"`，然后交由 `IndexingService` 执行向量化入库。如果 Phase 1 解析失败，系统调用 `_mark_failed()` 执行回滚——包括事务回退、文件清理、状态标记为 failed、错误信息持久化。这种基于状态字的显式流水线设计比隐式的异常处理更有利于监控、调试和断点恢复。

**技术点2：分块策略的文档级别个性化**

`_build_chunking_options()` 方法（第237-292行）从文档的 `extra_meta` JSON 字段中读取 11 个分块配置参数，实现了"同一套摄入代码，不同文档使用不同分块策略"的高度灵活性。每一个参数都有默认值和取值范围的限制，通过 `_read_int_option()` 和 `_read_float_option()` 两个辅助方法做边界保护。可配置参数包括：`strategy`（分块策略，如 `"fixed"`、`"semantic"`、`"hierarchical"`）、`max_chars`（每块最大字符数，默认 1200，范围 100-8000）、`overlap_chars`（重叠字符数，默认 150，范围 0-2000）、`table_rows_per_chunk`（表格每块行数，默认 20）、`parent_max_chars`（父文档最大字符数，默认 3000）、`child_max_chars`（子块最大字符数，默认 600）、`min_chunk_sentences`（最小句子数，默认 3）、`max_chunk_sentences`（最大句子数，默认 20）、`similarity_threshold`（语义分块的相似度阈值，默认 0.5）、`merge_window`（合并窗口大小，默认 3）。这种"文档级个性化"的设计非常实用——法律合同（需要更大的 chunk 保留条款完整性）和技术手册（需要更小的 chunk 进行精准检索）可以有不同的分块参数，而在同一个知识库中并存。

**技术点3：父文档检索的数据模型支持**

`DocumentChunk` 模型的构建代码（第79-104行）明确包含了 `parent_chunk_uuid`（父块 UUID）和 `chunk_group_uuid`（块组 UUID）两个字段，这是 Parent Document Retrieval（父文档检索）策略的数据模型基础。当 `payload.parent_chunk_uuid` 存在时，系统将其从字符串转换为 `uuid.UUID` 类型存储；为 `None` 时表示该块没有父块（即它自身就是顶层块）。`chunk_level` 字段（第100行）进一步支持了多级父子层次结构。在检索时，当某个子块被向量相似度命中，系统可以通过 `parent_chunk_uuid` 反查其所属的父块，取出更大范围的上下文文本，通过 `context_text` 字段补充缺失的语义信息——这正是"小块向量检索保证精准，命中后取完整父文档补充信息"的父文档检索机制。

**技术点4：失败处理的原子回滚设计**

`_mark_failed()` 方法（第199-229行）展示了生产级错误处理的完整设计。该方法在摄入流程的任何阶段失败时被调用，执行了四项操作：(1) `self.session.rollback()` 回滚数据库事务，撤销当前阶段的所有部分写入；(2) 删除已上传的文件——通过 `Path(document.file_path).unlink()` 清理磁盘上已保存的原始文件，避免失败文件堆积占用存储空间；(3) 统一设置失败状态——`document.status="failed"`、`parse_status="failed"`、`job.status="failed"`、`job.current_step="failed"`，并将 `error_code` 和 `error_message` 持久化到 Job 记录；(4) 异常重新抛出——`raise AppError(...)` 将原始异常包装为标准的业务异常，确保 API 层能返回结构化的错误响应而非内部堆栈信息。文件清理部分的 `try-except` 嵌套（第213-220行）确保了即使文件删除失败（如权限问题），也不会掩盖原始的摄入失败错误。

**技术点5：Chunk 数据模型的丰富字段**

`DocumentChunk` 的构造代码（第79-104行）展示了远超"文本 + 向量"的丰富元数据设计。除了基础的 `chunk_text`、`chunk_index`、`token_count`、`char_count` 外，每个 Chunk 还携带了 `section_title`（所在章节标题）、`page_no`（页码，对于 PDF 文档）、`sheet_name`（工作表名，对于 Excel 文档）、`row_start` 和 `row_end`（行范围，对于表格数据）、`chunk_type`（块类型，如 text/table/image）、`metadata_json`（扩展 JSON 元数据）。这些字段为检索结果的展示提供了丰富的上下文信息——当用户在搜索结果中看到"第 15 页第 3-5 段"、"薪资表/2024Q4"这类信息时，可以快速判断该结果是否相关，而不需要点击预览完整文档。`chunk_type` 字段还使得检索服务可以根据用户意图做类型过滤（例如只检索表格数据）。

**技术点6：解析器的延迟加载与缓存**

`self.parser_registry = ParserRegistry()`（第34行）在 `__init__` 中创建，但 ParserRegistry 内部的各个解析器（PdfParser、DocxParser 等）通过 `_register_defaults()` 方法在首次使用时才被延迟导入（从 parser_registry.py 第57-82行可以看到，解析器的导入发生在 `_register_defaults()` 方法内部而非模块顶层）。这种延迟加载设计避免了在不需要某些解析器的场景下加载不必要的依赖（如 `pdfplumber`、`python-docx` 等第三方库），缩短了服务启动时间。

### 三、涉及到的面试题

- 第16题：传统 RAG 完整执行链路是什么？（摄入侧）
- 第17题：文本分块（Chunk）有哪些策略？各自优缺点？
- 第26题：什么是父文档检索（Parent Document Retrieval）？
- 第21题：什么是混合检索（Hybrid Search）？
- 第22题：RAG 如何缓解大模型幻觉？
- 第28题：RAG 如何处理文档更新、删除？
- 第71题：一个典型 Agent 请求的延迟主要由哪些部分构成？

### 四、面试题详解（结合本项目）

---

#### 面试题16：传统 RAG 完整执行链路是什么？（摄入侧）

- **原有答案**：（同前，略）

- **结合本项目，我会这样回答**：

  在本项目中，传统 RAG 被拆成离线摄入和在线问答两条链路。摄入侧完成文件解析、结构化分块、向量化和元数据入库；在线侧先带着用户上下文进行权限范围收敛，再执行向量或混合检索、阈值过滤、重排和引用组装。

  这两个阶段通过统一的 chunk 数据契约连接。分块时保留的页码、章节、文档 ID 和父子关系会在检索结果中回填，因此生成模块不仅拿到文本，也能拿到可追溯的证据。

- **关键代码点（供理解）**：
  - 流程入口：`process_upload_with_cleanup()` (ingestion.py 第40-149行)
  - 解析阶段：`self.parser_registry.parse(document.file_ext, file_path)` (第56行)
  - 分块阶段：`build_chunks(parsed_blocks, options=chunk_options)` (第68-71行)
  - Chunk 持久化：`self.chunks.replace_for_document(document.doc_uuid, chunk_models)` (第106行)
  - 稀疏索引更新：`self.sparse_indexing.replace_document_chunks(...)` (第107-111行)
  - 向量化入库：`IndexingService(...).index_document_with_cleanup(...)` (第117-121行)
  - 断点续传：`resume_job()` 方法 (第177-197行)

---

#### 面试题17：文本分块（Chunk）有哪些策略？各自优缺点？

- **原有答案**：文本分块主流四种策略：固定长度切分开发简单，但容易切断完整语义；语义切分依靠嵌入向量判断语义边界，块内信息完整，但是处理速度较慢；标题层级切分依托文档目录结构拆分，仅适用于带层级标题的结构化文档；递归字符切分按换行、段落、标点多层分隔符依次切割，平衡语义完整性与处理速度，是企业 RAG 系统的通用方案。

- **结合本项目，我会这样回答**：

  在本项目中，我不会用一种固定长度规则处理所有文档，而是根据普通文本、标题结构、表格和父子上下文选择不同分块策略。结构化文档优先沿章节边界组织，表格尽量保留表头与数据行，父子分块则把检索粒度和上下文粒度分开。

  这些策略最终输出统一 chunk 结构并保留页码、章节、Token 估算等元数据。这样检索和索引层只消费统一契约，不需要了解前面具体采用了哪一种切分算法。

- **关键代码点（供理解）**：
  - 策略选择：`_get_chunking_strategy(document)` (ingestion.py 第232-234行)
  - 固定切分参数：`max_chars=1200`、`overlap_chars=150` (第241-242行)
  - 语义切分参数：`similarity_threshold=0.5`、`merge_window=3` (第278-291行)
  - 句子级参数：`min_chunk_sentences=3`、`max_chunk_sentences=20` (第264-276行)
  - 表格参数：`table_rows_per_chunk=20` (第243-249行)
  - 父文档参数：`parent_max_chars=3000`、`child_max_chars=600` (第250-263行)
  - 安全读取方法：`_read_int_option()` 和 `_read_float_option()` (第294-324行)
  - 所有参数均有默认值、最小值和最大值的边界保护 (第240-291行)

---

#### 面试题26：什么是父文档检索（Parent Document Retrieval）？

- **原有答案**：常规 RAG 使用细碎文本块生成向量检索，虽然匹配精准，但单块文本信息残缺，模型缺少完整上下文。父文档检索方案将完整大段落作为父文档，切割出子块生成向量存入向量库；检索时命中子块后，取出该子块所属的完整父文档送入大模型，同时兼顾检索精准度与回答信息完整度。

- **结合本项目，我会这样回答**：

  在本项目中，我通过父子分块解决“块小则召回准、块大则上下文全”的矛盾。较小的子块用于向量匹配，父块保存完整章节或段落；入库时写入父子关联、块级别和父级文本，为命中后的上下文扩展提供基础。

  需要如实说明的是，项目已完成父子数据建模和持久化，但当前检索结果仍主要返回命中的子块。后续可在检索侧按父块回填、去重和预算截断，形成完整的 Parent Document Retrieval 闭环。

- **关键代码点（供理解）**：
  - 父块引用字段：`parent_chunk_uuid=uuid.UUID(payload.parent_chunk_uuid) if payload.parent_chunk_uuid else None` (ingestion.py 第94-96行)
  - 块组字段：`chunk_group_uuid=uuid.UUID(payload.chunk_group_uuid) if payload.chunk_group_uuid else None` (第97-99行)
  - 层次级别：`chunk_level=payload.chunk_level` (第100行)
  - 上下文文本：`context_text=payload.context_text` (第101行)
  - 父块尺寸配置：`parent_max_chars` 默认 3000 (第250-256行)
  - 子块尺寸配置：`child_max_chars` 默认 600 (第257-263行)

---

#### 面试题21：什么是混合检索（Hybrid Search）？

- **原有答案**：（同 Day1，略）

- **结合本项目，我会这样回答**：

  在本项目中，我将混合检索设计为 dense 语义召回与 BM25 关键词召回的互补组合。dense 处理同义表达，BM25 处理编号、缩写和专有名词；两路候选独立生成后，再归一化和加权融合排序。

  融合后仍会经过阈值、可选重排和 Top-K 截断。项目还使用固定样本比较 dense 与 hybrid 的 Hit@1、MRR，确保改动不是只看感觉，而是有可重复的效果证据。

- **关键代码点（供理解）**：
  - 稀疏索引构建：`self.sparse_indexing.replace_document_chunks(document=..., chunks=...)` (ingestion.py 第107-111行)
  - 稠密向量索引构建：`IndexingService(...).index_document_with_cleanup(...)` (第117-121行)
  - 稀疏索引服务初始化：`self.sparse_indexing = SparseIndexingService()` (第35行)
  - 配置分离：`vector_store_provider` 和 `sparse_index_provider` (config.py 第93行、第73行)
  - Chunk 原子替换：`self.chunks.replace_for_document(document.doc_uuid, chunk_models)` (ingestion.py 第106行)
  - 旧数据清理：`stale_chunk_ids` 参数贯穿两路索引更新 (第40-149行)

---

#### 面试题22：RAG 如何缓解大模型幻觉？

- **原有答案**：（同 Day1，略）

- **结合本项目，我会这样回答**：

  在本项目中，我没有把 RAG 设计成“检索到内容就直接生成”。Agent 会先检索并判断证据质量，证据充分才生成带引用的回答；首轮不足会改写查询重试，仍不足则返回明确拒答。

  这样把证据门槛放在生成之前，减少模型在没有依据时自由补全。引用、低温度和固定评测集可以进一步降低风险，但需要明确：RAG 是降低幻觉概率的工程机制，不是绝对保证。

- **关键代码点（供理解）**：
  - 解析结果非空检查：`if not parsed_blocks: raise AppError(code=ErrorCode.DOCUMENT_PARSE_FAILED, ...)` (ingestion.py 第57-62行)
  - 分块结果非空检查：`if not chunk_payloads: raise AppError(code=ErrorCode.CHUNKING_FAILED, ...)` (第72-77行)
  - 引用溯源元数据：`section_title`、`page_no`、`sheet_name`、`row_start`、`row_end` (第85-89行)
  - 重叠窗口：`overlap_chars` 默认 150，范围 0-2000 (第242行)
  - 失败处理：`_mark_failed()` 方法清理无效文件并标记失败状态 (第199-229行)

---

#### 面试题28：RAG 如何处理文档更新、删除？

- **原有答案**：（同前，略）

- **结合本项目，我会这样回答**：

  在本项目中，文档更新和删除不能只改业务表记录，还要同步处理分块、向量索引和检索缓存。更新时重新解析、分块和写入新向量；删除时先限制其不再参与检索，再清理关联 chunk 和向量，避免出现“文档已删但还能被召回”。

  这类操作需要以文档 ID 为主线管理关联资源，并通过后台任务或事务边界降低半完成状态的风险。完成后还应保留审计记录，便于定位索引与业务数据不一致的问题。

- **关键代码点（供理解）**：
  - 重处理入口：`reprocess_document(doc_uuid)` (ingestion.py 第151-175行)
  - 文档存在性校验：`if document is None or document.deleted_at is not None: raise AppError(...)` (第153-158行)
  - 旧 Chunk ID 收集：`stale_chunk_ids = self.chunks.list_chunk_ids_for_document(document.doc_uuid)` (第169行)
  - Chunk 原子替换：`self.chunks.replace_for_document(document.doc_uuid, chunk_models)` (第106行)
  - 稀疏索引原子替换：`self.sparse_indexing.replace_document_chunks(...)` (第107-111行)
  - 向量索引原子替换：`IndexingService(...).index_document_with_cleanup(stale_chunk_ids=...)` (第117-121行)
  - 断点恢复：`resume_job(doc_uuid=..., job_uuid=...)` (第177-197行)

---

#### 面试题71：一个典型 Agent 请求的延迟主要由哪些部分构成？

- **原有答案**：Agent 请求的延迟是多环节叠加的结果，核心包括前端网络、后端业务逻辑、LLM 调用（最主要）、工具调用四部分，在多轮对话或多工具调用场景下，延迟会被循环次数进一步放大，因此延迟优化需要覆盖全链路环节。

- **结合本项目，我会这样回答**：

  在本项目中，我将一次 Agent 请求的耗时拆为规划、检索、生成和可选重排等阶段，并把总延迟、轮次和状态轨迹返回给调用方。检索侧通过候选数量、阈值、批量和缓存控制耗时，生成侧通过模型选择、上下文预算和输出长度控制成本。

  分段观测的价值是能定位瓶颈：是向量库慢、Rerank 慢，还是 LLM 调用慢，而不是只看到一个总耗时。优化时我会优先处理 P95 延迟和失败重试，而非只优化平均值。

- **关键代码点（供理解）**：
  - 解析耗时：`parsed_blocks = self.parser_registry.parse(...)` (ingestion.py 第56行)
  - 分块耗时：`chunk_payloads = build_chunks(parsed_blocks, options=chunk_options)` (第68-71行)
  - 稀疏索引耗时：`self.sparse_indexing.replace_document_chunks(...)` (第107-111行)
  - 向量化入库耗时：`IndexingService(...).index_document_with_cleanup(...)` (第117-121行)
  - 状态步进跟踪：`job.current_step = "parsing"` (第50行) -> `"chunking"` (第64行) -> `"chunking_completed"` (第125行)
  - 同步/异步模式：sync 模式在请求线程内完成全部步骤，async 模式通过 BackgroundJobRunner 异步化 (documents.py 第205-229行)

---

---

## 文件名：app/ingestion/parser_registry.py — 文档解析器注册中心

### 一、文件内容概述

`ParserRegistry` 是本项目文档解析层的插件注册中心，负责将文件扩展名（如 `pdf`、`docx`、`xlsx`）映射到对应的解析器实现（如 `PdfParser`、`DocxParser`、`XlsxParser`）。该文件实现了一个简洁而强大的装饰器式注册机制——通过 `register(*file_types)` 方法返回一个装饰器，开发者只需在解析器类上添加 `@registry.register("pdf")` 即可注册对应关系。内部使用字典 `_factories` 存储扩展名到解析器工厂函数的映射。`parse()` 方法是统一的外部调用接口——传入文件扩展名和文件路径，自动选择正确的解析器并执行解析，返回统一的 `ParsedBlock` 列表。文件在类初始化时通过 `_register_defaults()` 方法预加载 10 种内置解析器，覆盖了 PDF（`PdfParser`）、Word（`DocxParser`）、Excel（`XlsxParser`）、文本（`TextParser`）、Markdown（`MarkdownParser`）、HTML（`HtmlParser`）、CSV（`CsvParser`）、邮件（`EmlParser`）、聊天记录（`ChatParser`）、图片（`ImageParser`，支持 jpg/jpeg/png/gif/bmp/tiff/tif/webp 八种图片格式）等主流文件类型。文件同时还通过模块级变量 `parser_registry = ParserRegistry()` 导出一个全局单例，供 `IngestionService` 等调用方直接使用。

### 二、详细技术点解析

**技术点1：装饰器式注册机制**

`register()` 方法（第28-38行）是插件注册模式的经典实现。它接收可变数量的文件类型字符串（`*file_types`），返回一个装饰器函数。装饰器内部遍历所有文件类型，通过 `normalize_file_type()` 清洗后存入 `_factories` 字典。这种设计的优雅之处在于：解析器类的定义和注册是位置绑定的——在解析器模块中，开发者在类定义上方添加 `@registry.register("pdf")`，PdfParser 就自动注册到 Registry 中。注册逻辑和业务逻辑在物理位置和时间上都是解耦的。`register()` 返回的装饰器保留了 `ParserFactory` 类型签名（`Callable[[ParserFactory], ParserFactory]`），确保了类型安全。

**技术点2：延迟导入（Lazy Import）**

`_register_defaults()` 方法（第57-82行）在方法体内部通过局部 import 导入 10 个解析器类（第58-67行），而非在模块顶层导入。这种延迟导入模式有两个核心收益：第一，缩短模块的首次加载时间——如果某次运行不需要处理 EmlParser（邮件解析器），它的依赖库（如 `email` 标准库或第三方邮件解析库）就不会被加载；第二，避免循环导入——解析器模块本身可能依赖 `parser_registry`（例如解析器内部的子解析器也需要注册），如果 Registry 在顶层导入所有解析器，容易产生循环依赖。局部导入将依赖图的解析推迟到实际调用 `_register_defaults()` 时。

**技术点3：文件类型标准化**

`normalize_file_type()` 静态方法（第22-26行）对所有注册和查询的文件类型执行统一清洗：去掉首尾空白（`strip()`）、转为小写（`lower()`）、去掉前导点号（`lstrip(".")`）。这意味着无论调用方传入 `"pdf"`、`"PDF"`、`".pdf"`、`" .PDF "` 中的哪一种，Registry 都能正确定位到 `PdfParser`。这个设计的容错性在实际使用中非常重要——文件扩展名的来源多种多样（从上传来文件的 filename 属性提取、从数据库记录的 source_type 字段读取、从 API 参数传入），每种来源的格式习惯可能不同，统一的标准化层消除了这一变数。

**技术点4：解析器工厂模式**

`_factories` 字典的类型是 `dict[str, ParserFactory]`，其中 `ParserFactory = Callable[..., DocumentParser]`（第11行）。这意味着 Registry 存储的不是解析器实例，而是**创建解析器的工厂函数**（通常是解析器类本身，因为类是可调用的）。`get_parser()` 方法（第43-51行）通过 `factory(**kwargs)` 创建解析器实例，支持向解析器构造函数传递任意参数（如 `language="zh"` 指定中文解析模式、`ocr_enabled=True` 启用 OCR）。工厂模式的好处是：解析器实例是按需创建的，不需要在 Registry 中预创建所有解析器实例并常驻内存。`parse()` 便捷方法（第53-55行）封装了"获取解析器 + 调用 parse"的常见两步操作。

**技术点5：多模态解析支持**

`ImageParser` 的注册（第79行）是本项目支持多模态 RAG 的直接证据。`ImageParser` 被注册为处理八种图片格式：`jpg`、`jpeg`、`png`、`gif`、`bmp`、`tiff`、`tif`、`webp`。当用户上传一张 PNG 截图时，`ParserRegistry.parse("png", file_path)` 会路由到 `ImageParser`，该解析器内部调用 PaddleOCR 或 Multimodal LLM 进行图像文字识别，将图片中的文字提取为 `ParsedBlock` 列表。这意味着图片中的文字可以像普通文本文档一样被分块、向量化并检索，用户可以通过文字查询检索到图片内容。

**技术点6：聊天记录的结构化解析**

`ChatParser` 的注册（第78行）支持 `jsonl` 和 `chat.jsonl` 两种文件类型——这是专门为处理对话/聊天记录设计的解析器。在企业客服知识库场景中，历史聊天记录是重要的知识来源（包含大量真实问答对）。ChatParser 将 JSONL 格式的对话记录解析为结构化的 ParsedBlock，保留发言者角色（用户/客服）、时间戳、对话轮次等元数据，这些信息在分块和检索时可用于提升问答匹配精度。

**技术点7：全局单例导出**

第85行 `parser_registry = ParserRegistry()` 在模块级别创建了一个全局单例。`IngestionService` 在 `__init__` 中通过 `self.parser_registry = ParserRegistry()` 创建了自己的实例（ingestion.py 第34行），这意味着每个 `IngestionService` 实例都有独立的 Registry——这不是标准的单例用法，但由于解析器注册信息在每个 Registry 实例中相同（都通过 `_register_defaults()` 加载），所以不存在状态不一致的问题。如果想实现真正的全局共享，可以将 `IngestionService` 改为使用模块级单例 `parser_registry` 而非每次创建新实例。

### 三、涉及到的面试题

- 第23题：什么是多模态 RAG？支持哪些文件类型？
- 第16题：传统 RAG 完整执行链路是什么？（解析器第一步）
- 第91题：常见的 Agent 开发框架/组件化思路有哪些？

### 四、面试题详解（结合本项目）

---

#### 面试题23：什么是多模态 RAG？支持哪些文件类型？

- **原有答案**：传统 RAG 仅能处理纯文本文件，多模态 RAG 拓展支持 PDF 扫描件、图片、Excel 表格、PPT 等图文混合资料。系统通过 OCR 识别图片内文字，使用图像嵌入模型提取画面语义特征，将文字、图像向量统一存储至向量数据库，用户提问时同时检索文本与图像相关资料，实现图文联合问答。

- **结合本项目，我会这样回答**：

  在本项目中，摄入层支持 PDF、Word、文本和表格等文件，并将不同格式先解析为统一的文本与结构信息，再交给相同的分块和索引链路。这样上层检索不需要为每种文件格式重复实现一套逻辑。

  当前重点仍是从文档中提取可检索的文本、标题、页码和表格结构，而不是完整的图像、音频或视频语义理解。对于真正的多模态检索，后续需要补充图像向量、跨模态 Embedding 和对应的引用展示。

- **关键代码点（供理解）**：
  - 图片解析器注册：`(ImageParser, ("jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"))` (parser_registry.py 第79行)
  - 办公文档注册：`PdfParser`、`DocxParser`、`XlsxParser`、`CsvParser` (第70-73行、第76行)
  - 文本类注册：`TextParser`、`MarkdownParser`、`HtmlParser` (第73-75行)
  - 邮件与聊天：`EmlParser`、`ChatParser` (第77-78行)
  - OCR 配置：`image_recognition_provider: str = "paddle_ocr"` (config.py 第224-227行)
  - 多模态 LLM 配置：`multimodal_api_base`、`multimodal_api_key`、`multimodal_model` (config.py 第232-243行)
  - 统一解析接口：`parse(file_type: str, file_path: Path) -> list[ParsedBlock]` (parser_registry.py 第53-55行)

---

#### 面试题16：传统 RAG 完整执行链路是什么？（解析器第一步）

- **原有答案**：（同前，略）

- **结合本项目，我会这样回答**：

  在本项目中，传统 RAG 被拆成离线摄入和在线问答两条链路。摄入侧完成文件解析、结构化分块、向量化和元数据入库；在线侧先带着用户上下文进行权限范围收敛，再执行向量或混合检索、阈值过滤、重排和引用组装。

  这两个阶段通过统一的 chunk 数据契约连接。分块时保留的页码、章节、文档 ID 和父子关系会在检索结果中回填，因此生成模块不仅拿到文本，也能拿到可追溯的证据。

- **关键代码点（供理解）**：
  - 解析入口：`self.parser_registry.parse(document.file_ext, file_path)` (ingestion.py 第56行)
  - 统一输出格式：`list[ParsedBlock]` (parser_registry.py 第53行)
  - 解析器类型：10 种专用解析器覆盖 PDF、Word、Excel、图片等格式 (parser_registry.py 第69-80行)
  - 解析器接口：`DocumentParser` 基类 (parser_registry.py 第7行导入)
  - 文件类型标准化：`normalize_file_type()` (parser_registry.py 第22-26行)

---

#### 面试题91：常见的 Agent 开发框架/组件化思路有哪些？

- **原有答案**：（同 Day1，略）

- **结合本项目，我会这样回答**：

  在本项目中，我没有把所有 Agent 逻辑堆在路由层，而是拆为 API 接入、服务编排、检索、Provider、权限、后台任务和数据模型等模块。API 负责参数校验、身份与 trace_id 注入；服务层负责业务流程；底层组件通过稳定接口提供向量、模型和权限能力。

  这套组件化设计让替换 Provider、增加检索策略或调整任务执行方式时，改动集中在对应模块。配置和数据契约再把模块连接起来，既避免强耦合，也保证各层的输入输出可验证。

- **关键代码点（供理解）**：
  - 组件接口：`DocumentParser` 基类 (parser_registry.py 第7行导入)
  - 装饰器注册：`register(*file_types)` 方法 (第28-38行)
  - 组件路由：`get_parser(file_type, **kwargs)` (第43-51行)
  - 支持类型查询：`supported_file_types()` (第40-41行)
  - 10 种内置组件：`_register_defaults()` 方法 (第57-82行)
  - 全局单例：`parser_registry = ParserRegistry()` (第85行)
  - 开闭原则体现：新增解析器无需修改 Registry 核心逻辑


