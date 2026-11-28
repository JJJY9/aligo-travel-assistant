# Aligo 智能旅行助手

[![CI](https://github.com/JJJY9/aligo-travel-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/JJJY9/aligo-travel-assistant/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

基于**大语言模型**（默认 DeepSeek，兼容豆包 / OpenAI 等 OpenAI 兼容接口）和 **AgentScope 框架**的多智能体旅行规划系统，采用Plan-and-Execute架构，实现智能意图识别、两层记忆系统、RAG知识库、联网搜索和优先级并行调度。

> **English TL;DR** — Aligo is a local-first, multi-agent travel-planning assistant built with **FastAPI + AgentScope** and any OpenAI-compatible LLM (DeepSeek by default). It combines a two-tier memory system (in-process/Redis short-term, JSON/PostgreSQL long-term), a **Milvus Lite RAG** knowledge base with web maintenance (category filter, semantic search, CRUD, JSON export/import), parallel Plan-and-Execute agents with **live SSE progress**, and a dependency-free web UI (session history with search / pin / archive, memory/usage panel with manual preferences, trip management with calendar / CSV / ICS export, CSV / ICS import and keyword filtering, JSON backup export & idempotent restore (with dry-run preview), knowledge maintenance, self-diagnostics). A CLI covers maintenance tasks (`preferences` / `pref-set` / `pref-delete`, `trips` / `trip-add` / `trip-update` / `trip-delete` / `trip-calendar` / `trips-dedupe` / `trips-export` / `trips-import`, `sessions` / `session-archive` / `session-delete` / `session-title` / `session-pin` / `session-export` / `sessions-export`, `user-export` / `user-import`, `kb-*`, `eval`, `doctor`), responses are gzip-compressed except SSE, carry rate-limit headers and are exposed as in-process request metrics (`/api/metrics`). Tests run without API keys (`pytest tests -q`); CI covers Python 3.10/3.12 with Redis and PostgreSQL service containers.

> **实现现状**：本仓库是可直接运行的**本地实现**——短期记忆默认进程内滑动窗口，长期记忆默认 JSON 文件（`data/memory/`），RAG 使用 Milvus Lite 本地向量库。Redis / PostgreSQL 为**可选生产存储后端**（配置 `REDIS_URL` / `DATABASE_URL` 启用，未配置或连接失败时自动回退）；关键指标为开发期实验口径（见「关键指标」小节说明）。

## 🚀 v2.0 Personal Agent 能力

在原有「多智能体 + 两层记忆 + RAG」之上，v2.0 增加了以下可验证能力（均有回归测试）：

- **混合检索**：向量 + BM25（中文按字符二元组，无需分词器）双路召回，RRF 融合；可选 `RAG_RERANK_MODEL` 做 cross-encoder 精排（默认关闭）。BM25 按 IDF 加权覆盖度 × 绝对证据量归一（覆盖全部查询词才接近满分；只命中一部分查询词、或重复单个查询词都不会满分绕过 `min_score`；未在库的查询词按 df=0 计入分母；单字中文查询无法组成二元组，关键词路退化为纯向量），索引进程内缓存、写入 / 删除即时失效，构建 / 重排失败自动回退纯向量；重排模型进程级共享、冷加载单飞且有界等待（加载中本次检索按 RRF 顺序降级，不会把多个请求线程钉在分钟级下载上，也不会因其它会话的瞬时失败永久关闭本会话精排）；首轮结果全部低于阈值时用去掉疑问词 / 标点的简化查询重检索一次，仍无命中才拒答；精排只对通过 `min_score` 的候选排序（证据门槛先于精排 / 截断，不会把有证据的片段挤出 top-K）；管理 / 评测接口保持纯向量口径。
- **行程约束求解与校验**：需求提取（预算 / 必去项 / 天数）→ 必去项按天分配（OR-Tools CP-SAT 可选，未安装时确定性贪心）→ 五项校验（天数、时间冲突、预算、必去项、费用复核）；未通过时带原因重生成一次，仍失败保留原行程并返回 `validation`。
- **固定 DAG 调度**：行程规划依赖事项收集 / 偏好抽取；`trip-budget` / `local-transport` 依赖行程规划，`packing-list` / `travel-checklist` / `safety-tips` / `trip-notes` 依赖事项收集，优先级只上调不下调（迭代处理传递依赖）；事项缺失信息随结果返回 `missing_info` / `clarification`，不阻断规划。
- **持久化执行与防重**：`/api/chat` / `/api/chat/stream` 支持 `idempotency_key`（重复提交直接回放成功响应）；编排按 `user:session:turn_id` 落执行检查点到 `data/runtime/checkpoints`，失败重试 / 重启复用已成功的子智能体结果、只重跑失败者（复用要求两轮执行的计划字段——`agent_name` / `priority` / `reason` / `expected_output`——逐字段一致，且该步骤实际可见的前序结果 / 用户偏好 / 抽取实体 / 重写后的查询未变化；意图重新生成后措辞变化或上游重跑都会让对应步骤按未命中重跑，日志会记录「摘要不匹配」）；检查点超过上限时按用户公平淘汰（优先只淘汰条目最多的用户自己的最旧文件；最多条目数在多个用户间并列时退回淘汰全局最旧条目，属尽力而为——活跃用户数达到上限时没有绝对保证）；键绑定本轮提问内容，成功落库后清理，删除会话时按 `user:session` 作用域清理（落盘只存哈希，不等 TTL）。
- **记忆一致性**：最近 10 轮对话生成 ≤200 字滚动摘要（窗口指纹缓存、失败冷却）；PostgreSQL 后端偏好快照 5 秒缓存，写入后立即失效（配置 Redis 时按共享版本键跨进程即时失效）。
- **12 个技能**：原 6 个技能 + `trip-budget` / `packing-list` / `travel-checklist` / `local-transport` / `safety-tips` / `trip-notes`，统一 `SKILL.md` + 懒加载，意图阶段只暴露元数据。
- **评测骨架**：`utils/eval_harness.py`（规则验收 + 可选 LLM-as-Judge ≥0.6、Pass@1 / 分类准确率 / 失败原因）与 `utils/eval_cases.py`（18 类场景确定性生成 500 条分层用例，JSONL 导出）。
- **工程细节**：备份导入导出 50MB 可恢复性（原子写、隔离备份、跨进程锁、PG advisory lock、并发导入互斥）、固定 DAG 与检查点、2300+ 测试、CI 双 Python 版本。

> 说明：以上为已落地的工程能力；真实模型跑分需要 `LLM_API_KEY`，仓库不附带线上指标。

## 界面预览

| 对话与执行路线（浅色） | 深色模式 |
|---|---|
| ![对话与执行路线](docs/screenshots/webui-answer-light.jpg) | ![深色模式](docs/screenshots/webui-answer-dark.jpg) |
| **记忆与用量面板**（行程 / 统计 / 7 天趋势） | **欢迎页**（会话搜索 / 置顶） |
| ![记忆与用量面板](docs/screenshots/webui-memory.jpg) | ![欢迎页](docs/screenshots/webui-welcome.jpg) |
| **会话归档**（已归档视图 / 恢复） | **快捷键帮助**（按 `?` 打开） |
| ![会话归档](docs/screenshots/webui-archive.jpg) | ![快捷键帮助](docs/screenshots/webui-shortcuts.jpg) |

![知识库维护：查看 / 新增 / 编辑 / 删除知识片段](docs/screenshots/webui-knowledge.jpg)

*知识库维护：在记忆面板中查看、按分类筛选、新增、编辑、删除 RAG 知识片段，新增 / 编辑后立即可被检索。*

界面为 **FastAPI + 无构建前端**（原生 HTML / CSS / JS，无需 Node），支持流式执行进度、
会话搜索 / 重命名 / 置顶 / 归档、记忆与用量面板、知识库维护、深浅主题与移动端布局，启动方式见「Web 界面」小节。

## ✨ 核心亮点

### 🎯 智能意图识别
- 基于LLM语义理解的多意图识别（准确率90%+，对比关键词匹配提升25%）
- 支持6大类意图：行程规划、记忆查询、偏好管理、知识问答、信息查询、事项收集
- 自然语言理解，无需关键词匹配

### 🧠 两层记忆架构
- **短期记忆**：默认进程内滑动窗口（最近 10 轮对话）；配置 `REDIS_URL` 后使用 Redis（支持跨进程会话共享，键按用户 + 会话隔离）
- **长期记忆**：默认 JSON 文件持久化（`data/memory/{user_id}.json`）+ LLM 异步总结；配置 `DATABASE_URL` 后使用 PostgreSQL
- **最近对话滚动摘要（v2.0）**：MemoryManager 对最近 10 轮对话生成 ≤200 字摘要（按窗口内容指纹缓存、失败 30 秒冷却、输出 sanitize），与长期总结一起注入上下文；窗口不变时不重复调用模型
- **偏好快照缓存（v2.0）**：PostgreSQL 后端下 `get_preferences` 带 5 秒快照缓存（深拷贝），编排层写入偏好后立即失效；面板 / 导入直写时清空本进程所有已建会话的缓存；JSON 后端不缓存。配置 `REDIS_URL` 时，PostgreSQL 存储在偏好写入 / 删除 / 清空后推进共享版本键（`aligo:prefver:<用户>`，INCR 后 EXPIRE 1 天），各实例 / CLI 写入后其它进程下一次读取即按版本变化失效，不再等满 5 秒；版本读写失败进入 60 秒本地冷却（不会让故障期每次偏好读取 / 写入都等满 socket 超时）并退回 TTL + 本进程失效
- 智能识别偏好追加/覆盖动作（"我还喜欢如家" vs "我搬家到上海了"）

### 📚 RAG知识库
- Milvus Lite 本地向量库 + BGE-small-zh-v1.5 中文向量模型（本地部署）
- 按小节切分（Chunking，保留章节边界）+ 余弦相似度检索（Top-K=5）
- 知识溯源：返回文档来源（`metadata` 中保留原始文档路径），并在回答中附 Top 3 参考片段
- 进程内按数据库路径共享 Milvus Lite 客户端：多个 Web 会话 / 智能体不会重复打开同一本地库，避免文件锁冲突
- 知识库状态：`GET /api/knowledge`、`python cli.py kb` 与 Web 记忆面板显示集合名 / 片段数，便于确认知识库是否就绪；接口同时返回集合向量维度，进程内已加载嵌入模型时按实际维度、冷进程（重启后尚未检索 / 独立 CLI）按模型目录 `1_Pooling/config.json` 的配置维度附带 `dimension_mismatch`（换模型后不必等到问答报错才发现，`cli kb` 此时退出码 1，可直接当健康门禁）
- 知识库维护：记忆面板可搜索 / 分页查看、新增、编辑、删除知识片段（`GET/POST/PUT/DELETE /api/knowledge/documents`；长内容自动按语义边界拆分并带 `group` 标识，任一分片可**编辑整份文档**或**整组删除**），支持按分类筛选（`category` 参数与 `/api/knowledge/categories` 统计）与**语义检索**（`/api/knowledge/search`，与聊天共用向量模型），并可运行**检索评测**（`/api/knowledge/eval`，30 个问题（覆盖全部 8 份随仓库知识文档）计算 hit@1/3/5 与 MRR，不调用大模型，同一时间只允许一个评测：配置 `REDIS_URL` 时通过 Redis 锁跨实例共享，未配置或 Redis 故障时回退进程内；评测收尾前会只读复核一次锁归属（主从切换丢键等场景在心跳间隔内也能发现），失效时在结果 `warning` 中标注），面板「查重复」按钮与 `GET /api/knowledge/duplicates` 可列出内容完全相同的独立片段组（与 CLI `kb-duplicates` 同一实现，确认后到列表删除多余片段），「清理重复」按钮走 `POST /api/knowledge/duplicates/cleanup`（默认 dry-run 预览，确认后每组保留最早一条删除其余，与 CLI `kb-dedupe` 同一语义），导出 / 导入 JSON 备份（`/api/knowledge/export` / `import`；接口单次最多 20000 条（面板按 200 条分批、多分片组整组同批），CLI 不限条数；重复导入同一备份会按内容跳过已有片段）；新增 / 编辑后立即可被检索，删除后再新增按最大 id 递增，不会与已有主键冲突
- 检索评测：`python cli.py eval` 用 30 个差旅问题（覆盖全部 8 份随仓库知识文档）离线计算 hit@1/3/5 与 MRR（只检索、不调用大模型，纯向量 top-K 口径，不含 BM25 / 混合召回——实际聊天可用率可能更高），便于调整切分策略或补充知识后回归；Web 知识库维护面板也可直接「检索评测」

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
- **会话历史与搜索**：历史会话按日期分组、可恢复 / 删除，超过一页时提供「加载更多会话」（已显示 X / 共 Y），并支持按标题与消息内容全文检索（带命中片段）；点击搜索结果会定位并短暂高亮命中的那条消息；打开大历史时首屏只渲染最近 200 条，顶部「显示更早消息（还有 N 条）」按批补入（不再请求网络），补入时保持当前阅读位置；地址栏同步 `user` / `session` 参数，可直接打开或分享某个会话（顶栏「复制链接」）
- **现代交互**：深浅主题、停止生成、重新生成、复制回答、Markdown 表格 / 代码块、移动端抽屉布局
- **会话导出**：单个会话可导出 Markdown（附件）或单文件 HTML（内嵌样式，适合分享 / 打印 / 离线查看）
- **记忆与用量面板**：在线编辑 / 删除偏好、查看最近行程与统计、手动补录与编辑行程（出发地 / 目的地 / 日期 / 目的）、导入 CSV / iCalendar 行程文件（UTF-8 / GBK 自动解码，2 MB 上限，先预览新增 / 更新数量与跳过 / 跨行告警再确认；定时事件按浏览器上报的时区取日期）、删除误记录的行程（同步更新统计与常去目的地；同一行程重复添加自动去重）、按关键词 / 状态筛选导出行程台账（CSV / HTML / 日历 `.ics`，与 CLI `trips-export` 同口径；导出前做回导自检，返程日期无法解析 / 回导会被跳过的条数经响应头在面板提示；单条行程也可单独加入日历）、一键清理重复行程（同路线 + 同起止日期保留最早一条，与 CLI `trips-dedupe` 同语义；先预览数量再确认）、存储后端、模型用量（调用次数 / tokens / 平均耗时 / 常用智能体），一键导出 JSON / 从 JSON 备份恢复（可先预览）；单个会话可导出 Markdown

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

流内错误以 `error` 事件返回，文案分三类：熔断中「服务暂时不可用」、存储故障「存储暂时不可用」、以及意图 / 编排解析失败等固定用户提示；其余未预期异常只写服务端日志，客户端收到「服务器内部错误」与请求 ID（用于对照日志），不会透出 SQL、文件路径或上游响应体等内部细节。非 SSE 的对话接口把意图 / 编排解析失败等固定用户提示按 **502** 返回（整轮重试预算耗尽为 **503** + `Retry-After`）——它们来自上游 / 模型侧、客户端按原样重试即可，不再用 400（请求非法）表示；导入 / 导出 / 去重等维护类接口在存储故障时返回 503 固定可重试文案（底层原因只写日志，不回显 SQL / 路径），容量上限（偏好 / 行程 / 消息 / 知识片段）返回 409，请求内容非法才是 400。

### 连接与可用性

为保证 LLM 服务不稳定时的可用性，在调用链外增加了以下机制（不改变原有业务逻辑）：

| 机制 | 说明 |
|------|------|
| **熔断器** | 连续失败若干次后暂停调用 LLM，直接提示「服务暂时不可用」；一段时间后自动半开试探恢复（半开只放行一个试探请求，试探名额按归属任务 / 线程归还）。状态快照为进程内变量，多实例各自统计。 |
| **重试与退避** | 对意图识别、编排两次 LLM 调用做有限次重试，仅对超时、429、5xx 等可重试错误生效，采用指数退避；上游返回 `Retry-After`（秒数或 HTTP 日期）时按指示等待，仍受 `retry_max_delay_sec`（默认 30 秒）约束。本地存储类错误（磁盘满 / 只读 / I/O 错误）与 429 配额耗尽（`insufficient_quota`）不重试，后者按永久故障记账。外层整轮重试与内层逐智能体重试叠加时，单回合受 `turn_retry_budget_sec`（默认 300 秒）总预算约束：预算耗尽后抛出最后一次真实异常，不再发起新的尝试（不打断在途调用）；编排内的逐智能体重试与串行优先级批次共享同一截止时刻，预算不足一次退避时立即失败，不再先睡到截止时刻。 |
| **健康检查** | 会话内输入 `health` 可查看熔断状态并探测 LLM 是否可达；命令行执行 `python cli.py health` 可单独做一次探测（退出码 0/1，便于监控），`--json` 输出 `{"ok", "message"}`；交互模式 `health` / `doctor` 失败同样计入管道模式退出码。网关以 HTTP 200 返回错误对象（`{"error": …}` 等）时同样判为不可用，不会因「响应非空」给监控假绿。 |

配置见 `config.py` 中的 `RESILIENCE_CONFIG`（重试次数、熔断阈值、恢复时间等）。

---

## 📊 关键指标

| 指标 | 优化前 | 优化后 | 提升幅度 |
|------|--------|--------|----------|
| 意图识别准确率 | 65% | 90%+ | +25% |
| 知识库问答准确率 | - | 95% | 新增功能 |
| 用户偏好记忆准确率 | - | 95% | 新增功能 |
| 系统响应时间 | 30秒 | 15秒 | -50% |
| 用户偏好缓存命中率 | - | 可观测 | `/api/metrics` 的 `preference_cache` 计数（hits / misses / version_invalidations；版本失效是未命中的子集） |
| 系统启动速度 | 未优化 | 秒级（懒加载） | 启动仅扫描元数据 |

> **指标说明**：以上数据为开发期内部实验口径，仓库未附带对应评测脚本；「缓存命中率」已由 `/api/metrics` 的 `preference_cache` 计数呈现（PostgreSQL 后端的进程内偏好快照缓存，多实例各自统计），此前标注为未实现（存储层已支持 Redis / PostgreSQL，见「数据存储」）。
>
> **v2.0 评测骨架**：`utils/eval_harness.py` 提供规则验收（意图集合完全匹配、必需 / 禁用关键词、可选 LLM-as-Judge ≥0.6）与总体 Pass@1 / 分类准确率汇总；`utils/eval_cases.py` 可按 18 类场景确定性生成 500 条分层用例并导出 JSONL。仓库暂未附带真实模型跑分（需要 API Key）。

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
- 🎯 **用户偏好管理**：支持动态添加任意偏好类型，智能识别追加/覆盖动作；面板「添加偏好」对酒店品牌 / 航空公司等列表型偏好为追加合并（行内「编辑」为整值覆盖；数据概览尚未加载 / 加载失败时没有合并基线，会提示稍后重试而不会整值覆盖已有列表），单值上限 10000 字符（序列化后按码点计，与 CLI / 导入 / API 同口径），值嵌套不超过 16 层（Web PUT / CLI / 备份恢复同一上限——恢复更深的值会让面板「未修改直接保存」422）
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
- **Embedding模型**: 默认 BGE-small-zh-v1.5（中文向量化，本地部署 `data/models/bge-small-zh-v1.5`）；可用 `RAG_EMBEDDING_MODEL` 指向其它本地模型（如 BAAI/bge-m3），维度按模型配置探测；问答阈值 `RAG_MIN_SCORE` 可覆盖默认 0.5（钳制 0–1）
- **文档处理**: 按小节切分（Chunking，章节 / FAQ 边界强制断块，块长 ≤ 400 字）
- **检索算法（v2.0 混合检索）**: 向量余弦 + BM25（中文按字符二元组切词，无需分词器）双路召回，按 RRF 融合取 Top-K；可选 `RAG_RERANK_MODEL`（如 `BAAI/bge-reranker-base`）对候选做 cross-encoder 精排（默认关闭，不额外下载模型）。BM25 按 IDF 加权覆盖度 × 绝对证据量归一（部分覆盖 / 重复单个查询词不会满分绕过阈值；未在库的查询词按 df=0 计入分母），索引按知识库进程内缓存（`RAG_BM25_TTL_SEC`，写入 / 删除即时失效）；内存约为每片段 20–25KB（2 万片段约 0.4–0.5 GB），首次构建在请求线程同步完成，片段数超过 `RAG_BM25_MAX_DOCS` 时自动退回纯向量；首轮全被 `min_score` 过滤时用简化查询重检索一次。管理 / 评测接口保持纯向量口径
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
- 🔗 **固定 DAG 依赖（v2.0）**：行程规划必须在事项收集 / 偏好抽取之后一批执行（LLM 给的优先级只会被上调、不会被下调，传递依赖迭代校正）；事项收集的缺失字段随结果返回 `missing_info` / `clarification`，供前端提示补充而不阻断规划
- 🎯 **动态调度**：根据意图识别结果动态分配优先级
- 📈 **性能提升**：系统响应时间从30秒优化到15秒（-50%）

### 6. 行程约束校验（v2.0）

行程生成后做五项确定性检查：天数与要求一致、每日活动时间不重叠且区间合法、预计费用不超预算、必去项出现在行程中、分项费用与申报总额一致（±20%）。约束从用户原话与事项收集结果提取（预算支持「元 / 万元 / 千 / k / 千分位 / 中文数字」（含「两千五 = 2500」「一万五 = 15000」的位值写法）且不跨并列数字拼接、显式「预算 / 总预算」优先，未被总预算限定时分项花费（机票 / 酒店 / 住宿 / 交通 / 门票等）不当作总预算、时间数字与「N人分摊」不算金额；必去项支持顿号 / 逗号 / 「和 / 与 / 或」枚举拆分与后置表达「西湖、灵隐寺都要去 / 故宫必须去」，忽略「不想去 / 不太想去 / 不一定要去」等否定语境，「想去爬山 / 泡温泉 / 吃火锅」等动作诉求不算必去项、超过 200 字的长枚举完整保留，且「特别 / 分别 / 别人」里的「别」不算否定；天数支持小数（向上取整）与「一周」，跳过「第一天 / 5日到7日」这类序数与日期区间；时间区间支持跨午夜与「次日 02:00 / 24:00」；费用字段支持万 / 千 / 千分位，优先「共 / 合计」金额而不是人数；必去项只核对活动的名称 / 地点字段，备注与主题里顺带提到的地名不算已安排；activities 支持列表与「键→活动」字典 / schedule 别名键）。行程落库时目的地按行政后缀归一（「杭州」与「杭州市」视为同一趟，不重复建档）；同一趟行程再次规划但本轮缺返程日期 / 时长时沿用既有行程的真实返程与出发地，不按单日身份重复插入。生成前先用确定性求解器（可选 OR-Tools CP-SAT，未安装时贪心；天数上限 366）把必去项分配到每日时间窗（默认 09:00–18:00、单点 2 小时、通勤 30 分钟），把建议与「未安排项」注入 prompt（排不下时提示保留可行分配而不是硬塞）。校验未通过时把问题码与原因回填 prompt 重生成（`PLAN_REPAIR_ENABLED` 默认开、`PLAN_REPAIR_ATTEMPTS` 默认 1 次、上限 3；多次重试基于最近一次修复版收敛，上一版行程按整天结构感知裁剪、始终是合法 JSON），仍失败则保留原行程并随结果返回 `validation`（`passed` / `issues`），不阻断对话；展示层（实时回复 / CLI / 历史重渲染）会追加「⚠️ 未通过行程校验」与问题列表。

---

## 快速开始

### 1. 安装依赖

```bash
# 使用 requirements.txt 安装所有依赖
pip install -r requirements.txt

# 或者手动安装核心依赖（Windows 用户注意下方向量数据库的平台差异）
pip install "setuptools>=69.0.0,<82"  # milvus_lite 依赖
pip install agentscope==1.0.16        # 多智能体框架
# agentscope 1.0.16 的元数据未声明以下隐含依赖（缺失会导致 import 失败）：
# mcp 必须保持 <2（2.x 移除了 streamablehttp_client），greenlet 供 SQLAlchemy asyncio 使用
pip install mcp==1.30.0 tqdm==4.70.1 greenlet==3.5.6
pip install PyYAML==6.0.3             # 技能 frontmatter 解析（utils/skill_loader.py 顶层 import）
# 向量数据库（macOS / Linux）：milvus-lite 2.x 没有 Windows wheel
pip install "pymilvus[milvus_lite]==2.6.9"
# 向量数据库（Windows）：改用 pymilvus 3.x + milvus-lite 3.x（与 requirements.txt 一致）
pip install "pymilvus==3.0.3" "milvus-lite==3.2.2"
pip install sentence-transformers==6.3.0    # Embedding模型
pip install rich==15.0.0                    # CLI界面
pip install fastapi==0.162.0 uvicorn==0.54.0  # Web 界面
pip install tzdata==2026.6                  # Windows / 无系统时区库的容器：ICS 的 TZID 需要
pip install ddgs==9.16.1                    # 网络搜索
pip install redis==8.1.0 "psycopg[binary]==3.3.9"  # 生产存储后端（启用 REDIS_URL / DATABASE_URL 前安装）
```

也可以直接安装 [Releases](https://github.com/JJJY9/aligo-travel-assistant/releases) 中的 wheel（已包含 Web 静态资源与 `.claude/skills` 技能代码，普通安装时技能位于虚拟环境根目录，`pip install --user` 时位于用户目录）。**注意**：Releases 上最新正式发布仍是 v1.6.0（6 个技能）；下文 v2.0 能力（12 技能、固定 DAG、行程约束校验等）已合入 main、尚未发布新 wheel，需要这些能力时请按上面的方式从源码安装，或从 main 自行构建 wheel：

```bash
# 从 Releases 页面下载 wheel 后安装（文件名以页面实际文件为准）
pip install ./aligo_travel_assistant-<版本>-py3-none-any.whl
aligo-web        # 启动 Web 界面（也可用 python -m webapp.server）
aligo            # 启动交互式 CLI
```

wheel 安装后**数据目录不再写入安装前缀**（site-packages 会随 venv 重建 / `pipx reinstall` 整体消失）：默认使用用户数据目录（Windows `%LOCALAPPDATA%/aligo/data`，其它 `$XDG_DATA_HOME/aligo/data` 或 `~/.local/share/aligo/data`），可用 `ALIGO_DATA_DIR` 指定；源码目录运行时仍为 `<仓库>/data`。`config_local.py` 按**当前运行目录**显式加载，console script（`aligo-web`）启动也能生效。

安装后可访问 `/api/health` 确认 `skills.count` 与 `aligo skills` 列出的技能数一致（当前源码目录 / 由本仓库源码构建的 wheel 为 12 个；Releases 上的 v1.6.0 wheel 为 6 个）（或运行 `aligo skills` / `aligo doctor` 查看「技能目录」检查；源码目录下等价命令为 `python cli.py skills` / `python cli.py doctor`）；若为 0，用环境变量 `ALIGO_SKILLS_DIR` 指向技能目录。

RAG 知识库文档、嵌入模型与记忆数据属于运行时数据，不随安装包分发，需要按「知识库初始化」自行准备；技能目录可用环境变量 `ALIGO_SKILLS_DIR` 覆盖。

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
- 网络代理：LLM / 天气 / 嵌入模型等 HTTP 客户端沿用标准 `HTTP(S)_PROXY` / `ALL_PROXY` / `NO_PROXY`；网络搜索（ddgs）额外支持 `DDGS_PROXY`（优先于 `HTTPS_PROXY` / `ALL_PROXY`），只设 `ALL_PROXY` 时搜索也会走代理
- 切换其他服务商（豆包 / OpenAI 等）：覆盖上面两个变量，或修改 `config.py` 中的默认值（文件内有示例）
- `temperature` / `max_tokens`: 生成参数（0.7 / 8192）
- 主要环境变量清单与默认值见根目录 [`.env.example`](.env.example)（含 Redis / PostgreSQL、限流、RAG 调优、行程校验与数据目录等；项目不会自动加载 `.env`，请自行 export）。`config_local.py` 仅用于提供 `API_KEY`，其余环境变量写入该文件不会生效

### 3. 初始化知识库

```bash
python .claude/skills/ask-question/script/init_knowledge_base.py
```

行为与退出码（供 cron / CI 检测）：

- **先加载后建库**：任一文档读取 / 解码失败（如 GBK 编码的 txt）会立即中止且**不触碰现有知识库**——不会出现「缺一份文档的半成品重建」；空目录同样在改动之前退出。
- **保留库里已有的额外片段**：重建前会导出面板 / `cli.py kb-add` 添加、只存在于 Milvus 的片段（按内容去重），删除旧集合之前先把它们写入备份文件（`data/rag_knowledge.preserved_backup.json`），重建后按组边界分批补写；写入失败或中途 Ctrl-C 后重跑会自动并入备份并补齐，全部成功后清理备份——重复运行不会抹掉这些知识。
- **维度自愈**：旧集合的向量维度与当前嵌入模型不一致时，自动删除旧集合并按当前模型重建（不再要求手工处理）。
- **多分片元数据**：超过 400 字自动切分的分片带 `group / part / parts`，支持整组删除 / 编辑与备份还原。
- **面板编辑内置文档后重建不丢修改**：通过面板 / CLI 编辑过内置知识文档后，分片会带 `user_edited` 标记；下次重建把它们作为普通知识保留并跳过对应源文件导入（打印提示）。如需恢复内置原文，在面板删除该文档后重新运行本脚本。
- **严格自检**：初始化末尾的检索自检走后端严格模式并统计命中，任一步失败或全部未命中都以退出码 1 结束。

### 4. 启动系统

```bash
python cli.py                     # 命令行界面
python cli.py --help              # 列出一次性命令（health / doctor / weather / kb / eval 等）
python cli.py --version           # 显示 CLI 版本后退出
python -m webapp.server           # Web 界面：http://127.0.0.1:8000
python -m webapp.server --version # 显示 Web 版本后退出
```

Web 服务默认只监听本机，可用 `--host` / `--port` 调整（例如 `--port 8001`）。使用非环回地址（如 `--host 0.0.0.0`）启动时会输出安全警告：请通过反向代理提供认证与 HTTPS（见 [SECURITY.md](SECURITY.md)）。服务端默认只接受回环地址、`*.localhost` 与 IP 字面量形式的 `Host`；通过域名访问（反向代理 / 内网 DNS）时需设置 `ALIGO_ALLOWED_HOSTS=aligo.example.com,*.example.com`（逗号分隔，支持 Unicode 域名与 `域名:端口` / 尾点写法，默认不信任任何外部域名）——未列入的 `Host` 一律返回 400，防止 DNS rebinding 把攻击者域名解析到本机后同源读写数据；畸形（userinfo / 括号残留 / 逗号列表 / 空白）或重复的 `Host` 同样 400。同源尾斜杠 307 与静态目录 301 的 `Location` 为相对路径：TLS 终止反代（proxy_headers 关闭）下不会把 https 访问降级为 http。

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
| `doctor` | 环境自检：依赖 / API Key / 记忆存储后端 / 嵌入模型文件 / 知识库 / LLM 连通性 |
| `clear` | 清空当前任务（保留长期记忆） |
| `history` | 查看历史行程 |
| `preferences` | 查看用户偏好 |
| `export` | 导出用户数据（偏好 / 行程 / 对话历史 / 统计）为 JSON + Markdown（保存到 `exports/`） |
| `exit` | 退出程序 |

单独做健康检查（不进入交互）：`python cli.py health`，返回 `OK` / `FAIL: ...`，退出码 0/1；`--json` 输出 `{"ok", "message"}`，便于监控解析。

知识库状态（不进入交互）：`python cli.py kb`，输出集合名 / 片段数与数据库路径，退出码 0/1；`--json` 输出完整状态对象（`available` / `collection` / `documents` / `path` / `reason`）。集合维度与当前嵌入模型配置不一致时检索必然不可用：`--json` 的 `available` 为 `false` 并在 `reason` 里给出重建命令（与 Web `/api/knowledge` 同口径），文本模式同样以退出码 1 结束。

知识库检索评测（不进入交互、不调用大模型）：`python cli.py eval`，对 30 个差旅问题（覆盖全部 8 份随仓库知识文档）计算 hit@1 / hit@3 / hit@5 与 MRR（纯向量 top-K 口径，不含 BM25 / 混合召回，实际聊天可用率可能更高；题库与知识文档的一致性由 `test_rag_eval.py` 离线用例守护），hit@5 低于 80% 时退出码 1；知识库未初始化时提示跳过。`--json` 输出完整指标对象（`ok` / `skipped` / `threshold` / `total` / `hit1` / `hit3` / `hit5` / `mrr` / `rows`（逐题排名与 `source` 来源文档）/ `mode`（口径）/ `note`（口径说明）），便于 CI 或脚本回归。
评测用例生成（不进入交互、不调用大模型）：`python cli.py eval-cases [--count 500] [--seed 7] [--out cases.jsonl] [--force] [--json]` 按 18 类场景模板确定性生成分层评测用例（含 v2 六个扩展技能；无关问题按「不调度任何智能体」的空意图集合计分。`--count` 合法范围 1..5000，与 `eval-agent` 同口径；0 条不再被当成成功产物），可选写入 JSONL（目标已存在时默认拒绝覆盖，`--force` 才覆盖；原子写），供 `utils/eval_harness.py` 的规则 + Judge 回归使用。
意图评测（调用真实意图模型）：`python cli.py eval-agent [--count 20] [--seed 7] [--min-pass 0.0] [--json]` 生成分层用例并逐条跑 `IntentionAgent`，按标准智能体名集合完全匹配计算 Pass@1 / 分类准确率；未配置模型密钥或低于 `--min-pass` 时退出 1。默认 `--min-pass 0.0` 仅生成报告（任何得分都退出 0，不作为门禁），CI / 回归须显式设置阈值（如 `--min-pass 0.8`）；意图结果解析失败被智能体兜底成默认调度时按失败计，全部失败（含模型持续输出不可解析内容）会以退出码 1 报错而不是计成 0 分达标。

知识库导出 / 导入（不进入交互）：`python cli.py kb-export [path] [--stdout] [--force]` 导出全部片段为 JSON（默认 `exports/knowledge-export.json`，已存在时需 `--force` 覆盖；`--stdout` 直接打印到标准输出、与路径互斥；产物对元数据中的 NaN / Infinity / 孤立代理项做 JSON 清洗，标准解析器可回读）；`python cli.py kb-import <file.json> [--dry-run] [--json]` 批量导入（`--dry-run` 只校验 / 去重并报告将导入的数量，不加载嵌入模型；`--json` 输出结构化结果与跳过原因，供脚本消费；CLI 不限条数，Web 接口单次最多 20000 条（多分片组整组同批，请求体 2 MB 上限）），适合备份、换机与批量补充知识。全局知识片段总数与备份导入的条数上限同一口径（5 万条）：知识管理导入、CLI `kb-import` 与面板「添加知识」在写入前预检，越过上限会拒绝（`--dry-run` / 导入预览在 `export_limit_problem` 字段给出原因）——知识库越过导出上限后，所有用户的「备份（含知识库）」都只能省略知识段。

知识片段查看 / 删除（不进入交互）：`python cli.py kb-list [关键词] [--json]` 按 id 倒序列出片段（分类 + 分片序号 + 60 字预览；`--json` 输出原始 id / content / metadata）；`python cli.py kb-categories [--json]` 按数量倒序统计分类分布；`python cli.py kb-search <关键词> [--top-k N] [--json]` 语义检索（与 Web 面板、RAG 问答同一条链路）；`python cli.py kb-show <id> [--json]` 查看片段（多分片按整篇拼接，与面板「编辑整份」同一实现）；`python cli.py kb-add <file> [--category X] [--title Y]` / `kb-update <id> <file> [--category X] [--title Y]` 从文件新增 / 覆盖更新片段（长内容自动拆分、整组替换与面板同一实现；单篇上限 20000 字，超长请拆分后分次添加）；`python cli.py kb-delete <id> [--group]` 删除指定片段（`--group` 时连同同一拆分组的其余分片）；`python cli.py kb-duplicates [limit] [--json]` 只读扫描内容完全相同的**独立**片段组（`limit=0` 显示全部，`--json` 输出结构化分组；多分片记录不参与，需整份维护）；`python cli.py kb-dedupe [--dry-run] [--json]` 清理这些重复片段（每组保留最小 id，`--dry-run` 只列出将删除项；多分片记录不参与）。

偏好维护（不进入交互）：`python cli.py preferences <user_id> [--json]` 列出长期记忆中的偏好（中文类型名，酒店 / 航司等列表值用「、」拼接，布尔显示为是 / 否；`--json` 输出原始值）；`python cli.py pref-set <user_id> <偏好类型> <值> [--json-value] [--json]` 新增 / 覆盖一条偏好（与面板 PUT 同一存储语义：类型先去除首尾空白再校验，纯空白类型与 `.` / `..` 等面板路径无法寻址的键直接拒绝，避免写出精确键消费方读不到的影子偏好；默认按字符串保存，`--json-value` 按 JSON 解析以写入列表 / 数字 / 布尔等带类型值，序列化上限 10000 字符（按码点）与 Web 一致；`--` 之后的值即使以 `-` 开头也按值处理，如 `-5`）；`python cli.py pref-delete <user_id> <偏好类型> [--json]` 删除一条偏好（未设置返回 1）。保留类型 `preferences`（旧版嵌套偏好容器）只能删除、不能写入：历史脏数据里的该类型行不会被加载展开，删除是唯一清理入口（Web DELETE 同口径，PUT / pref-set 返回 422 / 1）。

行程列表（不进入交互）：`python cli.py trips <user_id> [--query 关键词] [--status active|upcoming|past] [--json]` 按新→旧列出全部行程（路线 / 起止日期 / 目的 / 「进行中 / 即将出行 / 已结束」状态，与其他行程日期重叠时提示；`--query` 按路线 / 日期 / 目的过滤、`--status` 按面板状态徽标过滤（active / upcoming / past，或直接写中文标签；无日期行程在状态过滤时不出现），冲突仍按完整历史计算）；`--json` 输出原始行程（含状态与冲突、`status` 为过滤所用标签）；`python cli.py trip-add <user_id> --destination X --start YYYY-MM-DD [--origin X] [--end YYYY-MM-DD] [--purpose X] [--json]` 保存一条行程（日期严格按 YYYY-MM-DD，`2026-1-1` 这类不补零写法会被拒绝，避免绕过去重生成重复行程；出发地 / 目的地 / 目的与 Web 请求模型同一上限 100 字）（与面板 / 对话同一存储路径：同路线同日期更新既有记录，并提示日期重叠）；`python cli.py trip-delete <user_id> <trip_id> [--json]` 删除指定行程（同一 trip_id 对应多条行程时拒绝并提示先运行 trips-dedupe）；`python cli.py trip-update <user_id> <trip_id> [--origin X] [--destination X] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--purpose X] [--json]` 按字段更新行程（只改提供的字段，日期按合并结果校验，更新后提示日期重叠；`--origin ""` / `--purpose ""` 可清空对应字段，与 Web PUT 一致）；与 Web 记忆面板共用 `utils/trip_conflicts.py` 的日期解析与重叠判断。

历史行程去重（不进入交互）：`python cli.py trips-dedupe <user_id> [--dry-run] [--json]` 删除同路线 + 同起止日期的重复行程（保留最早一条，统计与常去目的地同步更新；身份比对折叠路线首尾空白与大小写，备份保真保留的「 上海」/「Shanghai」与手输同一路线可被清理，内部空白 / 换行仍视为不同路线），`--dry-run` 只列出将被删除的记录（存在歧义 id 时同样退出码 1，与正式执行一致），`--json` 输出结构化结果（将删除 / 已删除条目、歧义 id 与 `needs_attention`）；同一 trip_id 出现在多条不同行程里（历史数据异常）时拒绝自动删除并提示人工处理；空 trip_id 的行无法定位删除，不进入计划（`would_remove` 与实际删除数一致）；配置了 PostgreSQL 但连接失败（回退本地 JSON）时会拒绝执行，避免清理错库。

行程导出（不进入交互）：`python cli.py trips-export <user_id> [path] [--stdout] [--force] [--ics|--html] [--query 关键词] [--status active|upcoming|past]` 导出全部行程为 CSV（UTF-8 BOM，默认 `exports/aligo_<用户>_trips.csv`）、iCalendar（`--ics`，与 Web 的 `/api/trips/calendar` 同一实现，默认 `exports/aligo_<用户>_trips.ics`）或可打印的 HTML 台账（`--html`，与 Web 的 `/api/trips/export?format=html` 同一实现，默认 `exports/aligo_<用户>_trips.html`，内容全部转义；`--ics` 与 `--html` 互斥）；已存在时需 `--force` 覆盖；`--query` 与 `--status`（接受中文标签）与面板 / CLI 列表同一口径过滤后导出。导出后会用真实导入解析器做一次回导自检并提示：CSV 中会被导入端整行拒绝的行数、ICS 中被拒绝的事件数（单位分别为「行」「事件」），返程日期无法解析、日历按单日导出的条数，以及导出文件整体超过导入端上限时的提示（超过 CLI 500 万字符会被整份拒绝、仅超过面板 50 万字符 / 2MB 时请改用 CLI 导入或 JSON 备份恢复；导出本身仍然成功）。——这类原始返程日期（如「待定」）经 `X-ALIGO-TRIP-END` 属性保真携带，回导后身份与原行程一致（旧版导出的文件没有该属性，仍按单日回导）。日历 UID 基于每条行程一次发放、永不改写的内部标识：面板 / CLI 编辑日期或路线后重新导出仍更新同一个日历事件；升级前录入的历史行程没有该标识，导出继续使用旧的内容摘要公式（既有 UID 不变），在下一次真正改动日期 / 路线时一次性切换（该次重导可能在日历里留下一条旧事件，之后保持稳定）。
行程导入（不进入交互）：`python cli.py trips-import <user_id> <path> [--ics] [--dry-run] [--json]` 读取 `trips-export` 导出的 CSV（UTF-8 BOM，中英文字段名均可、英文不区分大小写，表头前的空行会跳过；导出时为防公式注入加的前导单引号会被还原；文本字段保留首尾空白（导入写入保真，但身份比对与去重折叠首尾空白与行分隔符，与存储去重口径一致））或 `trips-export --ics` 导出的 iCalendar（`--ics` 指定，或以 `BEGIN:VCALENDAR` 开头时自动识别；支持 RFC 5545 折行与转义、排他 DTEND；带 `Z` / `±HHMM` 的 DATE-TIME 先换算本机时区再取日期，DATE-TIME 的结束时刻按含尾处理、仅恰为 00:00 时按排他日期减一天；`X-ALIGO-TRIP-END` 用于还原导出时无法解析的原始返程日期）逐条导入（批量写入、单次写盘、一次建索引）；同路线 + 同起止日期沿用存储层去重（未提供目的时保留既有目的，新行程默认「出差」），非法行 / 事件收集后列出并跳过（CSV 行号按物理行；ICS 为事件序号，从 1 起）、不中断其余项；未闭合引号读到文件末尾时，错误行指向该记录的起始行并按丢失的物理行数计入跳过总数；引号错位被后文引号「闭合」而把多条物理行合并成一条合法记录时只给「注意」告警（不改计数），可用 `--json` 的 `warnings` 读取；单次最多 5000 行，字段含 NUL / 孤立代理项时整份拒绝；`--dry-run` 只预览将新增 / 更新 / 跳过的数量、不写库；`--json` 输出 `{ok, path, format, rows, created, updated, skipped[{line, reason}]（最多 50 条）, skipped_total, warnings[{line, reason}], warnings_total}`，没有可导入项时退出码 1。
单条行程日历（不进入交互）：`python cli.py trip-calendar <user_id> <trip_id> [path] [--stdout] [--force]` 导出一条行程为 iCalendar（与 Web 的 `/api/trips/{trip_id}/calendar` 同一渲染口径与 VALARM 提醒，默认 `exports/aligo_<用户>_<trip_id>.ics`，用户名为 ASCII 安全形式以避免不同用户的同名 trip_id 互相覆盖）；同一 trip_id 对应多条行程时拒绝并提示先运行 trips-dedupe，缺少出发日期返回 1，已存在时需 `--force` 覆盖。

会话管理（不进入交互）：`python cli.py sessions <user_id>` 列出未归档会话（置顶优先，显示消息数与最近更新时间），`--archived` 只看归档会话、`--all` 查看全部、`--query <关键词>` 按标题与消息内容过滤并附命中片段、`--json` 输出机器可读列表（`updated_at` 统一为带偏移的 ISO 时间：naive 本地时间按本机时区补齐，与 Web 列表同口径；保留字会话 `export` 与 Web 面板同口径不计入列表与统计，但仍可用 `session-show` / `session-delete` 直接寻址清理）；`python cli.py session-archive <user_id> <session_id> [--restore] [--json]` 归档 / 恢复会话（`--json` 输出结构化结果）；`python cli.py session-delete <user_id> <session_id> [--json]` 删除会话的全部消息与元数据、同步清理短期记忆（Redis 会话键）并清空引用该会话的行程链接（行程本身保留），不可恢复（配置 `REDIS_URL` 时同时写入与 Web 共享的删除墓碑，其它实例的旧标签页下一次请求即 404；未配置时不同进程无法感知，请在旧页面刷新）；`python cli.py session-title <user_id> <session_id> <标题|--reset> [--json]` 设置 / 重置会话标题（上限 60 字，`--reset` 恢复自动标题；标题以 `--` 开头时用 `--` 分隔，如 `session-title u s -- "-- 北京"`）；`python cli.py session-pin <user_id> <session_id> [--off] [--json]` 置顶 / 取消置顶会话；`python cli.py session-show <user_id> <session_id> [--json]` 查看单个会话（标题 / 置顶 / 归档状态与按时间排序的消息，助手消息重新渲染，`--json` 输出原始消息；消息的 `timestamp` 与 `sessions --json` 同口径统一为带偏移的 ISO——JSON 后端的 naive 本地时间按本机时区补齐，PG 回读的带时区时间原样保留）；`python cli.py session-export <user_id> <session_id> [path] [--stdout] [--force] [--html]` 导出单个会话为 Markdown（默认 `exports/aligo_<用户>_<session>.md`，用户与会话 ID 均经清洗并在有撞名风险时追加短哈希）或独立 HTML（`--html`，单文件内嵌样式，默认 `.html` 后缀，所有内容转义）；助手消息按编排结果重新渲染。配置了 PostgreSQL 但连接失败（回退本地 JSON）时同样拒绝执行。以上会话与导出命令（`sessions` / `session-archive` / `session-export` / `sessions-export`，以及 `trips-dedupe` / `trip-calendar` / `trips-export`）的选项可出现在位置参数之前或之后（与 `stats` / `trips` 一致，如 `sessions --json alice`）；所有选项大小写不敏感（含 `kb-delete --group` 与 `user-export --with-knowledge`）。用户 / 会话 / 行程 / 偏好 ID 未通过字符集校验时统一视为用法错误：退出码 2，`--json` 输出合法的 `{"ok": false, "error": "用法…"}`。

全部数据导出（不进入交互）：`python cli.py user-export <user_id> [dir] [--with-knowledge]` 把偏好、历史行程、会话标题 / 置顶 / 归档、统计与全部对话历史导出为 JSON + Markdown 两份文件（默认 `exports/`，与交互模式 `export` 命令字段一致）；`--with-knowledge` 把全局知识库片段一并写入 JSON（一体化备份，导出知识库失败时中止；仅知识轴超限或分片计划暂无法核对、而用户数据本身仍可导出时，备份省略 `knowledge` 段继续写出并以退出码 0 + stderr 警告提示，知识库需先删除到导出上限以内，再单独 `kb-export` / 迁移，见已知限制）。备份恢复（`--dry-run` 只预览将新增 / 更新 / 跳过的数量、不写库）：`python cli.py user-import <user_id> <json_path> [--dry-run] [--skip-knowledge] [--json]` 从 `user-export` 的 JSON 恢复偏好、行程、聊天消息与会话标题 / 置顶 / 归档，备份含 `knowledge` 段时一并恢复全局知识库片段（`--skip-knowledge` 可跳过；知识库未初始化时用户数据仍恢复、退出码 1 并提示重跑）。（单份备份上限 50 MB；时间戳归一为带时区 ISO 后保真写入，naive 时间按本机时区解释；缺失 / 非法时间戳的消息沿用同会话相邻消息的时间（提问贴合自己的回答、回答贴合自己的提问）；消息按时间戳并入全局历史位置，恢复的更早消息回到会话中的历史位置；重复导入幂等——行程按路线+日期去重、消息按「会话/角色/内容」多重集去重，提问已在库中的轮次跳过备份里的回答（本地优先，见已知限制）；墓碑期内被删除或使用保留字（如 `export`）的会话与 Web 面板同一口径跳过（消息 / 标题 / 置顶 / 归档一并剔除、行程关联清空）；若备份中的用户数据因此全部被剔除，按「没有可恢复的数据」返回退出码 1（文本模式不再打印「已恢复」），灾难恢复脚本不会把空恢复当成功）；无法识别的日期原样保留，非法条目逐条跳过并在 `--json` 的 `skipped` 中列出；恢复按条目写入、非原子，失败后重复执行可补齐剩余部分；任一后续段（行程 / 消息 / 会话元数据）写入失败而前段已落库时，同样报告「已提交 N 条新增、M 条更新 + 可安全重跑」，不会把半份恢复谎报成整份失败；统计字段由存储层累计，不直接覆盖）。同一账号的并发导入互斥：JSON 后端用 `<db>.import.lock` 文件锁、PostgreSQL 用 `aligo-import!<user>` advisory 锁；等锁超时（含 PG 的 55P03）统一报「另一个导入正在进行，请稍后重试」并按可安全重试处理，不与真实存储故障混淆。

用户数据概览（不进入交互）：`python cli.py stats <user_id> [--json]` 汇总偏好条数、行程统计（总行程 / 出行天数 / 到访城市 / 进行中 / 即将出行）与会话 / 消息 / 归档数量，与 Web 记忆面板同口径（统计按行程 / 消息列表实时计算；JSON 后端加载时会把与列表长度不符的旧计数器按列表重建，与 PostgreSQL `COUNT(*)` 同口径）。

全部会话导出（不进入交互）：`python cli.py sessions-export <user_id> [path] [--stdout] [--force] [--html]` 把全部会话导出为单个 Markdown（默认 `exports/aligo_sessions_<user>.md`），`--html` 导出独立 HTML 合集（单文件内嵌样式、全部内容转义，默认 `.html` 后缀，与单会话 `session-export --html` 同一渲染口径）；中文或超长用户名会追加短哈希避免重名；按最近更新排序并含归档会话；已存在时需 `--force` 覆盖，无会话返回 1。所有导出命令（`kb-export` / `session-export` / `sessions-export` / `trips-export` / `trip-calendar` / `user-export`）传显式空路径（如脚本变量未设置）按用法错误处理，不会静默回退到默认 `exports/`；`--stdout` 模式下 stdout 只包含导出内容，失败提示一律走 stderr（重定向产物不会混入中文报错）；非 `--force` 导出在写入中断（Ctrl-C 等）时会清理占位文件，同名导出不会被 0 字节残留永久挡住。

环境自检（不进入交互）：`python cli.py doctor`，逐项检查运行环境并给出修复提示，退出码 0/1（存在必须处理的问题时为 1）；`python cli.py doctor --json` 输出机器可读的 `status` / `checks`，便于监控脚本解析，退出码不变；`--offline`（或 `--no-network`）跳过 LLM 连通性检查，适合网络不可达或 CI 环境——该开关**只**跳过 LLM 检查；配置了 `REDIS_URL` / `DATABASE_URL` 时仍会以 1.5s / 3s 超时探测存储（失败只记警告、不影响退出码 0，网络隔离 CI 若无需存储自检可不配置这两个变量）。

技能目录（不进入交互）：`python cli.py skills`，输出当前技能目录与已发现的技能名，退出码 0/1；`--json` 输出 `root` / `skills` / `ok`，便于脚本读取。安装 wheel 或设置 `ALIGO_SKILLS_DIR` 后可用它确认技能是否生效。

天气查询（不进入交互）：`python cli.py weather <城市>`，输出当前天气与未来几天预报（Open-Meteo，无需 API Key，10 分钟缓存，配置 `REDIS_URL` 时与 Web / 其它进程共享；上游故障时沿用 1 小时内的成功数据并标注）；`--json` 输出完整结果并附带 `ok`，城市不存在或接口不可用时退出码 1。

**脚本与监控示例**

一次性检查命令都支持 `--json`（退出码与文本模式一致），可以直接接到部署脚本或监控：

```bash
python cli.py health --json | jq -e '.ok'
python cli.py doctor --json | jq -e '.status == "ok"'
python cli.py skills --json | jq -e '(.skills | length) >= 6'
python cli.py kb --json | jq '{available, documents}'
python cli.py eval --json | jq -e '.skipped or (.ok and .hit5 >= 0.8)'
```

每次对话结束后会显示本次的 LLM 调用次数、token 用量与总耗时；`status` 命令可查看本次会话的累计用量。

执行过程中会实时显示当前正在运行的智能体（例如「正在执行 行程规划…」）；完成后智能体列表包含各自耗时（例如 `信息查询 ✓ 2.3s`），与 Web 执行路线一致。

> **排障提示**：启动时若出现 `No sentence-transformers model found ... mean pooling` 警告，说明本地模型目录缺少 `modules.json` / `1_Pooling/config.json` 等元数据文件（通常只复制了权重文件）。从 HuggingFace 下载完整模型仓库文件即可消除；`doctor` 会检查并提示。
>
> Milvus Lite 3.x 在**非 ASCII 路径**（例如用户名含中文的 `C:\Users\张三\...`）下可能无法写 Faiss 索引（`FileIOWriter ... No such file or directory`）。请把项目放在纯英文路径（如 `D:\Code\chailv`）后重新初始化知识库。wheel 安装时知识库默认在 `%LOCALAPPDATA%\aligo\data\rag_knowledge`，中文 Windows 用户名下同样命中该限制——唯一规避是把 `ALIGO_DATA_DIR`（或 `ALIGO_SKILLS_DIR`）指向纯英文目录；初始化脚本会提前失败并给出该提示，而不是「成功」后重启即不可用。

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
- **会话归档**：侧栏会话操作新增「归档」，归档后从默认列表隐藏但消息与标题保留；「已归档（N）」切换到归档列表，可恢复或删除，JSON / PostgreSQL 持久化；普通视图搜索时如有归档会话匹配会提示「另有 N 条归档会话匹配」并可直接切换
- **即将出行**：侧栏根据保存的行程日期显示出行提醒——有进行中的行程时优先展示并标注「进行中」，否则展示最近一次即将开始的行程（路线 / 日期 / 目的），点击回到对应对话（无关联会话时打开记忆面板）；卡片会异步显示目的地当前天气，行程在 3 天窗口内时优先展示「出发日」预报，窗口外显示「今日」温度（`/api/weather`，Open-Meteo，失败时静默隐藏，上游故障沿用最近数据时标注「较早」）
  ![即将出行：出发日天气](docs/screenshots/webui-trip-weather.jpg)
- **对话深链接**：地址栏同步 `user` / `session` 参数（例如 `/?user=alice&session=abc123`），打开链接即可恢复会话；顶栏提供「复制链接」，无效链接会自动清理参数并回到新对话
- **主题与布局**：浅色 / 深色主题跟随系统并可切换；窄屏下侧栏变为抽屉
- **记忆与用量面板**：在线编辑 / 删除偏好、手动添加常用偏好（常住地 / 交通 / 酒店 / 航司 / 座位 / 餐食 / 预算，多个值用「、」分隔）、查看最近 5 条行程（超过 5 条可「查看全部」展开最近 200 条，展开后可输入关键词按路线 / 日期 / 目的筛选，并可按状态（进行中 / 即将出行 / 已结束）过滤；带「进行中 / 即将出行 / 已结束」状态标签，与其它行程日期重叠时显示「重叠」徽标，新记录可点击回到对应对话、可导出 .ics 日历（含前一天提醒）、CSV 报销台账或可打印的 HTML 台账）与常去目的地、统计、存储后端、技能数量（用户卡片显示，安装后技能缺失可看到「技能 0」）、模型用量（调用次数 / tokens / 平均耗时 / 常用智能体）与近 7 天 tokens 趋势图、运行指标（API 请求数 / 平均与最大耗时 / P95 耗时 / 熔断器状态 / 运行时长与缓存会话 / 限流后端，来自 `/api/metrics`），修改用户 ID，导出长期记忆 JSON / Markdown、一键导出全部会话 Markdown 或 HTML（HTML 与 CLI `sessions-export --html` 同一渲染口径）、从 `user-export` 的 JSON 备份恢复偏好 / 行程 / 聊天记录与知识片段（知识片段进入共享知识库；单份备份上限 50 MB，重复恢复幂等，最近被删除的会话会被跳过）；补录 / 编辑行程时若与已有行程日期重叠会提示
- **消息操作**：复制、重新生成；最后一条提问可就地编辑并重新发送（`Ctrl/⌘ + Enter` 保存、`Esc` 取消）；停止 / 断流后前端对账最多等待约 12 秒（覆盖服务端 10 秒落库等待），未确认时保留原文并提示稍后刷新而不是断言未保存（无会话 ID 时同样提示结果未知，引导先点「重试」核对、避免直接重发产生重复回合）；超过 4000 字的提问（含程序化合并的草稿）在发送前拦截并保留全文；对话请求随轮携带 `idempotency_key`（重试按钮复用原键与 turn id，命中服务端回放 / 断连 409 / error 终态）；「重新生成」的未保存编辑确认在删除服务端上一轮之前，删除阶段按钮显示「准备中…」，超时如实提示结果未知；Markdown 支持标题、列表、引用、表格与代码块（带复制按钮）；会话可导出为 Markdown
- **跟进建议**：回答完成后根据本轮执行的智能体推荐 2–3 个下一步（如导出行程、查出发当天天气、调整行程），点击即可继续对话
- **打印 / 导出 PDF**：顶栏打印按钮调用浏览器打印；打印样式隐藏侧栏、输入区、执行路线与操作按钮，适合打印行程单
- **快捷键与键盘可达性**：`Ctrl/Cmd + K` 或 `/` 聚焦会话搜索，`Esc` 清空搜索 / 关闭面板；按 `?` 或点击顶栏「?」打开快捷键帮助面板；会话置顶 / 重命名 / 删除按钮支持键盘聚焦（删除 / 归档 / 取消归档 / 分页取尽导致目标行消失时，焦点按旧位置就近恢复而不是落到页面顶部）；记忆面板打开时聚焦到对话框内，`Tab` 在面板内循环，关闭后焦点回到触发按钮

**接口观测与限流**

- 所有 `/api/` 响应带 `X-Request-ID` 头（透传请求头中的值，否则自动生成 12 位十六进制）；错误详情也会附带请求 ID，便于排查。
- 访问日志默认输出单行文本（方法 / 路径 / 状态 / 耗时 / 请求 ID）；设置 `LOG_FORMAT=json` 后，应用日志（含访问日志）每行输出为**合法 JSON**（`time` / `level` / `logger` + `event=http_request` 等字段），便于日志采集与检索；uvicorn 自身的启动 / access 日志保持原格式。
- 静态资源与 JSON 响应启用 gzip（≥1KB）；`/api/chat/stream` 的 SSE 事件流保持不压缩，避免缓冲破坏实时进度。
- 系统自检：记忆面板「运行自检」调用 `/api/doctor`，展示 Python 版本、核心 / RAG 依赖、大模型配置、版本一致性（`pyproject.toml` / wheel 元数据 / `index.html` 静态资源 `?v=` / `utils/version.py`）、技能目录（发现数量）、记忆存储后端（配置 Redis / PostgreSQL 时检查连通性）、嵌入模型文件、知识库状态；勾选「检查 LLM 连通性」时额外发起一次真实健康检查（默认不请求网络）。
- 请求指标：`GET /api/metrics` 返回进程内 `/api` 请求计数（仅统计 `/api` 路由与 Host 白名单拒绝的探测；首页、`/static` 资源与 `/metrics` 抓取本身不计）、状态 / 方法分布、Top 路径（按路由模板聚合，如 `/api/sessions/{session_id}`；未匹配路径记为 `__unmatched__`，Host 白名单拒绝记为 `__host_rejected__`，路由前 413 与尾斜杠重定向按已知接口归一）、平均 / 最大 / P50 / P95 耗时（平均与最大为进程启动以来全部计时请求的累计口径，百分位基于最近 1000 个请求样本，两者窗口不同；流式与取消 / 客户端断开的 499 不计耗时；Host 拒绝探测、未匹配路径的 404、方法不符的 405 与尾斜杠 307/308 不受限流、可被扫描无限次触发，只计请求数、不进延迟分位；无计时样本时面板显示「—」）、熔断器状态（`opened_at` / `last_failure_time` 为本地墙钟秒，字段同锁读取）、缓存会话数、技能可用性与数量（`aligo_skills_ok` / `aligo_skills_count`，与 `/api/health` 的 `skills.ok` 同口径），以及当前限流后端（Redis 共享 / 进程内）、是否降级与用户 / IP / 知识库 / 天气额度配置（请求指标多进程各自统计）；同时提供 Prometheus 文本端点 `GET /metrics`，便于接入 Prometheus / Grafana，多实例需分别采集。

  Prometheus 抓取示例：

  ```yaml
  scrape_configs:
    - job_name: aligo
      static_configs:
        - targets: ["127.0.0.1:8000"]
  ```
- 对话接口默认按「客户端 + 用户」限流 **30 次 / 分钟**，同时按客户端 IP 限流 **120 次 / 分钟**（用户 ID 由请求方提供、可轮换，IP 桶防止换 ID 绕过），超限返回 `429` 与整数秒 `Retry-After`；响应同时附带 `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`，便于客户端感知额度。可用环境变量 `CHAT_RATE_LIMIT`（单用户次数）、`CHAT_RATE_LIMIT_IP`（单 IP 次数）与 `CHAT_RATE_WINDOW_SEC`（窗口秒数）调整。默认使用进程内滑动窗口（`utils/rate_limit.py`）；配置 `REDIS_URL` 后计数写入 Redis（`aligo:ratelimit:*`，使用 Redis 服务器时间），多实例共享同一份额度，Redis 运行中故障时自动回退进程内实现并在 60 秒后重试；只读探测（peek / status）失败只短暂（2 秒）暂停读探测、不降级整桶（避免已用额度被空的本地桶清零、多实例下按实例放大），权威记账（check / refund）失败才进入 60 秒回退；单个键被写成错误类型（WRONGTYPE）、无法共享计数时仅该键回退，并通过 `/api/health` 的 `rate_limit_corrupt_keys` 与 `/metrics` 的 `aligo_rate_limit_corrupt_keys` 单独暴露；启动时不可达的实例会在 Redis 恢复后通过客户端工厂自动重新接入共享计数（限流降级标志只有在下一次调用成功后才清除，冷却到期不视为恢复）。知识库写入 / 导出 / 列表 / 分类统计 / 语义检索 / 整篇回填都会触发嵌入模型推理或整库读取，按客户端 IP 单独限流（默认 **60 次 / 分钟**，`KB_RATE_LIMIT` 调整），与对话额度互不影响，429 同样附带额度头（`/api/doctor` 自检与 `/api/import` 备份恢复同样占用该桶，429 文案按实际被限操作区分）；知识库导入、导入预览与行程导入使用独立桶（默认 **1200 次 / 分钟**，`KB_IMPORT_RATE_LIMIT` 调整——面板分批导入需要远高于普通操作的额度，调整它会同时改变行程导入额度）；单 IP 单桶直接原子记录，不会在并发下超发。天气接口每次未命中缓存通常向上游发两次请求（中文名补查「裸名 / X市」变体、或别名城市 en 索引无可信结果回退 zh 时最多三次），也按客户端 IP 单独限流（默认 **30 次 / 分钟**，`WEATHER_RATE_LIMIT` 调整），保护 Open-Meteo 免费额度与共用线程池，不占用对话 / 知识库额度。面板数据读接口（会话列表 / 状态 / 导出 / 台账）每次请求都全量读取用户历史并走共享线程池，按客户端 IP 单独限流（默认 **600 次 / 分钟**，`READ_RATE_LIMIT` 调整），额度远高于正常面板刷新频率，只用于挡住并发拉满线程池的脚本行为。用户数据写接口（偏好 / 会话标题 / 置顶 / 归档 / 行程增删改 / 删除会话 / 删除最后一轮）每次都会全量重载并重写该用户记忆，按客户端 IP 单独限流（默认 **240 次 / 分钟**，`WRITE_RATE_LIMIT` 调整，超限 429）；偏好与行程的实时写入同样受导入上限约束（200 / 5000 条，超限 409），CSV / ICS 导入、`/api/import` 备份恢复与知识片段新增 / 导入越过容量上限时也统一返回 409（先删除旧数据再重试），400 只用于请求内容非法（解析失败、字段不合法），避免写入路径把用户数据撑到备份无法导出高开销伪 GET（`/api/doctor`、知识库搜索 / 评测、`/api/weather`）与全量读取 / 导出类只读接口要求同源客户端标记：面板自动携带 `X-Aligo-Client: web`，脚本调用同样需要携带该头（或提供 `Sec-Fetch-Site: same-origin`；Referer 不作为同源证据——应用对所有响应下发 `Referrer-Policy: no-referrer`，反代部署下后端也无法可靠得知外部 scheme），否则返回 403——HTTP 内网部署与旧版 Safari 不发送 `Sec-Fetch-Site`，该头不能作为唯一防线。部署在反向代理后面时设置 `TRUST_PROXY_HEADERS=1`，限流按 `X-Forwarded-For` **最后一个地址**计算客户端 IP（默认关闭以防客户端伪造；最靠近应用的代理用 `$proxy_add_x_forwarded_for` 追加，应用取它写入的那一项），且只信任来自 `TRUSTED_PROXY_CIDRS`（默认仅回环 `127.0.0.1/32,::1/128`；容器 / 跨主机代理必须显式配置其来源网段，否则 XFF 被忽略、所有用户合入代理 IP 桶并打印告警）；末跳接受裸 IP、`[IPv6]`、`[IPv6]:port` 与 `IP:port`（端口剥离后按地址归桶），无法解析时回退 TCP 对端并告警。429 的 `X-RateLimit-Reset` 与 `Retry-After` 都是**剩余秒数**（非 epoch）。IPv6 客户端默认按 `RATE_IPV6_PREFIX_LEN=64` 聚合（可按接入网络委派粒度改为 56 / 48，多实例须同值）。进程内限流器每个桶有键表上限 `RATE_LIMIT_MAX_KEYS`（默认 10000，钳制在 100..1000000）：满时对新客户端 429「限流容量暂满」并按最早到期键给出 Retry-After（不再固定整窗口）；小时 / 日窗口或单进程键基数可能超过上限的部署应改用 Redis 后端（无键表上限）。`with_knowledge=1` 的用户备份导出会整库扫描知识片段，因此在读桶之外**同时占用知识库桶**（先查 KB 桶：被 KB 额度拒绝的请求不消耗读额度）。
- 安全响应头：CSP（仅同源脚本）、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Permissions-Policy`；主题初始化脚本拆分为 `/static/theme.js` 以满足 CSP。`/docs`、`/redoc`、`/openapi.json` **默认关闭**（页面从 CDN 加载资源、schema 暴露完整端点结构，内网部署时是常见的信息泄露面），需要时设置环境变量 `ALIGO_ENABLE_DOCS=1` 开启，文档页使用仅对其放宽的 CSP（允许 CDN 资源；`frame-ancestors` 与全站一致保持 `'none'`，文档关闭时 `/docs*` 的 404 使用严格策略）；未处理异常返回的 500 同样带安全响应头与 `X-Request-ID`；`/api/` 响应一律带 `Cache-Control: no-store`（用户数据不进入浏览器 / 代理缓存；SSE 对话流为 `no-store, no-cache`，导出同样 no-store，无例外）。轻量探活路由（`/`、`/api/health`、`/api/metrics`、`/metrics`）支持 `HEAD`（RFC 9110），存活探测可用 `curl -I`；注意 `/api/health` 的 HTTP 状态恒为 200，降级信息在 body 的 `status` 字段（`ok` / `degraded`），就绪 / 降级探测必须读取 body，例如 `curl -fsS http://127.0.0.1:8000/api/health | grep -q '"status": *"ok"'`（注意：`status` 包含进程内熔断器状态，open / half_open 均报 `degraded`，而熔断恢复只由真实对话流量推进——把 body 的 `status` 直接当就绪探针，探针本身会把 OPEN 实例推进到 HALF_OPEN 并在持续探测下永久摘出负载均衡，不会再有任何真实流量让熔断关闭；就绪探针建议只看 `skills.ok` / `has_api_key` 等静态条件，熔断状态用于告警，被摘除后重启进程是最终兜底）；SSE 与高开销只读 / 导出 / 上游查询路由不补 `HEAD`——它会完整执行端点（读库、序列化、gzip、请求上游）再丢弃 body，没有探测价值；`Accept-Encoding` 按标准解析（`gzip;q=0`、大小写写法都按语义处理）。HSTS 默认不下发：应用自身不终止 TLS，**由 TLS 反向代理对 HTTPS 响应下发 `Strict-Transport-Security`**；确有需要时设置 `ALIGO_HSTS_MAX_AGE`（秒，默认 0 = 关闭）让应用在全部响应（含错误页）附加该头。

**接口**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 发送消息（一次性返回；`message` / `user_id` / `session_id`，`user_id` 必填（与其余数据接口同口径，缺省 422——不再回退固定账号，避免省略字段的不同调用方共读写同一份记忆），可选 `idempotency_key`（≤200 字，同用户 + 会话 + 键的重复提交直接回放上次成功响应，带 `X-Idempotent-Replay: 1`，可选 `timezone`（IANA 时区，相对日期按该时区解释，缺省服务器本地））；响应含 `persisted`，会话在其它进程被删除时整轮不落库并返回 `persisted: false`；客户端断开连接后服务端取消在途回合，不再继续消耗模型额度或写入历史） |
| POST | `/api/chat/stream` | 发送消息（SSE 流式返回执行进度，最后返回完整结果；`done` 事件同样携带 `persisted`；可选 `idempotency_key`，重复提交只回放 `session` + `done` 并标记 `idempotent_replay`；可选 `timezone`（IANA 时区，相对日期按该时区解释）） |
| GET | `/api/health` | 服务版本 / 模型 / 技能数 / 存储与限流后端状态；熔断器打开或技能目录不可用（`skills.ok` 为 `false`）时 `status` 为 `degraded` 并附 `circuit_breaker` / `skills` |
| GET | `/api/metrics` | 进程内 `/api` 请求指标（计数 / 状态分布 / Top 路径 / 平均、最大、P50、P95 耗时 / 熔断器状态 / SSE error 收尾计数 `sse_errors` / 偏好快照缓存 hits、misses、version_invalidations）与限流后端信息；Prometheus 文本端点（`/metrics`）另暴露 `aligo_preference_cache_hits_total` / `aligo_preference_cache_misses_total` / `aligo_preference_cache_version_invalidations_total`；SSE 长连接只计请求数、不计入延迟分位；首页、静态资源与抓取自身不计入 |
| GET | `/metrics` | Prometheus 文本指标（请求计数 / 状态 / 方法 / 路径 / 耗时 / 熔断器 / 缓存会话 / 技能可用性（`aligo_skills_ok` / `aligo_skills_count`）/ 限流与存储后端降级 / 共享墓碑状态；多实例分别采集） |
| GET | `/api/knowledge` | 知识库状态（集合名 / 片段数 / 是否可用 / 向量维度；已加载嵌入模型时按实际维度、冷进程按模型目录配置维度附带 `dimension_mismatch` 标记换模型后的维度不一致） |
| GET | `/api/doctor` | 环境自检（`network=true` 时包含 LLM 连通性检查）；熔断器打开 / 半开时附加「熔断器」检查项并让自检不再全绿；顶层 `degraded` 布尔与 `/api/health` 的 `status=degraded` 同口径（`status` 仍只有 `ok` / `fail`，半开期 `status=ok` 且 `degraded=true`） |
| GET | `/api/knowledge/documents` | 知识片段列表（`limit` / `offset` / `q` / `category`，按 id 倒序） |
| POST | `/api/knowledge/documents` | 新增知识片段（`content` / `metadata`；首次调用加载嵌入模型） |
| PUT | `/api/knowledge/documents/{id}` | 更新知识片段（内容重新向量化后覆盖原片段，不存在返回 404；带 `group` 的多分片记录按整组替换，内容上限 20000 字；可选 `expect` / `expect_parts` / `expect_group` / `expect_metadata` 传入编辑时载入的快照（元数据轴同样 409，另一会话只改分类 / 标题也不会被旧快照改回）；整组编辑保留逐片元数据，只把真正改动的字段应用到全组） |
| GET | `/api/knowledge/documents/{id}/group` | 返回片段所属整篇文档（多分片按 `part` 拼接），供面板「编辑整份文档」回填 |
| GET | `/api/knowledge/categories` | 知识片段分类统计（维护面板筛选下拉框） |
| GET | `/api/knowledge/search` | 语义检索知识片段（`q` / `top_k`，返回相似度） |
| GET | `/api/knowledge/eval` | 离线检索评测（30 个问题，覆盖全部随仓库知识文档；hit@1/3/5 与 MRR，不调用大模型） |
| GET | `/api/knowledge/duplicates` | 内容完全相同的独立知识片段组（与 CLI `kb-duplicates` 同一实现；`limit=0` 表示全部，Web 侧最多返回 2000 组并在 `truncated` 标记；无上限的全量审计请用 CLI `kb-duplicates 0`；多分片记录不计为重复） |
| POST | `/api/knowledge/duplicates/cleanup` | 清理内容完全相同的独立片段（每组保留最小 id，与 CLI `kb-dedupe` 同一语义；JSON body `{"dry_run": true}` 默认只返回计划，`false` 才在写锁内逐条复核并删除，已消失 / 已变化 / 变为多分片的条目记入 `skipped`、删除异常记入 `failed`；2xx 响应恒带 `plan_total` 与 `truncated`，`plan` 视图最多 2000 组——截断只影响返回视图，删除始终按全量计划执行；删除开始后的存储故障逐条记入 `failed` 并照常返回（已删部分不可逆，不变成 503 丢失计划），503 只出现在扫描 / 加载阶段的失败；必须 JSON body，跨站表单无法触发） |
| DELETE | `/api/knowledge/documents/{id}` | 删除知识片段；`group=1` 时连同同一拆分组的其余分片一起删除（带 `group` 的多分片记录不能单条删除，返回 400 提示整组删除） |
| GET | `/api/knowledge/export` | 导出全部知识片段为 JSON 文件（备份 / 迁移）；导出前按逐条紧凑序列化估算体积，超过 50 MB 面板导入上限时返回 413（该文件本也无法经面板 / `/api/import` 导回，提示先删除部分片段或改用 CLI `kb-export`） |
| POST | `/api/knowledge/import` | 批量导入知识片段（`documents` 数组，单次最多 20000 条；`dry_run=1` 只返回 `would_import` 不写库）；知识库写锁被长操作占用时快速返回 503「知识库正在写入」（零成本拒绝，不消耗导入额度） |
| POST | `/api/knowledge/import/preview` | 预览整个备份（一次最多 20000 条）：校验 / 去重后返回将导入 / 跳过的数量（`skipped_invalid` 无效内容 / `skipped_duplicate` 已存在分别统计），不写库；写锁忙同样快速 503 且不消耗导入额度 |
| GET | `/api/status` | 用户数据概览（偏好 / 最近行程 / 行程统计（总数 / 出行天数 / 到访城市 / 进行中 / 即将出行）/ 对话数 / 统计 / 存储后端 / 模型用量与近 7 天趋势（可按浏览器时区 `tz` 参数分桶）；可选 `today=YYYY-MM-DD` 指定行程状态的「今天」，缺省时若 `tz` 有效按该时区取当天（与用量窗口同口径）、否则按服务器本地日期；`user_id` 必填，缺省 422——不再回退固定账号 `web_user`，避免省略字段的不同调用方读到同一份数据） |
| GET | `/api/weather` | 目的地天气（Open-Meteo，无 API Key；实时 + 未来 3 天，成功结果 10 分钟进程内缓存（配置 `REDIS_URL` 时同时写入共享缓存 `aligo:weather:<城市>`，其它实例直接复用）、失败结果 60 秒；上游故障时沿用最近一次成功数据并标记 `stale`，窗口按最近一次成功计时、1 小时封顶，不会被反复降级刷新；按客户端 IP 限流，默认 30 次 / 分钟，10 分钟缓存命中与 60 秒降级复用（stale）不消耗额度（缓存命中的响应同样附带 `X-RateLimit-*` 额度头，与未命中的 200 形状一致）；全进程上游额度（120 次 / 分钟）用尽时返回 429「天气查询过于频繁」+ `Retry-After` / `X-RateLimit-*`，该拒绝未发上游请求、不消耗 IP 天气额度；本地排队繁忙（单飞等待耗尽 / 并发闸门超时 / 上游 429）返回 503 并归还本次 IP 天气额度，有 1 小时内旧数据时直接降级返回 `stale` |
| PUT | `/api/preferences/{type}` | 新增或覆盖一条偏好（JSON 值，记忆面板编辑入口） |
| DELETE | `/api/preferences/{type}` | 删除一条偏好 |
| GET | `/api/sessions` | 历史会话列表（`limit` / `offset` 分页并返回 `total`；`q` 可选：按标题与消息内容搜索，返回命中片段；`archived=true` 只返回归档会话，同时返回 `archived_count`） |
| GET | `/api/sessions/{id}` | 某个会话的消息（助手消息已渲染为 Markdown，并附执行元数据） |
| PUT | `/api/sessions/{id}/title` | 设置或清除会话自定义标题（空字符串恢复自动标题） |
| PUT | `/api/sessions/{id}/pin` | 置顶 / 取消置顶会话 |
| PUT | `/api/sessions/{id}/archive` | 归档 / 恢复会话（归档只从列表隐藏，不删除消息） |
| GET | `/api/sessions/{id}/export` | 把会话导出为附件：默认 Markdown（`format=md`），`format=html` 导出与 CLI `session-export --html` 同口径的独立 HTML（单文件内嵌样式、标题单次转义、可分享 / 打印） |
| GET | `/api/sessions/export` | 导出全部会话为单个附件（按最近更新排序，含归档会话；默认 Markdown，`format=html` 导出与 CLI `sessions-export --html` 同口径的独立 HTML） |
| DELETE | `/api/sessions/{id}` | 删除某个会话的聊天记录与元数据；引用该会话的行程保留、会话链接清空（与 CLI `session-delete` 同一实现） |
| DELETE | `/api/sessions/{id}/last-turn` | 删除最后一轮对话（用户提问 + 助手回答）；`expect` 与最后一轮用户提问不一致时不删除；可选 `turn_id`（助手消息 `_meta.client_turn_id`）用于精确匹配本轮，同文并发提问时不会误删其它客户端的回合；用户锁被长回合占用超过 10 秒返回 409「会话正在生成回复」（持锁者是备份恢复时文案改为「该账号正在恢复备份」），不排队到前端超时后仍执行 |
| POST | `/api/sessions/{id}/last-turn` | 同上，`expect` 走 JSON body（长提问不受查询串 4000 字上限），body 支持 `turn_id`；面板「编辑重发 / 重新生成」使用此入口并携带本轮身份 |
| GET | `/api/export` | 导出用户长期记忆（`format=json` 默认 / `format=md`，附件下载）；`with_knowledge=1`（仅 JSON）把全局知识库片段写入备份的 `knowledge` 字段（与 CLI `user-export --with-knowledge` 同一字段，可由 `/api/import` 一体化恢复）；仅知识轴超限或分片计划暂无法核对、而用户数据本身仍可导出时，返回 200 但省略 `knowledge` 段并带响应头 `X-Knowledge-Omitted: over-limit \| planning-failed`（响应头不随文件保存，知识库需先删除到导出上限以内，再另行 `kb-export`），用户数据轴超限仍返回 413（自检按「恢复到新账号后的导出形状」复核，时间戳归一 / 行程编号补全带来的增量也计入，不会产出导出成功但恢复必 413 的备份；JSON 后端在存储文件超过上限时读取前直接 413）；含知识段的备份下载名带 `_with_knowledge` 后缀；同账号恢复备份写入期间返回 409（避免导出半截快照） |
| POST | `/api/import?user_id=` | 从 `/api/export` 的 JSON 备份恢复（只接受 `Content-Type: application/json`，body `{backup: <导出 JSON 对象>}`，备份对象上限 50 MB（按导出格式计）/ 请求体上限 64 MB（Unicode 转义发送的体积放大也允许，读取预算 300 秒）；解析阶段整组丢弃的残缺知识分片同样计入 `knowledge_skipped`；偏好 / 行程 / 聊天消息 / 会话标题与置顶归档，重复导入幂等，最近被删除的会话跳过，备份含 `knowledge` 段时一并恢复知识片段（失败时 `knowledge_error` 字段说明）；非法条目在 `skipped` 列出；响应计数为「写入期间被删除的会话回滚后」的净生效值（按实际恢复的条目扣减）；PostgreSQL 不可用时 503；同一来源或全局导入槽位被占用时并发请求返回 503（带 `Retry-After: 5`，零成本拒绝与用户锁等待超时 / 断连都不消耗知识库额度）；`dry_run=1` 只预览不写库；备份条目全部落在墓碑期 / 保留字会话、实际什么都没恢复时返回 200 + `ok: false`（与 CLI 退出码 1 同口径），`skipped` 说明原因） |
| POST | `/api/trips` | 手动添加一条行程（同路线 + 同起止日期自动去重，返回 `trip_id`、`created`（取自存储写锁内，不受并发窗口影响）与日期重叠的 `conflicts`；未提供 `purpose` 时新行程落默认「出差」、命中既有行程保留原目的） |
| POST | `/api/trips/dedupe` | 清理重复行程（JSON body `{"user_id": ..., "dry_run": false}`；`dry_run` **默认 `true` 只预览**，真实删除必须显式传 `false`；同路线 + 同起止日期保留最早一条，与 CLI `trips-dedupe` 共用判定 / 复核 / 删除；同 id 多行程只列在 `ambiguous_ids` 不自动处理；PostgreSQL 配置但不可用时 503） |
| POST | `/api/trips/import` | 从 CSV / iCalendar 文本导入行程（JSON body `{"content": ..., "timezone": "Asia/Shanghai"}`，`timezone` 为浏览器 IANA 时区（可选，定时事件按它取日期；缺省回退服务器本地时区），自动识别格式；与 CLI `trips-import` 同一解析与去重语义，`dry_run=1` 只预览；在导入专用执行器 + 背压执行，单次最多 5000 行、`content` ≤ 50 万字符；缺省目的不覆盖已有行程；无有效行返回 400 并附行号（CSV）/ 事件序号（ICS）原因；响应含 `warnings` / `warnings_total`（疑似跨行合并记录，不影响计数）；按客户端 IP 与知识库导入共用导入桶（`KB_IMPORT_RATE_LIMIT`，超限 429） |
| PUT | `/api/trips/{id}` | 更新行程（部分字段；与其它行程完全重复时返回 409，返回日期重叠的 `conflicts`） |
| GET | `/api/trips/{id}/calendar` | 导出行程为 iCalendar（`.ics`）全天事件，含出行前一天 09:00 提醒；下载名 `aligo_<用户>_<trip_id>.ics`；响应头 `X-Aligo-Export-Truncated-End`（返程日期无法解析、按单日导出）与 `X-Aligo-Export-Unimportable`（回导会被拒绝）为 `0` / `1` |
| GET | `/api/trips/calendar` | 导出全部历史行程为一个 iCalendar 文件（缺出发日期的行程跳过；每个事件含前一天 09:00 提醒）；下载名 `aligo_<用户>_trips.ics`；`X-Aligo-Export-Skipped` / `X-Aligo-Export-Truncated-End` / `X-Aligo-Export-Unimportable` 分别报告缺出发日期、返程日期被截成单日、回导会被拒绝的条数（面板据此 toast）；导出整体超过导入上限时另带 `X-Aligo-Export-Over-Limit: panel|cli`（面板 / CLI 回导会被整份拒绝） |
| GET | `/api/trips/export` | 导出历史行程为 CSV（UTF-8 BOM，含表头，便于 Excel 打开）、HTML 台账（`format=html`，可打印 / 分享）或 iCalendar 合集（`format=ics`，与 CLI `trips-export --ics` 同一渲染）；`q` / `status` / `today=YYYY-MM-DD`（面板上报的浏览器本地日期，缺省服务器本地日期）与列表接口同一口径过滤后导出；下载名 `aligo_<用户>_trips.<ext>`；CSV 分支带 `X-Aligo-Export-Unimportable`（回导会被跳过的行数，与 CLI 回导自检同源），ICS 分支另带 `X-Aligo-Export-Skipped` / `X-Aligo-Export-Truncated-End` / `X-Aligo-Export-Unimportable` |
| GET | `/api/trips` | 历史行程列表（`limit` / `offset` 分页，`order=asc\|desc` 切换记录顺序，`q` 按路线 / 日期 / 目的过滤，`status=active\|upcoming\|past` 按面板状态徽标过滤、`today=YYYY-MM-DD` 指定「今天」（面板传浏览器本地日期，缺省服务器本地日期），附带日期重叠信息；PostgreSQL 配置但不可用时 503，与状态 / 导出 / CLI 同口径，不回退陈旧 JSON） |
| DELETE | `/api/trips/{id}` | 删除一条历史行程（同步统计与常去目的地） |

> 说明：短期记忆按会话级管理——进程内实现随服务重启清空，配置 `REDIS_URL` 后由 Redis TTL 控制；长期记忆与历史会话在默认 JSON（`data/memory/`）或 PostgreSQL 中持久化。用户 / 会话 ID 限制为中文、字母、数字、下划线与短横线（避免路径穿越）。服务默认只监听 `127.0.0.1`，按单机演示用途设计。

---

## 测试

### 回归测试（无需 API Key）

使用确定性 mock 模型覆盖核心逻辑，CI 中自动运行：

```bash
pytest tests/ -q
```

> 注意：Milvus Lite 是单文件数据库，同一时间只允许一个进程持有锁。请勿同时运行两个 `pytest` 进程（也不要一边跑全量测试、一边跑覆盖率或同时开着一个连着知识库的 Web 服务），否则 `test_orchestration` / `test_rag_agent` 等用例会因拿不到锁而瞬时失败或多出跳过；等前一个进程结束后单独重跑即可。

主要测试覆盖：

| 范围 | 文件 |
|------|------|
| Web API / SSE / 会话 / 导出 / 知识库接口 / 自检 | `test_webapp.py` |
| 前端行为（Node `node:vm`，无 node 时跳过） | `test_app_js_behaviors.py` + `app_js_harness.js`（会话请求竞态守卫、知识库预览 / 重复检查面板、渲染与转义基线） |
| 编排调度与记忆更新 | `test_orchestration_unit.py`、`test_orchestration.py` |
| 存储一致性与回退（JSON / Redis / PostgreSQL） | `test_storage_factory.py`、`test_storage_integration.py`、`test_webapp_postgres.py`、`test_webapp_redis.py`、`test_cli_postgres_integration.py`、`test_long_term_memory_lock.py`（跨进程写锁） |
| 知识库维护与状态 | `test_knowledge_admin*.py`、`test_knowledge_stats.py` |
| 检索评测与 CLI 退出码 | `test_rag_eval.py`、`test_cli_eval.py`（含 `--json`） |
| CLI 一次性命令与 JSON 输出 | `test_cli_skills.py`、`test_cli_json.py`、`test_doctor.py` |
| CLI 知识库 / 行程 / 会话命令 | `test_cli_kb_manage.py`、`test_cli_kb_transfer.py`、`test_cli_trips.py`、`test_cli_trips_dedupe.py`、`test_cli_trips_export.py`、`test_cli_sessions.py`、`test_cli_preferences.py`、`test_cli_stats.py`、`test_cli_user_export.py` |
| Milvus 客户端 | `test_milvus_client.py` |
| 偏好抽取 / 历史消息渲染 / 短期记忆 / 访问日志 / 限流 / 进度等工具 | `test_preference_context.py`、`test_stored_message.py`、`test_trip_export.py`、`test_short_term_memory.py`、`test_access_log.py`、`test_rate_limit.py`、`test_rate_limit_redis.py`、`test_progress.py`、`test_result_formatter.py`、`test_text_chunker.py`、`test_weather.py`、`test_skill_loader.py`、`test_conversation_runtime.py`、`test_travel_stats.py`、`test_console.py`（stdin 编码与 read(size) 余量）、`test_exporter.py`、`test_config_env.py`（密钥清洗 / 回退 / 健康检查预算）、`test_timestamps.py`、`test_session_html.py`、`test_trip_conflicts.py` 等 |

### 端到端测试（需要真实大模型）

```bash
python tests/test_cli_qa.py        # 完整跑通所有意图的 QA 测试
python tests/test_memory_system.py # 记忆系统（含 LLM 总结）
```

### 存储后端集成测试（需要 Redis / PostgreSQL）

```bash
docker compose up -d --wait
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
│   ├── workflows/
│   │   ├── ci.yml                   # CI：ruff + mypy + pytest（覆盖率 / wheel 打包检查）
│   │   └── release.yml              # 打 tag 时构建并发布 wheel / sdist
│   ├── dependabot.yml               # 依赖更新
│   ├── ISSUE_TEMPLATE/              # Issue 模板（Bug / 功能建议）
│   └── pull_request_template.md     # PR 模板
├── agents/                          # 核心编排层
│   ├── intention_agent.py           # 意图识别（语义理解）
│   ├── orchestration_agent.py       # 协调器（优先级 + 并行调度）
│   ├── lazy_agent_registry.py       # 智能体插件注册器（懒加载）
│   └── conversation.py              # 对话运行时（Web 界面的完整流水线）
├── .claude/skills/                  # Skill Plugins（12 个子智能体 / 技能）
│   ├── ask-question/                # RAG 知识库问答（含文档与向量库）
│   ├── event-collection/            # 事项收集
│   ├── plan-trip/                   # 行程规划
│   ├── preference/                  # 偏好管理
│   ├── query-info/                  # 联网信息查询
│   ├── memory-query/                # 记忆查询
│   ├── trip-budget/                 # 预算建议（v2.0）
│   ├── packing-list/                # 行李清单（v2.0）
│   ├── travel-checklist/            # 行前检查清单（v2.0）
│   ├── local-transport/             # 市内交通建议（v2.0）
│   ├── safety-tips/                 # 安全提醒（v2.0）
│   └── trip-notes/                  # 差旅贴士（v2.0）
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
│   ├── stored_message.py            # 历史消息渲染（Web 导出 / CLI 共用）
│   ├── trip_export.py               # 行程 CSV 渲染（Web / CLI 共用，防公式注入）
│   ├── rate_limit.py                # 对话接口进程内滑动窗口限流
│   ├── milvus_client.py             # 进程内共享 Milvus Lite 客户端（含首次打开重试）
│   ├── knowledge_paths.py           # 知识库路径 / 集合名统一解析
│   ├── knowledge_stats.py           # 知识库集合 / 片段数状态
│   ├── knowledge_admin.py           # 知识片段列表 / 新增 / 更新 / 删除 / 导出导入
│   ├── rag_eval.py                  # RAG 检索离线评测（hit@K / MRR）
│   ├── access_log.py                # 访问日志格式化（文本 / JSON lines）
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
├── SECURITY.md                      # 安全策略（漏洞报告 / 部署提示）
├── CHANGELOG.md                     # 更新日志
├── .env.example                     # 环境变量示例（LLM / 存储 / 限流 / 日志）
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
- ⚡ **Redis** - 短期记忆与共享限流可选后端（设置 `REDIS_URL` 启用，List + TTL）
- 🔍 **Milvus Lite** - 本地向量数据库（RAG 知识库）

### 向量化与检索
- 🧠 **BGE-small-zh-v1.5** - 中文Embedding模型（本地部署）
- 📚 **Sentence-Transformers 6.3.0** - 向量化工具库
- 🎯 **余弦相似度检索** - Top-K检索算法

### 联网与搜索
- 🌐 **DuckDuckGo (DDGS 9.16.1)** - 免费网络搜索引擎
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
- 🖥️ **Rich 15.0.0** - 精美的CLI终端界面

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
- **生产模式（可选）**：设置 `REDIS_URL` 启用 Redis 短期记忆（List + 滑动窗口 + TTL）；设置 `DATABASE_URL` 启用 PostgreSQL 长期记忆（表结构首次连接自动创建）；未配置或连接失败时自动回退默认实现（失败后 60 秒冷却，不阻塞启动）。`requirements.txt` 已包含 `redis` 与 `psycopg`；按「手动安装核心依赖」的清单安装时也需补装这两项，否则后端会静默回退
- **本地一键启动依赖**：`docker compose up -d --wait`（见 `docker-compose.yml`；`--wait` 要等 Redis / PostgreSQL 健康检查通过才返回，首次 initdb 未就绪时应用连接会触发 60 秒 JSON 回退、造成数据分叉），然后：

  ```bash
  export REDIS_URL=redis://localhost:6379/0
  export DATABASE_URL=postgresql://aligo:aligo@localhost:5432/aligo
  python cli.py        # 或 python -m webapp.server
  ```

  `docker-compose.yml` 中 Redis 已开启 AOF 并挂载 `aligo_redisdata` 卷：删除墓碑（TTL 1h）、会话纪元（TTL 24h）等跨实例保护在容器重启后仍然有效（`docker compose down -v` 会连同数据一起清空）；Redis **运行中**故障期间的删除仍只记录在进程内，恢复前进程退出则不再执行（见「已知限制」）。PostgreSQL 的 `POSTGRES_USER/PASSWORD/DB` 只在首次 initdb 生效，且健康检查（`pg_isready`）只验证 TCP 监听与服务器接受连接、**不校验账号 / 密码 / 库是否存在**：修改过这组变量或复用旧 `aligo_pgdata` 卷后，请 `docker compose down -v` 重新初始化，否则应用会连不上并静默回退 JSON（回退期写入恢复后不合并）。

### 多实例部署（可选）

同一份代码可以启动多个 Web 实例，共享 Redis / PostgreSQL 中的会话与限流额度：

```bash
export REDIS_URL=redis://localhost:6379/0
export DATABASE_URL=postgresql://aligo:aligo@localhost:5432/aligo
python -m webapp.server --port 8001
python -m webapp.server --port 8002   # 另一个进程 / 机器
```

- **限流**：两个实例的用户桶、IP 桶、知识库桶与天气桶计数都在 Redis（`aligo:ratelimit:*`），总额度按实例集合共享；`/api/health` 应返回 `rate_limit: redis`、`rate_limit_configured: true`、`rate_limit_degraded: false`。配置了 Redis 时 `rate_limit` 恒为 `redis`，降级由 `rate_limit_degraded` 标记：为 `true` 表示该进程当前回退进程内实现（启动时不可用或运行中故障），恢复共享后自动变回 `false`（60 秒冷却重试）。
- **知识库评测**：同一时间只允许一个评测；配置 Redis 后通过 `aligo:lock:kb_eval` 跨实例共享互斥（TTL 300 秒，token 比对释放，持有期间按 TTL/3 自动续期），进程内也会先互斥再尝试 Redis，因此 Redis 抖动不会让同一实例并发评测；Redis 故障只回退本次加锁并在下次请求重试，恢复后重新参与跨实例互斥。
- **会话**：Redis 短期记忆按「用户 + 会话」共享，同一会话在任一实例都能继续；每个实例的运行时缓存（上限 50）互相独立，同一会话的并发请求建议用会话粘滞（session affinity）保持消息顺序。会话删除的防复活墓碑在配置 Redis 后通过 `aligo:deleted:*` 跨实例共享（TTL 3600 秒与本地一致），删除请求先写墓碑再删库，删除进行中其它实例的新请求即被拒绝，删除失败会回滚墓碑（回滚按请求 token 只撤销自己写的那条；并发删除中实际删成功的一方的删除保护不会被另一方的回滚带走）；每次删除还会以单脚本原子递增会话纪元 `aligo:session-epoch:*`（INCR + EXPIRE 同一脚本，TTL 24 小时），删除前启动的长回合即使在墓碑被撤销后落库也会被纪元守卫否决。Redis 读写失败只回退本次并在 60 秒后自动重试共享墓碑，启动时未拿到 Redis 的实例也会在客户端可用后重新接入；故障期写入失败的墓碑会在恢复后补写（补写前复核删除是否仍有效，已回滚的删除不会被补写成真实墓碑）。进程内墓碑与故障期待补写表都限 1000 条，超限时按用户公平淘汰（只淘汰条目最多的用户自己最旧的一条）：一个用户的删除流量不会把另一用户仍有效的墓碑挤出。但删除与另一实例上**正在生成**的同一会话并发时，生成结束后仍可能写回消息，删除前请先停止生成，或用会话粘滞把同一会话固定到同一实例。
- **持久化**：长期记忆、偏好、行程与会话标题 / 置顶 / 归档都在 PostgreSQL；不要在多实例下使用默认 JSON 后端（文件在各实例本地，跨进程写会互相覆盖）。
- **请求幂等**：幂等缓存（进程内 TTL 10 分钟 + 上限 256 条，超限时先清理过期条目、再按用户公平淘汰——只淘汰当前条目数最多的用户自己最旧的一条：单一用户的突发流量不会把另一用户仍在 TTL 内的缓存条目 / 断连未完成标记挤出；多个用户共同占满上限时仍会淘汰某个用户的最旧条目，没有每用户配额时无法避免）与按用户串行的 asyncio 锁都是进程内状态。响应丢失后重试若被负载均衡分到另一实例，仍会重新执行整轮并重复落库；请配合会话粘滞（session affinity），或把 `IdempotencyCache` 替换为 Redis 实现。
- **PostgreSQL 连接**：生产连接默认 `lock_timeout=3000ms`（等表锁超过 3 秒按「存储暂时不可用」返回，而不是无限挂起）和 TCP keepalive（30s/10s/3，半开连接约一分钟内被发现）；`statement_timeout` 只在自检探测等显式场景设置，大备份导入等合法长事务不受默认语句上限影响。表结构在进程内每个 DSN 只初始化一次（首次持跨进程 advisory lock；另一实例正在建索引时最多等 5 秒即跳过本次 DDL，并在 30 秒内不再重试，表结构由其推进、幂等稍后补齐）。
- **反向代理**：前面有 Nginx / 负载均衡时设置 `TRUST_PROXY_HEADERS=1`，并让最靠近应用的代理以追加模式写入 `X-Forwarded-For`（应用默认取最后一项）；多层代理（CDN→nginx→应用）把 `TRUST_PROXY_HOPS` 设为实际代理层数（如 `2`），否则所有客户端会共用上游出口地址的 IP 桶、一个客户端即可打满全站额度。默认不信任该头是为了防止客户端伪造。
- **观测**：请求指标（`/api/metrics`）是进程内统计（只统计 `/api` 路由请求与 Host 白名单拒绝的探测，首页 / 静态资源 / 抓取自身不计），需要分别采集每个实例；SSE 长连接只统计请求数与状态码、不计入延迟分位（整段会话时长没有可比性），以 error 事件收尾（且终态帧已送达）的流单独计入 `sse_errors`（HTTP 已发出 200，按状态码看不到失败）；Host 拒绝探测、未匹配路径的 404、方法不符的 405 与尾斜杠 307/308 不受限流、只计请求数、不计入延迟分位；PostgreSQL 后端的偏好快照缓存命中 / 未命中 / 版本失效计数见 `preference_cache`（同样按实例统计，`version_invalidations` 是 misses 的子集：只统计快照仍在 TTL 内、因跨进程版本变化被迫回源），Prometheus 暴露对应计数，并新增 `aligo_skills_ok` / `aligo_skills_count`（与 `/api/health` 的 `skills.ok` / `skills.count` 同口径）；`/api/health` 在熔断打开或技能目录不可用（`skills.ok` 为 `false`）时 body 的 `status` 为 `degraded`（HTTP 仍返回 200，K8s / 负载均衡的就绪探针需要解析 body 或改用 `storage.*_degraded` 等指标），可用于实例级就绪检查；注意熔断状态（open / half_open）同样报 `degraded`，而熔断恢复只由真实对话流量推进——以 body 的 `status` 做就绪探针会把 OPEN 实例推进到 HALF_OPEN 后永久摘除（探测流量不会让熔断关闭），就绪探针建议只看 `skills.ok` 等静态条件、熔断状态用于告警，重启进程是最终兜底。
- **知识库**：Milvus Lite 是本地单进程文件锁，多个实例无法共享同一个向量库文件；这里的多实例共享适用于无 RAG 或各实例独立知识库的场景，需要共享向量库时请替换为 Milvus 服务端部署。

### 知识库初始化
- 首次运行前必须初始化RAG知识库
- 知识库文档位于 `.claude/skills/ask-question/data/documents/`
- **初始化命令**：源码运行用 `python .claude/skills/ask-question/script/init_knowledge_base.py`；wheel 安装后内置文档目录不随包分发，用 `aligo kb-init --documents <存放 .txt 文档的目录>`（或设置 `ALIGO_KB_DOCS_DIR`）指向自己的文档目录，脚本与数据库位置由安装目录 / 用户数据目录自动解析
- Milvus数据库文件生成在 `.claude/skills/ask-question/data/rag_knowledge/milvus_lite.db`（wheel 安装为 `<用户数据目录>/rag_knowledge/milvus_lite.db`）
- **Windows 说明**：`requirements.txt` 会按平台自动选择依赖（非 Windows 用 `pymilvus 2.6.9 + milvus-lite 2.x`，Windows 用 `pymilvus 3.x + milvus-lite 3.x`）。如果目录中已有旧版（2.x 时代）生成的 `milvus_lite.db` 文件，需先删除再运行初始化脚本（两个版本的数据文件互不兼容；该文件为可再生文件）

### 性能优化
- 懒加载机制：系统启动时仅扫描 Skill 元数据，首次调用时才加载
- 并行调度：同优先级 Agent 并发执行，缩短多信息收集类请求的总耗时
- 长期记忆总结缓存：同一会话内其它会话历史未变化时复用上次总结，避免每轮多一次 LLM 调用
- 存储连接复用：PostgreSQL 后端不再每轮对话重建连接；Web 会话缓存按 LRU 淘汰（上限 50）并在服务关闭时释放
- 快照写路径按磁盘指纹跳过重复解析：JSON 长期记忆的写操作 / 批量写在磁盘自上次加载后未变时不再整份重读（外部进程的原子替换、脏快照与损坏待复核仍完整重载）；导出清洗与 NUL 剔除以单遍完成
- 热路径索引与按需投影：PostgreSQL 首次初始化自动补建 `(user_id, session_id, created_at, id)` 消息索引（CONCURRENTLY，不阻塞写入），会话详情 / 删除 / 改名 / 置顶 / 归档不再扫描用户全部消息；知识库片段浏览按页取正文（只扫 id 排序分页），大库翻页不再整库物化
- Web 前端按需渲染：记忆面板关闭时每回合只刷新侧栏「即将出行」与归档入口；面板打开且行程展开时一次刷新只整块渲染一次；侧栏搜索的归档匹配提示改为后台探针，不阻塞主结果

### 已知限制
- **隔离数据的升级遗留与大小写变体（冻结项）**：记忆文件损坏时会被改名为 `<用户>.json.corrupt-*` 并写 `<用户>.json.quarantine` 标记。本版本之前的旧版会用「最先发现损坏的请求」的用户 ID 拼备份名：在大小写不敏感文件系统（Windows / macOS）上，若该写法与真实账号仅大小写不同，升级后真实账号可能被归属校验拒绝（普通请求与 `user-import` / `/api/import` 恢复入口都会提示「用户 ID 与已有记忆文件仅大小写或 Unicode 规范化形式不同」）——把受影响的 `.corrupt-*` 备份重命名为与账号大小写一致即可恢复正常；同族残留还包括 `delete_all` 对标记名 / 迁移副本的变体归属误判、`ensure_persisted` 在空导入时缺少隔离证据归属校验、PostgreSQL 接管时清理 JSON 标记不校验归属、仅标记无备份状态的写路径文案、会话中途被隔离后交互面板展示空数据。该子系统经连续四轮对抗复审持续产出同族边界（多次由前一轮修复引入），已按冻结策略只修崩溃 / 数据丢失级问题，上述项记录不修。**知识体积轴**（合并预检的全局知识库计入 / 增量拦截）同理冻结：保证不复引入「阻断用户数据恢复」类回归，其余同族边界记录不修。
- **CLI 管道输入编码**：交互模式从管道 / 重定向读取时逐行自动解码（UTF-8 优先，失败回退系统编码；支持 UTF-8 BOM 与 UTF-16，含无 BOM 的 UTF-16LE）；GBK 与 UTF-8 同时可解但结果不同的首行（如「唯一」「一一」「十」）会被拒绝并提示设置 `ALIGO_STDIN_ENCODING=gbk`（或 `utf-8`）后重试，后续歧义行按 UTF-8 解析并向 stderr 告警一次。管道模式下任一命令失败（模型调用错误、落库失败、回合被 Ctrl-C 中断、回合开始前检测到记忆隔离而中止等，凡是没有任何回答的回合）以退出码 1 结束；交互终端行为不变。从管道 / 重定向读取时提示符（`用户ID:`、`>`）输出到 stderr，stdout 只保留命令输出（不再出现 `用户ID: ✓ …`、`>: 表格` 的粘连）；`python cli.py > session.log` 等 stdout 重定向场景下提示符仍显示在终端。一次性命令的 stdout 与 stderr 都做了管道保护：下游（`| head`、`2>&1 | head`、提前退出的分页器）关闭管道后写入静默丢弃，成功命令不会以退出码 120 / traceback 结束；输出落到文件或终端时内容不变。
- **行程导入编码**：面板读取 CSV / ICS 文件时识别 UTF-16（BOM 或无 BOM 的 NUL 规律）、严格 UTF-8，失败后回退 GB18030（Excel 中文 CSV）；Big5 等与 GB18030 字节重叠的编码无法可靠区分（可能解码为乱码），请先另存为 UTF-8。服务端 `content` 上限 50 万字符、单次最多 5000 行（JSON 中文序列化后仍在 2 MB 请求体内），面板同时限制文件 ≤ 2 MB；缺省目的不会覆盖已有行程的目的。
- **Milvus Lite 是单进程文件锁**：Web 服务运行且已打开知识库后，外部再运行 `init_knowledge_base.py` / `cli.py kb*` / `doctor` 会提示锁占用；请先停止 Web 服务再执行这些命令。`reset_milvus_client` / `shutdown_milvus_clients` 会关闭客户端并停止对应的进程内 milvus-lite server（释放数据目录锁与文件句柄，初始化脚本「不可读库改名重建」与清理新建库目录因此可以在 Windows 上完成），但已持有旧客户端的在途请求无法阻止（Milvus Lite 不支持事务性替换）——重连仍失败时请重启进程（Web 服务）。
- **知识库目录在进程内固定**：`default_knowledge_dir()` 首次解析成功后固定在进程内（Skill 数据目录与项目根兼容目录二选一）——面板管理接口与 RAG 智能体始终使用同一个库，服务运行期间新建 / 迁移出另一个库目录不会出现「面板看似被清空、新增写进旧库」的分裂；切换库（重建 / 迁移）后请重启 Web 进程（或调用 `reset_default_knowledge_dir()`）。半初始化状态（库文件存在但集合缺失）下，管理接口与 `GET /api/knowledge`、`python cli.py doctor` 一致提示「集合不存在，请重新运行初始化脚本」。
- **技能模块在进程内缓存**：`LazyAgentRegistry` 按技能脚本路径缓存已加载模块（同一技能的多个会话注册器共享模块与模块级状态）；运行中修改技能脚本 / SKILL.md 不会热更新，需重启进程生效（`ALIGO_SKILLS_DIR` 指向不同目录的技能互不串用）。
- **技能元数据预算与重名**：SKILL.md 的 frontmatter `name` 会去首尾空白；同名技能按排序保留首个（目录名与 name 相同的目录优先）并向 stderr 告警（`doctor` 的「技能目录」检查同时报出重名）；description 单条 500 / 合计 4000 字符、技能正文注入上限 20000 字符（超出截断并注明）——外部技能包写出超长元数据不会再撑爆每轮意图 prompt 或技能 prompt。
- **非 ASCII 路径**：Milvus Lite 3.x 在含中文等非 ASCII 字符的目录下无法写入 Faiss 索引（同进程自检走内存索引仍会「通过」，重启后集合不可加载）；建议把项目放在纯英文路径（如 `D:\Code\chailv`）。wheel 安装的知识库固定在 `%LOCALAPPDATA%\aligo\data\rag_knowledge`，中文用户名下同样命中——请把 `ALIGO_DATA_DIR`（或 `ALIGO_SKILLS_DIR`）指向纯英文目录后重建。初始化脚本会提前失败并给出提示；状态接口在加载失败时也会直接给出该原因。
- **请求指标是进程内统计**：多实例部署时 `/api/metrics` 只反映当前进程；限流在配置 `REDIS_URL` 后由 Redis 共享计数，未配置时为进程内实现。
- **天气缓存**：`/api/weather` 的成功结果 10 分钟新鲜缓存默认在进程内；配置 `REDIS_URL` 时同时写入共享缓存（`aligo:weather:<城市>`，10 分钟 TTL），多实例直接复用、不再各自请求上游，Redis 故障进入 60 秒本地冷却后退回进程内。共享负载带抓取时刻并做结构与有限数值校验，回填进程内缓存按真实年龄计（10 分钟窗口不会因回填翻倍、stale 兜底窗口不被拉长）；同城并发合并（单飞）与失败 / 降级缓存仍在各进程内：多实例同时未命中的首个请求仍可能各自访问上游一次；失败缓存不共享，避免一个实例的上游故障把其它实例钉死。
- **天气地理编码的英文别名**：中文城市名走 Open-Meteo 的 `language=zh` 索引；该索引对境外城市覆盖与排序不可靠（同名小城 / 村庄会排在前面，如「罗马」只命中澳大利亚 Roma）。常用境外城市（61 个，如东京、伦敦、纽约、罗马、巴黎、首尔、悉尼）通过内置别名表改查英文索引；未收录的境外中文小地名仍可能解析不准，查不到或结果明显不符时请改用英文城市名（如 `London`）。
- **分享链接与用户 ID**：`?user=` / `?session=` 深链接在客户端按与服务端一致的规则校验（用户 ID ≤64 码点且 ≤210 UTF-8 字节、拒绝设备保留名；会话 ID 允许设备名会话、拒绝保留路由 `export`），非法值提示后忽略并从本地存储回退，不会让标签页停在所有请求 422 的状态；侧栏「复制对话链接」对不可分享的会话给出提示。
- **知识库一致性与检索细节**：导入 / 备份恢复接受的内容必须能被 UTF-8 编码且不含 NUL（与面板写接口同口径；NUL 片段入库后永远无法在面板保存）；换行统一为 LF（面板 textarea 的基线归一，含 CRLF 的片段原样保存不再误报「已在其他会话被修改」）。旧备份里被重新切分 / 合并的极长分片按「来源分片下标」对齐逐片元数据（title 的 …(Part N) 等不再整组退化成第一片）。备份里的多分片组内容若已以无 group 的独立片段存在，重复导入按「已存在」计（不再误报顺序冲突 / 备份未能恢复）。Milvus Lite 文件锁被其它进程（通常是 Web 服务）持有时，状态 / doctor / CLI 会提示「知识库正被另一个进程使用，请先停止该进程」，而不是笼统的 Open local milvus failed。聊天检索的关键词路不再被「跨词二元组」稀释（含全部真实内容词的片段能通过证据阈值）；只命中疑问词 / 跨词伪影的片段按无证据处理（归一化 BM25 为 0，不会以假高分越过 `RAG_MIN_SCORE`）；超长消息按末句（优先含疑问标记）检索，问题写在结尾也不会被嵌入截断；首轮只有擦边无关命中时也会用简化查询补召回，且救援成功后只保留与重检索同组的分片（无关擦边片段不再与真实证据同权注入，合并结果不超过 top_k）。整组拼装的参考来源与「参考片段」取命中分片而非 part 1；来源与片段按文档序号一一对应（面板新增的仅含分类的片段也能回指），CLI 实时问答与 Web / 历史 / 导出同口径展示参考来源。整份编辑把多分片文档缩成单片时以被点击片为底（分类 / 标题不再静默改回第 1 片），更新已删除片段在加载嵌入模型前即返回 404；导入备份里「内容齐全但 part 顺序不同」的组计入布局冲突（`skipped_conflict`）并提示整份编辑后重试，不再混入「N 条无效」；知识库未初始化 / 缺依赖的提示按安装形态给出可执行命令（源码为仓库相对脚本，wheel 为 `aligo kb-init`），原生库加载失败（如缺 VC++ 运行库）与真正缺依赖区分提示。
- **知识片段长度**：嵌入模型（BGE-small-zh）只编码前 512 token（约 400 个汉字）；Web / API 单条上限 2000 字，整份文档上限 20000 字（CLI `kb-add` 同样检查），超过 400 字的内容在新增 / 更新 / 导入时会自动按语义边界拆成多条片段（列表中各占一行）。每次拆分带 `group` / `part` / `parts` 元数据：面板点击任一分片的「编辑」会回填**整份文档**（`GET …/group`，整篇 ≤20000 字），保存后按**整组替换**（先写新分片、再覆盖首片、最后删除同组旧分片，中途失败不丢旧内容）；也可**整组删除**（`DELETE …?group=1`）。导出的备份再导入时按原始分片边界入库（空串拼接即原文，不再「拼回整篇再重切」）——文档大小多轮往返不膨胀；分片首尾空白在入库前剥除；备份恢复中超过 20000 字的文档按 20000 字分段导入（内容完整保留，分组边界会改变；不中断整份备份）；重复导入按内容跳过已存在分片并**补齐缺失分片**（上次中途失败的残缺组会被补全，组号与编号保持）；part 编号重复 / 跳号的组直接报出具体 group 与 part；单条新增 / 编辑内容超过 2000 字会被拒绝并提示拆分，按分片边界恢复的备份条目不受该单条上限约束（拼接后仍以 20000 字为界）。旧数据没有 `group` 标识，单条编辑仍返回 400，请删除后重新添加；任何带 `group` 的分片都不能单条删除（返回 400），请用 `group=1` 整组删除。
- **本地 JSON 长期记忆**：同进程写入按文件路径的 RLock 串行化并在修改前重载（等待上限同为 6 秒：大备份导入等长写入持锁期间，其它请求有界失败为可重试的存储错误，而不是无上限占用 Web 工作线程）；CLI 与 Web 同时修改**同一用户**时通过 `<data>.json.lock` 文件锁串行化（Windows / Unix 均支持，等待上限 6 秒——临界区含落库守卫的 Redis 墓碑 / 会话纪元检查与 fsync；超时按写盘失败处理）。网络文件系统（NFS / SMB）上的锁语义取决于挂载方式，演示时仍建议只保留一个写入端。写入用紧凑 JSON（无缩进空白），读接口（`/api/status`、`/api/sessions`、导出）仍按整文件快照解析，但每个请求只解析一次：导出快照在 `(ino, size, mtime_ns)` 指纹未变时复用实例刚加载的快照，其它进程经原子替换写入后指纹必变、照常重载（网盘等不提供稳定 inode 的文件系统退化为每次重读）。长寿命实例（交互 REPL 的每条命令、每轮对话开始）的重载同样按该指纹 + 脏标记跳过无变化重读；配置 PostgreSQL、写失败留下未落盘修改或损坏 / 隔离待复核时仍走完整重建。一轮对话内的多处落库（偏好 / 行程 / 会话元数据）经 `write_batch` 合并为一次重载 + 一次原子写回（批内异常整体回滚；仅对当前线程生效，其它线程的写照常各自加锁等待），大账号一轮由 3-4 次整份重写降为一次。会话列表的时间戳排序键（`chronological_key`）按「时区世代 + 原文」做进程内有界记忆化（20 万条上限，进程内 `tzset` 切换时区后旧结果不再命中）。无查询词的会话聚合（条数 / 标题 / 排序）按用户做进程内有界缓存，指纹取历史条数、首尾消息、会话标题 / 置顶 / 归档集合与时区偏移——翻页和每回合后的刷新命中即直接复用，任一会话元数据或历史末条变化都会自然失效；前端刷新按已加载条数一次取回整窗（`/api/sessions` 单页上限 1000），展开过的列表不再按 200 一页串行重扫。CLI `sessions` 列表只在 `--query` 时构建匹配文本（会话命中即停止后续渲染）；`session-show` 的消息预览同样只惰性扫描前 80 字（不再对整条最高 50 MB 的消息做 `split()`，内存峰值从约消息体积的 12 倍降到与预览长度相关），列表列宽按终端显示宽度计算（中文 / emoji 标题不再把消息数与时间列挤歪）；`sessions-export` 复用 CLI 已算好的会话分组；`user-export` / 交互导出把体积预检序列化出的同一串字节直接落盘（同一份备份不再清洗、序列化第二遍），导出产物逐字节不变。
- **流式对话的同一用户串行**：`/api/chat/stream` 在返回响应前等待该用户的对话锁，最多 50 秒（期间每 0.5 秒探测客户端是否断开，拿到锁后再确认一次）；超时返回 503 并带 `Retry-After: 5`，避免排队请求被反向代理读超时变成 504；持锁者是备份恢复时文案改为「该账号正在恢复备份」，否则为「同一用户已有对话正在进行」。上一回合结束后重试即可。
- **用户 ID 大小写与 Unicode 规范化**：Windows / macOS 默认文件系统大小写不敏感，macOS 的 APFS/HFS+ 对 NFC/NFD 规范等价也不敏感；JSON 后端中仅大小写不同（`Alice` / `alice`）或规范等价（`José` 与 `Jose`+组合音标、合成/分解 Hangul）的用户 ID 会映射到同一记忆文件。程序检测到文件记录的 user_id 与请求仅大小写或规范化形式不同时拒绝访问并提示使用一致写法；PostgreSQL 故障回退到本地 JSON 期间，这类冲突按「PostgreSQL 暂不可用，请稍后重试」的可重试提示返回（PG 恢复后自动回到原存储）。PostgreSQL 后端按列存储，两种写法是不同账号。用户 ID 另限 UTF-8 共 210 字节（会成为文件名，按最长派生名 `.corrupt-*` 给 POSIX 单文件名上限 255 字节留足余量；240 字节的 ID 会让导入锁 / 隔离 / 迁移备份名超限）。
- **知识库问答的相似度阈值**：低于 `RAG_CONFIG["min_score"]`（默认 0.5）的检索结果不再注入回答提示词。实测 BGE-small-zh 的余弦基线就在 0.5 附近：0.5 能滤掉明显无关的问题（如「推荐一部电影」「Python 怎么安装」），0.5–0.7 之间的擦边问题（如「护照怎么办理」命中应急文档里提到护照的片段，0.68）仍可能注入，由回答提示词要求模型只取相关部分；调高阈值必须同步运行 `python cli.py eval` 复核 30 个合法问题的命中率（实测合法题最低 0.60，存在与擦边问题的分数重叠，单一阈值无法完全分离）；`cli.py eval` 与管理接口保持纯向量口径，同一 `min_score` 过滤的是向量余弦距离（聊天链路过滤的是 `max(余弦, 归一化 BM25)` 的混合 distance——数值同源但不在同一分数空间）。知识库未初始化（找不到 `milvus_lite.db`）时问答会明确提示先运行初始化脚本，不会创建空库后回答「没有找到」。
- **单字中文查询的 BM25**：中文按字符二元组切词，单字查询（如「京」）与二元组索引不可比，关键词路不参与、检索退回纯向量；完整词（「北京」）不受影响。
- **整组替换与检索并发**：面板编辑多分片文档按「写新分片 → 覆盖首片 → 删除旧分片」多步提交，每步单独写库；与针对该组的检索并发时，可能读到新旧混合的窗口期内容（几秒内）。检索侧已按 `group` / `part` 拼回整篇并对重复分片去重；BM25 索引的全量扫描与写入共用知识库读写锁（写忙时本次混合检索退回纯向量），仍建议在低峰期维护多分片文档。
- **存储后端故障回退**：配置的 PostgreSQL 连接失败时自动回退本地 JSON；故障期间新会话写入 JSON 的消息 / 偏好不会在恢复后自动合并到 PostgreSQL（文件保留在 `data/memory/`，恢复时日志会给出警告），需要时请手动迁移。**删除、元数据读写与偏好 / 行程写入例外**：删除会话、删除最后一轮、删除行程、删除偏好，以及行程新增 / 编辑（Web 接口与导入端点）、偏好设置（PUT）、会话标题 / 置顶 / 归档、**会话详情读取（`GET /api/sessions/{id}`，避免把回退库里的「空会话」误当成已删除）与会话列表 / 状态概览读取（`GET /api/sessions`、`/api/status`，避免列表展示回退库的陈旧会话、点开详情却 503 的口径分叉），以及交互模式的 `status` / `history` / `preferences` 读命令（同样不把回退 JSON 当账号现状展示）**在 PostgreSQL 不可用（连接失败 / 60 秒冷却期）时返回 503，而不是作用在回退的 JSON 库上——否则恢复后数据会「复活」或「凭空消失」；恢复后再执行即可。
- **本地 JSON 损坏文件的保护**：加载时发现记忆文件无法解析会先备份为 `<data>.corrupt-<时间戳>-<pid>` 再以空数据启动；若加载时等锁超时或备份失败（Windows 上文件被其它进程占用），下一次写操作会在持锁状态下补做复核 / 备份，备份仍失败则拒绝写入并提示手动处理文件路径，绝不会用空快照直接覆盖损坏文件。
- **`/api/health` 的存储描述**：`storage.short_term` / `storage.long_term` 与 `*_degraded` 反映**最近一次实际创建或健康探测**的后端；本进程尚未创建过长期记忆时，`/api/health`、`/api/metrics` 与 `/api/status` 会对 PostgreSQL 做一次有界（≤2 秒）只读连通性探测并缓存结果（失败进入 60 秒冷却），数据库不可达的新实例不会乐观误报 `postgres`。已经成功启用的 PostgreSQL 也按 30 秒节流复用同一只读探测复验：运行中停机后纯健康 / 指标轮询不再一直报 `postgres`（有应用流量时下一次建连失败同样会纠正）。运行中 Redis 故障回退进程内缓存时 `short_term_degraded` 为 `true`；该标记按**最近一次真实 Redis 操作**判断——冷却到期但期间没有任何成功操作时仍为 `true`，不会因「冷却结束」误报恢复。
- **Redis 短期记忆的故障重放**：运行中 Redis 故障时短期记忆回退进程内影子缓存，故障期的消息与删除操作记入**进程内**日志，恢复后由同一进程的下一次访问按顺序重放（多进程部署下各进程各自补写；限流与删除墓碑仍跨实例共享）。截断 / 清空与回填去重按消息时间戳判断新旧，前提是同一部署内各实例**时钟基本同步且进程时区一致**（建议全部 `TZ=UTC`；仅时钟同步不够——跨时区实例会把同一时刻归一成不同字符串，指纹去重、有序插入与截断守卫都会失效）。故障期日志只存在于当前进程：进程重启、或一次性命令（如 `cli.py session-delete`）在 Redis 恢复前退出后，未重放的删除不再执行，该会话键会按 TTL（默认 1 小时）自然过期；删除墓碑的共享检查在 Redis 抖动后有 60 秒失败冷却——删除会话（Web / CLI）与故障重放前的墓碑检查属于低频关键路径，会**绕过冷却强制真实读取一次**（避免冷却期静默放行已删除会话），其余高频枚举（如备份恢复逐会话检查）在冷却期内按 fail-open 跳过（与存储回退同一口径）。
- **Redis 配置热更新**：修改 `REDIS_URL` 后调用 `reset_failure_cache()` 只影响之后的创建调用（客户端缓存按 URL 区分、reset 与在途创建互斥）；限流器实例、已建会话的短期记忆与 Web 状态已直接缓存的客户端仍指向旧实例，需要重启进程才能全部切换。
- **交互模式首次加载技能**：首次触发知识库问答等技能会在后台线程加载嵌入模型（torch / BGE，可能数十秒），加载期间按 Ctrl-C 只取消本轮等待，进程仍会等加载线程结束后退出；技能加载完成后再按 Ctrl-C 可在提示符处立即返回。
- **Markdown 导出的链接语法**：会话 Markdown 导出的正文会转义行内代码之外的尖括号与链接 / 图片语法（`[x](url)` 渲染为字面量文本），预览器里不可点击、也不会自动加载远程图片；代码块 / 行内代码内的内容为保真原样保留（其中的 `[...](...)` 由预览器按代码显示，不会成为链接）。HTML 导出为全字段转义，不受影响。
- **前导 `-` 的用户 / 会话 ID**：Web 与深链接（`?user=-foo`）允许这类 ID，但 CLI 把它当命令行选项拒绝（`user-export` / `session-*` 不可用）——此类账号请用 Web 面板导出 / 恢复备份。
- **Windows / macOS 文件名大小写与规范化不敏感**：用户 ID `Alice` 与 `alice`、或规范等价的不同写法（NFC/NFD）会读写同一个 JSON 记忆文件；需要区分用户时请使用字符形式与大小写都不同的名字。
- **PostgreSQL 的行程去重 / 编号为应用层检查**：`trip_id` 使用 `pg_advisory_lock` 串行生成，新增 / 更新 / 删除行程共用同一把锁；单实例 Web 的行程写入已串行化，多实例部署仍建议自行加唯一约束兜底。连接在「语句已提交、响应丢失」时（网络闪断 / 数据库重启）：消息 / 查询计数 / 单条行程插入与删除最后一轮**不会自动重放**（不会重复写入或误删期间新到的回合），按持久化失败上报——极端情况可能出现「实际已写入但本次提示失败」，重试前请先刷新查看；幂等写（偏好 / 标题等 UPSERT、去重导入）仍会自动重连重试。
- **停止生成与后台落库**：用户停止只取消协程，记忆写入线程会继续完成；服务端在释放用户锁前最多等待约 10 秒（`drain_pending_persist` 是总预算：落库、回滚与延迟记忆提交共享同一截止时刻），超时后迟到的写入仍会落地（回滚 / 墓碑保证不写回已删除的会话：Web 与 CLI 的在途轮次都据共享墓碑否决写入）。极端慢存储下「停止 → 立刻删除 / 再次提问」可能与未结束的写入交错，界面可能短暂显示上次的刷新结果。
- **删除最后一轮与元数据**：`DELETE /api/sessions/{id}/last-turn`（面板「编辑重发 / 重新生成」）删空会话时会同步清理该会话的标题 / 置顶 / 归档，避免已不存在的会话残留幽灵元数据；因此只有一轮、且设置过这些元数据的会话重新生成后会丢失它们。同一用户的长回合进行中，删除请求最多等 10 秒，超时返回 409（界面提示稍后再试），不会在前端超时后仍偷偷删除历史；整会话删除（`DELETE /api/sessions/{id}`）采用同一有界语义。多实例部署下短期记忆的截断按「末尾用户提问的回合 id」精确匹配（Web 暂存的提问带 `metadata.client_turn_id`，重启 / LRU 淘汰后从长期记忆回填的 Web 回合会从助手消息 `_meta` 补上回合 id）：另一实例正在生成同文本提问时不会误删对方的轮次；仅无 `_meta` 的旧数据 / CLI 轮次或 turn_id 为空串时退回按末尾提问文本匹配，同文本并发仍可能互相影响。
- **幂等键与断连重试**：`idempotency_key` 绑定请求正文摘要与端点（`/api/chat` / `/api/chat/stream` 互不回放），同键换消息会按新请求执行；换端点、同键同消息重试也视为新请求，但若另一端点已为该键写入「未完成」防重标记（该轮已落库而响应未送达），会返回 409 / 回放 error 终态而不是重复执行（成功负载仍只按原端点回放）；键里的会话取请求的 `session_id`（缺省空串），不带 `session_id` 的首条消息同键重试同样回放，响应里的 `session_id` 是首次执行生成的会话。服务端同时把结果索引到「请求的会话 id」与「首次生成的会话 id」下：客户端在处理 session 事件学会会话 id 后同键重试也能命中回放（成功响应或未完成标记），不会重新执行。面板对话随每次发送生成并携带幂等键，重试按钮复用原键与 turn id，因此网络层重试命中回放而不是重复执行。客户端断连按 499 记账（流式响应在发出 200 头后断开时，线上状态保持已发出的 200，访问日志与 `/api/metrics` 记 499 以便区分完整送达与断连半截流）；熔断打开 / 半开期间同键重试仍回放已缓存结果（回放零 LLM 开销，不受熔断限制）。非流式 `/api/chat` 若该轮已落库，会写入「未完成」标记，复用同一键重试返回 409（不重放空答案、也不重复执行），提示刷新会话历史对账。流式端点同样：该轮已落库时同键重试回放 error 终态（带 idempotent_replay），不重新执行；流式断连的标记在 drain（等待落库线程）之后按最终落库结果写入，取消场景不会因等待被中断而漏写；任一终态（done / error）已送达或服务器已缓存完整结果时不再改写缓存。未能确认落库时仍可能重新执行一遍——客户端应把断连视为结果未知，先按会话历史对账。幂等缓存与按用户串行的 asyncio 锁都是进程内状态：多实例部署时同键重试落到另一实例仍会重新执行（见「多实例部署」）。
- **偏好快照缓存**：PostgreSQL 后端偏好读取有 5 秒进程内快照缓存；本进程的面板 PUT / DELETE 与备份导入直写后会清空所有已建会话的缓存。配置 `REDIS_URL` 时，存储层在偏好写入 / 删除 / 清空后推进共享版本键（`aligo:prefver:<用户>`，INCR 后 EXPIRE 1 天，过期只会让下一次读取多一次重读），跨进程（多实例 / CLI）写入后下一次读取即失效；版本读写失败进入 60 秒本地冷却并按 5 秒 TTL 兜底，不会让故障期每次偏好读取 / 写入都等满 socket 超时；未配置 Redis 时跨进程写入仍可能短暂读到旧偏好。
- **知识库导出的可恢复性自检**：导出按「导入空库」重新规划分片并检查体积 / 条数，但不会拒绝导入端会跳过的旧版超长单条或残缺分片组；这些行仍保留在备份 JSON 的 `knowledge` 段中，恢复时计入 `knowledge_skipped`。导出不会静默：CLI `user-export` 向 stderr 打印跳过提示，Web `/api/export` 响应头 `X-Knowledge-Skipped` 给出跳过计数，提醒该备份不含完整知识。若全局知识库已超出导出上限或分片计划无法规划，`with_knowledge` 导出不再永久失败：用户数据本身仍在限额内时省略 `knowledge` 段继续导出（Web 响应头 `X-Knowledge-Omitted` 携带原因 `over-limit` / `planning-failed`，面板按原因提示本次备份不含知识片段；CLI 向 stderr 打印对应警告；知识库需先把片段删除到导出上限以内，再单独 `kb-export` / 迁移）；用户数据轴超限仍按原口径 413 / 中止。知识库仍可能越过上限的途径：50 MB 字节轴不受写入预检约束（大量长片段可累积到体积上限之外）；升级前已有的越限存量库不会被自动清退；知识管理导入 / 面板添加只按 5 万条行数预检。这些情况下按上述降级处理（响应头携带 `over-limit`），先删除部分旧片段即可恢复完整备份。响应头不随文件保存：按备份文件迁移时请另行判断是否需要知识库。知识导出（`/api/knowledge/export` / `kb-export`）对元数据中的 NaN / Infinity 与孤立代理项做与用户备份同口径的清洗，产物可被标准 JSON 解析器回读并再次导入。
- **旧版检查点目录**：升级前的检查点按 CWD 相对路径写在 `data/runtime/checkpoints`（且负载含原始用户 / 会话键）；新版本默认目录跟随数据根目录，使用默认存储启动时会清理旧 CWD / 包根目录中已超过 TTL 的文件（TTL 内的近期文件保留，避免误删旧进程仍在用的检查点），仍可手动删除剩余目录。
- **反向代理与 `X-Forwarded-For`**：默认按 TCP 对端地址限流。部署在反向代理后需设置 `TRUST_PROXY_HEADERS=1`，并让最靠近应用的代理以追加模式写入 `X-Forwarded-For`（应用默认取**最后一个地址**，前面的伪造前缀不被采用）；**多层代理**（如 CDN → nginx → 应用）时「最后一个地址」是 CDN 出口而非终端客户端，需把 `TRUST_PROXY_HOPS` 设为实际代理层数（1..8，默认 1），应用取倒数第 N 跳；链长不足时回退 TCP 对端并告警。直接暴露服务时请保持默认关闭。uvicorn 的 `proxy_headers` 在启动时已关闭，XFF 只由应用层按 `TRUST_PROXY_HEADERS` 采信。
- **并发新建会话与数据库连接峰值**：会话缓存上限（`max_sessions`）约束的是缓存中的会话数；并发首次请求在淘汰旧会话前各自新建会话，构造中的连接与缓存中的连接会短暂并存（峰值可达上限的数倍）。高并发部署请相应提高 PostgreSQL `max_connections`，或调低会话上限，避免连接失败后进程级回退本地 JSON。
- **限流在请求体解析之后**：`/api/chat` 等接口的限流在请求体解析与 schema 校验之后执行——schema 非法的请求（422）不消耗额度，且请求体（≤2 MB）的 JSON 解析发生在事件循环上。反向代理层限制请求体大小 / 速率可缓解解析类无效请求的刷量。读请求体有总时限：普通接口 60 秒、`/api/import` 300 秒，超时返回 408 并关闭连接；请求体未读尽就拒绝（Host 校验失败、405 / 404 / 尾斜杠 307）同样关闭连接，慢速 / 滴灌上传不会无限期占用连接与内存。
- **导出与进行中的备份恢复**：恢复备份按条目逐条提交，期间导出会得到「只有偏好没有消息」的半截快照。Web 端恢复写入期间对同账号的导出接口（`/api/export`、`/api/sessions/export`、`/api/sessions/{id}/export`、`/api/trips/export`、`/api/trips/calendar`）返回 409「正在恢复备份」并带 `Retry-After`；导出在读取前原子地取该账号的恢复纪元、读完快照后复核——读取期间才开始（即使读完时已经结束）的恢复同样让本次导出作废（409「发生过备份恢复」，PG 快照 / JSON 打开时快照可能定格在写入中途，只查「当前是否在恢复」会漏掉）。恢复标记与纪元是进程内状态：多实例部署或 CLI 与 Web 分属不同进程时互相看不到对方的恢复，跨进程导出仍请在恢复结束后进行。
- **备份恢复的本地优先**：同一会话里内容完全相同的提问（例如导出后在面板编辑重发 / 重新生成过）以目标库为准——备份在该轮次随后的回答（重复或另一台机器上的备选版本）不会追加，避免恢复出没有对应提问的尾部回答、破坏面板的问答配对与「删除最后一轮」。缺失 / 非法时间戳的恢复消息沿用同会话相邻消息的时间；提问 / 行程 / 偏好等其它数据不受影响。需要保留另一台机器的回答时，请在备份侧另行保存。
- **备份恢复的超长行程字段**：面板 / CLI 写接口的出发地 / 目的地 / 目的上限是 100 字；`user-import` / `/api/import` 恢复超过上限的字段时按 100 字截断并在 `skipped` 中逐条提示（保真优先，不整条丢弃）——修复前恢复出的超长行程在面板 / CLI 编辑时必然 422。用旧版本恢复过超长字段的账号再次恢复同一备份时，截断后身份与原行不同，可能新增一条重复行程（错误列表可见，可删除旧行）。
- **备份恢复的时间戳口径**：JSON 后端的消息时间戳是本地墙钟（naive），恢复时的归位排序按当前机器的时区解释——跨时区旅行 / 夏令时回拨 / 校时后写入的两段对话，存储时间的先后可能与真实发生顺序不同，恢复（包括重复恢复同一备份）会按时间戳重排；PostgreSQL 的 `created_at` 是数据库绝对时间，不受影响。恢复带未来时间戳的消息（导出机时钟超前或手工备份，年份上限 2100）后，PostgreSQL 会把之后实时发送的回合排在它之前，JSON 则追加在末尾，两后端对「最后一轮」的展示不同；删除最后一轮按界面展示的最后一轮执行（未来时间戳行也可编辑 / 重新生成）。请在恢复前检查备份里的异常时间戳。
- **导出可恢复性自检按本机后端估算**：`user-export` / `/api/export` 的「恢复到新账号后仍可导出」自检按**本机部署**的落库形状计算（JSON 与 PostgreSQL 对缺失时间戳消息的占位时间戳长度、行程空键补全不同）。JSON 部署导出的备份迁移到 PostgreSQL 部署恢复时，PG 形状略大（每条无时间戳锚点消息 +6 字节、每条新建行程约 +56 字节）：只影响临近 50 MB 上限的极少数备份，可能在 PG 新账号导入时按更大形状 413，而源端导出为 200。跨后端迁移超大备份时请在源端先清理到上限以下，或直接改用 PostgreSQL 部署导出。
- **停机与在途导入**：关闭服务（Ctrl+C / SIGTERM）**不会**等待在途的备份恢复 / 知识库导入——导入执行器是 daemon 线程且刻意不登记 atexit join，lifespan 以 `shutdown(wait=False)` 关闭，端口先关闭、进程随即退出，未完成的导入可能被中断。导入是幂等的（行程按路线 + 日期去重、消息按多重集去重），重新运行同一备份即可补齐；JSON 后端按批原子写回不会留下半写文件，PostgreSQL 已提交的行保留、中断的批次需重跑。需要保证完成时请等待导入结束再退出。
- **模型调用重试会叠加**：应用层对意图 / 编排调用最多重试 3 次；OpenAI SDK 的内建重试已默认关闭（`create_chat_model` 默认 0，由应用层统一重试——避免 3×2 叠加，也让每次尝试的用量统计可见）。非流式请求的 read 超时默认 120 秒（覆盖 8192 tokens 长生成；`LLM_REQUEST_TIMEOUT_SEC` 可调），超时会按可重试错误重跑整轮。注意该值只决定单次 HTTP 请求的 read 超时：编排层对每个子智能体另有独立的 300 秒总执行上限（含懒加载与逐智能体的一次重试），设得更大不会延长子智能体执行，超时任务仍会在上限处被取消。故障期单轮对话可能连续尝试多次、耗时数分钟：外层整轮重试与内层逐智能注重试共享 `turn_retry_budget_sec`（默认 300 秒）总预算，预算耗尽后停止发起新的尝试（不打断在途调用）；熔断半开试探名额的租约（`max(恢复期 × 5, 300s)`）按最长调用时间设计，慢试探未结束前不会再放行第二个。整轮重试会复用首轮已完成的子智能体结果（`retry_cache`），只重跑失败的智能体（限同一请求内部）；面板「重试」会重新执行意图识别，计划措辞漂移时按未命中重跑对应步骤。
- **未结算用量的归属**：失败 / 取消回合没有助手消息承载 `_meta.usage`，其 token 用量先记账在运行体内存、并入该会话下一轮成功回合；Web 会话被 LRU 淘汰时结转给下次重建的同名会话（按用户分桶、每人有界，桶数另有全局上限：一个用户的会话抖动不会把另一用户结转的账目挤出；同名会话已有结转账目时合并而非覆盖），但进程重启或会话被删除时这部分用量会丢失（关闭时日志告警「丢弃未结算用量」），面板用量可能略少于上游实际计费。
- **9999-12-31 返程占位**：模型常用的「返程未定」占位日期在存储 / 导出中保真保留，但状态徽标、出行天数与冲突检测按出发当日（单日）计算——与对话落库时丢弃占位返程的口径一致；按真实万年区间统计会把出行天数放大到数百万天、行程永久「进行中」并与所有未来行程互相重叠。需要真实返程时请编辑行程。
- **模型回答非流式**：受 agentscope 1.0.16 与 DeepSeek 流式解析兼容性限制，回答在完成后一次性返回（进度通过 SSE 实时展示）。
- **知识库导入**：Web 接口单次最多 20000 条（面板按 200 条 / 20000 字自动分批上传，多分片组整组同批；请求体上限 2 MB），CLI 不限条数；导出与列表为全量扫描，不再受 Milvus 单页 16384 条上限约束。

安全问题报告方式与对外部署注意事项见 [SECURITY.md](SECURITY.md)。

---

## 🚀 未来规划

- [x] 完整实现PostgreSQL持久化存储（可选后端，`DATABASE_URL` 启用）
- [x] 完整实现Redis短期记忆（可选后端，`REDIS_URL` 启用）
- [x] Web 界面升级（SSE 流式执行进度、会话历史、深色模式；保持无构建前端）
- [x] 会话管理（搜索 / 重命名 / 置顶 / 归档、分页、编辑重发、Markdown / HTML 导出）
- [x] 知识库维护（Web / CLI 增删改查、分类筛选、导出导入、离线检索评测）
- [x] 行程管理（删除误记录、手动补录 / 编辑、日历与 CSV 导出、重复保存自动去重）
- [x] 性能与可观测性（gzip、JSON 访问日志、请求 ID、限流、系统自检、请求指标）
- [ ] 支持更多 LLM 服务商（OpenAI / Claude 兼容网关等，当前任意 OpenAI 兼容接口可用）
- [ ] 回答逐 token 流式输出（依赖 agentscope 流式解析兼容性修复）
- [x] 多实例部署（`REDIS_URL` 启用共享限流与跨进程会话存储；请求指标仍为进程内统计）
- [x] 可观测性（结构化日志与请求指标：`/api/metrics`、`LOG_FORMAT=json`）

---

## 许可证

MIT License
