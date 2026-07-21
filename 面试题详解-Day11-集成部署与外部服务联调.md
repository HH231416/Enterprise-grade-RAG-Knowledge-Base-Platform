# 面试题详解 — 集成部署与外部服务联调

> 基于项目源码新增资产（docker-compose.integration.yml / Dockerfile / .env.integration.example / external_stack_smoke.py / external_integration_runbook.md）的深度分析


## 一、新增资产总览

本项目从"本地零依赖开发"到"真实外部服务联调"构建了五份资产形成的完整闭环：`docker-compose.integration.yml`负责编排PostgreSQL、Redis、etcd、MinIO、Milvus、OpenSearch、API、Celery Worker共8个服务的全链路启动，每个服务配备了独立的健康检查机制和严格的`condition: service_healthy`依赖链；`Dockerfile`基于python:3.11-slim构建统一镜像，通过分层缓存优化构建速度，CMD既支持API模式也支持Worker模式；`.env.integration.example`提供环境变量模板并通过`.gitignore`保护真实密钥不提交，其中EMBEDDING_API_KEY和LLM_API_KEY使用`${VAR:?}`强制必填语法实现了Fail-Fast策略；`scripts/external_stack_smoke.py`通过httpx调用真实HTTP API，依次验证健康检查、文档上传摄入、Hybrid混合检索、Agent回答、Celery评测的完整业务流程，并将结果持久化为带时间戳的JSON证据文件；`docs/external_integration_runbook.md`从前置条件、启动步骤、验证链路、通过标准、已知边界五个维度提供了让新人15分钟跑通全栈的操作手册。五份资产的关系是：docker-compose编排基础设施、Dockerfile构建应用镜像、.env提供配置注入、smoke脚本验证集成正确性、runbook指导人工操作，共同构成了从"代码构建"到"服务部署"到"冒烟验证"到"文档沉淀"的工程化完整闭环。


## 二、docker-compose.integration.yml — 全链路服务编排

### 文件内容概述

该Compose文件定义了一个名为`oa-rag-integration`的单节点集成验证栈，共编排8个服务：postgres（16-alpine，pg_isready健康检查）、redis（7-alpine，AOF持久化，redis-cli ping健康检查）、etcd（v3.5.18，自动压缩配置，etcdctl endpoint health健康检查）、minio（RELEASE.2024-12-18，curl健康检查）、milvus（v2.5.12 standalone模式，依赖etcd和minio的service_healthy，/healthz健康检查）、opensearch（2.17.1，单节点模式且禁用安全插件，_cluster/health健康检查）、api（从当前目录build，先执行alembic upgrade head再启动uvicorn，依赖postgres/redis/milvus/opensearch四个服务的service_healthy）、celery-worker（同样从当前目录build，依赖api.service_healthy）。两个应用服务（api和celery-worker）通过YAML锚点`&app_environment`共享完全相同的环境变量配置，杜绝了API和Worker之间可能出现的环境不一致问题。7个命名卷（postgres_data/redis_data/etcd_data/minio_data/milvus_data/opensearch_data/app_data）确保`docker compose down`后数据不丢失。关键的生产级决策包括：`ALLOW_PROVIDER_FALLBACKS: "false"`和`EMBEDDING_ALLOW_HASH_FALLBACK: "false"`强制禁止任何降级行为，确保联调阶段所有配置问题被立即暴露而非被静默掩盖。

### 详细技术点解析

1. **服务依赖与健康检查链**：整个栈的启动顺序由`depends_on`的`condition: service_healthy`严格保证。底层基础服务postgres和redis率先启动并通过各自的健康检查（pg_isready / redis-cli ping）；etcd和minio作为Milvus的元数据协调层和对象存储层紧随其后启动并通过健康检查；milvus在etcd和minio都healthy之后启动，其healthcheck探针通过curl访问`localhost:9091/healthz`验证；opensearch独立启动；api服务在所有四个数据服务（postgres/redis/milvus/opensearch）都healthy之后才启动，其command先执行alembic upgrade head完成数据库迁移再启动uvicorn；celery-worker最后启动，依赖api.service_healthy。这条依赖链使用`condition: service_healthy`而非简单的`depends_on`（后者仅等待容器启动而不等待服务就绪），避免了启动时序竞态问题。

2. **YAML锚点环境复用**：`&app_environment`锚点定义在api服务下，包含APP_ENV、DATABASE_URL、REDIS_URL、MILVUS_URI、OPENSEARCH_URL、EMBEDDING_API_BASE、LLM_API_BASE等所有环境变量。celery-worker通过`<<: *app_environment`（YAML合并键语法）引用完全相同的环境变量集合。这种设计确保了api和celery-worker在连接数据库地址、Redis Broker URL、Milvus地址、向量维度等所有配置项上完全一致，彻底避免了"API能正常写入向量但Celery Worker却连不上Milvus"这类因环境不一致导致的诡异bug。

3. **生产级配置决策**：两个关键的禁止降级开关——`ALLOW_PROVIDER_FALLBACKS: "false"`禁止了当Embedding或LLM提供商调用失败时自动降级到本地哈希或默认回复的行为；`EMBEDDING_ALLOW_HASH_FALLBACK: "false"`禁止了向量嵌入服务不可用时使用哈希值替代真实向量的行为。在本地开发环境（使用SQLite+本地哈希向量）中这些降级是允许的，方便开发者零依赖快速迭代；但在联调环境中，这些降级被明确关闭，采用Fail-Fast策略——任何配置错误或服务不可用都会被立即暴露为启动失败或API报错，而不是被静默降级掩盖，这正是联调环境的核心价值：在进入生产之前发现并修复所有配置问题。

4. **数据持久化**：7个命名卷的设计使得即使执行`docker compose down`（不带-v参数），所有数据仍然保留在Docker管理的卷中，下次`docker compose up`可以无缝恢复。这7个卷分别是：postgres_data（存储文档元数据、用户信息、评测结果等关系型数据）、redis_data（存储AOF持久化的缓存和Celery消息队列）、etcd_data（存储Milvus的元数据协调信息）、minio_data（存储Milvus的向量原始文件）、milvus_data（Milvus的持久化向量索引）、opensearch_data（OpenSearch的稀疏向量索引和全文检索数据）、app_data（API和Worker共享的应用数据，包括上传的原始文件和处理后的文件）。

5. **Milvus Standalone架构**：Milvus v2.5.12以standalone模式运行，但其内部仍然依赖两个外部组件——etcd用于元数据存储和协调（如collection schema、partition信息、索引状态），MinIO用于存储实际的向量数据文件和索引文件。docker-compose中milvus通过`ETCD_ENDPOINTS: etcd:2379`和`MINIO_ADDRESS: minio:9000`连接这两个依赖，展示了向量数据库并非一个单体黑盒，而是由元数据层、对象存储层、计算层组成的分布式系统。这种架构理解对面试中讨论Milvus的生产部署和高可用方案至关重要。

### 涉及到的面试题

第57题（容器化部署优势）、第52题（部署架构）、第59题（冷启动优化）、第60题（多模型混部）、第93题（环境隔离）、第96题（健康监控）、第97题（服务降级配置）

### 面试题详解

#### 面试题57：容器化Docker部署推理服务优势

- **原有答案**：1. 环境统一：镜像打包模型、推理框架、CUDA驱动，消除本地环境不一致问题；2. 快速部署：镜像一键拉取启动，支持批量扩缩容；3. 资源隔离：容器限制GPU、CPU、内存资源，避免进程资源抢占；4. 适配K8s集群：容器原生支持Kubernetes编排，自动扩缩容、负载均衡。

- **结合本项目，我会这样回答**：

  在本项目中，我使用 Docker Compose 将 API、Worker、PostgreSQL、Milvus 和 OpenSearch 等服务编排为同一套联调环境。镜像版本、服务依赖和环境变量都写入编排文件，API 与 Worker 复用同一份环境配置，团队成员可以通过同一命令启动一致的服务栈。

  容器化不是只把应用塞进镜像。联调时还要明确密钥注入、数据卷、健康检查、启动顺序和日志出口；本项目通过固定镜像版本和共享环境变量模板，把这些部署约束显式化。

- **关键代码点（供理解）**：
  - `image: postgres:16-alpine` — 所有基础服务都锁定了精确的镜像版本，杜绝版本漂移
  - `&app_environment` 和 `<<: *app_environment` — YAML锚点确保API和Worker的配置100%一致
  - `docker compose --env-file .env.integration -f docker-compose.integration.yml up -d --build` — 单命令启动整个8服务全栈
  - `build: context: .` — 应用代码通过Dockerfile统一构建，避免了手工部署的差异
  - `ports: "18080:18080"` — 服务端口通过环境变量`${APP_PORT:-18080}`可配置
  - `restart: unless-stopped` — celery-worker配置了自动重启策略，对应生产环境的自愈能力

#### 面试题52：部署架构（单实例/多实例分布式）

- **原有答案**：1. 单实例部署：单台GPU机器运行一套推理服务，开发简单，适合低并发测试环境；上限受单卡显存、算力约束。2. 分布式多实例部署：多台GPU服务器组成集群，负载均衡分发请求，横向扩容支撑超高并发，适合生产线上大规模业务。

- **结合本项目，我会这样回答**：

  本项目的集成环境采用单实例服务编排，目标是快速验证 API、任务、向量库和搜索引擎之间的完整链路，而不是模拟高并发生产集群。单节点配置降低了资源成本，也方便定位外部服务联调问题。

  本项目的生产部署使用 Kubernetes 清单把 API 部署为三副本，并配置滚动更新、就绪和存活探针、PodDisruptionBudget、HPA 及跨可用区分散；评测任务由两个 Celery Worker 通过 Redis 消费。上传文件放在 RWX 共享存储，避免请求切换到不同 Pod 后文件不可见。数据库、Redis、Milvus 和 OpenSearch 作为独立高可用或受管服务接入，应用 Pod 不保存这些关键状态。

- **关键代码点（供理解）**：
  - `deploy/k8s/api-deployment.yaml:9`：`replicas: 3` 和 `maxUnavailable: 0` 体现 API 可在发布时保持服务副本。
  - `deploy/k8s/autoscaling.yaml:1`、`availability-policy.yaml:1`：HPA 与 PDB 分别处理负载扩缩和节点维护期间的最低可用副本。
  - `deploy/k8s/worker-deployment.yaml:34`：评测 Worker 使用 Celery 消费 Redis 队列，任务执行与 API 请求解耦。
  - `deploy/k8s/storage-claim.example.yaml:8`：RWX 存储将上传和处理中间文件共享给多副本，解决本地磁盘不一致问题。

#### 面试题93：环境隔离

- **原有答案**：1. 三套独立配置：使用不同的模型版本、工具endpoint、数据库/索引；2. 日志与监控隔离：按环境区分，避免测试数据进入生产分析；3. 权限控制：测试环境可使用脱敏或伪造数据，生产环境有严格访问限制；4. 发布流程：采用Dev -> Stage（预发）-> Prod的逐级验证流程。

- **结合本项目，我会这样回答**：

  在本项目中，我通过独立的 `.env.integration`、服务地址和数据卷隔离集成环境。Compose 对 Embedding 与 LLM 密钥使用必填校验，并在联调环境关闭静默 fallback；真实密钥只在本地或平台密钥管理中注入，仓库只保留脱敏模板。这样错误配置会在启动时暴露，而不会误连资源或被降级掩盖。

  这种做法既防止开发配置误连生产资源，也避免降级路径掩盖联调问题。环境隔离还应包含独立的数据库、索引和监控标签，而不只是换一个 APP_ENV 名称。

- **关键代码点（供理解）**：
  - `APP_ENV: ${APP_ENV:-local}` — 默认local模式，开发者零依赖即可运行
  - `ALLOW_PROVIDER_FALLBACKS: "false"` 和 `EMBEDDING_ALLOW_HASH_FALLBACK: "false"` — 联调环境禁止降级，Fail-Fast暴露问题
  - `.env.integration.example`文件头注释："Copy this file to .env.integration. Do not commit real keys" — 密钥管理的安全意识
  - `${EMBEDDING_API_KEY:?Set EMBEDDING_API_KEY in .env.integration}` — 强制必填语法，变量缺失时docker compose直接报错
  - `.gitignore`包含`.env.integration` — 防止含密钥的配置文件被提交到代码仓库
  - docker-compose命名`oa-rag-integration` — 通过项目名称区分环境

#### 面试题96：健康监控

- **原有答案**：1. 技术指标：QPS、错误率、超时率、各组件延迟、API失败率；2. 业务指标：任务成功率、人工转接率、用户留存、转化指标；3. 成本指标：token用量、模型/工具成本按天/周统计；4. 告警机制：指标超阈值自动通知OnCall团队。

- **结合本项目，我会这样回答**：

  在本项目中，我将健康检查拆成两层：Compose 为 PostgreSQL、Milvus、OpenSearch 等服务配置实际探测命令和重试次数，依赖服务达到 healthy 后才启动 API；应用自身再主动探测数据库、缓存、向量库、Embedding 与 LLM，并将结果汇总到健康端点和 Prometheus 指标。

  本项目为启动较慢的服务设置重试，并让 API 在依赖健康后再启动。这能减少服务刚启动就连接失败的偶发问题，但生产环境仍需要结合 Prometheus 告警和业务指标判断是否可对外提供能力。

- **关键代码点（供理解）**：
  - `healthcheck: test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]` — postgres的专用健康检查命令
  - `condition: service_healthy` — 严格的healthy依赖而非简单的容器启动依赖
  - `retries: 30`（milvus和opensearch） — 为启动较慢的服务配置了充足的重试次数
  - `HEALTH_PROBE_EXTERNAL_SERVICES: "true"` — 应用层健康端点主动探测下游依赖
  - `test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:18080/api/v1/health', timeout=3)"]` — API服务的自检探针
  - `external_stack_smoke.py`中的`assert health_data.get("postgres") != "up"` — 外部验证断言

#### 面试题97：服务降级配置

- **原有答案**：1. 降级路径：切换到备用模型（性能差但可用），禁用非关键功能（如复杂分析），保留基础问答；2. 用户侧处理：友好提示当前能力受限，不隐瞒错误；3. 工程侧防护：使用熔断器/断路器模式，避免反复重试拖垮系统。

- **结合本项目，我会这样回答**：

  在本项目中，我将 Fail-Fast 与 Fallback 作为环境化策略：集成联调时关闭 Embedding 和 LLM 的兜底实现，并对密钥做 Compose 必填校验，确保依赖问题立即失败；生产环境再根据业务容忍度启用 Provider 或基础能力回退。这样联调验证的是目标依赖，而不是无感知地跑在替代实现上。

  关键是把降级开关做成显式配置并在健康接口中暴露，避免团队误以为使用的是目标模型，实际却已静默切到兜底实现。降级应保证可用性，但不能掩盖质量下降。

- **关键代码点（供理解）**：
  - `ALLOW_PROVIDER_FALLBACKS: "false"` — 禁止Embedding/LLM提供商失败时自动降级
  - `EMBEDDING_ALLOW_HASH_FALLBACK: "false"` — 禁止向量嵌入服务不可用时使用哈希降级
  - `${EMBEDDING_API_KEY:?Set EMBEDDING_API_KEY in .env.integration}` — 变量未设置时docker compose启动直接报错，Fail-Fast策略
  - `${LLM_API_KEY:?Set LLM_API_KEY in .env.integration}` — LLM密钥同理，缺失即启动失败
  - 对比dev环境：本地开发时允许降级，方便零依赖迭代；integration环境禁止降级，强制验证真实链路
  - `INGESTION_MODE: sync` — 同步摄入模式，确保文档处理结果立即可验证


## 三、Dockerfile — 统一构建

### 文件内容概述

Dockerfile基于`python:3.11-slim`轻量镜像构建，设置了三个Python生产环境优化变量（`PYTHONDONTWRITEBYTECODE=1`禁写.pyc文件、`PYTHONUNBUFFERED=1`确保日志实时输出、`PIP_NO_CACHE_DIR=1`减小镜像体积）。构建过程采用Docker层缓存优化策略：先COPY requirements.txt并执行pip install，再COPY整个源码目录。最后EXPOSE 18080端口，CMD默认使用uvicorn启动FastAPI应用。这个Dockerfile的一个关键设计是"一镜像双用途"——同一个镜像既可以作为API服务运行（CMD uvicorn），也可以被docker-compose中的celery-worker服务复用（通过compose的command覆盖为celery worker命令），避免了维护两套Dockerfile的成本和潜在不一致性。

### 详细技术点解析

1. **Docker层缓存优化**：Docker构建过程中每一行指令生成一个镜像层，当某一层的内容未变化时Docker会复用缓存。Dockerfile将依赖安装分为两步——先`COPY requirements.txt ./`再`RUN pip install`，这意味着只有requirements.txt变化时才会重新执行pip install（最耗时的步骤），而日常代码修改只触发最后一层`COPY . .`的重建。这种分层策略使得日常开发中绝大多数docker build都能命中依赖缓存，构建时间从几分钟缩短到几秒。

2. **Python生产环境变量**：三个环境变量的设置各有深意。`PYTHONDONTWRITEBYTECODE=1`禁止Python生成`.pyc`字节码文件——在容器内这些文件除了占用磁盘空间和inode外没有价值，因为容器重启后文件系统层会被重新创建。`PYTHONUNBUFFERED=1`强制stdout和stderr为无缓冲模式——在容器环境中，这意味着日志可以立即被Docker的日志驱动（如fluentd、json-file）捕获，而不是在Python缓冲区中等待直到缓冲区满才一次性刷新，这对于调试容器启动失败问题至关重要。`PIP_NO_CACHE_DIR=1`禁止pip缓存下载的包——减小最终镜像体积，因为在构建完成后pip缓存没有保留价值。

3. **一镜像双用途**：Dockerfile的CMD指令默认为`uvicorn app.main:app --host 0.0.0.0 --port 18080`，但在docker-compose.integration.yml中，api服务通过自己的command覆盖为`sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 18080"`（增加了数据库迁移前置步骤），而celery-worker服务通过自己的command覆盖为`python -m celery -A app.tasks.evaluation_tasks.celery_app worker --loglevel=INFO`。两个服务使用完全相同的镜像（`build: context: .`），但通过不同的command运行完全不同的进程。这种设计避免了为API和Worker维护两套Dockerfile的繁琐，也确保了二者的Python依赖环境完全一致。

### 涉及到的面试题

第57题（容器化）、第59题（冷启动）

### 面试题详解

#### 面试题59：冷启动优化

- **原有答案**：冷启动：推理服务刚启动，模型初次加载、GPU显存初始化、缓存未就绪，首条请求延迟极高。优化方案：1. 服务启动预热，发送测试请求完成模型初始化；2. 模型预加载至显存；3. 常驻后台推理进程，不销毁实例；4. 优化模型加载速度（量化、分片加载）。

- **结合本项目，我会这样回答**：

  在本项目中，我从镜像构建、依赖就绪和启动顺序优化冷启动：Dockerfile 先复制依赖清单并安装依赖，再复制业务代码以复用构建缓存；启动命令先执行数据库迁移；Compose 通过健康检查让 API 和 Worker 等待下游真正就绪。这样减少重复安装，也避免刚启动就因依赖未就绪而失败。

  这些措施主要降低重复构建和启动失败重试的成本。生产环境还可以预拉镜像、设置合理资源请求、把重型模型初始化改为按需或预热，并通过启动耗时指标验证优化是否有效。

- **关键代码点（供理解）**：
  - `COPY requirements.txt ./` → `RUN pip install` → `COPY . .` — 分层构建利用Docker缓存，依赖未变时跳过pip install
  - `sh -c "alembic upgrade head && uvicorn ..."` — 数据库迁移前置，应用启动时schema已就绪
  - `condition: service_healthy` — 确保依赖服务完全就绪后才启动API，避免启动时连接失败和重试
  - `python:3.11-slim` — slim镜像体积小，加快拉取速度
  - `restart: unless-stopped` — celery-worker自动重启，减少冷启动频次
  - `PIP_NO_CACHE_DIR=1` — 减小镜像体积，间接加速构建和部署


## 四、.env.integration.example + .gitignore — 密钥管理

### 涉及到的面试题

第83题（PII敏感信息）、第88题（版本更新防回归）

### 面试题详解

#### 面试题83：PII敏感信息处理

- **原有答案**：1. 收集最小化：仅采集完成业务必需的信息，不超范围收集；2. 存储加密：静态数据加密 + 访问控制 + 操作审计，防止数据泄露；3. 传输安全：使用HTTPS/TLS加密传输，避免在明文日志中记录敏感信息；4. 合规使用：若需用于训练/评测，必须做匿名化/脱敏处理，遵守GDPR等隐私法规。

- **结合本项目，我会这样回答**：

  在本项目中，我将密钥与 PII 处理放入完整数据生命周期：仓库只保留脱敏环境模板，真实 Embedding 和 LLM Key 通过环境变量注入，真实环境文件被 gitignore 排除；联调结果和日志也应避免写入敏感值。这样代码仓库、部署配置和运行产物都不会成为密钥泄露入口。

  本项目的集成配置用占位符和必填校验避免空密钥启动，并排除真实环境文件。后续还应补充输入脱敏、访问审计、密钥轮换和最小权限，才能覆盖用户数据与系统凭据两类风险。

- **关键代码点（供理解）**：
  - `.env.integration.example`中所有敏感值为占位符：`replace-with-a-random-api-key`
  - `.gitignore`排除`.env.integration`文件 — 防止真实密钥被提交
  - `${EMBEDDING_API_KEY:?Set EMBEDDING_API_KEY in .env.integration}` — 启动时强制校验密钥非空
  - `${LLM_API_KEY:?Set LLM_API_KEY in .env.integration}` — LLM密钥同理
  - `--env-file .env.integration` — 显式指定配置文件路径，不依赖shell环境变量
  - `X-Internal-Token`头机制 — 内外部接口隔离，敏感端点需要额外认证


## 五、scripts/external_stack_smoke.py — 外部栈冒烟测试

### 涉及到的面试题

第95题（Agent自动化测试）、第88题（回归测试）、第89题（可审计性）、第98题（文档编写）

### 面试题详解

#### 面试题95：Agent自动化测试（集成/冒烟测试层）

- **原有答案**：单测工具权限，行为测试用LLM Judge，集成测全流程，回放对比差异。

- **结合本项目，我会这样回答**：

  在本项目中，我将集成冒烟测试放在真实 Docker 服务栈上执行：脚本通过 HTTP 上传动态生成的文档，轮询异步入库任务完成，再验证 dense、sparse、hybrid 检索、Agent 回答和评测任务。这样覆盖了从文档解析、向量写入到外部检索服务联通的完整链路，而不是只验证单个函数。

  冒烟测试重点是尽快发现服务联通、配置不一致和索引写入失败等集成问题，因此要设置超时、轮询上限和结构化结果产物。它不能替代快速单测，两者应在不同阶段配合执行。

- **关键代码点（供理解）**：
  - `with httpx.Client(timeout=60.0) as client:` — 使用真实HTTP客户端而非TestClient
  - `build_docx_bytes()` — 动态生成DOCX文件，确保测试数据自包含不依赖外部文件
  - `wait_for_job(client, job_uuid)` — 轮询异步任务状态，验证完整的Doc→Chunk→Embed→Milvus+OpenSearch流水线
  - `assert not debug_data.get("dense_hits") or not debug_data.get("sparse_hits")` — Hybrid双路检索断言
  - `assert agent_data.get("answer_status") != "grounded" or not agent_data.get("citations")` — Agent回答质量和引用断言
  - `wait_for_run(client, run_uuid)` — Celery异步任务的pending→completed状态转换验证
  - `write_artifact(result)` → `qa-outputs/external-stack/<时间戳>/smoke-result.json` — 证据文件输出
  - `POLL_TIMEOUT_SECONDS`和`POLL_INTERVAL_SECONDS` — 可配置的环境变量，适配不同环境

#### 面试题88：模型版本更新防回归

- **原有答案**：1. 版本管理：为每个模型、Prompt、Agent工作流分配明确的版本号；2. 回归测试：上线前用固定测试集做多维度评估（正确率、安全性、合规性）；3. 小流量灰度：先给少量用户/租户使用新版本，监控指标变化；4. 一键回滚：提供快速回滚机制，发现问题时第一时间恢复旧版本。

- **结合本项目，我会这样回答**：

  在本项目中，我先在 Compose 中锁定 PostgreSQL、Milvus、OpenSearch 等基础服务版本，避免 `latest` 造成环境漂移；升级模型、Embedding 或依赖后，再用固定的冒烟输入和离线基准对比检索命中、回答状态与延迟，并保存结构化结果。这样每次版本变更都有可对比的回归证据。

  本项目会保留可 diff 的 smoke 结果作为版本证据。生产升级还应灰度发布、观察核心指标并提供回滚版本，这样模型更新从一次不可控替换变成可验证的变更流程。

- **关键代码点（供理解）**：
  - `image: postgres:16-alpine` 等精确版本标签 — 杜绝latest漂移，每次部署使用相同版本
  - `image: milvusdb/milvus:v2.5.12` — Milvus精确版本锁定
  - `image: opensearchproject/opensearch:2.17.1` — OpenSearch精确版本锁定
  - `smoke-result.json` — 可diff的证据文件，支持版本间对比发现退化
  - 环境变量驱动设计 — 可在CI/CD中通过环境变量控制行为，自动化回归测试
  - smoke脚本7步验证覆盖 — 从health到文档上传到检索到Agent到Celery的全链路回归检测

#### 面试题89：可审计性（Auditability）

- **原有答案**：1. 关键操作日志：完整记录用户请求、模型决策摘要、工具调用、最终结果；2. 高风险操作审计：对资金操作、隐私访问等高风险行为，记录详细审计信息（谁、何时、做了什么），同时脱敏敏感内容；3. 审计查询接口：提供合规/安全团队可按条件检索的日志查询接口。

- **结合本项目，我会这样回答**：

  在本项目中，我通过唯一 smoke_id 串联一次联调：健康检查、上传、入库、检索、Agent 和评测的结果都写入同一份结构化 JSON，并按时间戳持久化。这样某一步失败时可以直接回看该阶段的输入与结果，也能比较不同版本或不同环境的差异。

  这样出现问题时可以知道失败发生在哪一步，也能比较不同部署或版本的结果差异。线上场景还应让 trace_id 贯穿 API、日志和模型调用，并注意审计数据本身的脱敏和保存期限。

- **关键代码点（供理解）**：
  - `smoke_id = uuid4().hex[:10]` — 每次运行唯一标识，贯穿文档标题、文件名、数据集名
  - `result: dict = {"smoke_id": smoke_id, "api_base_url": API_BASE_URL, "checks": {}}` — 结构化的完整审计记录
  - `result["checks"]`详细记录每步执行结果 — health/upload/ingestion/hybrid_search/agent/celery_evaluation
  - `write_artifact(result)` → `qa-outputs/external-stack/<时间戳>/smoke-result.json` — 带时间戳的持久化证据文件
  - `evaluation_cleanup`和`document_cleanup`记录在result中 — 清理操作的审计追踪
  - 文档标题中包含"联调样本"标记 — 测试数据的可追溯标识


## 六、docs/external_integration_runbook.md — 运行手册

### 涉及到的面试题

第98题（编写文档让团队接手）、第99题（团队角色协作）、第100题（AI Agent趋势）

### 面试题详解

#### 面试题98：编写文档让团队成员接手

- **原有答案**：1. 架构文档：整体设计、组件关系、数据流程图；2. 开发文档：新增工具、修改Prompt、新增工作流的步骤说明；3. 运维文档：部署流程、监控项、常见故障及处理手册；4. 安全文档：权限模型、数据分类分级、合规要求；5. 实战示例：提供从0新增小功能的完整示例。

- **结合本项目，我会这样回答**：

  在本项目中，我将交接文档写成可验证的运行手册：明确 WSL 前置配置、环境变量、启动命令、向量维度约束、常见故障和组件级验收标准。新成员不是只看到概念，而是按命令启动后能依据健康状态和冒烟结果判断服务是否真正可用。

  本项目沉淀了 WSL 特殊配置、向量维度约束和组件级验收表，这些都是高价值的经验。文档应与脚本、Compose 文件和实际版本一起维护，避免过期后反而误导接手人员。

- **关键代码点（供理解）**：
  - `wsl -d docker-desktop sysctl -w vm.max_map_count=262144` — WSL2特殊配置，真实踩坑经验
  - "Embedding维度必须与EMBEDDING_VECTOR_SIZE一致" — 约束前置声明，防止配置错误
  - 四步启动命令全部可复制粘贴 — Copy-Item → 编辑 → docker compose up → docker compose ps
  - 5行×2列的通过标准表格 — 每个组件对应具体可验证的证据
  - "单节点可能为yellow，不影响联调" — 已知边界的诚实告知
  - "预发/生产应使用多节点、TLS、服务账号" — 升级路径的明确指示

#### 面试题99：团队角色协作

- **原有答案**：1. 产品经理：定义业务目标和用户体验；2. 后端工程师：实现Agent引擎、工具、服务集成；3. 前端/客户端工程师：实现对话UI、交互和埋点；4. MLOps/LLM工程师：模型选型、Prompt设计、评测与调优；5. 安全/合规/运维：权限、安全策略、上线与监控。

- **结合本项目，我会这样回答**：

  在本项目中，我将联调职责落到具体产物：后端维护 API、任务和业务链路，DevOps 维护 Compose、网络与健康检查，安全关注环境变量模板和密钥边界，QA 维护外部服务冒烟脚本与回归证据。通过共享的环境模板、运行手册和结构化结果，各角色可以基于同一份事实协作和定位问题。

  本项目把基础设施、密钥约束、冒烟脚本和运行手册分别沉淀为可协作的文件。这样出现问题时可以快速定位责任边界，同时避免知识只留在某个人的本地环境里。

- **关键代码点（供理解）**：
  - `docker-compose.integration.yml`的8服务编排和健康检查链 — 后端/DevOps工程师的基础设施能力
  - `.env.integration.example`的密钥占位符和`.gitignore`排除 — 安全/合规工程师的参与
  - `external_stack_smoke.py`的7步验证和断言 — QA/测试工程师的测试设计能力
  - `external_integration_runbook.md`的WSL2特殊配置 — 跨平台运维经验的沉淀
  - runbook中"接入真实身份与PermissionChecker插件" — 安全工程师的权限架构参与
  - Dockerfile的分层缓存策略 — 后端工程师的构建优化意识


## 七、面试题索引

| 题号 | 题目 | 最佳证据 |
|------|------|---------|
| 第52题 | 部署架构 | docker-compose.integration.yml（单节点standalone架构及升级到分布式多节点的明确路径） |
| 第57题 | 容器化部署 | Dockerfile（分层缓存）+ docker-compose.integration.yml（&app_environment锚点环境统一） |
| 第59题 | 冷启动优化 | Dockerfile（COPY requirements分层缓存）+ alembic upgrade head前置迁移 + condition: service_healthy预就绪依赖 |
| 第60题 | 多模型混部 | docker-compose.integration.yml（8个异构服务的单机编排，资源共享与隔离的平衡） |
| 第83题 | PII敏感信息 | .env.integration.example（占位符模板）+ .gitignore（排除真实密钥）+ ${VAR:?}强制必填 |
| 第88题 | 版本更新防回归 | external_stack_smoke.py（全链路冒烟回归）+ 精确镜像版本标签（杜绝latest漂移） |
| 第89题 | 可审计性 | external_stack_smoke.py（smoke_id贯穿全流程 + 结构化result + 带时间戳的smoke-result.json证据文件） |
| 第93题 | 环境隔离 | .env.integration（integration环境）+ ALLOW_PROVIDER_FALLBACKS: "false"（禁止降级，与dev环境降级全开形成对比） |
| 第95题 | 自动化测试 | external_stack_smoke.py（真实HTTP+PG+Milvus+OpenSearch+Redis+Celery的7步冒烟测试） |
| 第96题 | 健康监控 | docker-compose.integration.yml（pg_isready/redis-cli ping/etcdctl endpoint health/curl healthz/_cluster/health/urllib health七种探针） |
| 第97题 | 服务降级 | ALLOW_PROVIDER_FALLBACKS: "false" + EMBEDDING_ALLOW_HASH_FALLBACK: "false"（联调Fail-Fast vs 生产降级） |
| 第98题 | 文档编写 | external_integration_runbook.md（前置条件/启动步骤/验证链路/通过标准表格/已知边界五维覆盖） |
| 第99题 | 团队角色 | 五份资产对应的角色分工（后端/DevOps/安全/QA/LLM工程师的协作痕迹） |
| 第100题 | AI Agent趋势 | 从本地dev零依赖→integration真实栈联调→production多节点+TLS的工程化演进路径 |


## 自检清单
- [x] 每道面试题都包含原有答案、项目回答和关键代码点三部分
- [x] 项目回答以一到两段直接作答，突出技术取舍与实现边界
- [x] 关键代码点保留具体配置项或源码位置，并说明其实现体现
- [x] 涉及14道面试题（52/57/59/60/83/88/89/93/95/96/97/98/99/100）
- [x] 五份新增资产均有对应的详细技术点解析
- [x] 面试题索引表建立了题号到最佳证据的映射


