# 多轮问答功能设计文档

**日期**: 2026-06-14
**状态**: 已确认

---

## 1. 概述

为知识库系统新增多轮对话问答功能。用户可创建对话、连续提问，系统基于历史上下文 + 知识库检索生成回答，对话持久化到数据库。

## 2. 数据模型

### conversations

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | UUID, PK | 对话唯一标识 |
| `title` | VARCHAR(255) | 对话标题（默认为首条用户消息前 50 字） |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 最后活跃时间 |

### conversation_messages

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | UUID, PK | 消息唯一标识 |
| `conversation_id` | UUID, FK → conversations.id | 所属对话 |
| `role` | VARCHAR(16) | `user` 或 `assistant` |
| `content` | TEXT | 消息内容 |
| `citations` | JSONB, nullable | 引用来源（仅 assistant 消息） |
| `metadata` | JSONB, nullable | 额外元数据（latency 等） |
| `created_at` | TIMESTAMP | 消息时间 |

## 3. API 设计

路由前缀：`/api/v1/qa/conversations`

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/conversations` | 创建新对话，可选携带第一条消息 |
| `GET` | `/conversations` | 获取对话列表，按 `updated_at` 倒序 |
| `GET` | `/conversations/{id}` | 获取对话详情（含消息列表） |
| `DELETE` | `/conversations/{id}` | 删除对话及其所有消息 |
| `POST` | `/conversations/{id}/messages` | 发送消息，返回 AI 回复 |

### POST /conversations/{id}/messages

请求体：
```json
{
  "question": "用户问题"
}
```

响应体：
```json
{
  "code": 0,
  "data": {
    "message_id": "uuid",
    "conversation_id": "uuid",
    "answer": "AI 回答内容",
    "answer_status": "grounded",
    "citations": [],
    "matched_documents": [],
    "latency_ms": { "retrieval": 120, "generation": 800, "total": 920 }
  }
}
```

## 4. 问答流程

```
POST /conversations/{id}/messages
│
├── 1. 鉴权校验（internal_token / 用户身份）
├── 2. 加载该对话的历史消息
├── 3. 组装 LLM messages 上下文（system + history + 当前问题）
├── 4. 执行检索（复用现有 QaService 检索逻辑）
├── 5. 调用 LLM 生成回答（带历史上下文 + citations）
├── 6. 保存 user 消息 + assistant 消息到数据库
└── 7. 返回响应
```

## 5. 前端设计

### 路由

| 路径 | 页面 | 说明 |
|------|------|------|
| `/qa` | 对话列表页 | 卡片式布局，点击进入对话 |
| `/qa/:id` | 聊天详情页 | 类 ChatGPT 的聊天界面 |
| `/qa/new` | 新建对话 | 可选：直接进入空白聊天页 |

### 对话列表页 (`/qa`)

- 顶部：标题 + "新建对话" 按钮
- 主体：对话卡片列表，每条显示 title、更新时间、最后一条消息摘要
- 空状态：引导用户创建第一个对话

### 聊天详情页 (`/qa/:id`)

- 顶部：对话标题（可编辑）、返回按钮
- 主体：消息气泡列表（user 右对齐蓝色，assistant 左对齐灰色）
  - assistant 消息底部折叠展示引用来源
- 底部：输入框 + 发送按钮，支持 Enter 发送

### 导航

侧边栏新增导航项「智能问答」，图标/标签，位于「检索测试」上方。

## 6. 技术实现

### 后端新增文件

| 文件 | 说明 |
|------|------|
| `app/models/qa_conversation.py` | conversation + message ORM 模型 |
| `app/schemas/qa_conversation.py` | Pydantic schema |
| `app/repositories/qa_conversation.py` | 数据访问层 |
| `app/api/qa_conversation.py` | API 路由 |
| `migrations/versions/YYYYMMDD_0011_qa_conversations.py` | 数据库迁移 |

### 后端修改文件

| 文件 | 修改内容 |
|------|----------|
| `app/api/router.py` | 注册新路由 |
| `app/models/__init__.py` | 导出新模型 |
| `app/services/qa.py` | 新增 `answer_with_context()` 方法，支持传入历史消息 |
| `app/integrations/llm.py` | `generate_answer()` 新增 `history` 参数 |

### 前端新增文件

| 文件 | 说明 |
|------|------|
| `frontend/src/pages/QaListPage.tsx` | 对话列表页 |
| `frontend/src/pages/QaChatPage.tsx` | 聊天详情页 |

### 前端修改文件

| 文件 | 修改内容 |
|------|----------|
| `frontend/src/App.tsx` | 注册新路由 |
| `frontend/src/components/Layout.tsx` | 添加导航项 |
| `frontend/src/api.ts` | 添加 API 调用函数 |
| `frontend/src/types.ts` | 添加类型定义 |

## 7. 自检清单

- [x] 数据模型字段完整
- [x] API 路径和响应格式明确
- [x] 前端页面和交互描述清晰
- [x] 与技术现有模式一致（FastAPI Router + SQLAlchemy Model + Pydantic Schema + Repository）
- [x] 迁移策略明确（新增 migration，不修改已有表）
- [x] 历史上下文构建方式明确
