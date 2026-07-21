# Day 8：评测系统与后台任务 -- 面试题详解

> 本日涵盖3个源文件的深度技术解析，重点覆盖 **RAG评测体系**、**环境隔离**、**自动化测试**、**并行化**、**服务降级**等核心面试考点。

---

---

## 文件名：app/services/evaluation.py -- RAG 评测服务

### 一、文件内容概述

`EvaluationService` 是本项目 RAG 检索系统的离线评测服务，实现了从数据集管理、评测运行到指标计算的完整闭环。核心功能包括：数据集的 CRUD（创建、列表、详情、删除），评测运行的创建与执行，以及检索效果的多维度评估。该服务的核心设计原则是"环境隔离"——评测运行使用独立的 `evaluation_chunks` 表和临时向量集合 `eval_<run_uuid>`，绝不触碰生产环境的 `document_chunks` 和正式向量集合，保证了评测数据不会污染线上数据。评测指标涵盖 Hit@K（K=1/3/5）、MRR（平均倒数排名）、Expected Term Hit Rate（期望关键词命中率）和平均检索延迟，覆盖了 RAG 检索环节的召回率、排序质量和性能三大维度。执行模式支持同步执行、后台线程执行和 Celery 分布式任务三种方式。

### 二、详细技术点解析

**技术点1：评测环境隔离（Isolated Evaluation）**

`_execute_run()` 方法（第271-303行）展示了完整的隔离评测流程。第一步：`_copy_chunks_to_eval()` 从生产环境的 `DocumentChunk` 表复制 chunk 数据到独立的 `EvaluationChunk` 表，新表的每条记录都标记了 `run_uuid`，写入 `zilliz_collection=f"eval_{run.run_uuid}"` 字段。第二步：`_build_eval_vector_index()` 使用 `eval_<run_uuid>` 作为临时向量集合名称，独立生成向量索引，不依赖也不修改 production 的向量集合。第三步：`_evaluate_query()` 在独立的向量集合上执行检索。异常时自动清理：`self.vector_store.drop_collection(collection_name=self._eval_collection_name(run.run_uuid))`。删除运行时同步清理：`delete(EvaluationChunk).where(EvaluationChunk.run_uuid == run.run_uuid)`。

**技术点2：乐观锁（Optimistic Locking）**

`_execute_run()` 入口处通过 `if run.status != "pending": return` 实现乐观锁（第274-276行）。只有状态为 "pending" 的 Run 才能被启动，启动时立即将状态设为 "running"。这种设计避免了多个 Worker 同时拾取同一个 Run 的问题——两个 Worker 同时读取到 pending 状态，但写入 running 的 SQL UPDATE 操作在数据库层面是原子的，只有一个能成功。虽然本项目实现相对简单（依赖 Python 层面的 check），但在 Celery 分发的场景下，`execute_run_by_uuid()` 入口方法被设计为"幂等且安全重试"（第173行注释）。

**技术点3：多维检索评测指标体系**

本项目的评测体系覆盖了 RAG 检索的三个核心维度。Hit@K 指标：`_hit_at_k()` 方法（第443-448行）判断 expected_titles 中是否有任意一个出现在 hit_titles 的前 K 个结果中，返回布尔值。最终通过 `RunSummary` 计算 `hit_at_1_rate`、`hit_at_3_rate`、`hit_at_5_rate` 的比率（第236-238行），即"在所有查询中，前K个结果命中期望文档的比例"。MRR 指标：`_compute_mrr()` 方法（第450-458行）计算第一个期望文档在结果列表中的排名的倒数（1/rank），未命中返回 0。Term Hit Rate：`_compute_term_hit_rate()` 方法（第461-471行）评估期望关键词在检索结果 snippet 中的出现比例，独立于文档级别的 Hit@K 指标。延迟指标：每次查询独立计时 `elapsed_ms`，最终在 Summary 中计算 `mean_latency_ms`。这五个指标从召回能力、排序质量、内容匹配和性能四个维度全面评估检索系统。

**技术点4：Dataset 数据集的 CRUD 设计**

`create_dataset()` 方法（第58-86行）展示了规范的数据集管理。数据集和查询是 1:N 关系：`EvaluationDataset` 包含名称和描述，`EvaluationQuery` 包含 `query_text`、`expected_doc_titles`（期望召回的文档标题列表）、`expected_terms`（期望出现的检索词列表）和 `notes`（备注）。创建时先 flush 获取 dataset_uuid，再批量创建查询，最后 commit。查询数量通过 `_count_queries()` 的 `select(func.count(...))` 聚合查询获取，避免加载全部查询数据。

**技术点5：三种执行模式的配置驱动**

`create_run()` 方法（第131-170行）通过 `settings.evaluation_execution_mode` 配置项实现三种执行模式的运行时切换。`"sync"` 模式：在当前线程同步执行 `_execute_run()`，适用于小规模测试和调试。`"background"` 模式：通过 `BackgroundJobRunner.submit_evaluation_run()` 提交到线程池异步执行，适用于中等规模评测。其他模式（生产环境）：通过 `enqueue_evaluation_run()` 将任务推送到 Celery 消息队列，由独立 Worker 进程执行，适用于大规模评测和分布式部署。每次执行都通过 `doc_uuids` 参数限定评测范围。

**技术点6：评测检索的完整管线**

`_search_eval_hits()` 方法（第511-576行）展示了评测场景下从查询到 Citation 的完整管线。第一步：文本清洗 `_rewrite_query()`。第二步：策略校验——当前评测仅支持 dense 模式（第520-525行的 warning 日志）。第三步：向量生成和检索——调用 `self.embedding_provider.embed_texts()` 生成查询向量，`self.vector_store.search()` 在独立向量集合中检索。第四步：数据填充——通过 `_load_eval_chunks_by_uuid()` 和 `_load_documents_by_uuid()` 加载 chunk 和 document 完整信息，构建 `Citation` 对象并返回。整个管线与生产环境的 `RetrievalService` 逻辑类似但完全隔离。

**技术点7：Run 生命周期管理**

评测运行的状态机包含四个状态：`pending`（已创建等待执行）、`running`（执行中）、`completed`（成功完成）、`failed`（执行失败）。状态转换在 `_execute_run()` 中集中管理：pending -> running（第278行），running -> completed（第296行），running -> failed（第299行异常捕获）。删除 Run 时（`delete_run()` 第259-267行），系统按顺序清理：先删除向量集合，再删除数据库中的 EvaluationChunk 记录，最后删除 Run 本身。这种级联清理策略确保无孤儿数据和资源泄漏。

### 三、涉及到的面试题

- 第41题：微调后的模型如何评测效果？
- 第93题：如何划分开发、测试、生产环境，避免相互污染？
- 第95题：如何对 Agent 做自动化测试（单测/集成测试）？
- 第37题：微调数据泄露是什么？如何避免？
- 第76题：工具调用并行化能带来什么收益？如何安全实现？
- 速记第10题：如何评估 RAG 系统的效果？（RAGAS框架）

### 四、面试题详解（结合本项目）

---

#### 面试题41：微调后的模型如何评测效果？

- **原有答案**：微调后模型效果评测分为三层方案：第一层自动指标评测，使用 BLEU、ROUGE 文本相似度指标，或者 LLM Judge 大模型自动评估回答质量；第二层人工评测，标注人员针对指令跟随能力、事实准确性、话术流畅度人工打分；第三层线下业务仿真测试，导入批量线上真实业务问题，统计模型任务完成率，全方位验证微调效果。

- **结合本项目，我会这样回答**：

  本项目评测的重点是 RAG 检索系统，而不是微调模型本身。因此重点关注检索有没有把正确文档找回来、排序靠不靠前、关键词覆盖够不够，以及延迟是否能接受。

  不同系统应使用不同的指标。这个项目里，Hit@1、MRR、Term Hit Rate 和平均延迟一起构成较完整的检索评估视角，可概括为“质量指标 + 性能指标”的组合评测。

- **关键代码点（供理解）**：
  - **评测服务主体**：`app/services/evaluation.py:48` 集中编排一次评测 Run 的创建、执行、统计和清理，体现评测不是零散脚本而是服务化能力。
  - **Hit@K / MRR / Term Hit Rate**：`app/services/evaluation.py:548`、`:570`、`:590` 分别对命中、排名和关键词覆盖做计算，体现项目从多个角度衡量检索质量。
  - **结果聚合**：`app/services/evaluation.py:292-297` 把单条 case 的结果聚合为 `RunSummary`；其结构在 `app/schemas/evaluation.py:95` 固定，体现对外返回有稳定的数据契约。
  - **延迟指标**：`app/services/evaluation.py:532` 将单查询耗时累计并写入 `avg_latency_ms`，体现效果评估同时考虑性能成本。

---

#### 面试题93：如何划分开发、测试、生产环境，避免相互污染？

- **原有答案**：环境隔离的核心是强隔离：三套独立的配置和基础设施，日志监控分环境，测试环境用脱敏数据，同时采用 Dev-Setup-Staging-Production 的逐级发布流程，确保测试和生产环境互不干扰。

- **结合本项目，我会这样回答**：

  这个项目虽然不是典型的 Dev/Test/Prod 三套业务系统，但它在评测链路上做出了非常标准的环境隔离思路。我的理解是，评测最怕的不是结果差，而是把生产数据、生产索引和评测运行混在一起，最后谁都不敢确认数据有没有被污染。

  本项目采用“评测专用数据副本 + 独立向量集合 + 自动清理”。这样即使评测过程出错，也不会影响生产环境，体现了对环境边界和运维风险的考虑。

- **关键代码点（供理解）**：
  - **复制评测数据**：`app/services/evaluation.py:380` 将当前 chunk 复制到评测专用数据集，体现评测不会直接改写线上原始数据。
  - **独立向量索引**：`:422` 为该 Run 单独构建向量索引，`:644` 用 `eval_<run_uuid>` 生成集合名，体现数据和索引都按实验隔离。
  - **清理机制**：`app/services/evaluation.py:315` 删除 Run 时同步清理关联资源，体现隔离资源具有生命周期管理而非无限堆积。
  - **执行模式分环境**：`app/services/evaluation.py:184-187` 按配置选择同步、进程内后台或 Celery，体现开发和生产可采用不同的任务拓扑。

---

#### 面试题95：如何对 Agent 做自动化测试（单测/集成测试）？

- **原有答案**：Agent 的自动化测试需要覆盖不同层级：工具、RAG、权限做单元测试，LLM 行为用固定样例 + LLM Judge 评估，集成测试验证完整流程，同时用线上请求回放对比新版本行为。

- **结合本项目，我会这样回答**：

  本项目已经具备一套类似“离线回放平台”的 Agent 自动化测试骨架。它不是只测一个函数，而是把数据集、评测参数、检索执行和结果汇总串成一条可重复运行的完整链路。

  这类设计特别适合做检索策略回归测试。比如你换了分块策略、换了 embedding、调了 hybrid 参数，只要重新跑同一份数据集，就能直接比较哪套配置更好。对 RAG 系统来说，这比只做单点单元测试更贴近真实效果。

- **关键代码点（供理解）**：
  - **Run 创建入口**：`app/services/evaluation.py:160` 创建一条独立的评测 Run，作为后续状态、配置和结果的统一载体。
  - **完整执行链路**：`:334` 从复制数据、建索引、逐条检索到统计指标串成完整执行流程，体现评测有明确的任务边界。
  - **评测参数持久化**：`:174-177` 将 `chunking_strategy`、`retrieval_strategy` 及参数保存到 Run，体现之后查看结果时可追溯“这次到底怎么跑的”。
  - **结果查询与汇总**：`:292-297` 汇总 Run 的各项统计信息，体现调用方不需要自行扫描明细来获取核心结论。

---

#### 面试题37：微调数据泄露是什么？如何避免？

- **原有答案**：（同前，省略）

- **结合本项目，我会这样回答**：

  在本项目里，评测数据和生产数据不能共用同一套执行面。因为一旦评测直接跑生产索引、复用生产表，测试过程本身就可能反过来影响线上系统，甚至让评测结论失真。

  防泄露不只是防训练集和测试集重合，也包括防评测过程污染生产环境。物理隔离、逻辑隔离和生命周期清理都很重要，这体现的是工程边界意识，而不只是概念理解。

- **关键代码点（供理解）**：
  - **评测数据副本表**：`app/services/evaluation.py:380` 会将 chunk 复制到评测专用表，体现评测过程拥有独立的数据快照。
  - **独立集合命名**：`:644` 以 `eval_<run_uuid>` 命名向量集合，体现并行 Run 不会写入同一个索引。
  - **评测检索入口**：`:657` 只对当前 Run 的评测集合搜索，体现评测命中不会受生产集合变化干扰。
  - **运行后清理**：`:315` 在删除 Run 时清理副本和索引，体现隔离环境不会持续占用存储或污染后续实验。

---

#### 面试题76：工具调用并行化能带来什么收益？如何安全实现？

- **原有答案**：工具调用并行化的核心收益是降低多工具调用的总等待时间，尤其适合慢 IO 工具组合场景；实现时需由后端解析 LLM 的调用计划，识别可并行任务，同时做好幂等性和资源争用设计，避免并发冲突。

- **结合本项目，我会这样回答**：

  从评测服务层面看，并行化有两个直接收益：第一，长任务不阻塞接口；第二，可以根据环境选择不同执行方式。用户创建评测 Run 后不需要等待完整结果，系统可将真正耗时的工作放到后台执行。

  但我也会补一句，并行化不是只追求快，还要保证任务不会相互踩数据。这个项目里评测运行有自己的状态流转、独立数据副本和独立向量集合，所以并发跑多个 Run 的时候边界会更清楚。

- **关键代码点（供理解）**：
  - **执行模式切换**：`app/services/evaluation.py:184-187` 根据配置将同一 Run 分发为 sync、background 或 Celery，体现调用接口不必随部署方式改变。
  - **Run 状态驱动**：`:334` 围绕 Run 的生命周期推进实际工作，体现后台化后仍以数据库状态作为可查询的进度载体。
  - **批量向量构建**：`:422` 在评测开始时构建本次 Run 的向量索引，体现耗时计算从 Web 请求链路中移出。
  - **环境隔离支撑并行**：`:644` 为每个 Run 生成不同集合名，体现多个任务并行时索引写入不会互相覆盖。

---

#### 速记第10题：如何评估 RAG 系统的效果？（RAGAS框架）

- **原有答案**：RAGAS 框架包含三个核心指标：Faithfulness 忠实度检验答案是否由检索内容生成；Answer Relevance 答案相关性检验是否精准回应用户问题；Context Precision 上下文准确性检验检索召回是否包含答题关键信息。

- **结合本项目，我会这样回答**：

  本项目采用工程化的双层评测：检索层通过 Hit@K、MRR、词项命中率验证召回和排序；答案层在同一份隔离检索结果上生成候选回答，再调用 LLM Judge 对忠实度、答案相关性和引用覆盖度打分。评测运行会持久化候选答案、分数、结论和原因，因此低分结果可以直接定位到检索、生成或引用证据环节。

  这个设计与 RAGAS 的指标思想一致，但实现上使用项目自身的 OpenAI-compatible 裁判契约，避免把评测能力绑定到某个外部框架。裁判默认可配置关闭；未配置模型或模型调用异常时，Run 仍完成并明确记录状态，保证离线检索基准的稳定性。

- **关键代码点（供理解）**：
  - **检索评估指标**：`app/services/evaluation.py:548`、`:570`、`:590` 分别计算命中、排名和词项覆盖，因此度量对象是检索结果本身。
  - **汇总视图**：`app/schemas/evaluation.py:95` 的 `RunSummary` 固定了对外展示的核心统计字段，体现评测结果可被界面或接口稳定消费。
  - **运行配置可复现**：`app/schemas/evaluation.py:50` 对 Run 参数做结构化约束，并在服务层保存，体现同一实验可按原配置重跑。
  - **答案层评测**：`app/services/evaluation.py:604` 执行候选答案生成与裁判调用，`app/models/evaluation_result.py:37` 持久化三类分数、结论和延迟，形成可回溯的端到端质量记录。

---

---

## 文件名：app/services/background_jobs.py -- 线程池后台任务执行器

### 一、文件内容概述

`BackgroundJobRunner` 是一个基于线程池的单例后台任务管理器，使用 Python 标准库的 `concurrent.futures.ThreadPoolExecutor` 实现。它提供了三个核心方法：`submit_ingest_job` 用于提交文档异步摄取任务，`submit` 用于提交任意可调用对象，`submit_evaluation_run` 用于提交评测执行任务。线程池通过类级别的懒汉单例模式管理（`_executor` 和 `_lock` 组合），默认最大 4 个工作线程，线程名称前缀为 `oa-rag-bg` 便于日志识别。每个任务执行时独立获取数据库 Session（通过 `get_session_factory()()`），执行完毕后关闭 Session，确保线程安全。该类也提供了 `shutdown()` 方法用于优雅关闭线程池。

### 二、详细技术点解析

**技术点1：线程安全的单例线程池**

`BackgroundJobRunner` 使用类变量 `_executor` 和 `_lock` 实现线程安全的单例模式（第17-18行）。`_get_executor()` 方法（第42-47行）在 `with cls._lock:` 上下文中检查 `_executor` 是否为 None，若否则直接返回，若是则创建新的 `ThreadPoolExecutor`。这种"懒加载 + 双重检查锁定"的模式避免了多次初始化造成的资源浪费，同时 `Lock` 保证了多线程环境下的初始化安全。`max_workers=4` 是合理的默认值，既防止过多线程竞争数据库连接池，又提供了足够的并发能力。

**技术点2：独立的数据库 Session 管理**

每个后台任务通过 `get_session_factory()()` 获取全新的数据库 Session（`_run_ingest_job` 第51行，`_run_evaluation_run` 第64行），在 `finally` 块中确保关闭（第58行）。这种设计的关键在于：后台线程运行在不同的线程上下文中，不能复用 API 请求线程的 Session 对象，否则会导致线程间数据污染和连接竞争。独立 Session + finally 关闭的模式保证了线程安全的数据库访问。

**技术点3：通用的任务提交接口**

`submit()` 方法（第26-28行）接收任意可调用对象和参数，为系统提供了最大的灵活性。但实际使用中，项目更推荐专用方法（`submit_ingest_job`、`submit_evaluation_run`），因为专用方法封装了错误处理和日志记录逻辑，调用方无需关心线程管理的细节。

**技术点4：资源生命周期管理**

`shutdown()` 方法（第36-40行）提供优雅关闭能力。`wait=True` 参数确保所有在队列中的任务执行完毕后才关闭，`cancel_futures=False` 避免中断正在执行的任务。应用关闭时调用此方法可防止线程泄漏。

### 三、涉及到的面试题

- 第76题：工具调用并行化能带来什么收益？如何安全实现？
- 第97题：当模型服务或关键工具宕机时，Agent 应该如何"降级"？

### 四、面试题详解（结合本项目）

---

#### 面试题76：工具调用并行化能带来什么收益？如何安全实现？

- **原有答案**：工具调用并行化的核心收益是降低多工具调用的总等待时间，尤其适合慢 IO 工具组合场景；实现时需由后端解析 LLM 的调用计划，识别可并行任务，同时做好幂等性和资源争用设计，避免并发冲突。

- **结合本项目，我会这样回答**：

  从后台任务视角看，并行化解决的是“接口不被长任务拖死”。评测属于 I/O 较重的流程，同步执行会明显影响前端体验。本项目用线程池将任务交给后台执行，接口可以先返回、后续再查状态，这是典型的工程做法。

  安全实现上，我会重点讲独立 Session 和并发上限。因为线程池不是开得越大越好，数据库连接、向量服务和 CPU 都会被拖住。这个项目把线程数限制住，同时每个任务单独拿数据库会话，这就是比较稳的做法。

- **关键代码点（供理解）**：
  - **线程池实现**：`app/services/background_jobs.py:21`、`:76`、`:84`。
  - **固定并发上限**：`ThreadPoolExecutor(max_workers=4)` 在 `app/services/background_jobs.py:84`。
  - **独立数据库会话**：每个后台任务通过 `get_session_factory()()` 获取 Session。
  - **优雅关闭**：线程池关闭逻辑在 `app/services/background_jobs.py` 的 `shutdown()` 中。

---

#### 面试题97：当模型服务或关键工具宕机时，Agent 应该如何"降级"？

- **原有答案**：Agent 降级的核心是保障基础可用：模型/工具宕机时，切换到备用模型，禁用复杂功能，优先保留基础问答；同时向用户友好提示当前状态，工程上用熔断器避免反复重试导致系统雪崩。

- **结合本项目，我会这样回答**：

  如果关键服务出问题，这个项目的后台任务层给我的感觉是“先保活，再谈性能”。也就是说，Celery 不可用可以退到线程池，线程池不合适还可以同步执行；单个任务失败也不会把整个执行器拖挂。

  这属于多级降级：先降执行模式，再做异常隔离，最后做资源清理。即使高阶能力不可用，系统仍有基础运行路径，不至于整条链路全挂。

- **关键代码点（供理解）**：
  - **异常隔离**：后台任务的 `try/except` 在 `app/services/background_jobs.py` 任务执行函数里。
  - **Session 回收**：失败后仍会走 `finally` 关闭连接。
  - **执行模式回退**：外层由 `app/services/evaluation.py:184-187` 控制 sync/background/Celery 切换。
  - **设计关键词**：Graceful Degradation、Failure Isolation、Resource Cleanup。

---

---

## 文件名：app/tasks/evaluation_tasks.py -- Celery 分布式评测任务

### 一、文件内容概述

本文件封装了基于 Celery 的分布式评测任务执行器。通过 `_create_celery_app()` 工厂函数创建 Celery 应用实例，配置 broker（消息队列）和 backend（结果存储）。核心任务 `execute_evaluation_run_task` 被注册为 Celery 任务，支持失败自动重试（最多2次，指数退避），任务内部独立获取数据库 Session，委托 `EvaluationService.execute_run_by_uuid()` 执行评测逻辑。`enqueue_evaluation_run()` 是便利函数，将评测运行推送到 Celery 队列并返回任务 ID。整个设计实现了评测执行与 Web 服务的彻底解耦——API 接口只负责创建 Run 并投递任务，实际执行由独立的 Worker 进程在后台完成。

### 二、详细技术点解析

**技术点1：Celery 应用工厂模式**

`_create_celery_app()` 函数（第9-15行）封装了 Celery 实例的创建逻辑。从 `settings.celery_broker_url` 读取消息队列地址，从 `settings.celery_result_backend` 读取结果存储地址（回退到 broker URL）。这种工厂模式使得测试环境可以注入不同的配置，也便于更换消息队列（如从 Redis 切换到 RabbitMQ）。

**技术点2：自动重试机制**

`execute_evaluation_run_task` 装饰器（第21-27行）配置了关键的弹性参数：`bind=True` 允许 task 访问自身的 `self` 对象（可用于 `self.retry()` 手动触发重试）；`autoretry_for=(Exception,)` 对所有异常自动重试；`retry_backoff=True` 启用指数退避，避免重试风暴；`retry_kwargs={"max_retries": 2}` 限制最多重试 2 次。这套配置确保了偶发性故障（如数据库短暂不可用、网络抖动）能够自动恢复，同时又不会无限重试消耗资源。

**技术点3：独立 Session 管理**

任务执行时通过 `get_session_factory()()` 获取独立数据库 Session（第31行），在 `finally` 块中确保关闭（第35行）。这与 `BackgroundJobRunner` 的模式一致，都是"线程/进程级别独立 Session"的设计——Celery Worker 运行在独立进程中，不能复用 Web 进程的数据库连接。

**技术点4：任务追踪**

`enqueue_evaluation_run()` 返回 `str(task.id)`，调用方可以保存此 ID 用于追踪任务状态（PENDING、STARTED、SUCCESS、FAILURE）。配合 Celery 的 result backend，可以实现异步任务的进度查询和结果获取。

### 三、涉及到的面试题

- 第76题：工具调用并行化能带来什么收益？如何安全实现？
- 第93题：如何划分开发、测试、生产环境，避免相互污染？
- 第97题：当模型服务或关键工具宕机时，Agent 应该如何"降级"？

### 四、面试题详解（结合本项目）

---

#### 面试题76：工具调用并行化能带来什么收益？如何安全实现？

- **原有答案**：工具调用并行化的核心收益是降低多工具调用的总等待时间，尤其适合慢 IO 工具组合场景；实现时需由后端解析 LLM 的调用计划，识别可并行任务，同时做好幂等性和资源争用设计，避免并发冲突。

- **结合本项目，我会这样回答**：

  从 Celery 角度看，这不只是“后台跑一下”，而是将长任务从 Web 进程彻底解耦，交给分布式 Worker 执行。它的最大价值是任务量上来后可以横向扩容，不必把所有压力压在接口进程上。

  Celery 还天然适合做重试和任务追踪。线程池更适合轻量后台执行，Celery 更适合生产级异步任务体系，两者解决的问题层级不同。

- **关键代码点（供理解）**：
  - **Celery 应用创建**：`app/tasks/evaluation_tasks.py:18`、`:21`。
  - **任务定义**：`execute_evaluation_run_task` 在 `app/tasks/evaluation_tasks.py:39`。
  - **异步投递**：`enqueue_evaluation_run()` 在 `app/tasks/evaluation_tasks.py:54`，`delay()` 在 `:56`。
  - **与评测服务衔接**：Web 层通过 `app/services/evaluation.py:184-187` 选择是否走 Celery。

---

#### 面试题93：如何划分开发、测试、生产环境，避免相互污染？

- **原有答案**：
避免环境污染的核心是强隔离：三套独立的配置和基础设施，日志监控分环境，测试环境用脱敏数据，同时采用Dev→预发→生产的逐级发布流程，确保测试和生产环境互不干扰，避免测试数据或未验证版本影响线上。

- **结合本项目，我会这样回答**：

  Celery 的环境隔离主要依靠配置和部署拓扑。不同环境可连接不同的 broker、result backend 和 worker 进程，消息与执行资源不会串；开发环境甚至可以不启 Celery，直接用同步或线程池模式。

  这种设计很实用，因为它允许每个环境用最合适的复杂度运行系统。开发环境追求简单，测试环境追求接近线上，生产环境追求吞吐和可扩展性，没必要一套架构硬套到底。

- **关键代码点（供理解）**：
  - **Broker / Backend 配置入口**：`app/tasks/evaluation_tasks.py:18-21`。
  - **执行模式选择**：`app/services/evaluation.py:184-187`。
  - **任务命名空间**：`app/tasks/evaluation_tasks.py:34` 的任务名有明确前缀。
  - **架构分层**：Web 进程与 Worker 进程物理分离，属于典型的 Environment-Specific Topology。

---

#### 面试题97：当模型服务或关键工具宕机时，Agent 应该如何"降级"？

- **原有答案**：Agent 降级的核心是保障基础可用：模型/工具宕机时，切换到备用模型，禁用复杂功能，优先保留基础问答；同时向用户友好提示当前状态，工程上用熔断器避免反复重试导致系统雪崩。

- **结合本项目，我会这样回答**：

  当关键中间件或外部服务宕机时，Celery 层的降级是“异步能力失效时，退回更朴素但可用的执行方式”。消息队列挂了不代表评测系统完全不能运行，而是可以改成线程池或同步执行，只是吞吐会下降。

  我会特别强调这里的重试和隔离。Celery Worker 失败不会影响 Web 接口继续收请求，偶发故障还可以通过自动重试恢复。这体现的是系统弹性，而不是单点完美运行。

- **关键代码点（供理解）**：
  - **自动重试**：`autoretry_for`、`retry_backoff`、`max_retries` 在 `app/tasks/evaluation_tasks.py:34-37`。
  - **独立 Session**：Worker 内部会单独创建数据库会话，见 `app/tasks/evaluation_tasks.py:46`。
  - **异步任务投递**：`app/tasks/evaluation_tasks.py:54-56`。
  - **执行模式降级入口**：`app/services/evaluation.py:184-187`。
