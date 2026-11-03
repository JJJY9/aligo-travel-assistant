# Aligo 智能旅行助手

[![CI](https://github.com/JJJY9/aligo-travel-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/JJJY9/aligo-travel-assistant/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

基于**大语言模型**（默认 DeepSeek，兼容豆包 / OpenAI 等 OpenAI 兼容接口）和 **AgentScope 框架**的多智能体旅行规划系统，采用Plan-and-Execute架构，实现智能意图识别、两层记忆系统、RAG知识库、联网搜索和优先级并行调度。

> **实现现状**：本仓库是可直接运行的**本地实现**——短期记忆默认进程内滑动窗口，长期记忆默认 JSON 文件（`data/memory/`），RAG 使用 Milvus Lite 本地向量库。Redis / PostgreSQL 为**可选生产存储后端**（配置 `REDIS_URL` / `DATABASE_URL` 启用，未配置或连接失败时自动回退）；关键指标为开发期实验口径（见「关键指标」小节说明）。

## 界面预览

| 对话与执行路线（浅色） | 深色模式 |
|---|---|
| ![对话与执行路线](docs/screenshots/webui-answer-light.jpg) | ![深色模式](docs/screenshots/webui-answer-dark.jpg) |
| **记忆与用量面板**（偏好 / 行程 / 7 天趋势） | **欢迎页**（会话搜索 / 置顶） |
| ![记忆与用量面板](docs/screenshots/webui-memory.jpg) | ![欢迎页](docs/screenshots/webui-welcome.jpg) |

![知识库维护：查看 / 新增 / 删除知识片段](docs/screenshots/webui-knowledge.jpg)

*知识库维护：在记忆面板中查看、新增、删除 RAG 知识片段，新增后立即可被检索。*

界面为 **FastAPI + 无构建前端**（原生 HTML / CSS / JS，无需 Node），支持流式执行进度、
会话搜索 / 重命名 / 置顶、记忆与用量面板、知识库维护、深浅主题与移动端布局，启动方式见「Web 界面」小节。

## ✨ 核心亮点

### 🎯 智能意图识别
- 基于LLM语义理解的多意图识别（准确率90%+，对比关键词匹配提升25%）
- 支持6大类意图：行程规划、记忆查询、偏好管理、知识问答、信息查询、事项收集
- 自然语言理解，无需关键词匹配

### 🧠 两层记忆架构
- **短期记忆**：默认进程内滑动窗口（最近 10 轮对话）；配置 `REDIS_URL` 后使用 Redis（支持跨进程会话共享）
- **长期记忆**：默认 JSON 文件持久化（`data/memory/{user_id}.json`）+ LLM 异步总结；配置 `DATABASE_URL` 后使用 PostgreSQL
- 智能识别偏好追加/覆盖动作（"我还喜欢如家" vs "我搬家到上海了"）

### 📚 RAG知识库
- Milvus Lite 本地向量库 + BGE-small-zh-v1.5 中文向量模型（本地部署）
- 按小节切分（Chunking，保留章节边界）+ 余弦相似度检索（Top-K=5）
- 知识溯源：返回文档来源（`metadata` 中保留原始文档路径），并在回答中附 Top 3 参考片段
- 进程内按数据库路径共享 Milvus Lite 客户端：多个 Web 会话 / 智能体不会重复打开同一本地库，避免文件锁冲突
- 知识库状态：`GET /api/knowledge`、`python cli.py kb` 与 Web 记忆面板显示集合名 / 片段数，便于确认知识库是否就绪
- 知识库维护：记忆面板可搜索 / 分页查看、新增、删除知识片段（`GET/POST/DELETE /api/knowledge/documents`），支持导出 / 导入 JSON 备份（`/api/knowledge/export` / `import`；接口单次最多 200 条，Web 导入自动分批、CLI 不限条数）；新增后立即可被检索，删除后再新增按最大 id 递增，不会与已有主键冲突
- 检索评测：`python cli.py eval` 用 14 个差旅问题离线计算 hit@1/3/5 与 MRR（只检索、不调用大模型），便于调整切分策略或补充知识后回归

### ⚡ 优先级并行调度
- Plan-and-Execute架构：IntentionAgent → OrchestrationAgent → 子Agent
- 同优先级Agent并行执行（asyncio.gather）
- 系统响应时间从30秒优化到15秒（-50%）

### 🏗️ 插件化架构
- **Skill Plugins**：所有子Agent重构为独立插件（`.claude/skills/`）
- **LazyAgentRegistry**：动态发现机制，自动扫描注册
- **懒加载**：未使用的 Skill 不加载，启动时仅扫描技能元数据
- **Progressive Disclosure**：渐进式暴露，意图识别阶段仅加载元数据

### 🛡️ 稳定性保障
- **熔断器**：连续失败后自动熔断，保护服务
- **指数退避重试**：自动重试失败请求（最大3次）
- **健康检查**：实时监控LLM服务可用性

### 🌐 Web 界面
- **SSE 流式执行进度**：意图识别与每个子智能体的执行状态实时点亮，形成一条「执行路线」，每站显示单独耗时，完成后自动折叠为总耗时摘要
- **执行路线可回看**：每轮助手消息随附执行元数据（意图 / 调度 / 状态 / 耗时 / 用量），恢复历史会话时可展开查看
- **会话历史与搜索**：历史会话按日期分组、可恢复 / 删除，并支持按标题与消息内容全文检索（带命中片段）；点击搜索结果会定位并短暂高亮命中的那条消息；地址栏同步 `user` / `session` 参数，可直接打开或分享某个会话（顶栏「复制链接」）
- **现代交互**：深浅主题、停止生成、重新生成、复制回答、Markdown 表格 / 代码块、移动端抽屉布局
- **记忆与用量面板**：在线编辑 / 删除偏好、查看最近行程与统计、删除误记录的行程（同步更新统计与常去目的地；同一行程重复规划会自动去重）、存储后端、模型用量（调用次数 / tokens / 平均耗时 / 常用智能体），一键导出 JSON；单个会话可导出 Markdown

---

## 系统架构

```
用户输入
   ↓
┌──────────────────────────────────────────────────────────┐
│  IntentionAgent (意图识别智能体)                          │
│  - 语义理解用户意图（不使用关键词匹配）                    │
│  - 识别关键实体                                           │
│  - 生成调度计划                                           │
│  - 确定智能体优先级                                       │
│  - 动态加载 Skills Metadata (Progressive Disclosure)     │
└──────────────────────────────────────────────────────────┘
   ↓
┌──────────────────────────────────────────────────────────┐
│  OrchestrationAgent (协调器智能体)                       │
│  - 按优先级调度子智能体                                   │
│  - 同优先级并行执行                                       │
│  - 管理智能体间消息传递                                   │
│  - 集成两层记忆系统                                       │
│  - 动态实例化 Skills (Plugin Architecture)               │
└──────────────────────────────────────────────────────────┘
   ↓
┌─────────────────────── 优先级 1 (并行执行) ──────────────┐
│                                                           │
│  ┌─────────────────────┐  ┌──────────────────────────┐  │
│  │ MemoryQuery Skill   │  │ EventCollection Skill    │  │
│  │ 记忆查询智能体       │  │ 事项收集智能体            │  │
│  │ - 查询旅行记录      │  │ - 出发地/目的地           │  │
│  │ - 查询用户偏好      │  │ - 出行时间/返程地         │  │
│  │ - 查询历史对话      │  │ - 出行目的                │  │
│  └─────────────────────┘  └──────────────────────────┘  │
│                                                           │
│  ┌─────────────────────┐  ┌──────────────────────────┐  │
│  │ Preference Skill    │  │ InformationQuery Skill   │  │
│  │ 偏好管理智能体       │  │ 信息查询智能体            │  │
│  │ - 酒店/航空偏好     │  │ - 网络搜索 (DuckDuckGo)  │  │
│  │ - 座位/房型偏好     │  │ - 实时信息查询           │  │
│  │ - 机型/餐饮偏好     │  │ - LLM摘要生成            │  │
│  │ - 支持追加/覆盖     │  │                          │  │
│  └─────────────────────┘  └──────────────────────────┘  │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ RAGKnowledgeAgent Skill (知识库查询智能体)          │ │
│  │ - 差旅政策文档查询 (Milvus Lite + RAG)             │ │
│  │ - 企业内部知识检索                                  │ │
│  │ - 自动文档切分 (Chunking) + 向量检索                │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                           │
└───────────────────────────────────────────────────────────┘
   ↓
┌─────────────────────── 优先级 2 (依赖优先级1) ───────────┐
│                                                           │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ ItineraryPlanningAgent Skill (行程规划智能体)       │ │
│  │ - 整合所有前序智能体信息                            │ │
│  │ - 生成完整行程计划                                  │ │
│  │ - 包含：景点、交通、酒店、餐饮                      │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                           │
└───────────────────────────────────────────────────────────┘
   ↓
┌──────────────────────────────────────────────────────────┐
│  结果聚合与记忆更新                                       │
│  - 聚合所有智能体结果                                     │
│  - 更新长期记忆（偏好、行程历史、聊天记录）                │
│  - 生成人性化回复                                         │
└──────────────────────────────────────────────────────────┘
   ↓
最终结果
   ↓
用户看到结果
```

### 一次 Web 对话的请求流程（SSE）

```mermaid
sequenceDiagram
    participant U as 浏览器
    participant W as FastAPI /api/chat/stream
    participant C as ConversationRuntime
    participant I as IntentionAgent
    participant O as OrchestrationAgent
    participant A as 子智能体（Skill）
    participant M as 记忆（Redis / JSON / PostgreSQL）

    U->>W: POST 消息（user_id / session_id）
    W->>W: 限流 / 熔断检查
    W->>C: ask(on_event)
    C->>M: 重载长期记忆、组装上下文
    C->>I: 意图识别（带重试）
    I-->>C: intents / agent_schedule
    C-->>U: SSE phase / plan
    C->>M: 写入用户消息
    C->>O: reply(intention)
    O->>A: 按优先级并行执行
    A-->>O: 结果 + 耗时
    O->>M: 保存偏好 / 行程
    O-->>C: 聚合结果
    C-->>U: SSE agent_start / agent_done
    C->>M: 写入助手消息（含执行元数据）
    C-->>U: SSE done（回复 + 用量 + 耗时）
```

RAG 检索在 `RAGKnowledgeAgent` 内使用 Milvus Lite（与记忆读写分离，进程内共享客户端）；
SSE 每 15 秒发送一次 `: keep-alive` 心跳，浏览器断开时服务端取消本轮任务。

### 连接与可用性

为保证 LLM 服务不稳定时的可用性，在调用链外增加了以下机制（不改变原有业务逻辑）：

| 机制 | 说明 |
|------|------|
| **熔断器** | 连续失败若干次后暂停调用 LLM，直接提示「服务暂时不可用」；一段时间后自动半开试探恢复。 |
| **重试与退避** | 对意图识别、编排两次 LLM 调用做有限次重试，仅对超时、429、5xx 等可重试错误生效，采用指数退避。 |
| **健康检查** | 会话内输入 `health` 可查看熔断状态并探测 LLM 是否可达；命令行执行 `python cli.py health` 可单独做一次探测（退出码 0/1，便于监控）。 |

配置见 `config.py` 中的 `RESILIENCE_CONFIG`（重试次数、熔断阈值、恢复时间等）。

---

## 📊 关键指标

| 指标 | 优化前 | 优化后 | 提升幅度 |
|------|--------|--------|----------|
| 意图识别准确率 | 65% | 90%+ | +25% |
| 知识库问答准确率 | - | 95% | 新增功能 |
| 用户偏好记忆准确率 | - | 95% | 新增功能 |
| 系统响应时间 | 30秒 | 15秒 | -50% |
| 用户偏好缓存命中率（设计目标） | - | - | 尚未实现 |
| 系统启动速度 | 未优化 | 秒级（懒加载） | 启动仅扫描元数据 |

> **指标说明**：以上数据为开发期内部实验口径，仓库未附带对应评测脚本；「缓存命中率」为设计目标，当前未提供独立缓存层（存储层已支持 Redis / PostgreSQL，见「数据存储」）。

**优化路径**：
1. **V1.0**: 关键词匹配意图识别（准确率65%） + 串行调度（响应时间30秒）
2. **V2.0**: 两层记忆系统 + RAG知识库 + 联网搜索
3. **V3.0**: LLM语义理解意图识别（准确率90%+） + 优先级并行调度（响应时间15秒）
4. **V4.0**: Skill Plugins插件化架构 + LazyAgentRegistry

---

## 核心功能

### 1. 意图识别（基于LLM语义理解）

系统支持**6大类意图**自动识别（准确率90%+）：

- ✅ **itinerary_planning**: 规划未来行程
  - 示例："我想3月11日从北京去杭州出差一周"
- ✅ **memory_query**: 查询历史记忆
  - 示例："我去过哪里？"、"我之前说过什么偏好？"
- ✅ **preference**: 管理用户偏好（支持追加/覆盖）
  - 示例："我喜欢住汉庭酒店"、"我还喜欢如家"、"我搬家到上海了"
- ✅ **rag_knowledge**: 查询企业差旅知识库
  - 示例："差旅标准是什么？"、"报销政策是什么？"
- ✅ **information_query**: 联网查询实时信息
  - 示例："杭州明天天气怎么样？"、"北京明天限行吗？"
- ✅ **event_collection**: 收集行程要素
  - 自动提取：出发地、目的地、出发时间、返程时间、出行目的

**意图识别示例**：
```
用户: "我过去都去哪旅游过？"
→ IntentionAgent 识别为 memory_query
→ 调度 MemoryQueryAgent
→ 从 trip_history 查询并回答

用户: "我还喜欢7天酒店"
→ IntentionAgent 识别为 preference
→ 调度 PreferenceAgent
→ LLM 识别「还」字，判断为 append 模式
→ 追加到 hotel_brands 列表
```

### 2. 两层记忆系统

**短期记忆（会话级）**
- 基于**滑动窗口**的对话管理（默认进程内；配置 `REDIS_URL` 后使用 Redis，支持跨进程会话共享）
- 保存最近10轮对话
- 会话结束即清空
- 用于上下文理解和快速访问

**长期记忆（持久化）**
- 💾 **持久化存储**：默认 JSON 文件（`data/memory/{user_id}.json`）；配置 `DATABASE_URL` 后使用 PostgreSQL（表结构自动创建）
- 🎯 **用户偏好管理**：支持动态添加任意偏好类型，智能识别追加/覆盖动作
- 📅 **历史行程记录**：出发地、目的地、时间、目的，支持跨会话查询
- 📊 **统计信息**：常去目的地、总行程数
- 🤖 **LLM异步总结**：自动总结历史会话和行程记录

**测试记忆系统**：
```bash
python tests/test_memory_system.py
```

测试覆盖：
- ✅ 短期记忆：添加、查询、统计
- ✅ 长期记忆-偏好：动态添加、跨会话访问
- ✅ 长期记忆-行程：保存、查询、高频目的地统计
- ✅ 长期记忆-聊天历史：持久化对话记录
- ✅ LLM总结：异步生成历史摘要（包含行程记录）
- ✅ 跨会话持久化：新会话访问旧数据

### 3. RAG 知识库

基于 **Milvus Lite** 和 **BGE-small-zh-v1.5** 向量模型的企业差旅知识检索系统。

**技术方案**：
- **向量数据库**: Milvus（本地存储）
- **Embedding模型**: BGE-small-zh-v1.5（中文向量化，本地部署 `data/models/bge-small-zh-v1.5`）
- **文档处理**: 按小节切分（Chunking，章节 / FAQ 边界强制断块，块长 ≤ 400 字）
- **检索算法**: 余弦相似度检索（Top-K=5）
- **可追溯性**: 返回文档来源，支持知识溯源
- **准确率**: 95%（知识库问答准确率）

**初始化知识库**：
```bash
python .claude/skills/ask-question/script/init_knowledge_base.py
```

**知识库内容**（8类文档）：
- 差旅标准和规定
- 报销政策
- 预订指南
- 常见问题FAQ
- 紧急情况处理
- 平台使用指南
- 城市差旅指南
- 环保倡议


### 4. 信息查询（联网搜索）

基于 **DuckDuckGo (DDGS)** 的免费网络搜索功能：
- 🌐 实时网络搜索（天气、景点、实时新闻）
- 📝 LLM自动摘要（提取关键信息）
- 🔗 来源追踪（返回搜索来源）
- 🚀 异步查询（提升响应速度）

### 5. 优先级并行调度

基于 **asyncio.gather** 的智能并行调度机制：
- 📋 **多意图识别**：支持6大类意图（规划行程、查询记忆、管理偏好、知识问答、信息查询、实时检索）
- ⚡ **优先级+并行混合模式**：同优先级Agent并行执行，不同优先级串行依赖
- 🎯 **动态调度**：根据意图识别结果动态分配优先级
- 📈 **性能提升**：系统响应时间从30秒优化到15秒（-50%）

---

## 快速开始

### 1. 安装依赖

```bash
# 使用 requirements.txt 安装所有依赖
pip install -r requirements.txt

# 或者手动安装核心依赖
pip install "setuptools>=69.0.0,<82"  # milvus_lite 依赖
pip install agentscope==1.0.16        # 多智能体框架
pip install "pymilvus[milvus_lite]==2.6.9"  # 向量数据库
pip install sentence-transformers==5.2.3    # Embedding模型
pip install rich==13.9.4                    # CLI界面
pip install ddgs==9.10.0                    # 网络搜索
```

### 2. 配置模型

项目默认使用 **DeepSeek**（OpenAI 兼容接口），API 密钥二选一：

**方式一（推荐）：本地配置文件**

在项目根目录创建 `config_local.py`：

```python
API_KEY = "sk-你的密钥"
```

该文件已在 `.gitignore` 中，不会被误提交。

**方式二：环境变量**

```bash
# macOS / Linux
export LLM_API_KEY=你的API密钥

# Windows PowerShell
$env:LLM_API_KEY="你的API密钥"
```

**配置说明**：
- `LLM_API_KEY`: 大模型API密钥（必填，从 [DeepSeek 开放平台](https://platform.deepseek.com) 获取）
- `LLM_MODEL_NAME`: 模型名称（可选，默认 `deepseek-chat`；也可用 `deepseek-reasoner`）
- `LLM_BASE_URL`: 接口地址（可选，默认 `https://api.deepseek.com`）
- 切换其他服务商（豆包 / OpenAI 等）：覆盖上面两个变量，或修改 `config.py` 中的默认值（文件内有示例）
- `temperature` / `max_tokens`: 生成参数（0.7 / 8192）
- 完整环境变量清单见根目录 [`.env.example`](.env.example)（含 Redis / PostgreSQL 与限流配置；项目不会自动加载 `.env`，请自行 export 或使用 `config_local.py`）

### 3. 初始化知识库

```bash
python .claude/skills/ask-question/script/init_knowledge_base.py
```

### 4. 启动系统

```bash
python cli.py                  # 命令行界面
python -m webapp.server        # Web 界面：http://127.0.0.1:8000
```

Web 服务默认只监听本机，可用 `--host` / `--port` 调整（例如 `--port 8001`）。

---

## 子智能体详解 (Skills)

所有子智能体已重构为 **Skill Plugins**，位于 `.claude/skills/` 目录下，支持动态发现与加载。

### 1. MemoryQueryAgent (记忆查询智能体) 

- **职责**: 查询用户的历史记忆
- **查询内容**:
  - 旅行历史（trip_history）
  - 用户偏好（preferences）
  - 历史对话摘要（chat_history）
- **特点**:
  - 直接查询本地记忆，无需联网
  - 使用 LLM 生成自然语言回答
  - 支持复杂的记忆推理
- **示例**: "我过去去过哪些地方？"、"我上次去北京是什么时候？"

### 2. EventCollectionAgent (事项收集智能体)

- **职责**: 收集行程规划的核心信息
- **收集内容**: 出发地、目的地、出发时间、返程时间、出行目的
- **特点**: 主动推断缺失信息

### 3. PreferenceAgent (偏好管理智能体)

- **职责**: 识别和管理用户所有偏好
- **管理偏好**:
  - 酒店品牌、航空公司、座位偏好、房型偏好
  - 机型偏好、餐饮偏好、交通偏好、预算等级
  - 支持任意自定义偏好类型
- **智能模式**:
  - **追加模式**：识别「还」、「也」等关键词，追加到现有偏好
  - **覆盖模式**：识别「搬家到」、「改成」等关键词，替换旧偏好
  - **示例**: "我还喜欢汉庭" → 追加；"我搬家到上海" → 覆盖
- **特点**:
  - 感知当前已有偏好，避免重复
  - 所有偏好作为长期偏好持久化保存
  - 从对话中提取隐含偏好

### 4. InformationQueryAgent (信息查询智能体)

- **职责**: 实时信息检索（联网）
- **查询能力**: DuckDuckGo 搜索 + LLM 摘要
- **查询场景**: 天气、景点、实时新闻、通用问答

### 5. ItineraryPlanningAgent (行程规划智能体)

- **职责**: 生成完整行程计划
- **规划内容**: 每日时间表、住宿建议、餐饮建议、交通路线、注意事项
- **特点**: 即使信息不完整也给出合理建议

### 6. RAGKnowledgeAgent (知识库查询智能体)

- **职责**: 查询企业商旅知识库
- **技术栈**: Milvus Lite + BGE 中文向量模型
- **特点**: 提供文档溯源，返回参考来源

---

## CLI 使用指南

### 启动

```bash
python cli.py
```

启动时仅扫描技能元数据（懒加载），首次调用某个技能时才加载其代码。

### 内置命令

| 命令 | 说明 |
|------|------|
| `help` | 显示帮助信息 |
| `status` | 查看当前状态和记忆 |
| `health` | 检查 LLM 服务是否可用并显示熔断器状态 |
| `doctor` | 环境自检：依赖 / API Key / 嵌入模型文件 / 知识库 / LLM 连通性 |
| `clear` | 清空当前任务（保留长期记忆） |
| `history` | 查看历史行程 |
| `preferences` | 查看用户偏好 |
| `export` | 导出用户数据（偏好 / 行程 / 对话历史 / 统计）为 JSON + Markdown（保存到 `exports/`） |
| `exit` | 退出程序 |

单独做健康检查（不进入交互）：`python cli.py health`，返回 `OK` / `FAIL: ...`，退出码 0/1。

知识库状态（不进入交互）：`python cli.py kb`，输出集合名 / 片段数与数据库路径，退出码 0/1。

知识库检索评测（不进入交互、不调用大模型）：`python cli.py eval`，对 14 个差旅问题计算 hit@1 / hit@3 / hit@5 与 MRR（当前基线：hit@5 100%，MRR 0.80），hit@5 低于 80% 时退出码 1；知识库未初始化时提示跳过。

知识库导出 / 导入（不进入交互）：`python cli.py kb-export [path]` 导出全部片段为 JSON（默认 `exports/knowledge-export.json`）；`python cli.py kb-import <file.json>` 批量导入（单次最多 200 条），适合备份、换机与批量补充知识。

知识片段查看 / 删除（不进入交互）：`python cli.py kb-list [关键词]` 按 id 倒序列出片段（分类 + 60 字预览）；`python cli.py kb-delete <id>` 删除指定片段。

环境自检（不进入交互）：`python cli.py doctor`，逐项检查运行环境并给出修复提示，退出码 0/1（存在必须处理的问题时为 1）。

每次对话结束后会显示本次的 LLM 调用次数、token 用量与总耗时；`status` 命令可查看本次会话的累计用量。

执行过程中会实时显示当前正在运行的智能体（例如「正在执行 行程规划…」）；完成后智能体列表包含各自耗时（例如 `信息查询 ✓ 2.3s`），与 Web 执行路线一致。

> **排障提示**：启动时若出现 `No sentence-transformers model found ... mean pooling` 警告，说明本地模型目录缺少 `modules.json` / `1_Pooling/config.json` 等元数据文件（通常只复制了权重文件）。从 HuggingFace 下载完整模型仓库文件即可消除；`doctor` 会检查并提示。
>
> Milvus Lite 3.x 在**非 ASCII 路径**（例如用户名含中文的 `C:\Users\张三\...`）下可能无法写 Faiss 索引（`FileIOWriter ... No such file or directory`）。请把项目放在纯英文路径（如 `D:\Code\chailv`）后重新初始化知识库。

---

## Web 界面

除 CLI 外，项目提供完整的智能体对话界面（FastAPI 后端 + 无构建前端，无需 Node）：

```bash
pip install -r requirements.txt   # 已包含 fastapi / uvicorn
python -m webapp.server            # 安装后也可直接执行 aligo-web
```

打开 <http://127.0.0.1:8000> 即可对话。

**界面能力**

- **执行路线**：`/api/chat/stream` 通过 SSE 推送 `phase`（阶段）、`plan`（调度计划）、`agent_start` / `agent_done`（子智能体进度）与 `done`（最终结果 / 用量 / 耗时），前端实时渲染为路线图，每个智能体显示单独耗时；生成过程中可随时停止
- **路线回看**：每轮助手消息会附带 `_meta` 元数据（意图、调度计划、各智能体状态与耗时、用量），恢复历史会话时同样可展开「执行路线」
- **会话历史与搜索**：侧栏按日期分组展示历史会话，点击恢复完整对话（助手消息从编排结果重新渲染为 Markdown），悬停删除；搜索框按标题与消息内容全文检索，命中片段中高亮关键词
- **会话重命名**：侧栏铅笔按钮内联编辑标题（Enter 保存 / Esc 取消），自定义标题持久化在 JSON / PostgreSQL 中并参与搜索；清空标题恢复自动标题
- **会话置顶**：侧栏图钉按钮置顶 / 取消置顶，置顶会话进入「置顶」分组并排在日期分组之前；JSON / PostgreSQL 持久化
- **即将出行**：侧栏根据保存的行程日期显示最近一次即将开始的行程（路线 / 日期 / 目的），点击回到对应对话（无关联会话时打开记忆面板）
- **对话深链接**：地址栏同步 `user` / `session` 参数（例如 `/?user=alice&session=abc123`），打开链接即可恢复会话；顶栏提供「复制链接」，无效链接会自动清理参数并回到新对话
- **主题与布局**：浅色 / 深色主题跟随系统并可切换；窄屏下侧栏变为抽屉
- **记忆与用量面板**：在线编辑 / 删除偏好、查看最近 5 条行程（新记录可点击回到对应对话、可导出 .ics 日历）与常去目的地、统计、存储后端、模型用量（调用次数 / tokens / 平均耗时 / 常用智能体）与近 7 天 tokens 趋势图，修改用户 ID，导出长期记忆 JSON / Markdown
- **消息操作**：复制、重新生成；Markdown 支持标题、列表、引用、表格与代码块（带复制按钮）；会话可导出为 Markdown
- **跟进建议**：回答完成后根据本轮执行的智能体推荐 2–3 个下一步（如导出行程、查出发当天天气、调整行程），点击即可继续对话
- **打印 / 导出 PDF**：顶栏打印按钮调用浏览器打印；打印样式隐藏侧栏、输入区、执行路线与操作按钮，适合打印行程单
- **快捷键与键盘可达性**：`Ctrl/Cmd + K` 或 `/` 聚焦会话搜索，`Esc` 清空搜索 / 关闭面板；会话置顶 / 重命名 / 删除按钮支持键盘聚焦；记忆面板打开时聚焦到对话框内，`Tab` 在面板内循环，关闭后焦点回到触发按钮

**接口观测与限流**

- 所有 `/api/` 响应带 `X-Request-ID` 头（透传请求头中的值，否则自动生成 12 位十六进制）；错误详情也会附带请求 ID，便于排查。
- 系统自检：记忆面板「运行自检」调用 `/api/doctor`，展示 Python 版本、核心 / RAG 依赖、大模型配置、嵌入模型文件、知识库状态；勾选「检查 LLM 连通性」时额外发起一次真实健康检查（默认不请求网络）。
- 对话接口默认按「客户端 + 用户」限流 **30 次 / 分钟**，同时按客户端 IP 限流 **120 次 / 分钟**（用户 ID 由请求方提供、可轮换，IP 桶防止换 ID 绕过），超限返回 `429` 与整数秒 `Retry-After`；可用环境变量 `CHAT_RATE_LIMIT`（单用户次数）、`CHAT_RATE_LIMIT_IP`（单 IP 次数）与 `CHAT_RATE_WINDOW_SEC`（窗口秒数）调整。实现为进程内滑动窗口（`utils/rate_limit.py`），多实例部署时需替换为 Redis 等共享存储。
- 安全响应头：CSP（仅同源脚本）、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Permissions-Policy`；主题初始化脚本拆分为 `/static/theme.js` 以满足 CSP。`/docs`、`/redoc` 使用仅对文档页放宽的 CSP（允许 CDN 资源）；未处理异常返回的 500 同样带安全响应头与 `X-Request-ID`。

**接口**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 发送消息（一次性返回；`message` / `user_id` / `session_id`） |
| POST | `/api/chat/stream` | 发送消息（SSE 流式返回执行进度，最后返回完整结果） |
| GET | `/api/health` | 服务版本 / 模型 / 存储后端状态 |
| GET | `/api/knowledge` | 知识库状态（集合名 / 片段数 / 是否可用） |
| GET | `/api/doctor` | 环境自检（`network=true` 时包含 LLM 连通性检查） |
| GET | `/api/knowledge/documents` | 知识片段列表（`limit` / `offset` / `q`，按 id 倒序） |
| POST | `/api/knowledge/documents` | 新增知识片段（`content` / `metadata`；首次调用加载嵌入模型） |
| DELETE | `/api/knowledge/documents/{id}` | 删除知识片段 |
| GET | `/api/knowledge/export` | 导出全部知识片段为 JSON 文件（备份 / 迁移） |
| POST | `/api/knowledge/import` | 批量导入知识片段（`documents` 数组，单次最多 200 条） |
| GET | `/api/status` | 用户数据概览（偏好 / 最近行程 / 行程数 / 对话数 / 统计 / 存储后端 / 模型用量与近 7 天趋势） |
| PUT | `/api/preferences/{type}` | 新增或覆盖一条偏好（JSON 值，记忆面板编辑入口） |
| DELETE | `/api/preferences/{type}` | 删除一条偏好 |
| GET | `/api/sessions` | 历史会话列表（`q` 可选：按标题与消息内容搜索，返回命中片段） |
| GET | `/api/sessions/{id}` | 某个会话的消息（助手消息已渲染为 Markdown，并附执行元数据） |
| PUT | `/api/sessions/{id}/title` | 设置或清除会话自定义标题（空字符串恢复自动标题） |
| PUT | `/api/sessions/{id}/pin` | 置顶 / 取消置顶会话 |
| GET | `/api/sessions/{id}/export` | 把会话导出为 Markdown 附件 |
| DELETE | `/api/sessions/{id}` | 删除某个会话的聊天记录 |
| GET | `/api/export` | 导出用户长期记忆（`format=json` 默认 / `format=md`，附件下载） |
| GET | `/api/trips/{id}/calendar` | 导出行程为 iCalendar（`.ics`）全天事件 |
| DELETE | `/api/trips/{id}` | 删除一条历史行程（同步统计与常去目的地） |

> 说明：短期记忆按会话级管理——进程内实现随服务重启清空，配置 `REDIS_URL` 后由 Redis TTL 控制；长期记忆与历史会话在默认 JSON（`data/memory/`）或 PostgreSQL 中持久化。用户 / 会话 ID 限制为中文、字母、数字、下划线与短横线（避免路径穿越）。服务默认只监听 `127.0.0.1`，按单机演示用途设计。

---

## 测试

### 回归测试（无需 API Key）

使用确定性 mock 模型覆盖核心逻辑，CI 中自动运行：

```bash
pytest tests/ -q
```

### 端到端测试（需要真实大模型）

```bash
python tests/test_cli_qa.py        # 完整跑通所有意图的 QA 测试
python tests/test_memory_system.py # 记忆系统（含 LLM 总结）
```

### 存储后端集成测试（需要 Redis / PostgreSQL）

```bash
docker compose up -d
TEST_REDIS_URL=redis://localhost:6379/0 \
TEST_POSTGRES_DSN=postgresql://aligo:aligo@localhost:5432/aligo \
pytest tests/test_storage_integration.py -q
```

CI 中通过 service 容器自动启用；未配置环境变量时相关用例自动跳过。

### 代码检查

```bash
ruff check .   # 代码风格
mypy .         # 类型检查
```

---

## 项目结构

```
aligo-travel-assistant/
├── .github/
│   ├── workflows/ci.yml             # CI：ruff + mypy + pytest
│   ├── ISSUE_TEMPLATE/              # Issue 模板（Bug / 功能建议）
│   └── pull_request_template.md     # PR 模板
├── agents/                          # 核心编排层
│   ├── intention_agent.py           # 意图识别（语义理解）
│   ├── orchestration_agent.py       # 协调器（优先级 + 并行调度）
│   ├── lazy_agent_registry.py       # 智能体插件注册器（懒加载）
│   └── conversation.py              # 对话运行时（Web 界面的完整流水线）
├── .claude/skills/                  # Skill Plugins（子智能体）
│   ├── ask-question/                # RAG 知识库问答（含文档与向量库）
│   ├── event-collection/            # 事项收集
│   ├── plan-trip/                   # 行程规划
│   ├── preference/                  # 偏好管理
│   ├── query-info/                  # 联网信息查询
│   └── memory-query/                # 记忆查询
├── context/                         # 两层记忆（可插拔后端）
│   ├── memory_manager.py            # 记忆管理器（统一门面）
│   ├── storage.py                   # 存储工厂（Redis / PostgreSQL 自动启用与回退）
│   ├── short_term_memory.py         # 短期记忆（进程内 / Redis）
│   ├── long_term_memory.py          # 长期记忆（JSON 文件）
│   └── postgres_long_term_memory.py # 长期记忆（PostgreSQL）
├── utils/                           # 工具与稳定性
│   ├── circuit_breaker.py           # 熔断器
│   ├── llm_resilience.py            # 重试退避、健康检查
│   ├── json_parser.py               # 鲁棒 JSON 解析
│   ├── result_formatter.py          # 编排结果 → Markdown
│   ├── turn_meta.py                 # 执行路线元数据（意图 / 调度 / 用量）
│   ├── progress.py                  # 智能体进度事件 → CLI 文本
│   ├── exporter.py                  # 用户数据导出（JSON / Markdown）
│   ├── rate_limit.py                # 对话接口进程内滑动窗口限流
│   ├── milvus_client.py             # 进程内共享 Milvus Lite 客户端
│   ├── knowledge_stats.py           # 知识库集合 / 片段数状态
│   ├── text_chunker.py              # RAG 文本按小节切分
│   ├── doctor.py                    # 环境自检
│   └── skill_loader.py              # SKILL.md 加载器
├── webapp/                          # Web 界面（FastAPI）
│   ├── server.py                    # API 服务（对话 / SSE 流式 / 会话历史 / 导出）
│   └── static/                      # 无构建前端（HTML / CSS / JS）
├── docs/
│   └── screenshots/                 # README 界面截图
├── tests/                           # 测试（多数无需 API Key）
├── data/                            # 运行数据（均不提交）
│   ├── memory/                      # 长期记忆（{user_id}.json，gitignored）
│   └── models/bge-small-zh-v1.5/    # 本地向量模型（gitignored，需自行下载）
├── docker-compose.yml               # 本地启动 Redis + PostgreSQL（可选）
├── cli.py                           # CLI 主程序
├── config.py                        # 配置（模型 / 容错 / RAG）
├── config_local.py                  # 本地密钥（自行创建，gitignored）
├── config_agentscope.py             # AgentScope 初始化
├── pyproject.toml                   # 打包与 ruff/mypy/pytest 配置
├── requirements.txt                 # 运行依赖
├── requirements-ci.txt              # CI 轻量依赖
├── CONTRIBUTING.md                  # 贡献指南
└── README.md                        # 本文件
```

---

## 技术栈总览

### 核心框架
- 📦 **AgentScope 1.0.16** - 多智能体框架
- 🤖 **DeepSeek (deepseek-chat)** - 大语言模型（OpenAI 兼容接口，可切换豆包 / OpenAI 等）

### 数据存储
- 💾 **JSON 文件** - 长期记忆默认实现（`data/memory/{user_id}.json`）
- 🗄️ **PostgreSQL** - 长期记忆可选后端（设置 `DATABASE_URL` 启用，表结构自动创建）
- ⚡ **Redis** - 短期记忆可选后端（设置 `REDIS_URL` 启用，List + TTL）
- 🔍 **Milvus Lite** - 本地向量数据库（RAG 知识库）

### 向量化与检索
- 🧠 **BGE-small-zh-v1.5** - 中文Embedding模型（本地部署）
- 📚 **Sentence-Transformers 5.2.3** - 向量化工具库
- 🎯 **余弦相似度检索** - Top-K检索算法

### 联网与搜索
- 🌐 **DuckDuckGo (DDGS 9.10.0)** - 免费网络搜索引擎
- 📝 **LLM自动摘要** - 搜索结果智能提取

### 架构设计
- 🏗️ **Skill Plugins插件化架构** - 独立开发、测试、部署
- 🔄 **LazyAgentRegistry动态发现** - 自动扫描注册Agent插件
- ⚡ **懒加载机制** - 未使用的 Skill 不加载，启动时仅扫描技能元数据
- 🔀 **Progressive Disclosure渐进式暴露** - 意图识别阶段仅加载元数据，执行阶段按需加载
- 🎯 **优先级+并行混合调度** - asyncio.gather并发执行

### 稳定性保障
- 🔁 **指数退避重试** - 自动重试失败请求（最大3次）
- 🩺 **熔断器机制** - 连续失败后暂停调用
- 💊 **健康检查** - 实时监控LLM服务可用性

### 用户界面
- 🖥️ **Rich 13.9.4** - 精美的CLI终端界面

---

## ⚠️ 注意事项

### 模型配置
- 必须配置大模型API密钥（默认 DeepSeek：环境变量 `LLM_API_KEY`，或本地 `config_local.py`）
- 默认使用 `deepseek-chat`（响应快、成本低），可在 `config.py` / 环境变量中切换模型与服务商
- **兼容性说明**：agentscope 1.0.16 的流式解析在 DeepSeek 等标准 OpenAI 兼容服务上会报错（上游对 `delta.audio` 的判断缺陷），项目统一使用**非流式**调用；`temperature` / `max_tokens` 通过 `generate_kwargs` 传入（顶层参数会被静默忽略）。模型统一由 `config_agentscope.create_chat_model()` 创建
- BGE Embedding模型需下载到 `data/models/bge-small-zh-v1.5/`（仓库不含模型文件）：
  - HuggingFace: https://huggingface.co/BAAI/bge-small-zh-v1.5
  - ModelScope（国内更快）: https://modelscope.cn/models/BAAI/bge-small-zh-v1.5
  - 将仓库内全部文件放入该目录即可

### 数据存储
- **默认（开箱即用）**：短期记忆为进程内滑动窗口，长期记忆为 JSON 文件（`data/memory/{user_id}.json`）
- **生产模式（可选）**：设置 `REDIS_URL` 启用 Redis 短期记忆（List + 滑动窗口 + TTL）；设置 `DATABASE_URL` 启用 PostgreSQL 长期记忆（表结构首次连接自动创建）；未配置或连接失败时自动回退默认实现（失败后 60 秒冷却，不阻塞启动）
- **本地一键启动依赖**：`docker compose up -d`（见 `docker-compose.yml`），然后：

  ```bash
  export REDIS_URL=redis://localhost:6379/0
  export DATABASE_URL=postgresql://aligo:aligo@localhost:5432/aligo
  python cli.py        # 或 python -m webapp.server
  ```

### 知识库初始化
- 首次运行前必须初始化RAG知识库
- 知识库文档位于 `.claude/skills/ask-question/data/documents/`
- Milvus数据库文件生成在 `.claude/skills/ask-question/data/rag_knowledge/milvus_lite.db`
- **Windows 说明**：`requirements.txt` 会按平台自动选择依赖（非 Windows 用 `pymilvus 2.6.9 + milvus-lite 2.x`，Windows 用 `pymilvus 3.x + milvus-lite 3.x`）。如果目录中已有旧版（2.x 时代）生成的 `milvus_lite.db` 文件，需先删除再运行初始化脚本（两个版本的数据文件互不兼容；该文件为可再生文件）

### 性能优化
- 懒加载机制：系统启动时仅扫描 Skill 元数据，首次调用时才加载
- 并行调度：同优先级 Agent 并发执行，缩短多信息收集类请求的总耗时
- 长期记忆总结缓存：同一会话内其它会话历史未变化时复用上次总结，避免每轮多一次 LLM 调用
- 存储连接复用：PostgreSQL 后端不再每轮对话重建连接；Web 会话缓存按 LRU 淘汰（上限 50）并在服务关闭时释放

### 已知限制
- **Milvus Lite 是单进程文件锁**：Web 服务运行且已打开知识库后，外部再运行 `init_knowledge_base.py` / `cli.py kb*` / `doctor` 会提示锁占用；请先停止 Web 服务再执行这些命令。
- **非 ASCII 路径**：Milvus Lite 3.x 在含中文等非 ASCII 字符的目录下可能无法写入 Faiss 索引，建议把项目放在纯英文路径（如 `D:\Code\chailv`）。
- **限流是进程内实现**：多实例部署时各实例独立计数，需替换为 Redis 等共享存储。
- **本地 JSON 长期记忆**：同进程写入已加锁并在修改前重载；但 CLI 与 Web 服务同时修改**同一用户**时仍是「后写覆盖先写」，演示时建议只保留一个写入端。
- **模型回答非流式**：受 agentscope 1.0.16 与 DeepSeek 流式解析兼容性限制，回答在完成后一次性返回（进度通过 SSE 实时展示）。
- **知识库导出**单次最多 16384 条（Milvus 单次查询上限）；导入接口每次最多 200 条（Web 端自动分批上传）。

---

## 🚀 未来规划

- [x] 完整实现PostgreSQL持久化存储（可选后端，`DATABASE_URL` 启用）
- [x] 完整实现Redis短期记忆（可选后端，`REDIS_URL` 启用）
- [x] Web 界面升级（SSE 流式执行进度、会话历史、深色模式；保持无构建前端）
- [ ] 支持更多LLM模型（OpenAI、Claude等）
- [ ] 更多Skill插件（酒店预订、机票查询等）
- [ ] 监控和日志系统

---

## 许可证

MIT License
