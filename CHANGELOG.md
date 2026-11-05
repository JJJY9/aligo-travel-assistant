# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增
- 检索评测互斥升级为 Redis 跨实例锁：配置 `REDIS_URL` 时多个 Web 实例共享同一把锁（`SET NX EX` + token 比对释放，TTL 300 秒），未配置或 Redis 故障时回退进程内锁；并发请求仍返回 429
- Prometheus 文本指标端点 `GET /metrics`：请求总数 / 状态 / 方法 / 路径 / 耗时分位 / 熔断器 / 缓存会话 / 限流后端与降级状态，`text/plain; version=0.0.4`，多实例需分别采集；README 补抓取配置示例
- 会话删除墓碑在配置 `REDIS_URL` 时写入 Redis（`aligo:deleted:*`，TTL 与本地一致），其它实例的新请求会被拒绝；Redis 故障时回退进程内墓碑
- 侧栏「即将出行」显示目的地天气：新增 `GET /api/weather`（Open-Meteo，无 Key，实时 + 未来 3 天，10 分钟进程内缓存），前端异步展示，失败时静默隐藏

### 变更
- `query-info` 技能的天气查询改为复用 `utils/weather.py`：删除重复的天气码映射与 HTTP 请求逻辑，城市提取后调用共享 `fetch_weather`（同样 10 分钟缓存、按地点自身时区），Web 侧栏与技能结果保持一致
- 天气查询失败结果（城市不存在 / 上游故障）增加 60 秒短时缓存：TTL 内同一城市直接复用错误、不再请求上游，成功结果会清除对应失败缓存；缓存键忽略大小写与多余空白（`Beijing` / `beijing` 共享一条）
- `/api/weather` 改为在专用天气线程池中执行（8 个线程）：上游变慢时不会占用 FastAPI 共享线程池，其它同步接口保持可用

### 修复
- 天气工具加固：上游响应形状异常统一映射为 `WeatherError`（此前可能抛 `AttributeError` 导致 500）；缓存改为有界（写入时清理过期条目，上限 256 条）；同一城市的并发未命中合并为一次上游请求
- `/api/weather` 按客户端 IP 独立限流（`WEATHER_RATE_LIMIT`，默认 30 次 / 分钟），成功响应附带额度头，失败写入服务端日志；空白城市在计额度前返回 422
- 侧栏天气：缺失温度不再显示为「0°C」；切换行程时在途请求会失效并清空文本，避免上一个城市的天气落到新卡片上；移除前端重复的 10 分钟缓存，统一由服务端缓存负责，避免最长约 20 分钟的过期展示
- `query-info` 技能：天气与联网搜索都失败时返回具体的天气失败原因（如城市名拼错），不再被「搜索库未安装 / 未找到相关结果」覆盖

### 测试
- 新增 `tests/test_redis_lock.py`：Redis 故障回退进程内互斥、两个实例通过 Redis 共享同一把锁
- `tests/test_metrics.py` 覆盖 Prometheus 渲染与标签转义；`test_webapp.py` 覆盖 `/metrics` 端点
- 新增 `tests/test_tombstone_redis.py`（真实 Redis 跨实例墓碑）；`test_webapp.py` 补假 Redis 共享墓碑用例
- 新增 `tests/test_weather.py`（查询 / 缓存 / 未知城市 / 接口故障 / 地点时区 / 429 映射 / 响应形状异常 / 缓存上限与过期清理 / 同城并发合并 / 失败短时缓存与键归一化）；`test_webapp.py` 补 `/api/weather` 用例与独立限流用例
- `test_query_info_fallback.py` / `test_skill_agents.py` 天气用例改为校验技能委托 `utils.weather.fetch_weather`，不再模拟内部 HTTP；补天气与搜索都失败时返回天气原因的用例

## [1.5.0] - 2026-11-05

### 新增
- 多实例共享限流：配置 `REDIS_URL` 后对话接口的用户桶与 IP 桶计数写入 Redis 有序集合（`aligo:ratelimit:*`），多个 Web 实例共享同一份额度；窗口时间取自 Redis 服务器时钟，避免实例间时钟不一致
- Redis 限流故障自动回退进程内实现（60 秒冷却后重试），限流后端故障不影响请求处理
- `/api/health` 新增 `rate_limit` 字段（`redis` / `memory`），便于确认当前限流后端
- `/api/health` 新增 `rate_limit_configured` / `rate_limit_degraded` 字段：配置了 Redis 但限流器回退进程内（启动时不可用或运行中故障）时 `degraded` 为 `true`
- `/api/metrics` 新增 `rate_limit` 字段（后端 / 是否配置 / 是否降级 / 用户与 IP 额度 / 窗口秒数），记忆面板运行指标说明行显示当前限流后端
- 反向代理部署：设置 `TRUST_PROXY_HEADERS=1` 后限流按 `X-Forwarded-For` 第一个地址计算客户端 IP；默认关闭，避免客户端伪造该头绕过 IP 桶
- 知识库接口独立限流：写入 / 更新 / 删除 / 导入 / 导出 / 语义检索按客户端 IP 限流（默认 60 次 / 分钟，`KB_RATE_LIMIT` 调整），与对话额度互不影响；配置 Redis 时与其他桶共享后端，`/api/metrics.rate_limit` 增加 `kb_limit`
- 知识库维护面板新增「检索评测」按钮（`/api/knowledge/eval`）：14 个问题离线计算 hit@1/3/5 与 MRR，不调用大模型，受知识库限流保护；hit@5 低于 80% 时结果标红提示；同一进程内只允许一个评测（并发请求返回 429）
- 请求指标新增 P50 / P95 耗时（最近 1000 个请求样本，内存有界）与 `duration_samples`；记忆面板运行指标新增「P95 耗时」卡片
- CLI `doctor` / `skills` 支持 `--json`：`doctor` 输出 `status` / `checks`，`skills` 输出 `root` / `skills` / `ok`，便于监控与脚本读取；非法参数退出码 2
- CLI `doctor --offline`（`--no-network`）跳过 LLM 连通性检查，网络不可达 / CI 环境不再长时间等待
- CLI `health` / `kb` 也支持 `--json`：`health` 输出 `{"ok", "message"}`，`kb` 输出完整知识库状态对象；非法参数退出码 2
- CLI `eval` 也支持 `--json`：输出 `ok` / `skipped` / `threshold` / `hit@K` / `mrr` / `rows`，成功、未达标、跳过与故障四种状态的退出码与文本模式一致
- 行程日历提醒：导出的 `.ics` 事件新增 `VALARM`，出行前一天 09:00 提醒（单条行程与全部行程导出均生效）
- 安全响应头：`/api/` 响应默认带 `Cache-Control: no-store`，用户数据不进入浏览器 / 代理缓存（导出接口保留自身的 `no-cache`）
- 记忆面板用户卡片显示技能数量（来自 `/api/health`），安装后技能缺失可直接看到「技能 0」
- Web 启动参数：`aligo-web --version` / `python -m webapp.server --version` 输出版本号；监听非环回地址（如 `0.0.0.0`）时启动日志输出安全警告，提示通过反向代理提供认证 / HTTPS 并配置 `TRUST_PROXY_HEADERS`
- 版本号统一到 `utils/version.py`（Web / CLI 共用）；`python cli.py --version` / `-V` 输出 `Aligo 差旅助手 x.y.z`
- `doctor` 新增「版本一致性」检查：`pyproject.toml`、`index.html` 静态资源 `?v=` 与 `utils/version.py` 不一致时警告（发布时漏改版本能直接发现）；安装 wheel 没有 pyproject 时仍检查静态资源
- 技能可观测性：`/api/health` 返回 `skills.count` / `skills.dir_found`；`doctor` 新增「技能目录」检查（发现数量、缺失时提示 `ALIGO_SKILLS_DIR`），安装后技能未注册可直接定位
- `python cli.py skills`：输出技能目录与已发现技能（目录缺失或没有可加载技能时退出码 1），便于确认 wheel 安装 / `ALIGO_SKILLS_DIR` 是否生效；`skill_scripts()` 在目录不可读时安全返回空，健康检查不会因此报错

### 修复
- `pip install --user` 安装 wheel 时技能文件落在用户目录（`site.getuserbase()`），此前解析器只查 `sys.prefix`，导致智能体无法注册；现在按 `ALIGO_SKILLS_DIR` → 源码目录 → `sys.prefix` → 用户目录的顺序查找
- Redis 限流响应解析兼容未开启 `decode_responses` 的客户端（bytes 返回值），避免 `float()` 抛错
- 本地 JSON 长期记忆：CLI 与 Web 等多进程同时写同一用户时，修改操作使用 `<data>.json.lock` 跨进程文件锁串行化（Windows `msvcrt` / Unix `flock`），避免后写覆盖先写
- 知识库限流改为单桶直接 `check()` 原子记录，并发请求不会超发；已有评测运行时的 429 不再消耗知识库配额
- 知识库检索评测使用 `strict=True`，后端 / 嵌入失败返回 503，而不是当作 0% 命中的成功评测
- 进程内限流桶增加线程锁，FastAPI 线程池并发调用不再可能因 deque 竞态报 500；`/api/health` 聚合用户 / IP / 知识库三个桶的降级状态
- 本地 JSON 长期记忆：写锁等待上限从 10 秒收紧到 2 秒（Web 事件循环同步等待），超时抛 `MemoryPersistenceError`，不再被判定为可重试网络错误；Windows 上其他进程短暂读文件导致 `os.replace` 失败时自动重试 3 次
- `doctor` 版本一致性：读取非 UTF-8 文件时给出警告而不是崩溃；wheel 安装改用 dist-info 元数据校验，详情只列实际检查过的项
- 知识库评测接口捕获 RAG 模块导入失败（如 wheel 缺少技能文件），返回 503 而不是未处理的 500
- 启动时的环回地址判断支持整个 `127.0.0.0/8` 与 IPv6 `::1`，`127.0.0.2` 等地址不再误报对外监听警告

### 测试
- 新增 `tests/test_rate_limit_redis.py`：两个限流器实例共享计数、`peek` 不消耗额度、窗口过期、桶名隔离与 `reset`，以及两个 Web 实例共享额度并返回 429 的集成用例；并发 check（多线程同时请求）严格只放行额度内的次数
- `tests/test_rate_limit.py` 补充 Redis 故障回退与限流器工厂用例
- `tests/test_skill_paths.py` 覆盖 `skill_scripts` 技能发现；`test_doctor.py` 覆盖技能目录检查（正常 / 缺失）；健康接口断言 `skills.count` 与 `dir_found`
- 新增 `tests/test_cli_skills.py`（正常 / 目录缺失 / 空目录 / 命令分发）；`tests/test_skill_paths.py` 补充技能目录不可读时安全返回空的用例
- 新增知识库接口限流用例：首次写入 201 并带额度头，第二次 429；语义检索与导出均与写入共享知识库桶；对话不受该桶影响
- 新增 `/api/knowledge/eval` 用例：正常返回 hit@K / MRR 与额度头，知识库不可用返回 503，已有评测运行时返回 429
- 内存限流并发用例：10 线程同时请求严格放行 50 次；评测用例断言 `strict=True`、并发锁 429 不消耗额度、任一桶降级都会上报
- 跨进程写锁用例改为 3 进程 × 20 次、同一屏障启动、每次新建实例；补写锁超时映射与 `os.replace` 重试用例
- `test_doctor` 补 wheel 静态资源 / 元数据不一致、非 UTF-8 文件用例
- 评测模块 import 失败返回 503、ICS VALARM 位于 DTEND 与 END:VEVENT 之间、`127.0.0.0/8` 环回判断用例
- `tests/test_metrics.py` 补充 P50 / P95、有界样本窗口与重置用例；`/api/metrics` 接口用例断言百分位与样本数
- `test_cli_skills.py` / `test_doctor.py` 补充 `--json` 输出、失败退出码与未知参数用例
- 新增 `tests/test_cli_json.py`：health JSON 成功 / 失败、kb 状态可用 / 不可用的退出码与字段
- `test_cli_eval.py` 补充 `eval --json` 四种状态（成功 / 未达标 / 跳过 / 故障）与退出码断言
- 新增 `tests/test_long_term_memory_lock.py`：两个子进程各 25 次写入，断言总计数不丢
- 新增 `tests/test_server_cli.py`：环回地址判断、`--version`、非环回监听警告
- 新增 `tests/test_version.py`：版本格式、Web / CLI 共用常量、`--version` / `-V` 分发
- `test_doctor.py` 补充版本一致性（一致 / 不一致警告 / 无版本文件跳过 / 静态资源不一致）用例
- `test_doctor.py` 补 `doctor --offline` 分发与未知参数退出码用例
- ICS 日历用例断言每个事件包含 `VALARM` 与 `TRIGGER:-PT15H`
- 安全头用例断言 `/api/` 响应带 `Cache-Control: no-store`，页面与静态资源不受影响
- 浏览器实测：用户卡片显示「本地内存 · JSON · 技能 6」

## [1.4.1] - 2026-11-05

### 修复
- wheel 未包含 `.claude/skills`：安装后所有智能体无法注册，对话只会返回空结果。现在通过 setuptools data-files 把技能代码与 `SKILL.md` 安装到虚拟环境的 `.claude/skills`，并新增统一技能目录解析 `utils/skill_paths.py`（支持 `ALIGO_SKILLS_DIR` 覆盖，源码 / wheel 安装两种位置），`LazyAgentRegistry`、`SkillLoader` 与知识库路径共用
- sdist / wheel 仍不包含 `data/` 下的知识库文档、嵌入模型与记忆数据；安装后 RAG 需按「知识库初始化」自行准备

### 测试
- 新增 `tests/test_skill_paths.py`：源码目录 / 环境变量 / `sys.prefix` 回退 / pyproject data-files 覆盖全部技能文件
- 发布验证：fresh venv 安装 wheel 后六个智能体全部可注册、`aligo-web --help` 与健康接口正常、静态页面可访问

## [1.4.0] - 2026-11-05

### 新增
- 会话全文搜索：`/api/sessions?q=` 按标题与消息内容检索（助手消息按渲染后的 Markdown 匹配），返回命中片段；侧栏新增搜索框
- 模型用量汇总：`/api/status` 从执行元数据汇总调用次数 / tokens / 平均耗时 / 常用智能体，Web「记忆与数据」面板展示
- 执行路线显示每个智能体的单独耗时（实时进度与历史回看一致）
- 记忆面板支持在线编辑 / 删除偏好（`PUT` / `DELETE /api/preferences/{type}`），并展示最近 5 条行程
- 对话深链接：地址栏同步 `user` / `session` 参数，可直接打开或分享会话；顶栏新增「复制链接」，无效链接自动清理
- CLI 实时进度：调度阶段显示当前正在运行的智能体，完成后智能体列表附带各自耗时（与 Web 执行路线一致）
- 接口观测与限流：`/api/` 响应携带 `X-Request-ID`（透传或自动生成），请求日志包含方法 / 状态 / 耗时；对话接口默认按「客户端 + 用户」限流 30 次/分钟（429 + `Retry-After`，可用 `CHAT_RATE_LIMIT` / `CHAT_RATE_WINDOW_SEC` 调整）
- 请求指标：`GET /api/metrics` 返回进程内请求总数、状态 / 方法分布、Top 20 路径、平均与最大耗时、熔断器状态与缓存会话数（路径基数上限 100，超出归入 `__other__`；控制字符清理）
- 记忆面板「运行指标」区块：请求总数 / 平均与最大耗时 / 熔断器状态 / 运行时长与缓存会话（数据来自 `/api/metrics`）
- 全部会话导出：`GET /api/sessions/export` 把全部会话渲染为单个 Markdown（按最近更新排序，含归档会话；无标题时用首条提问），记忆面板新增「导出全部会话」按钮；CLI 对应 `python cli.py sessions-export <user_id> [path] [--stdout] [--force]`（默认 `exports/aligo_sessions_<user>.md`）
- 跟进建议：回答完成后根据本轮执行的智能体推荐 2–3 个下一步操作（导出行程 / 查出发当天天气 / 调整行程等），点击直接继续对话
- 会话重命名：侧栏内联编辑标题（`PUT /api/sessions/{id}/title`），JSON / PostgreSQL 持久化；自定义标题参与搜索，空标题恢复自动标题
- 会话置顶：侧栏图钉按钮置顶 / 取消置顶（`PUT /api/sessions/{id}/pin`），置顶会话进入「置顶」分组并优先展示，JSON / PostgreSQL 持久化
- 会话归档：侧栏会话操作新增「归档」，归档后从默认列表隐藏但消息保留；侧栏「已归档（N）」切换到归档列表，可恢复或删除；普通视图搜索时显示「另有 N 条归档会话匹配」并可直接切换；`PUT /api/sessions/{id}/archive`、`GET /api/sessions?archived=true`，`/api/status` 返回 `archived_count`
- CLI 行程导出：`python cli.py trips-export <user_id> [path] [--stdout] [--force]`（默认 `exports/aligo_trips.csv`，CSV 渲染与 Web 共用 `utils/trip_export.py`）
- CLI 行程列表：`python cli.py trips <user_id>` 按新→旧列出全部行程，标注「进行中 / 即将出行 / 已结束」与日期重叠提示；日期解析 / 重叠判断抽到 `utils/trip_conflicts.py`，Web 与 CLI 共用
- CLI 偏好列表：`python cli.py preferences <user_id>` 列出长期记忆中的偏好（中文类型名、列表值用「、」拼接、布尔显示为是 / 否）
- CLI 会话管理：`python cli.py sessions <user_id> [--archived|--all]` 列出会话（置顶优先，显示消息数与最近更新时间），`python cli.py session-archive <user_id> <session_id> [--restore]` 归档 / 恢复，`python cli.py session-export <user_id> <session_id> [path] [--stdout]` 导出单个会话为 Markdown；PostgreSQL 回退本地 JSON 时拒绝执行
- 会话列表分页：`GET /api/sessions` 支持 `offset` 并返回 `total`，侧栏超过一页时显示「加载更多会话（已显示 X / 共 Y）」
- 侧栏「即将出行」卡片：根据行程日期展示最近一次即将开始的行程（路线 / 日期 / 目的），点击回到对应对话（无关联会话时打开记忆面板）
- 行程记录关联来源会话：保存行程时写入 `session_id`（PostgreSQL 自动加列），记忆面板点击行程可回到对应对话
- 模型用量趋势：`/api/status` 的 `usage.by_day` 按消息时间聚合最近 7 天 calls / tokens，记忆面板新增柱状图（悬停提示 + 屏幕阅读器表格）
- 行程导出 iCalendar：`GET /api/trips/{id}/calendar` 生成 `.ics` 全天事件（结束日期 exclusive），记忆面板行程行新增「加入日历」按钮
- 打印 / 导出 PDF：顶栏打印按钮 + 打印样式（隐藏侧栏 / 输入区 / 执行路线 / 操作按钮，用户消息浅灰、回答正文黑色）
- 会话搜索命中片段支持关键词高亮；记忆面板「最近行程」下方展示常去目的地（Top 5）
- 会话搜索跳转：搜索结果附带命中消息序号，点击会话后自动滚动到该消息并短暂高亮
- RAG 回答附带 Top 3 参考片段（140 字截断），便于核对知识来源
- 知识库状态：`GET /api/knowledge`、`python cli.py kb` 与记忆面板「知识库 · N 片段」；Milvus 客户端抽到 `utils/milvus_client.py` 供 RAG Skill 与状态查询共享
- 知识库维护：记忆面板查看 / 新增 / 删除知识片段（`GET/POST/DELETE /api/knowledge/documents`），新增后立即可检索，列表按 id 倒序显示最新片段；新增按最大 id 递增，删除后再添加不会与已有主键冲突
- 知识片段编辑：记忆面板知识库列表新增编辑按钮，修改内容 / 分类后重新向量化并覆盖原片段（`PUT /api/knowledge/documents/{id}`，片段不存在返回 404；增删改共用写锁，避免并发覆盖）
- 知识库分类筛选：`GET /api/knowledge/documents` 支持 `category` 参数，新增 `GET /api/knowledge/categories` 统计各分类片段数；维护面板新增分类下拉框，可与关键词搜索组合使用
- 知识库搜索命中高亮：片段预览围绕首个命中位置截取并高亮关键词，分类 / 标题同样高亮
- 对话接口响应附带限流额度头：`X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`（429 时 Remaining 为 0，Reset 与 Retry-After 一致）
- 知识库语义检索：`GET /api/knowledge/search`（`q` + `top_k` 1–10，返回 id / content / metadata / 相似度）；维护面板新增「语义搜索」按钮，结果按相似度排序并可一键返回全部片段
- 检索评测：`python cli.py eval` 用 14 个差旅问题离线计算 hit@1 / hit@3 / hit@5 与 MRR（只检索、不调用大模型），知识库未初始化时提示跳过，hit@5 低于 80% 退出码 1
- 系统自检新增记忆存储检查：报告短期 / 长期记忆实际使用的后端，配置 Redis / PostgreSQL 后测试连通性，不可用时提示将回退本地默认实现
- 系统自检：`GET /api/doctor` 返回 Python / 依赖 / 配置 / 嵌入模型 / 知识库检查结果（`network=true` 时包含 LLM 连通性），记忆面板新增「运行自检」区块
- 知识库导出 / 导入：`GET /api/knowledge/export` 下载 JSON 备份，`POST /api/knowledge/import` 批量导入（单次最多 200 条，超长字段自动截断）；记忆面板新增「导出知识库 / 导入 JSON」；CLI 对应 `python cli.py kb-export [path]` 与 `kb-import <file>`
- CLI 知识片段管理：`python cli.py kb-list [关键词]` 列出片段（分类 + 预览），`python cli.py kb-delete <id>` 删除片段
- CLI 行程去重：`python cli.py trips-dedupe <user_id>` 清理去重功能上线前已存在的重复行程（保留最早一条，统计同步更新）；新增 `--dry-run` 只列出将被删除的记录
- 行程去重 / CLI 健壮性：删除前复核保留行程仍存在（并发删除时不误删仅剩副本），删除失败返回 1；PostgreSQL 配置错误回退本地 JSON 时拒绝执行；ICS 文本转义孤立 CR
- 行程管理：记忆面板可删除误记录的行程（`DELETE /api/trips/{id}`），JSON / PostgreSQL 同步更新统计与常去目的地
- 记忆面板「最近行程」标题右侧的「添加行程 / 导出全部日历」两个按钮增加间距（此前贴在一起难以区分），并更新 README 记忆面板截图
- 会话导出 HTML：顶栏新增「导出对话（HTML）」，生成内嵌样式的单文件 HTML（去除执行路线与操作按钮，可直接分享 / 打印 / 离线查看）
- 行程日历导出全部：`GET /api/trips/calendar` 把一个用户的历史行程合并为一个 `.ics`（缺少出发日期的跳过），记忆面板「最近行程」标题右侧新增「导出全部日历」
- 行程列表查看全部：`GET /api/trips`（`limit` / `offset` 分页，附带日期重叠信息），记忆面板超过 5 条时显示「查看全部（N）」，展开最近 200 条并可收起
- 行程筛选：展开全部行程后可按路线 / 日期 / 目的关键词筛选（客户端过滤，显示「匹配 X / Y 条」与空态提示），切换用户时重置
- 行程导出 CSV：`GET /api/trips/export` 返回带 UTF-8 BOM 的 CSV（出发地 / 目的地 / 出发日期 / 返程日期 / 目的，便于 Excel 报销台账），记忆面板新增「导出 CSV」
- 手动添加行程：`POST /api/trips`（同路线 + 同起止日期自动去重，返回 `trip_id`），记忆面板「最近行程」新增「添加行程」表单（出发地 / 目的地 / 起止日期 / 目的）
- 手动添加行程的响应新增 `created` 标志：重复添加已有行程时界面提示「已存在相同行程，已更新该记录」，不再误报为新增
- 编辑行程：`PUT /api/trips/{id}` 支持部分字段更新（显式传 `null` 可清空返程日期；与其它行程重复时返回 409），记忆面板行程行新增编辑按钮（复用「添加行程」表单，保存后原记录就地更新）
- 行程状态与日期重叠提醒：新增 / 编辑行程时接口返回与之日期重叠的其它行程（`conflicts`），界面保存后提示「与 XX 的行程日期重叠」；记忆面板每条行程展示「进行中 / 即将出行 / 已结束」状态标签
- 行程列表重叠徽标：`/api/status` 的最近 5 条行程附带 `conflicts`（与其它行程的日期重叠），记忆面板对重叠记录显示「重叠」徽标，悬停可查看重叠路线与日期
- 删除最后一轮对话：`DELETE /api/sessions/{id}/last-turn` 成对删除最后一轮用户提问与助手回答（回答中断时只删除提问），JSON 长期记忆、PostgreSQL 与 Redis 短期记忆同步清理，供「编辑上一条提问后重新发送」使用
- 编辑上一条提问：最后一条用户消息旁新增「编辑」按钮，就地修改后「保存并重新发送」会先删除上一轮问答再重新提问；`Ctrl/⌘ + Enter` 保存、`Esc` 取消，重新发送后历史与短期记忆保持一致
- Web 快捷键与键盘可达性：`Ctrl/Cmd + K` 或 `/` 聚焦搜索，`Esc` 清空搜索 / 关闭面板；会话操作按钮支持键盘聚焦
- 快捷键帮助面板：顶栏「?」按钮或按 `?` 打开，列出 Enter / Shift+Enter、搜索、编辑重发、Esc 等快捷键；Esc / 点击遮罩关闭，焦点回到触发按钮
- 用户数据导出新增 Markdown：`GET /api/export?format=md` 汇总偏好、最近行程、统计与最近会话；Web 记忆面板新增「导出 Markdown」按钮
- 记忆面板偏好支持手动添加：常用类型（常住地 / 交通 / 酒店 / 航司 / 座位 / 餐食 / 预算）+ 值输入，多个值用「、」分隔，酒店与航司自动存为列表、预算自动转数字
- 用户数据 Markdown 导出标注归档会话（「最近会话」后缀「（已归档）」），统计中加入归档会话数
- 安全响应头：CSP（仅同源脚本）/ `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / `Permissions-Policy`；主题初始化拆分为 `/static/theme.js` 以满足 CSP

### 变更
- 浅色主题可读性：次要文字（`--ink-3`）与信号橙调整到 WCAG AA 对比度（≥4.5:1），侧栏「即将出行」标签改用更亮的橙色
- 记忆面板存储状态在「已配置 Redis / PostgreSQL 但当前回退本地」时显示红色提示，避免误以为数据写入了远端后端
- Redis 短期记忆键改为 `aligo:session:{user_id}:{session_id}:messages`（用户级隔离；删除会话时同时清理旧格式键）；未缓存会话的「删除最后一轮」也会清理 Redis，多用户复用同一 session_id 不再串上下文
- 响应压缩：静态资源与 JSON 响应启用 gzip（≥1KB），`/api/chat/stream`（SSE）保持不压缩，避免缓冲破坏实时进度
- 侧栏出行卡片优先展示进行中的行程（标注「进行中」，多条时取结束最晚的），没有进行中行程时再展示最近一次即将开始的行程；`/api/status` 的 `next_trip` 同步支持进行中行程
- 编排器在聚合结果与 `agent_done` 事件中附带 `duration`
- 复制文本在 Clipboard API 不可用时回退到 `execCommand`
- 对话接口错误详情附带请求 ID，便于定位日志
- 前端静态资源版本号更新为 1.4.0（`style.css` / `app.js` / `theme.js`），避免浏览器使用旧缓存
- 限流新增客户端 IP 维度：`CHAT_RATE_LIMIT_IP`（默认 120 次/窗口），用户 ID 由请求方提供、可轮换，IP 桶防止绕过
- 访问日志支持 JSON lines：设置 `LOG_FORMAT=json` 后应用日志每行为合法 JSON（time / level / logger + event=http_request 等字段），默认保持原单行文本格式
- Web 服务启动时配置应用日志 handler：访问日志（含请求 ID / 耗时）与智能体日志此前在默认运行下不可见，现已输出到控制台
- 写入行程时按现有最大 `trip_N` 序号递增（此前按条数生成，配合删除功能会与已有行程 id 冲突）

### 修复
- 交互式 CLI 的用户 ID 增加与 Web 一致的校验（拒绝路径穿越 / 短横线开头），README 补充 Windows 大小写不敏感说明
- 用户锁表增加上限（1000，淘汰未持有的旧锁），避免轮换用户 ID 造成内存持续增长
- 安全加固：sdist 排除本地知识库 / 模型 / 记忆 / 导出目录（`MANIFEST.in`），避免分享安装包时带出业务文档；请求体超过 2 MB 返回 413（带安全头 / 请求 ID）；`docker-compose.yml` 的 Redis / PostgreSQL 仅绑定 127.0.0.1；CSV 日期列同样做公式注入转义；偏好拒绝 NaN / Infinity（历史遗留值读取时归一为 None）；Redis URL 日志隐藏密码；关闭 uvicorn 自带 access log（避免查询串中的用户原文入日志）
- JSON 长期记忆：偏好 / 消息 / 行程 / 删除等写盘失败统一抛 `OSError`（不再静默成功）；损坏的记忆文件先备份为 `.corrupt-<时间戳>` 再重建，避免直接覆盖
- PostgreSQL 行程 `trip_id` 使用 advisory lock 串行生成，并尝试建立 `(user_id, trip_id)` 唯一索引（历史重复时跳过建索引）
- 会话生命周期：回答生成期间禁止删除会话 / 切换用户，后端删除也会等待该用户进行中的对话结束，避免生成结果写回已删会话或串到新用户；切换用户同时重置行程 / 知识编辑表单
- SSE 先推送会话 ID：首条提问被停止或失败后仍可编辑 / 重试；「重新生成」先删除服务端上一轮，历史不再叠加重复问答
- 搜索或归档视图切换的加载期间忽略「加载更多」，避免旧列表 offset 与新查询混用
- 会话标题 / 置顶 / 归档与行程 CSV 导出在存储读取或写盘失败时统一返回 503（此前会话元数据可能 500，CSV 导出为通用 500）
- 行程 CSV 导出对以 `=` / `+` / `-` / `@` 开头的内容加前导单引号，防止 Excel 公式注入；测试断言 UTF-8 BOM 原始字节
- CLI `session-export`：`--stdout` 与路径同时使用返回用法错误；默认拒绝覆盖已有文件（`--force` 覆盖）；补充默认路径与 dispatch 用例
- Markdown 用户数据导出测试增加未归档会话对照，避免「全部标注已归档」的回归被掩盖
- JSON 后端的会话标题 / 置顶 / 归档在写盘失败时抛错，Web 与 CLI 不再静默报告成功
- CLI `sessions` / `session-archive` 严格校验参数：未知 / 冲突参数与 `--restore=1` 等写法返回用法错误，以短横线开头的 ID 被拒绝；补充 PG 正常放行与写入失败用例
- gzip 中间件移到 `RequestContextMiddleware` 内层，压缩响应（含异常 500）仍带安全响应头与请求 ID；新增中间件级用例，SSE 按路径排除不再依赖响应 Content-Type
- 会话归档：`clear_history` / `delete_all` / 删除最后一轮对话同步清理归档标记（JSON 与 PostgreSQL），避免留下幽灵归档；删除会话测试改为直接断言存储中的归档列表
- 会话归档：归档视图下「新对话」会自动切回普通列表，新会话不再从侧栏消失；直接打开归档会话的深链接时标题从服务端读取，不再显示为「对话」
- 会话归档：`/api/status` 请求增加序号与用户校验，过期响应不再覆盖新的归档计数导致入口消失
- 快捷键帮助面板叠在记忆面板上时，`Tab` 不再被记忆面板的焦点圈禁抢走；关闭后焦点回到打开前的元素（不可聚焦时回到顶栏按钮）
- 编辑提问：删除最后一轮前校验提问原文（新增 `expect` 参数），内容不一致时（提问在意图识别阶段失败、并未写入历史）不删除任何内容，避免误删上一轮
- 编辑提问：短期记忆清理失败不再让接口返回 500（此前长期记忆已删除、前端重试会再删一轮）；Redis 短期记忆改用 Lua 脚本原子裁剪，并修复 `RTRIM` 命令不存在导致多轮会话裁剪失败的问题
- 知识片段编辑：保留原有 metadata 的其它字段（此前只回填分类，`title` / `source` / `file_path` 等会被清空）；保存期间禁止切换编辑或删除，避免表单状态错乱
- 知识片段删除：正确禁用「删除」按钮（此前禁用到了新增的「编辑」按钮）
- `/api/status` 的进行中行程判断改为解析日期（此前按字符串比较，非 ISO 结束日期会被误判为进行中）
- 系统自检的记忆存储项按实际生效的后端报告，并在回退时同时说明配置项与实际使用的实现
- 编辑提问期间导出 HTML / 打印会先还原被编辑的气泡（此前导出的 HTML 中是一个内容为空的 textarea）；回答生成期间隐藏上一条消息的编辑入口，避免误以为可以编辑历史提问
- 偏好追加时若存量与新值相同（字符串 → 列表），不再产生重复项（如「汉庭、汉庭」）
- RAG 智能体在进程内按数据库路径共享 Milvus Lite 客户端，修复多会话 / 全量测试中重复打开本地库导致的偶发文件锁错误
- 删除无消息的会话时也会清理其标题与置顶元数据（此前仅在有消息时清理）
- 长期记忆 JSON 改为临时文件 + 原子替换写入，加载时不再无条件重写文件，避免中途退出留下损坏的记忆文件
- 对话期间通过 API 修改的会话标题 / 置顶 / 偏好不再被本轮写回覆盖（快照型后端写入前重新加载）
- 清空历史 / 删除全部数据同步清理会话标题与置顶（JSON / PostgreSQL）；`export_data` 包含标题与置顶
- 用量 `by_day` 按本地时区归日；`0` / `false` 偏好不再被真值过滤隐藏
- 会话 Markdown 导出使用自定义标题；ICS 结束日期早于出发日期时按单日处理，UID 稳定避免重复导入
- RAG 检索结果非列表时格式化兜底；429 的 `Retry-After` 向上取整为整数秒
- 新增知识片段时先 `load_collection` 再查询最大 id；查询失败不再回退到行数递增（删除过片段后旧逻辑会与已有主键冲突并静默覆盖旧知识）
- 知识库列表 / 删除时查询失败正确返回 503，此前被伪装成“空库 200 / 片段不存在 404”
- 知识库维护列表支持搜索与分页（每页 20 条、可加载更多），触屏设备删除按钮可见，提交期间的新输入不会被误清空，删除失败按状态码提示并刷新列表
- `/api/doctor` 的同步检查移到线程池，冷启动打开 Milvus 时不再阻塞对话 SSE 与其它接口
- 知识片段 `metadata` 限制键数（最多 10）与键值长度（键 32 / 值 200 字符），防止超大对象拖垮列表接口
- `python cli.py eval` 区分“未初始化（跳过）”与“后端故障（失败）”；检索全部未命中时提示索引故障而非切分问题；RAG Agent 初始化失败给出明确错误
- 知识片段写入成功后统计失败不再把已成功的添加报成失败
- JSON 长期记忆的所有写操作改为「按文件路径加锁 + 重载后修改」：对话回合结束时用旧快照写回，不会再复活中途删除的行程或覆盖新写入的偏好 / 标题
- 行程删除写入失败（磁盘只读 / 文件被占用）返回 503，不再提示删除成功；ICS UID 纳入行程内容，删除最新行程后 id 复用不会覆盖日历中已存在的旧事件
- 知识库导入：Web 端按每批 200 条上传（超过 200 条的导出文件也能完整恢复），CLI 不再限制 200 条；导入条目放宽为“跳过空内容 / 截断超长字段”，与 CLI 行为一致，不再因一条不合规导致整批失败
- 知识库导入按内容去重（已有片段 / 同批次重复自动跳过），重复导入同一备份或分批失败后重试不会产生重复片段；中途失败会报告已导入数量并刷新列表，导入期间按钮立即禁用避免并发导入
- 知识库导入的去重判断移入写锁（并发导入不会再基于旧快照插入同一批内容），并按每 500 条分块嵌入 / 插入，控制大文件导入的峰值内存
- 记忆面板行程操作按钮此前因按钮自身 `opacity: 0` 永远不可见（含触屏），已修复；键盘打开搜索结果同样会定位并高亮命中消息；行程不存在（404）时自动刷新列表
- 记忆面板焦点管理：打开时聚焦面板内按钮、`Tab` / `Shift+Tab` 在面板内循环，关闭后焦点回到触发按钮（配合 `aria-modal` 完善键盘可访问性）
- 记忆面板焦点陷阱改为 document 级监听：列表重渲染 / 按钮禁用导致焦点落回 body 后，下一次 `Tab` 仍会被拉回面板；关闭时触发按钮已隐藏则聚焦输入框；偏好编辑按 `Esc` 只取消编辑、不再关闭整个面板
- 会话导出 HTML：补齐打印样式的深色气泡与代码块前景色（白底打印不再不可见）、文件名使用本地日期、Blob URL 延迟释放（避免 Firefox / Safari 取消下载）；状态加载失败时隐藏「导出全部日历」
- 同一行程（同路线 + 同起止日期）重复保存时更新既有记录，不再产生重复行程、重复统计与重复日历事件（JSON / PostgreSQL）
- 偏好抽取只依据用户本轮原话（`context.user_query`），不再把上下文或助手建议（如行程里的舱位、预算）误记为用户偏好；缺少原话时回退到重写查询
- 知识库客户端首次打开遇到其它进程刚释放文件锁的瞬时失败时重试一次（修复服务重启后立刻打开记忆面板可能出现的 503）
- Milvus 共享客户端注册表加锁，RAG Agent 重连后不再持有已关闭句柄；知识库路径与集合名统一到 `utils/knowledge_paths.py`，Web / CLI / doctor 共用
- 知识库状态查询优先复用已缓存客户端，未缓存时用临时客户端并即时关闭，查看状态不再长期占用 Milvus 文件锁
- 每个 Web 会话使用独立的 token 用量计数（共享底层模型），修复多会话并发时单轮用量互相污染甚至为负
- 限流 key 数量有上限并清理过期条目，修复轮换随机用户 ID 导致的内存增长
- 文档页（`/docs`、`/redoc`）使用放宽的 CSP，Swagger UI / ReDoc 恢复渲染；普通页面策略不变
- 未处理异常返回的 500 响应补齐安全响应头与 `X-Request-ID`（此前绕过中间件直接返回）
- Web 交互：会话行键盘操作不再串扰其余按钮、IME 组合输入不误提交、搜索高亮不破坏 HTML 实体、Escape / 切换用户清理搜索防抖、「即将出行」优先使用后端 `next_trip`、深链接的 `user` 不再覆盖本地长期身份、偏好编辑保留含逗号的列表项并识别数字 / 布尔
- 长期记忆写盘失败不再被当作可重试的网络错误：新增 `MemoryPersistenceError`（不继承 `OSError`），磁盘只读 / 空间不足时不会重跑整轮对话、重复计费或误开熔断器；偏好新增 / 删除写盘失败返回 503（此前为通用 500）
- 请求体 2 MB 上限覆盖分块传输（无 `Content-Length` 的 chunked 请求），此前可被绕过并把超大请求体完整读入内存
- 损坏记忆文件备份前加锁复核：解析失败与备份之间若其它线程已写入新的有效文件，不再把新数据当作损坏文件移走；备份名附加微秒与进程号，同一秒内多次损坏不再互相覆盖
- 知识库语义检索接口在后端故障 / 未初始化时返回 503（此前 RAG Agent 吞掉异常返回 200 空结果，界面误报「没有相关内容」）
- 知识库维护面板：语义检索结果不再被并发的关键词列表刷新覆盖；删除 / 编辑 / 导入后保持语义模式并刷新当前结果；在搜索框输入或切换分类会退出语义模式
- 限流额度头 `X-RateLimit-Limit` / `Remaining` / `Reset` 统一取自当前更紧的限流桶（此前 Limit 恒为用户桶，与 IP 桶的 Remaining 自相矛盾）
- 用户锁表占满且全部在用（并发高峰）时新用户改用共享兜底锁，锁表不再随轮换用户 ID 无限增长
- Redis URL 日志隐藏完整密码（含密码中未转义 `@` 的其余部分）
- 记忆面板「查看全部」改为展示最近 200 条（此前取的是最早的 200 条，最新的行程反而看不到）；`GET /api/trips` 新增 `order=asc|desc`
- 损坏记忆文件复核路径：合法 JSON 但不是对象 / 字段结构异常时同样备份重建，不再抛 `TypeError` 导致相关接口永久 500
- 切换用户时进行中的「查看全部行程」响应不再应用到新用户（避免旧用户行程显示在新用户名下、误删同名 `trip_id`）
- 语义检索进行中隐藏「加载更多」，语义模式下不再混入关键词列表行
- 只保留最新一条回答的「重新生成」按钮；服务端最后一轮与点击的回答不匹配时提示并终止，不再删除旧回答后重发旧问题（避免打乱消息顺序、在历史中留下重复问答）
- 回复完成时不再抢焦点，也不在记忆面板打开 / 侧栏重命名期间重建列表，避免打断正在编辑的偏好或会话标题
- 切换用户时立即关闭记忆面板并清空旧用户的会话列表 / 概览 / 即将出行卡片，新用户数据返回前旧行不再可误删误改
- 偏好编辑与添加一致识别 ASCII 逗号（`汉庭,如家` 保存为两项，此前存成一项）
- 启动恢复会话失败时丢弃旧 session_id 并回到新对话，避免下一条消息写入界面未展示的隐藏会话
- 删除会话会作废在途的会话加载，删除后晚到的响应不再渲染已删除的会话或使它「复活」
- PostgreSQL 删除行程改为按主键删除（历史库存在重复 `trip_id` 时，此前一次操作会删掉多条不同行程）；重复保存行程时未提供的 `purpose` / `session_id` 保留原值（与 JSON 后端一致，此前会被默认值覆盖 / 置空）
- JSON 长期记忆读取全程持有按路径的锁：Windows 上打开的文件句柄会阻止并发 `os.replace`，此前并发读写偶发「长期记忆写入失败」
- `trips-dedupe` 删除前强制从磁盘重载：JSON 后端返回进程启动快照，此前「保留者已被并发删除」的复核永远不生效，可能删掉仅剩副本
- 长期记忆对 `preferences` / `chat_history` / `trip_history` 为 `null`（或类型错误）的文件在读取时修复为空集合，此前这些合法 JSON 文件会让相关接口永久 500
- 跨会话总结按「当前会话消息数 + 上限」取窗口，长会话不再饿死其它会话的历史（此前聊满 30 条后跨会话记忆消失）
- `python cli.py kb-export` 默认拒绝覆盖已有文件（此前无条件覆盖，误把记忆文件当导出路径会被静默替换），新增 `--force`；未知 / 冲突参数返回用法错误
- CLI `sessions` 把 PostgreSQL 的 UTC 时间转换为本地时区展示（此前 JSON 后端显示本地时间、PG 后端显示 UTC，相差数小时）
- Redis 短期记忆的「删除最后一轮」按与 Python `strip()` 一致的空白字符集校验提问内容（此前全角空格结尾的提问会被误判为不一致，长期记忆已删除而短期记忆残留旧一轮）
- 意图识别 / 事项收集 / 偏好提取的 JSON 解析改为扫描第一个完整对象：结尾说明里的花括号（如「如 {日期} 未提供可默认明天」）不再让合法输出解析失败并降级为默认策略
- 编排器按 `status=error` 判定智能体失败、`error: null` / 空串判定成功（此前记忆查询、未初始化 RAG 的失败被当成成功，行程结果带 `error: null` 反而被判失败）
- 非法 `agent_schedule`（字符串项 / 非数字 priority / null）先清洗再排序，单条坏计划不再让整轮对话 500
- 智能体懒加载失败（缺依赖 / 模型文件损坏）转为单个智能体的错误并发出 `agent_done`，不再整轮 500 或让进度停在执行中
- 行程结果渲染对非标准结构（`itinerary` 为字符串、`daily_plans` 为字典、`notes` 为字符串等）容错，渲染失败不再让已持久化的回合返回 500
- RAG 检索在 `rewritten_query` 缺失 / 为空 / null 时回退到原始提问；空查询直接视为无结果，不再返回任意 top-k 片段
- 网页搜索（DDGS）与 RAG 向量检索移到线程池执行，不再阻塞事件循环（SSE 心跳与其它会话不再停顿）
- RAG 嵌入模型进程内共享，每个会话不再各自加载约 90 MB 的模型
- 偏好追加改为存储层锁内「重载 + 合并」的原子操作，不再覆盖本轮 LLM 调用期间在记忆面板写入的偏好
- 跨会话总结指纹包含行程内容，编辑行程（改日期 / 目的）后不再复用旧总结
- PostgreSQL 从故障恢复、会话从本地 JSON 切回 PostgreSQL 时记录警告：故障期间写入 JSON 的消息 / 偏好不会自动合并（README 已知限制同步说明）
- 知识库未初始化时语义检索返回 503 且不再悄悄新建空库（此前一次检索会把缺失的库建成「可用 0 片段」）；`init_knowledge_base.py` 失败时以非零退出码结束（此前恒为 0），验证检索的相似度不再显示 `1-distance`
- 知识库状态查询改用共享 Milvus 客户端，并修正「关闭客户端即可释放文件锁」的错误说明（Milvus Lite 的目录锁在进程生命周期内保持）
- 知识库导入跳过 `content` 为 `null` / `false` / `0` 的条目（此前会写成 "None" / "False" / "0" 片段）；Web 导入接口允许非对象条目并跳过（此前整批 422）
- `kb-import` 读取非 UTF-8 文件时返回「读取失败」（此前抛 `UnicodeDecodeError` 堆栈）
- 分批导入失败时报告已导入数量（此前丢弃计数，用户无法判断库是否已被部分修改）
- 可重试错误判定识别 OpenAI SDK 的 `APIConnectionError` 等连接类异常（此前网络中断被当成永久错误：意图识别降级、熔断器不打开）；报错文本里的数字（列号 / token 数）不再被误当作 HTTP 状态码
- 健康检查要求模型返回非空文本（部分兼容网关以 200 返回错误体、SDK 不抛异常，此前误报 ok）
- 熔断器半开状态同一时刻只放行一个探测请求（此前恢复瞬间全部放量），并用实际状态处理迟到的失败（刷新打开时间）
- 意图识别拒绝顶层非对象 / 含 NaN 的 JSON（此前 500，或 NaN 写入记忆导致会话历史无法打开）并降级为默认策略
- 意图 / 执行元数据对非列表 `agent_schedule` / `intents` / `results` 容错（此前 `TypeError` 让整轮 500）
- 访问日志文本模式清理 CR/LF / 控制字符（uvicorn 会 percent-decode 路径，此前 `%0A` 可伪造日志行）
- 删除会话与在途 SSE 对话的竞态：流在推送第一个事件前先取得用户锁，并记录短时「已删除」墓碑——删除后旧 `session_id` 的新消息返回 404，已删除的会话不再被写回
- 会话缓存键改为「用户 + session_id」：删除 B 的同名会话不再关闭 A 的运行时或清空 A 的进程内短期记忆
- 限流先检查两个桶再记录：被 429 的请求不再消耗另一个桶的配额（一个客户端的重试循环不再连坐同 IP 的其它客户端）
- 编辑 / 重发的 `expect` 查询串可达 4000 字中文：uvicorn 的 h11 请求行上限提升到 64 KB（默认 16 KB 会在协议层直接拒绝）
- 会话标题 / 置顶拒绝不存在的会话（404），不再写入会被复用 id 的新对话继承的幽灵元数据
- 纯空白目的地 / 聊天消息返回 422（此前空白目的地绕过校验并产生重复记录，空白消息会触发一次 LLM 调用）；目的地自动去除首尾空白
- 结束日期为 9999-12-31 的行程不再让单个 / 全部日历导出 500（DTEND 无法再加一天）
- 慢 PostgreSQL / Redis 下构造会话与删除会话 / 删除最后一轮移入线程池，不再阻塞事件循环（SSE 心跳与其它会话）
- 交互式 CLI 在 stdin EOF（管道 / IDE 运行）时正常退出：用户 ID 提示不再抛 traceback，主循环不再每轮报错刷屏；查询进行中按 Ctrl-C 不再抛原始 traceback
- 模型返回列表形式的追加偏好不再存成嵌套列表（否则跨会话总结的 `", ".join` 崩溃、之后每个问题都失败）；摘要生成对历史嵌套偏好 / 非字符串行程字段容错
- CLI 行程渲染对非标准结构（`itinerary` 为字符串、`daily_plans` 为字典、`meals` 为列表等）容错，渲染失败不再丢掉整轮回答
- CLI 中回答 / 偏好 / 行程 / 参考来源等外部文本按原文显示：rich 标记不再吞掉方括号内容，或遇到 `[/red]` 之类关闭标签时抛异常
- CLI 的 preferences / history / status / export 先重载长期记忆，避免导出其它进程刚写入前的旧快照
- Windows 管道输入统一按 UTF-8 解码（此前 GBK locale 下 UTF-8 管道输入乱码）
- 知识库 `next_entity_id` 在集合超过 16384 条时按 id 分页扫描，避免只看到任意子集取到偏小最大值、与已有主键冲突并静默覆盖
- CLI 调用智能体汇总行 / 健康检查错误 / 主循环错误处理中的模型文本统一转义（此前智能体名含 `[/red]` 会让错误处理自身再抛 `MarkupError` 并直接崩溃）
- 事项收集的 `missing_info` 对字符串 / 字典列表容错并按原文显示（此前字典元素抛 TypeError、字符串被按字拆开）；空字符串行程回退到通用字段；移除重复的行程渲染死代码
- `/api/metrics` 的方法名基数上限 16（HTTP 方法由客户端任意指定，此前可让指标载荷与内存无界增长）
- 测试补强：智能体名转义 / `missing_info` 类型 / 空行程回退 / GBK 管道 / 指标方法基数

### 测试
- 新增偏好编辑 / 删除 API、最近行程、JSON / PostgreSQL 删除偏好、追加去重用例
- 新增对话失败路径测试：`/api/chat` 返回 500（含请求 ID 与失败原因）、SSE 推送 `error` 事件
- 新增存储一致性（原子写入 / 对话中途元数据 / 清理完整 / 导出 / 时区归日 / ICS）、每会话用量隔离、IP 限流与 key 淘汰、文档页 CSP、未处理异常 500 响应头用例
- 新增知识库维护（列表解析 / 排序 / 分页、接口校验与 404、真实 Milvus 增删闭环）与检索评测指标（hit@K / MRR / top_k 截断）用例
- 新增 `/api/doctor` 自检接口用例（结构 / 状态取值 / 默认不请求网络）
- 新增 `next_entity_id`（load 顺序 / 最大 id / 查询失败传播）、知识库查询失败透传与 503 映射、metadata 限长、集成测试语义检索与“不覆盖已有片段”用例
- 新增 `cli.py eval` 退出码语义用例（未初始化跳过 / 后端故障失败 / Agent 初始化失败 / 阈值与全量未命中提示）
- 新增知识库导出（排序 / 不含 id）与导入（跳过空项 / 截断超长字段 / 空导入 / 未初始化）用例、导出下载头与导入参数校验接口用例
- 新增 CLI `kb-export` / `kb-import` 用例（导出文件 / 未初始化 / 文件缺失 / 格式错误 / 批量上限 / 纯数组格式）
- 新增 CLI `kb-list` / `kb-delete` 用例（列表输出 / 关键词透传 / 不可用 / 读取失败 / 删除成功 / 不存在 / 删除失败）
- 会话搜索用例增加命中消息序号断言（用户消息 0 / 助手消息 1）
- 新增行程删除用例（JSON 统计 / 常去目的地递减、删除后新增 id 不与已有记录冲突；PostgreSQL 同步断言）与 `DELETE /api/trips/{id}` 接口用例
- 新增旧快照写回不复活已删除行程、删除写入失败映射 503、ICS UID 随行程内容变化、CLI 大文件导入不受 200 条限制、导入空条目跳过等用例
- 新增行程重复保存去重用例（JSON：同日期更新 / 不同日期新增 / 无日期不去重；PostgreSQL 集成断言）
- 新增偏好抽取输入选择用例（优先用户原话 / 回退重写查询）与编排上下文 `user_query` 用例
- 新增全部行程日历用例（多事件合并 / 跳过缺日期行程 / 空列表；接口附件头与事件数）
- 新增访问日志格式用例（默认文本 / JSON 字段 / `LOG_FORMAT` 环境变量切换）
- 新增 CLI `trips-dedupe` 用例（去重保留最早 / 无日期跳过 / 空列表 / 非法 ID / 读取失败 / 打开失败）
- 新增知识库导入去重用例（已有内容 / 同批次重复跳过）
- 新增 JSON 日志 formatter 合法 JSON 用例、导入 500 条分块用例、行程去重并发 / 删除失败 / PG 回退用例、ICS 孤立 CR 用例
- 新增「导出→导入幂等」集成用例（原样导入备份时已有片段全部按内容跳过、数量不变）
- 新增手动添加行程接口用例（去重返回同一 trip_id / 结束早于出发 / 非法日期 / 缺少目的地）
- 新增会话分页用例（`total` / `offset` / 页间不重复 / 搜索结果计数）
- 新增 Web API + PostgreSQL 集成用例（偏好 / 行程 / 对话 / 重命名 / 置顶 / 状态 / 导出 / 删除，并断言配置 DSN 后不回退 JSON；CI 自动启用，本地以 `TEST_POSTGRES_DSN` 运行）
- 新增 Web API + Redis 短期记忆集成用例（对话写入 Redis List、状态显示 redis 后端、删除会话清理 Redis 键；CI 自动启用，本地以 `TEST_REDIS_URL` 运行）
- 新增行程更新用例（JSON 字段 / 统计 / 清空日期 / 冲突拒绝；PostgreSQL 集成更新断言；接口部分更新 / 400 / 404 / 409 / 422）
- 新增 Milvus 客户端首次打开重试用例（一次失败后成功 / 持续失败上抛且不缓存坏客户端）
- CI 回归测试附加覆盖率报告（`pytest-cov`，仅展示不设阈值；本地全量环境覆盖率更高）
- CI 增加 wheel 打包检查（Python 3.12 job：`python -m build --wheel`，提前发现 pyproject / 包数据问题）
- 新增写盘失败不可重试、偏好写盘失败映射 503、损坏文件并发写入不被误移、含 `@` 密码脱敏用例；分块请求体超限 413 与小请求放行、IP 桶额度头、锁表占满共享兜底锁用例；RAG Agent 严格检索（未初始化抛错）用例
- 新增记忆文件为合法 JSON 非对象 / 结构异常时的备份重建、`/api/trips` 倒序分页用例；并发写入用例改为断言新偏好被真正加载
- 新增 CLI `trips` 用例（列表输出 / 状态 / 重叠 / 空列表 / 读取与打开失败 / 非法用户 / 严格参数）与行程日期工具用例
- 前端浏览器实测（延迟请求模拟）：重新生成仅保留最新按钮且 mismatch 不重发、回复完成不破坏偏好编辑、切换用户立即清空旧数据、ASCII 逗号拆分、语义检索期间加载更多不回归、恢复会话失败回到新对话、删除会话不复活
- 新增 null 字段修复、读取持锁、PG 单行删除、长会话总结、trips-dedupe JSON 重载用例；PG 集成补充 `purpose` / `session_id` 保留断言
- 新增 kb-export 覆盖保护与严格参数、CLI UTC 时间本地化、Redis 全角空格裁剪用例
- 新增 JSON 尾部花括号解析、`status=error` / `error=null` 判定、非法调度清洗、懒加载失败、行程渲染容错、RAG 提问兜底、偏好原子追加、行程编辑使总结缓存失效、嵌入模型共享用例；PG 集成补充原子追加断言
- 新增语义检索拒绝未初始化库、导入跳过非字符串内容、分批失败报告已导入数量、Web 导入非对象条目、kb-import 非 UTF-8 文件、初始化脚本退出码用例
- 新增 `tests/test_reliability_utils.py`（可重试错误判定 / 重试不误伤 / 健康检查空响应 / 熔断器半开单飞与迟到失败 / 元数据非列表容错 / 访问日志控制字符）与意图 NaN / 非对象输出用例
- 新增会话缓存用户隔离 / 删除墓碑 / 限流桶不连坐 / 空白目的地与消息 / 9999-12-31 日历边界 / 幽灵元数据 404 用例与 `RateLimiter.peek` 用例
- 新增 CLI `preferences` 用例（列表输出 / 空列表 / 读取与打开失败 / 非法用户 / 严格参数）
- 测试补强（针对「形同虚设」的用例）：SSE 流结束后用户锁释放、LRU 淘汰关闭会话、删除最后一轮清理缓存的进程内短期记忆、IP 桶拒绝对称、知识库导出不受默认分页截断、跨会话总结提示词内容断言、写盘失败不计入熔断器；真实 LLM 意图脚本补跳过标记与失败断言（CI 不再以空密钥发真实请求）
- 智能体层：规划失败的兜底 itinerary 不再写入行程历史（此前 status=error 仍产生一条从未规划的行程）；调度使用技能目录名（`plan-trip` / `event-collection` 等）时结果与记忆写入按标准名处理并正常保存行程
- 天气查询：城市按在问题中出现的位置选取（修复「帮我查一下三亚天气」被解析成「帮我查一下三」、深圳/广州与多城市对比选错）；预报使用地点自身时区（此前固定 `Asia/Shanghai`，非中国城市的日期与最高最低温按上海日历日聚合）
- 搜索摘要把网页内容标记为数据并明确禁止执行其中指令，降低搜索结果提示注入风险；query-info 与 RAG 共用 `utils/user_query.extract_user_query`，`rewritten_query` 为空 / null 时回退原始提问（不再把整段 Orchestrator JSON 当作搜索词 / 城市来源）
- memory-query 不再截断到最近 50 条行程（超过 50 条时「去过某地吗」会误答没有记录），并为 null 行程字段显示「未知 / 旅游」；plan-trip 兼容 SKILL.md 文档中的 `previous_results` 简化结构
- 新增 `tests/test_cli_interactive.py`（EOF / 主循环 EOF / UTF-8 管道 / 富文本转义 / 非标准行程 / 快照刷新 / 嵌套偏好）与超过 16384 条的 `next_entity_id` 分页用例；偏好追加补列表值用例
- 新增 `tests/test_metrics.py`（计数 / 状态 / 耗时 / 路径基数上限 / 控制字符 / 重置）与 `/api/metrics` 接口用例
- 新增 `tests/test_skill_agents.py`（天气城市提取 / 时区 / 搜索摘要防注入 / 共享提问提取 / null 行程字段）与编排器「规划失败不入库 / 技能目录名规范化」用例
- 新增 `tests/test_session_markdown.py` 与 `/api/sessions/export` 接口用例（全部会话 / 归档 / 404 / 503 / 静态路由不被动态路由捕获）
- 新增 CLI `sessions-export` 用例（stdout / 文件 / 覆盖保护 / 无会话 / 读取与打开失败 / 非法用户 / 严格参数）

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
