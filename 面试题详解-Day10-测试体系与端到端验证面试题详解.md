# Day 10：测试体系与端到端验证面试题详解

本日聚焦项目中 4 个核心测试文件，涵盖 Agentic RAG 的端到端验证、检索与问答的集成测试、混合检索基准测试、评估模式行为验证。重点面试题覆盖第 12、14、21、22、69、84、86、92、93、95、96 题。

---

## 文件名：test_qa_agent.py -- Agentic RAG 端到端测试

### 一、文件内容概述

`tests/test_qa_agent.py` 是本项目 Agentic RAG 核心流程的端到端测试文件，包含三个精心设计的测试用例，验证了 QA Agent 服务的关键行为。第一个测试 `test_agent_answer_endpoint_returns_trace` 通过完整 HTTP 请求链路验证 Agent 回答接口的端到端行为，包括文档上传、状态机状态流转（plan -> ... -> finish）、检索轮次、trace 记录、final_query 生成和 latency_ms 测量。第二个测试 `test_agent_service_rewrites_once_when_first_retrieval_is_weak` 通过 monkeypatch 模拟首次检索返回空结果、二次检索命中文档的场景，验证 Agent 的自动 Query 改写和重试机制。第三个测试 `test_agent_prefers_structured_llm_rewrite_when_provider_is_available` 验证当 LLM Provider 可用时，Agent 优先使用 LLM 结构化改写而非简单的关键词拼接，并验证改写后的查询能被正确记录到 trace 中。

### 二、详细技术点解析

**1. monkeypatch 驱动的依赖隔离测试**

两个服务层测试使用了 pytest 的 `monkeypatch` 机制实现对外部依赖的精确控制。`test_agent_service_rewrites_once_when_first_retrieval_is_weak`（46-95 行）中，通过 `monkeypatch.setattr("app.services.retrieval.RetrievalService.search", fake_search)`（81 行）替换了检索服务的方法为自定义的 fake 函数。这个 fake 函数使用闭包变量 `calls["count"]` 追踪调用次数，第一次调用返回空的 `SearchResponseData(hits=[])` 模拟弱检索结果，第二次调用返回带有一条 `Citation` 的高分文档。这种设计比 mock 框架更直观：它测试的是真实的 `QaAgentService.answer()` 方法执行流程，只替换了最外层的检索依赖，内部的所有 Agent 逻辑（状态机流转、重写判断、证据评估）都在真实运行。

**2. Agent 状态机的 trace 验证**

`test_agent_answer_endpoint_returns_trace`（21-43 行）对 Agent 状态机进行了全面的 trace 验证。断言 `payload["trace"][0]["state"] == "plan"`（40 行）验证了状态机的初始状态是 "plan"（规划阶段）；`payload["trace"][-1]["state"] == "finish"`（41 行）验证了最终状态是 "finish"（完成阶段）。这两个断言共同确认了 Agent 状态机完成了 plan -> (retrieve/rewrite/generate 等中间状态) -> finish 的完整流转。`assert payload["retrieval_rounds"] >= 1`（38 行）验证了至少执行了一轮检索。`assert payload["answer_status"] in {"grounded", "insufficient_evidence"}`（37 行）验证了回答状态是预定义的枚举值之一。

**3. Agentic RAG 的 Query 改写验证**

`test_agent_prefers_structured_llm_rewrite_when_provider_is_available`（98-164 行）深入验证了 Agentic RAG 的 Query 改写机制。首先通过 monkeypatch 设置 `service.llm_provider.is_configured` 返回 True（134 行），模拟 LLM Provider 可用的场景。然后通过 monkeypatch 设置 `service.llm_provider.rewrite_query` 返回 `LlmResult(answer="employee transfer manager HR approval workflow", ...)`（136-145 行），模拟 LLM 将原始问题 "How does employee transfer approval work?" 改写为更丰富关键词的检索查询。最后的断言 `result.final_query == "employee transfer manager HR approval workflow"`（162 行）验证了改写后的查询被正确使用。`assert any(step.state == "rewrite" for step in result.trace)`（94 行，第二个测试中）和 `rewrite_step = next(step for step in result.trace if step.state == "rewrite")`（163 行）确认了 rewrite 步骤被记录到了 trace 中。这完整验证了 Agentic RAG 的核心能力：自主改写检索词 -> 多轮检索 -> 动态优化召回。

**4. 结构化 LLM 调用结果的类型安全传递**

在 monkeypatch 设置中，`rewrite_query` 的返回值使用了 `LlmResult` 类型（138-145 行），这是一个包含 `answer`（改写后的查询文本）、`provider_name`、`model_name`、`request_tokens`、`response_tokens`、`latency_ms` 六个字段的结构化对象。这种设计确保了 LLM 调用的结果在整个链路中保持类型安全和可追溯性。测试中通过 `generate_answer_with_fallback` 返回 None（149-152 行）和 `_record_llm_log` 替换为空操作（152 行），避免测试对 LLM 生成答案和日志记录的真实依赖。

**5. HTTP 层端到端测试的完整链路**

`test_agent_answer_endpoint_returns_trace` 函数（21-43 行）通过 FastAPI 的 TestClient 完成了从文档上传到 Agent 回答的完整 HTTP 链路测试。先调用 `_upload_sample_document` 上传 PDF 文档（22 行），然后 POST 到 `/api/v1/qa/agent-answer` 端点（23-33 行），携带 `X-Internal-Token` 认证头、检索策略（dense）、权限过滤条件（source_module: ["hr"]）和用户上下文（user_id: "u1001"）。这个测试覆盖了认证、文档入库、Agent 检索规划、多轮检索、答案生成、trace 记录的全流程，是一个典型的集成测试。

### 三、涉及到的面试题

- **第 12 题：什么是 Agentic RAG？和传统 RAG 有什么区别？**
- **第 14 题：什么是 Agent 状态机？作用是什么？**
- **第 22 题：RAG 如何缓解大模型幻觉？**
- **第 69 题：有哪些自动检测幻觉/错误回答的方法？**
- **第 95 题：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）**

### 四、面试题详解（结合本项目）

---

#### 面试题 12：什么是 Agentic RAG？和传统 RAG 有什么区别？

- **原有答案**：传统 RAG 固定检索 Query，一次性召回文档，直接交给模型生成回答。Agentic RAG 的 RAG 检索流程交由 Agent 自主控制，可以改写检索词、多次检索、筛选文档、拆分查询，动态调整检索策略。传统 RAG 固定一次检索，Agentic RAG 自主改写 Query、多轮检索优化召回。传统 RAG 采用固定检索语句，一次性完成文档召回，无法根据问题动态调整检索策略；Agentic RAG 将检索流程交给 Agent 自主决策，智能体可以改写查询关键词、执行多轮检索、过滤无效文档，动态优化召回内容，大幅提升复杂问题的检索准确率。

- **结合本项目，我会这样回答**：

  我理解 Agentic RAG 的区别不在于多接了一个大模型，而在于检索变成可决策流程。传统 RAG 多数是固定地检索一次后直接生成；本项目会先检索并判断证据是否充分，证据不足时改写查询并进行下一轮检索，最后才决定回答或拒答。

  这种设计能提升复杂问题的召回机会，但必须设置轮次上限、记录状态轨迹并保留拒答分支，否则容易出现无限循环、成本失控和不可解释的问题。

- **关键代码点（供理解）**：
  - `test_qa_agent.py` 第 46-95 行：首次检索空结果 -> 自动 rewrite -> 二次检索命中，验证多轮检索。
  - `test_qa_agent.py` 第 98-164 行：LLM 改写 query 从 "How does employee transfer approval work?" 变为 "employee transfer manager HR approval workflow"。
  - `test_qa_agent.py` 第 40-41 行：trace 状态从 plan 到 finish，验证完整的 Agentic 决策链路。
  - `test_qa_agent.py` 第 93 行：`retrieval_rounds == 2` 确认两轮自主检索。
  - `test_qa_agent.py` 第 30 行：`retrieval_strategy` 参数由 Agent 上层控制。

---

#### 面试题 14：什么是 Agent 状态机？作用是什么？

- **原有答案**：Agent 状态机将 Agent 执行流程抽象成有限状态（等待用户输入、规划任务、调用工具、反思结果、任务结束），通过状态流转控制执行逻辑。作用：约束模型行为、避免无限循环、方便异常捕获、简化流程管控。划分执行状态，管控流程流转，防循环、易捕获异常。Agent 状态机把智能体执行全过程拆解为标准化有限状态，例如等待用户输入、任务规划、工具调用、结果反思、任务完成等，依靠状态流转控制整体执行逻辑。状态机能够有效约束模型行为、避免 Agent 陷入无限循环，同时统一捕获各阶段异常，大幅简化复杂工作流的管控逻辑。

- **结合本项目，我会这样回答**：

  在本项目中，我用状态机承载 Agent 流程：计划、检索、改写、生成和结束被记录为明确状态；检索轮次与证据判断决定是否进入改写或结束；最终只能收敛为 grounded 或 insufficient_evidence。这样状态、轮次与最终结果绑定，避免模型在不可见的循环中不断调用工具。

  它的价值是限制循环次数、方便故障定位，也让前端和监控系统能够看到请求走到了哪一步。相比只记录一段最终答案，状态机更适合线上排查和持续优化。

- **关键代码点（供理解）**：
  - `test_qa_agent.py` 第 40-41 行：trace[0].state == "plan" 和 trace[-1].state == "finish"，验证初始和终止状态。
  - `test_qa_agent.py` 第 94 行：`any(step.state == "rewrite" for step in result.trace)` 验证中间状态。
  - `test_qa_agent.py` 第 163-164 行：提取 rewrite 状态并验证 query 内容。
  - `test_qa_agent.py` 第 87、157 行：`max_retrieval_rounds=2` 限制检索轮次上限，防止无限循环。
  - `test_qa_agent.py` 第 92 行：`answer_status == "grounded"` 验证状态机正确收敛。

---

#### 面试题 22：RAG 如何缓解大模型幻觉？

- **原有答案**：RAG 通过多层手段抑制模型幻觉：第一，检索真实私有文档作为回答事实依据，约束模型基于召回文本生成答案；第二，系统 Prompt 明确规定知识库无对应信息时直接回复不清楚，禁止编造事实；第三，依靠重排模型过滤低相关干扰文档；第四，要求模型输出回答对应的文档来源，方便人工校验内容真实性，多重方案结合有效降低幻觉概率。

- **结合本项目，我会这样回答**：

  在本项目中，我没有把 RAG 设计成“检索到内容就直接生成”。Agent 会先检索并判断证据质量，证据充分才生成带引用的回答；首轮不足会改写查询重试，仍不足则返回明确拒答。这样把证据门槛放在生成之前，减少模型在没有依据时自由补全。

  这将模型输出约束在可追溯的检索证据上。低温度、引用展示和检索质量评估可以进一步降低风险，但最终仍要承认：RAG 是降低幻觉概率的工程机制，不是绝对保证。

- **关键代码点（供理解）**：
  - `test_qa_agent.py` 第 93 行：`retrieval_rounds == 2`，Agent 在首次检索空结果时选择重新检索而非编造。
  - `test_qa_agent.py` 第 37 行：`answer_status` 字段区分 "grounded" 和 "insufficient_evidence"。
  - `test_retrieval_qa.py` 第 249-250 行：无关问题时返回 "insufficient_evidence" + "未找到足够依据"。
  - `test_qa_agent.py` 第 39-41 行：trace 记录完整检索链路，支持幻觉追溯。
  - `test_qa_agent.py` 第 138-145 行：LLM 改写查询增强检索命中率，间接减少幻觉。

---

#### 面试题 69：有哪些自动检测幻觉/错误回答的方法？

- **原有答案**：幻觉检测的核心是多维度交叉验证，从不同层面识别编造事实或错误。RAG 场景校验：要求模型标注回答依据的文档片段，无对应依据的回答判定为高风险。模型自检反诘：让模型对自己的回答打置信度，并尝试自我批判，识别逻辑矛盾或不确定表述。后验对比验证：对可验证任务用程序或外部工具校验结果正确性。LLM 交叉判定：用另一个模型判断回答中是否存在编造事实的痕迹，高风险问题需人工复审。RAG 标依据，自检打置信，后验做校验，交叉再判定。

- **结合本项目，我会这样回答**：

  在本项目中，我将错误回答检测做成两层：生成前由证据阈值、检索分数和命中情况决定是否允许回答；生成后通过回答状态、引用、trace 以及固定评测集的 Hit@K、MRR 持续验证效果。这样既拦截单次请求的无依据回答，也能发现版本迭代带来的整体检索退化。

  本项目已经具备证据不足状态、检索轨迹和受控基准测试。后续可补充 LLM Judge 或人工抽检，用于覆盖“检索到了资料但回答仍然误读”的更高层错误。

- **关键代码点（供理解）**：
  - `test_qa_agent.py` 第 37 行：`answer_status` 字段（grounded/insufficient_evidence）是自动幻觉检测结果。
  - `test_qa_agent.py` 第 46-95 行：Agent 检测检索为空后触发 rewrite，是对检索策略的自检反诘。
  - `test_qa_agent.py` 第 98-164 行：LLM 改写查询是模型对检索质量的自我优化。
  - `test_retrieval_qa.py` 第 413-418 行：hit_at_k、mrr、expected_term_hit_rate 等自动化评估指标。
  - `test_controlled_hybrid_benchmark.py` 第 9-21 行：12 个受控查询的 benchmark 实现后验对比验证。

---

#### 面试题 95：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）

- **原有答案**：Agent 自动化测试需覆盖单元-行为-集成-回放四个层级。单元测试：对工具函数、RAG 管道、权限逻辑做传统单元测试。LLM 行为测试：基于固定输入样例和"可接受输出"集合，用 LLM Judge 或人工打分评估。集成测试：模拟真实用户请求，验证完整 Agent 流程（含工具、记忆、RAG）。回放测试：从线上日志中抽样请求，回放到新版本，对比行为差异。单测工具权限，行为测试用 LLM Judge，集成测全流程，回放对比差异。

- **结合本项目，我会这样回答**：

  在本项目中，我按风险将 Agent 测试分为三层：通过 monkeypatch 替换模型、检索等外部依赖，验证改写、证据判断和状态流转；通过真实 HTTP 请求验证上传、入库、检索、生成和引用的完整链路；通过固定数据集的受控基准保护混合检索排序不在迭代中退化。

  本项目的测试不仅断言接口状态码，还检查回答状态、引用、检索轮次、trace、延迟和异常路径。测试结束后恢复配置和全局状态，保证用例可重复，避免异步、模型和环境变量导致偶发失败。

- **关键代码点（供理解）**：
  - `test_qa_agent.py` 第 81、134-152 行：monkeypatch 精确替换外部依赖，实现单元级隔离。
  - `test_qa_agent.py` 第 21-43 行：HTTP 端到端测试覆盖文档上传->Agent 决策->生成答案全流程。
  - `test_qa_agent.py` 第 36-43 行：断言覆盖 answer/answer_status/retrieval_rounds/trace/final_query/latency_ms 六个维度。
  - `test_qa_agent.py` 第 92-95 行：固定输入+固定依赖行为 -> 固定预期输出，确定性可重复。
  - `test_qa_agent.py` 第 11-18 行：`_upload_sample_document` 测试辅助函数的复用。

---

## 文件名：test_retrieval_qa.py -- 检索与问答集成测试

### 一、文件内容概述

`tests/test_retrieval_qa.py` 是本项目最大的测试文件，覆盖了检索（search/debug-search）、QA 问答、权限验证、评估系统和隔离性验证等全方位测试场景。包含 11 个测试函数和 7 个辅助函数。`test_search_returns_hits_for_authorized_user` 验证授权用户能检索到文档并返回 vector_score 和 text_score。`test_qa_answer_returns_grounded_citations` 验证 QA 回答包含引用且状态为 grounded。`test_qa_answer_rejects_bad_internal_token` 验证错误的 Internal-Token 被拒绝（401）。`test_debug_search_returns_score_breakdown` 验证 debug-search 端点返回分数字段。`test_qa_answer_returns_insufficient_evidence_for_high_threshold` 验证无关问题时返回 insufficient_evidence。`test_search_supports_natural_chinese_query` 和 `test_qa_answer_supports_natural_chinese_question` 验证中文自然语言查询支持。`test_evaluation_run_uses_isolated_vector_collection_only` 验证评估系统使用隔离的向量集合。辅助函数覆盖了文档上传（PDF/DOCX/自定义）、身份用户创建、用户上下文构建和评估数据集创建。

### 二、详细技术点解析

**1. 同步模式的环境切换模式**

几乎所有测试函数都包含一段重复的模式（以 `test_search_returns_hits_for_authorized_user` 为例，131-157 行）：`os.environ["INGESTION_MODE"] = "sync"` -> `reset_settings_cache()` -> 执行测试逻辑 -> `os.environ["INGESTION_MODE"] = "async"` -> `reset_settings_cache()`。这个模式解决了文档入库的异步/同步切换问题：测试需要文档立即可检索（同步模式），但测试完成后需要恢复默认的异步模式以避免影响后续测试。`reset_settings_cache()` 清除了 `get_settings()` 的 lru_cache 缓存，确保环境变量的变更立即被 `get_settings()` 读取到。

**2. 多种文档上传方式的辅助函数体系**

测试文件提供了 5 个文档上传辅助函数来覆盖不同的测试场景。`_upload_sample_document`（5-18 行）使用 fixture 提供的固定样本 PDF；`_upload_custom_pdf_document`（15-38 行）使用 PyMuPDF（fitz）库在内存中动态创建包含自定义文本的 PDF，无需依赖外部文件；`_upload_document_with_resource_meta`（40-64 行）上传带有额外元数据的文档（extra_meta、tags、version）；`_upload_document_without_owner`（66-73 行）测试无属主文档的访问权限场景；`_upload_sample_docx_document`（75-88 行）测试 DOCX 文档类型支持。这些辅助函数都返回 `doc_uuid`，使得后续的检索和问答测试可以直接引用上传的文档。

**3. RBAC 权限控制的全面测试**

`_create_identity_user` 辅助函数（90-125 行）构建了完整的 RBAC 测试基础设施。它先创建部门（departments）和角色（roles），然后创建用户并关联部门和角色。`assert response.status_code in {200, 409}` 的断言模式（106、112、125 行）同时兼容首次创建（200）和重复创建（409）的情况，使得测试可以安全地重复执行。`_user_context` 辅助函数（127-128 行）生成包含 `user_id` 的用户上下文字典，模拟从 JWT Token 或 Session 中解析出的用户身份信息。`test_search_returns_hits_for_authorized_user`（130-157 行）验证了授权用户能检索到关联部门的文档；`test_qa_answer_rejects_bad_internal_token`（188-206 行）验证了错误令牌被正确拒绝并返回 401 状态码。

**4. 评估系统的隔离性验证**

`test_evaluation_run_uses_isolated_vector_collection_only`（363-421 行）是本文件中最精密的测试。它上传两份文档：一份名为 "Production Match" 的文档作为"正确答案"存在于生产环境中（370-373 行），另一份名为 "Isolated Eval Document" 的文档作为评估的隔离文档（375-379 行）。评估数据集期望命中 "Production Match" 文档（387-389 行），但通过 `RunCreateRequest` 的 `doc_uuids=[isolated_doc_uuid]`（401 行）将评估限制在隔离文档上。测试断言 `result["hit_at_1"] is False`（413 行）、`result["mrr"] == 0.0`（416 行）以及所有 top_hits 都不包含 "Production Match"（418 行），证明评估系统严格使用了隔离的向量集合，没有混入生产环境的文档。

**5. 中文自然语言处理的测试覆盖**

`test_search_supports_natural_chinese_query`（251-272 行）和 `test_qa_answer_supports_natural_chinese_question`（306-328 行）专门验证了系统对中文自然语言查询的支持。测试使用中文问题 "调岗申请的二级审批是怎么要求的？" 并断言 `text_score > 0`（272 行），证明系统的 BM25 关键词检索组件能正确处理中文分词。"调岗申请"、"二级审批"等中文业务术语的召回情况直接反映了系统的中文检索能力。

### 三、涉及到的面试题

- **第 22 题：RAG 如何缓解大模型幻觉？**
- **第 69 题：有哪些自动检测幻觉/错误回答的方法？**
- **第 84 题：如何在企业内做权限控制（RBAC）保证数据边界？**
- **第 86 题：如何防御"越权操作"和"越权询问"？**
- **第 95 题：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）**

### 四、面试题详解（结合本项目）

---

#### 面试题 22：RAG 如何缓解大模型幻觉？

- **原有答案**：（已在 test_qa_agent.py 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在本项目中，我没有把 RAG 设计成“检索到内容就直接生成”。Agent 会先检索并判断证据质量，证据充分才生成带引用的回答；首轮不足会改写查询重试，仍不足则返回明确拒答。这样把证据门槛放在生成之前，减少模型在没有依据时自由补全。

  这将模型输出约束在可追溯的检索证据上。低温度、引用展示和检索质量评估可以进一步降低风险，但最终仍要承认：RAG 是降低幻觉概率的工程机制，不是绝对保证。

- **关键代码点（供理解）**：
  - `test_retrieval_qa.py` 第 181 行：`payload["data"]["citations"]` 提供回答的文档引用来源。
  - `test_retrieval_qa.py` 第 182 行：`answer_status == "grounded"` 标识回答有据可查。
  - `test_retrieval_qa.py` 第 249-250 行：无关问题时 `answer_status == "insufficient_evidence"` + "未找到足够依据"。
  - `test_retrieval_qa.py` 第 413-418 行：自动化评估指标持续监测检索质量。
  - `test_retrieval_qa.py` 第 175 行：`temperature: 0.1` 低温度降低 LLM 随机编造可能。

---

#### 面试题 69：有哪些自动检测幻觉/错误回答的方法？

- **原有答案**：（已在 test_qa_agent.py 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在本项目中，我将错误回答检测做成两层：生成前由证据阈值、检索分数和命中情况决定是否允许回答；生成后通过回答状态、引用、trace 以及固定评测集的 Hit@K、MRR 持续验证效果。这样既拦截单次请求的无依据回答，也能发现版本迭代带来的整体检索退化。

  本项目已经具备证据不足状态、检索轨迹和受控基准测试。后续可补充 LLM Judge 或人工抽检，用于覆盖“检索到了资料但回答仍然误读”的更高层错误。

- **关键代码点（供理解）**：
  - `test_retrieval_qa.py` 第 413-418 行：hit_at_k、mrr、expected_term_hit_rate 自动化评估指标。
  - `test_retrieval_qa.py` 第 387-389 行：`expected_doc_titles` 和 `expected_terms` 定义正确答案。
  - `test_retrieval_qa.py` 第 227-228 行：debug-search 返回 vector_score 和 text_score 用于质量判断。
  - `test_retrieval_qa.py` 第 296 行：大小写不敏感过滤 `"OA"` vs `"oa"`。

---

#### 面试题 84：如何在企业内做权限控制（RBAC）保证数据边界？

- **原有答案**：（已在 permissions 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在企业知识库场景，我会让用户身份、角色、部门等可信上下文随请求进入检索链路，并在召回之前先筛选可访问的文档集合。这样后续向量检索、重排和生成都不会接触越权数据，权限控制不只是最后展示时打码。

  本项目提供可插拔权限检查器，便于将现有 RBAC 或组织权限系统接入。真正上线时，还应采用默认拒绝、审计日志和权限回归测试，确保插件配置错误不会放大为数据泄露。

- **关键代码点（供理解）**：
  - `test_retrieval_qa.py` 第 90-125 行：`_create_identity_user` 创建用户/部门/角色，构建 RBAC 测试基础。
  - `test_retrieval_qa.py` 第 127-128 行：`_user_context` 生成用户身份标识，模拟 JWT 解析。
  - `test_retrieval_qa.py` 第 145 行：`user_context: _user_context("u1001")` 携带用户上下文调用接口。
  - `test_retrieval_qa.py` 第 188-206 行：`test_qa_answer_rejects_bad_internal_token` 验证认证层边界。
  - `test_retrieval_qa.py` 第 66-73 行：`_upload_document_without_owner` 预留 access_level 精细权限测试。

---

#### 面试题 86：如何防御"越权操作"和"越权询问"？

- **原有答案**：（已在 permissions 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  我会从认证、授权和数据隔离三层防御。认证层先确认调用方身份；授权层根据可信用户上下文过滤文档和工具权限；数据隔离层让评测、生产和不同文档集合使用明确范围，避免内部功能绕过边界。

  本项目的测试覆盖了错误 Token 直接拒绝、用户上下文随请求透传、无充分证据时拒答以及评测集合隔离。重点是不依赖模型“自觉保密”，而是让模型从输入上就拿不到越权内容。

- **关键代码点（供理解）**：
  - `test_retrieval_qa.py` 第 145 行：`user_context: _user_context("u1001")` 传递用户身份给工具层校验。
  - `test_retrieval_qa.py` 第 249-250 行：无关问题时返回 "insufficient_evidence"，不依赖 LLM 自觉判断。
  - `test_retrieval_qa.py` 第 188-206 行：错误 Token 直接返回 401，认证层完全阻断。
  - `test_retrieval_qa.py` 第 169-176 行：请求体仅包含业务必需字段，接口最小暴露。
  - `test_retrieval_qa.py` 第 401、413-418 行：doc_uuids 限制 + 断言隔离性，评估系统不能越权访问生产数据。

---

#### 面试题 95：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）

- **原有答案**：（已在 test_qa_agent.py 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在本项目中，我按风险将 Agent 测试分为三层：通过 monkeypatch 替换模型、检索等外部依赖，验证改写、证据判断和状态流转；通过真实 HTTP 请求验证上传、入库、检索、生成和引用的完整链路；通过固定数据集的受控基准保护混合检索排序不在迭代中退化。

  本项目的测试不仅断言接口状态码，还检查回答状态、引用、检索轮次、trace、延迟和异常路径。测试结束后恢复配置和全局状态，保证用例可重复，避免异步、模型和环境变量导致偶发失败。

- **关键代码点（供理解）**：
  - `test_retrieval_qa.py` 第 131-135 行：每个测试显式设置环境变量 + 重置配置缓存，确保可重复性。
  - `test_retrieval_qa.py` 第 90-125 行：`_create_identity_user` 安全的重复调用设计（{200, 409}）。
  - `test_retrieval_qa.py` 第 229-250 行：边界条件测试（无相关文档）。
  - `test_retrieval_qa.py` 第 188-206 行：异常路径测试（认证失败）。
  - `test_retrieval_qa.py` 第 251-328 行：多语言覆盖（中文查询/中文问答）。
  - `test_retrieval_qa.py` 第 152-153 行：vector_score 和 text_score 既可验证功能也可监控性能。
  - `test_retrieval_qa.py` 第 401、413-418 行：数据隔离测试。

---

## 文件名：test_controlled_hybrid_benchmark.py -- 混合检索基准测试

### 一、文件内容概述

`tests/test_controlled_hybrid_benchmark.py` 是混合检索算法效果的受控基准测试。它只有一个测试函数 `test_controlled_hybrid_benchmark_improves_exact_term_cases`（6-22 行），通过调用 `app.retrieval.controlled_benchmark.run_controlled_hybrid_benchmark()` 函数，在 12 个精心设计的受控查询上对比 Dense（纯向量）检索和 Hybrid（向量+关键词混合）检索的效果。测试断言 Hybrid 检索在 hit@1 从 7/12（58.33%）提升到 10/12（83.33%），MRR 从 0.7917 提升到 0.9167，验证了混合检索在需要精确术语匹配的场景下确实比纯语义检索更优。此外还断言了具体查询的 Hybrid 排名（第 4 和第 5 个查询的 hybrid_rank 均为 2）。

### 二、详细技术点解析

**1. 受控基准测试的确定性设计**

`run_controlled_hybrid_benchmark()` 函数使用 12 个固定的受控查询（`query_count == 12`，9 行），每个查询的正确答案是预先知道并硬编码在 benchmark 函数内部的。这种设计使得测试具有完全确定性：每次运行都会得到相同的数值结果，不依赖随机因素。测试断言的精度达到了小数点后四位（如 `0.5833`、`0.7917`，12-13 行），要求 Dense 和 Hybrid 的 hit@1 和 MRR 值与预期精确匹配。这种严格程度使得该测试成为 CI/CD 流水线中的回归检测器：如果任何对检索逻辑的修改导致 hit@1 或 MRR 下降，测试将立即失败。

**2. 混合检索效果的量化和对比**

测试直接对比了 Dense（纯向量检索）和 Hybrid（混合检索）两个策略的效果指标。`hit_at_1_count` 字段（11、16 行）统计 12 个查询中正确答案排在第一位的有几个，`hit_at_1` 字段（12、17 行）是命中率（count/12），`mrr` 字段（13、18 行）是平均倒数排名。Hybrid 相比 Dense 提升了 3 个 hit@1（从 7 到 10），MRR 从 0.7917 提升到 0.9167（提升了 15.8%），用量化数据证明了混合检索在"需要精确匹配专有名词"的场景下的优势。

**3. 细节排名的验证**

`report["details"][4]["hybrid_rank"] == 2` 和 `report["details"][5]["hybrid_rank"] == 2`（20-21 行）验证了具体查询的混合检索排名。这里只要求排名为 2 而非 1，说明 benchmark 设计者承认混合检索在某些查询上虽未达到 top-1 但相比 Dense 检索有明显改善，这种设计比简单地要求所有查询都达到 top-1 更加务实。

### 三、涉及到的面试题

- **第 21 题：什么是混合检索（Hybrid Search）？**
- **第 95 题：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）**

### 四、面试题详解（结合本项目）

---

#### 面试题 21：什么是混合检索（Hybrid Search）？

- **原有答案**：混合检索 = 关键词全文检索 + 向量语义检索，两路检索结果融合打分。关键词检索擅长精准匹配专有名词、编号；向量检索擅长模糊语义理解，二者互补，解决单一检索方案短板。关键词搜专有名词，向量搜模糊语义，两路结果融合打分。混合检索同时启用关键词全文检索与向量语义检索两种检索方式，分别得到两份文档列表后通过算法融合排序。关键词检索可以精准匹配订单号、专业术语等专有名词；向量检索擅长理解模糊语义、同义转述问题，两种检索机制优势互补，大幅提升复杂问题的召回完整性。

- **结合本项目，我会这样回答**：

  我在项目中将混合检索设计为 dense 语义召回与 BM25 关键词召回的互补组合。dense 擅长理解同义表达，BM25 擅长编号、缩写和专有名词；两路候选先独立生成，再经过归一化和加权融合排序。

  项目的受控基准用固定样本显示 hybrid 的 Hit@1 和 MRR 高于纯 dense。这个结果说明融合策略在当前样本上有效，但权重仍需结合真实业务数据持续调优，不能把一次基准结果当成所有场景的结论。

- **关键代码点（供理解）**：
  - `test_controlled_hybrid_benchmark.py` 第 9 行：`query_count == 12`，12 个受控查询确保测试的确定性。
  - `test_controlled_hybrid_benchmark.py` 第 10-14 行：Dense hit@1=7/12(58.33%), MRR=0.7917。
  - `test_controlled_hybrid_benchmark.py` 第 15-19 行：Hybrid hit@1=10/12(83.33%), MRR=0.9167。
  - `test_controlled_hybrid_benchmark.py` 第 20-21 行：具体查询的 hybrid_rank 细节验证。
  - 提升幅度：hit@1 从 58.33% 提升到 83.33%（+25 个百分点），MRR 从 0.7917 提升到 0.9167（+15.8%）。

---

#### 面试题 95：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）

- **原有答案**：（已在 test_qa_agent.py 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在本项目中，我按风险将 Agent 测试分为三层：通过 monkeypatch 替换模型、检索等外部依赖，验证改写、证据判断和状态流转；通过真实 HTTP 请求验证上传、入库、检索、生成和引用的完整链路；通过固定数据集的受控基准保护混合检索排序不在迭代中退化。

  本项目的测试不仅断言接口状态码，还检查回答状态、引用、检索轮次、trace、延迟和异常路径。测试结束后恢复配置和全局状态，保证用例可重复，避免异步、模型和环境变量导致偶发失败。

- **关键代码点（供理解）**：
  - `test_controlled_hybrid_benchmark.py` 第 11-13 行：Dense 的精确断言值 7/0.5833/0.7917。
  - `test_controlled_hybrid_benchmark.py` 第 16-18 行：Hybrid 的精确断言值 10/0.8333/0.9167。
  - `test_controlled_hybrid_benchmark.py` 第 9 行：`query_count == 12` 验证 benchmark 规模。
  - `test_controlled_hybrid_benchmark.py` 第 20-21 行：逐查询细节验证支持故障定位。
  - 严格确定性：不依赖 HTTP/LLM/DB，纯函数调用 + 固定数据集。

---

## 文件名：test_evaluation_execution_mode.py -- 评估执行模式测试

### 一、文件内容概述

`tests/test_evaluation_execution_mode.py` 验证评估系统的后台执行模式行为。只有一个测试函数 `test_background_evaluation_mode_enqueues_without_blocking`（8-33 行），它通过 monkeypatch 设置环境变量 `EVALUATION_EXECUTION_MODE=background`，然后创建评估数据集并提交评估运行，验证当执行模式为 background 时，`BackgroundJobRunner.submit_evaluation_run` 被正确调用（用 `fake_submit` 闭包捕获调用参数），且评估运行的状态立即返回为 `"pending"` 而非阻塞等待结果。测试结束后调用 `reset_settings_cache()` 清理环境。

### 二、详细技术点解析

**1. 环境变量驱动的模式切换**

测试通过 `monkeypatch.setenv("EVALUATION_EXECUTION_MODE", "background")`（9 行）修改环境变量，然后调用 `reset_settings_cache()`（10 行）清除 `get_settings()` 的 LRU 缓存，使得 `EvaluationService` 在后续调用中读取到的 `evaluation_execution_mode` 为 `"background"`。这种方式与 `test_retrieval_qa.py` 中的 `INGESTION_MODE` 切换使用了相同的模式，体现了一致的测试基础设施设计。

**2. 后台任务提交的行为验证**

测试通过 `fake_submit` 闭包函数（20-22 行）捕获 `BackgroundJobRunner.submit_evaluation_run` 的调用参数。`fake_submit` 接收 `run_uuid` 和 `doc_uuids` 两个关键字参数，将它们存入外部字典 `submitted` 中。测试断言 `submitted == {"run_uuid": run.run_uuid, "doc_uuids": []}`（32 行），验证了后台任务提交时传递了正确的 run_uuid 和空的 doc_uuids 列表。更重要的是，测试断言 `run.status == "pending"`（31 行），验证了在后台模式下，`create_run` 方法立即返回 pending 状态，不阻塞等待评估完成。

**3. 异步/同步模式的正确行为区分**

这个测试的隐含价值是验证了系统在两种评价执行模式（sync/background）下的行为差异。在 sync 模式下（默认），`create_run` 会阻塞等待评估完成并返回 `"completed"` 状态；在 background 模式下，`create_run` 立即返回 `"pending"` 状态，实际评估在后台异步执行。测试通过 `assert run.status == "pending"`（31 行）和 `assert submitted["run_uuid"] == run.run_uuid`（32 行）双重断言确认了后台模式的正确行为。

### 三、涉及到的面试题

- **第 92 题：为什么提倡"配置驱动"的 Agent 工作流，而不是写死在代码里？**
- **第 93 题：如何划分开发、测试、生产环境，避免相互污染？**
- **第 95 题：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）**
- **第 96 题：上线后如何监控 Agent 的健康状况？**

### 四、面试题详解（结合本项目）

---

#### 面试题 92：为什么提倡"配置驱动"的 Agent 工作流，而不是写死在代码里？

- **原有答案**：（已在 permissions 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在本项目中，我将稳定的执行框架留在代码里，把连接池规模、权限模式、外部 Provider 和评测执行方式放进配置。服务启动时读取配置决定实际策略，配置变更后通过重置运行时缓存重新初始化，因此同一套业务代码可以适配本地、测试和生产环境。

  这能降低环境差异带来的改动成本，也便于灰度和故障切换。但配置驱动不等于任意配置都能上线，关键参数仍要有类型校验、默认值、健康检查和变更后的重新初始化机制。

- **关键代码点（供理解）**：
  - `test_evaluation_execution_mode.py` 第 9 行：`EVALUATION_EXECUTION_MODE=background` 环境变量驱动模式切换。
  - `test_evaluation_execution_mode.py` 第 31 行：`run.status == "pending"` 验证后台模式的行为。
  - `test_evaluation_execution_mode.py` 第 10、33 行：`reset_settings_cache()` 使配置变更立即生效。
  - `health.py` 第 79 行：`evaluation_execution_mode` 通过健康检查暴露当前模式。

---

#### 面试题 93：如何划分开发、测试、生产环境，避免相互污染？

- **原有答案**：环境隔离的核心是强隔离、分流程。三套独立配置：使用不同的模型版本、工具 endpoint、数据库/索引。日志与监控隔离：日志与监控按环境区分，避免测试数据进入生产分析。权限控制：测试环境可使用脱敏或伪造数据，生产环境有严格访问限制。发布流程：采用 Dev -> Stage（预发）-> Prod 的逐级验证流程。三套独立配置，日志监控隔离，测试数据脱敏，预发验证再上生产。

- **结合本项目，我会这样回答**：

  在本项目中，我从配置、数据、凭据和执行方式四个层面隔离环境：测试通过环境变量与缓存重置切换运行模式，评测使用独立向量集合和文档范围，生产凭据不进入测试配置，开发与测试可使用同步路径保证可重复。这样测试、评测不会直接污染生产索引和数据。

  本项目通过环境变量、配置缓存重置、独立向量集合和身份校验验证这种隔离。每个测试还需要清理自己修改的全局配置和数据，避免一个用例的状态泄露到下一个用例。

- **关键代码点（供理解）**：
  - `test_evaluation_execution_mode.py` 第 9-10 行：环境变量注入 + reset_settings_cache 隔离测试配置。
  - `test_retrieval_qa.py` 第 133-156 行：INGESTION_MODE 的 sync/async 切换，隔离测试与生产的入库模式。
  - `test_retrieval_qa.py` 第 401、413-418 行：doc_uuids 限制 + 断言隔离性。
  - `test_retrieval_qa.py` 第 188-206 行：错误 Token 的 401 验证，认证凭据隔离。
  - `test_retrieval_qa.py` 第 106 行：`{200, 409}` 幂等创建，确保测试可重复执行。

---

#### 面试题 95：如何对 Agent 做自动化测试（单测/集成测试）？（最重要）

- **原有答案**：（已在 test_qa_agent.py 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在本项目中，我按风险将 Agent 测试分为三层：通过 monkeypatch 替换模型、检索等外部依赖，验证改写、证据判断和状态流转；通过真实 HTTP 请求验证上传、入库、检索、生成和引用的完整链路；通过固定数据集的受控基准保护混合检索排序不在迭代中退化。

  本项目的测试不仅断言接口状态码，还检查回答状态、引用、检索轮次、trace、延迟和异常路径。测试结束后恢复配置和全局状态，保证用例可重复，避免异步、模型和环境变量导致偶发失败。

- **关键代码点（供理解）**：
  - `test_evaluation_execution_mode.py` 第 9-10 行：配置注入 + 缓存重置，将系统切换到目标测试模式。
  - `test_evaluation_execution_mode.py` 第 20-27 行：fake 函数捕获后台任务调用参数，将异步行为变为可验证的确定性行为。
  - `test_evaluation_execution_mode.py` 第 31 行：`run.status == "pending"` 验证后台模式不阻塞。
  - `test_evaluation_execution_mode.py` 第 32 行：精确验证提交参数（run_uuid + doc_uuids）。
  - `test_evaluation_execution_mode.py` 第 33 行：`reset_settings_cache()` 清理全局状态，防止测试间污染。

---

#### 面试题 96：上线后如何监控 Agent 的健康状况？

- **原有答案**：（已在 health.py 章节中列出，此处不再重复）

- **结合本项目，我会这样回答**：

  在本项目中，我将 Agent 健康监控拆为可用性与业务链路两部分：健康服务主动探测数据库、缓存、向量库、Embedding 和 LLM，指标层持续记录请求成功率、检索与生成耗时、Provider 失败率和连接池余量。这样健康端点负责快速诊断，指标系统负责发现长期趋势。

  本项目的健康检查会汇总依赖状态、连接池画像和关键运行配置，Prometheus 指标则用于长期趋势和告警。这样既能快速判断“服务是否活着”，也能判断“虽然活着但回答链路是否已经变差”。

- **关键代码点（供理解）**：
  - `health.py` 第 79 行：`evaluation_execution_mode` 通过健康检查端点暴露。
  - `test_evaluation_execution_mode.py` 第 31 行：验证 background 模式下的 pending 状态。
  - `test_evaluation_execution_mode.py` 第 20-22 行：验证后台任务提交的正确性，确保后台任务系统的可用性。
  - `test_evaluation_execution_mode.py` 第 10、33 行：`reset_settings_cache()` 验证配置缓存清除的可靠性。
  - 监控链路：健康检查暴露模式 -> 运维人员确认 -> 如果 background 模式需检查后台队列状态。

---

## 自检清单

- [x] Part 3 列出的每一道面试题，Part 4 都有对应详解
  - test_qa_agent.py: 第 12、14、22、69、95 题 -- 全部详解
  - test_retrieval_qa.py: 第 22、69、84、86、95 题 -- 全部详解
  - test_controlled_hybrid_benchmark.py: 第 21、95 题 -- 全部详解
  - test_evaluation_execution_mode.py: 第 92、93、95、96 题 -- 全部详解
- [x] 每道题的项目回答均以可直接口述的项目理解和工程取舍为主
- [x] 每个技术点都引用了具体代码片段、函数名、参数值、设计模式
- [x] 重点关注面试题全部覆盖：54、56、78、82、84、86、92、96、97、12、14、22、69、95


