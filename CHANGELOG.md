# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增
- 会话全文搜索：`/api/sessions?q=` 按标题与消息内容检索（助手消息按渲染后的 Markdown 匹配），返回命中片段；侧栏新增搜索框
- 模型用量汇总：`/api/status` 从执行元数据汇总调用次数 / tokens / 平均耗时 / 常用智能体，Web「记忆与数据」面板展示
- 执行路线显示每个智能体的单独耗时（实时进度与历史回看一致）
- 记忆面板支持在线编辑 / 删除偏好（`PUT` / `DELETE /api/preferences/{type}`），并展示最近 5 条行程
- 对话深链接：地址栏同步 `user` / `session` 参数，可直接打开或分享会话；顶栏新增「复制链接」，无效链接自动清理
- CLI 实时进度：调度阶段显示当前正在运行的智能体，完成后智能体列表附带各自耗时（与 Web 执行路线一致）
- 接口观测与限流：`/api/` 响应携带 `X-Request-ID`（透传或自动生成），请求日志包含方法 / 状态 / 耗时；对话接口默认按「客户端 + 用户」限流 30 次/分钟（429 + `Retry-After`，可用 `CHAT_RATE_LIMIT` / `CHAT_RATE_WINDOW_SEC` 调整）
- 跟进建议：回答完成后根据本轮执行的智能体推荐 2–3 个下一步操作（导出行程 / 查出发当天天气 / 调整行程等），点击直接继续对话
- 会话重命名：侧栏内联编辑标题（`PUT /api/sessions/{id}/title`），JSON / PostgreSQL 持久化；自定义标题参与搜索，空标题恢复自动标题
- 会话置顶：侧栏图钉按钮置顶 / 取消置顶（`PUT /api/sessions/{id}/pin`），置顶会话进入「置顶」分组并优先展示，JSON / PostgreSQL 持久化
- 行程记录关联来源会话：保存行程时写入 `session_id`（PostgreSQL 自动加列），记忆面板点击行程可回到对应对话
- 模型用量趋势：`/api/status` 的 `usage.by_day` 按消息时间聚合最近 7 天 calls / tokens，记忆面板新增柱状图（悬停提示 + 屏幕阅读器表格）
- 用户数据导出新增 Markdown：`GET /api/export?format=md` 汇总偏好、最近行程、统计与最近会话；Web 记忆面板新增「导出 Markdown」按钮
- 安全响应头：CSP（仅同源脚本）/ `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / `Permissions-Policy`；主题初始化拆分为 `/static/theme.js` 以满足 CSP

### 变更
- 编排器在聚合结果与 `agent_done` 事件中附带 `duration`
- 复制文本在 Clipboard API 不可用时回退到 `execCommand`
- 对话接口错误详情附带请求 ID，便于定位日志
- 前端静态资源版本号更新为 1.3.0（`style.css` / `app.js` / `theme.js`）

### 修复
- 偏好追加时若存量与新值相同（字符串 → 列表），不再产生重复项（如「汉庭、汉庭」）
- RAG 智能体在进程内按数据库路径共享 Milvus Lite 客户端，修复多会话 / 全量测试中重复打开本地库导致的偶发文件锁错误
- 删除无消息的会话时也会清理其标题与置顶元数据（此前仅在有消息时清理）

### 测试
- 新增偏好编辑 / 删除 API、最近行程、JSON / PostgreSQL 删除偏好、追加去重用例

## [1.3.0] - 2026-11-03

### 新增
- 历史会话回看「执行路线」：助手消息持久化 `_meta`（意图 / 调度计划 / 智能体状态 / 用量 / 耗时），Web 恢复会话时渲染可展开的路线，CLI 会话同样记录
- 会话导出为 Markdown：`GET /api/sessions/{id}/export`，Web 顶部新增导出按钮
- Web 会话缓存改为 LRU（上限 50），淘汰或服务关闭时释放数据库连接

### 性能
- 长期记忆总结按“其它会话历史 + 行程”指纹缓存，未变化时复用，减少重复 LLM 调用
- PostgreSQL 长记忆不再每轮 `reload_long_term` 新建连接（无内存快照，直接查询）

### 修复
- 用户 / 会话 ID 含中文时，下载文件名改为 ASCII，避免响应头编码错误

### 测试
- 新增执行元数据工具、总结缓存、LRU 淘汰、Markdown 导出与中文文件名用例

## [1.2.0] - 2026-11-03

### 新增
- **Web 界面全面升级**（保持无构建前端）：执行路线（智能体进度实时可视化）、会话历史（按日期分组 / 恢复 / 删除）、深色模式、记忆与数据面板、停止生成 / 重新生成 / 复制回答、移动端抽屉布局、Markdown 表格与代码块
- **SSE 流式对话**：`POST /api/chat/stream` 推送 `phase` / `plan` / `agent_start` / `agent_done` / `done` 事件；`ConversationRuntime` 与 `OrchestrationAgent` 增加进度回调
- 会话历史接口：`GET /api/sessions`、`GET /api/sessions/{id}`（助手消息重新渲染为 Markdown）、`DELETE /api/sessions/{id}`；JSON 与 PostgreSQL 后端均支持按会话删除
- `GET /api/health`：返回版本、模型与当前存储后端；`/api/status`、`/api/export` 改为经存储工厂读取，PostgreSQL 后端下数据一致
- **可插拔存储后端**：设置 `REDIS_URL` 启用 Redis 短期记忆（List + 滑动窗口 + TTL）；设置 `DATABASE_URL` 启用 PostgreSQL 长期记忆（偏好 / 行程 / 对话 / 统计，表结构自动创建）；未配置或连接失败时自动回退本地实现
- `docker-compose.yml` 一键启动 Redis + PostgreSQL；存储后端集成测试（CI 通过 service 容器自动运行）
- README 增加界面截图与 Web 接口说明

### 修复
- 历史会话中的助手消息此前以原始编排 JSON 存储，恢复会话时无法阅读；现在读取时重新渲染为 Markdown
- 用户 / 会话 ID 增加字符校验（中文、字母、数字、下划线、短横线），避免拼接到文件路径时发生路径穿越

### 变更
- Web 服务版本号升至 1.2.0；新增 `tests/test_webapp.py` 流式接口、会话历史与 ID 校验用例

## [1.1.0] - 2026-11-03

### 新增
- **Web 界面**（FastAPI + 无构建前端）：多轮对话、示例问题、偏好 / 行程概览、数据导出、会话管理；提供 `/api/chat`、`/api/status`、`/api/export` 接口
- `export` 命令：导出用户数据（偏好 / 行程 / 对话历史 / 统计）为 JSON + Markdown，保存到 `exports/`（已 gitignore）

### 修复
- 编排器：未注册智能体的错误原因在结果聚合后不再丢失（界面不再显示「未知错误」）
- Windows GBK 控制台下打印 ✓ / emoji 崩溃（CLI / Web / 知识库初始化入口统一切换 UTF-8）

### 变更
- CI 增加 Python 3.10 / 3.12 测试矩阵与 Dependabot 自动依赖更新
- 依赖升级：actions/setup-python 7、actions/checkout 7、requests 2.34.2、milvus-lite 3.2.2

## [1.0.0] - 2026-11-03

首个可运行版本：基于 AgentScope 1.0.16 的多智能体差旅助手。

### 新增
- Plan-and-Execute 架构：LLM 意图识别 → 优先级并行调度 → 6 个 Skill 子智能体
- 两层记忆：进程内短期记忆（滑动窗口）+ JSON 文件长期记忆（偏好 / 行程 / 对话历史）
- RAG 知识库：Milvus Lite + BGE-small-zh-v1.5，Top-K 余弦检索与知识溯源
- 联网搜索（DDGS）与天气查询（Open-Meteo）
- 稳定性：指数退避重试、熔断器、健康检查
- CLI（Rich）：`doctor` 环境自检 / `health` / `status` / `history` / `preferences` 等命令
- LLM 用量统计：每次对话展示调用次数、token 用量与耗时
- GitHub Actions CI（ruff + mypy + pytest，无需密钥）
- 发布流水线：推送 `v*` 标签自动构建 sdist/wheel 并创建 GitHub Release
- 中文协作规范：PR / Issue 模板、`CONTRIBUTING.md`、pre-commit 配置

### 改进
- RAG 知识库按小节切分（章节 / FAQ 边界强制断块，块长 ≤ 400 字），Top-K 由 3 提升到 5
- 本地嵌入模型补齐官方元数据（`modules.json` 等），恢复 CLS 池化 + 归一化，消除 mean pooling 回退警告
- 天气查询数据源由 wttr.in 改为 Open-Meteo（免费、无需 Key，国内网络可达性更好）

### 修复
- agentscope 1.0.16 流式解析与 DeepSeek 不兼容（改用非流式 + 统一响应解析）
- `temperature` / `max_tokens` 未生效（改用 `generate_kwargs` 传递）
- 全新环境安装缺 `mcp<2` / `greenlet` / `tqdm` 导致 `import agentscope` 失败
- Windows 下 RAG 不可用（按平台选择 pymilvus / milvus-lite，检索前 load collection）
- 偏好查询永远为空（偏好类型映射错误）
- 偏好值为字符串时被 `', '.join` 拆成单字
- 意图识别吞掉可重试异常，导致重试与熔断器失效
- 多处 CWD 依赖路径（技能发现 / 长期记忆 / 嵌入模型）
- `robust_json_parse` 抛 `NameError`、`increment_query_count` 抛 `KeyError`
- 信息查询：天气接口不可用时自动回退到联网搜索，不再直接返回接口错误

### 变更
- 默认模型切换为 DeepSeek（兼容豆包 / OpenAI 等 OpenAI 兼容接口）
- 配置改用环境变量 / 本地 `config_local.py`，密钥不入库
- 知识库演示文档中的虚构企业邮箱与公司抬头已清理
