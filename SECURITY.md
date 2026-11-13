# 安全策略

## 报告安全问题

如果发现安全问题（例如密钥泄露、注入、越权访问等），请不要公开提交 Issue，优先通过仓库 **Security → Report a vulnerability** 私下联系维护者；不方便使用该入口时，可先提交一个不含利用细节的 Issue 说明情况，维护者会跟进。

## 仓库约定

- 仓库**不包含任何真实密钥**：大模型 API Key 通过环境变量 `LLM_API_KEY` 或本地 `config_local.py`（已 gitignore）提供，示例值见 `.env.example`
- `data/memory/`、`data/models/`、`rag_knowledge/`、`.env` 等运行时产物与本地配置不入库
- Web 服务默认仅监听 `127.0.0.1`；用户 / 会话 ID 会做字符集校验，接口带基础安全响应头（CSP / `X-Frame-Options` / `/api/` 的 `Cache-Control: no-store` 等）与请求频率限制（对话接口按用户 + IP，知识库写入 / 导入 / 导出 / 语义检索按 IP，天气查询按 IP）

## 部署提示

本项目定位为**本地演示实现**。如需对外部署，请自行补充身份认证与授权、HTTPS、反向代理，并建议：

- 设置 `REDIS_URL`：短期记忆与限流计数（用户 / IP / 知识库 / 天气四个桶）多实例共享，运行中 Redis 故障会回退进程内实现
- 设置 `DATABASE_URL`：长期记忆、偏好、行程与会话元数据使用 PostgreSQL；多实例部署不要使用默认 JSON 后端
- 反向代理后设置 `TRUST_PROXY_HEADERS=1`，最靠近应用的代理应**追加写入** `X-Forwarded-For`（应用取**最后一个地址**——最近一跳代理看到的对端地址；前面的条目可能被客户端伪造，不会被采用）：
  ```nginx
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  ```
  多层代理时每一层都会追加，应用取到的最后一项即内层代理看到的客户端地址；直接暴露服务时保持默认关闭。
- 限制知识库维护接口（写入 / 导入 / 导出）的访问范围，并按部署规模调整 `CHAT_RATE_LIMIT` / `CHAT_RATE_LIMIT_IP` / `KB_RATE_LIMIT` / `KB_IMPORT_RATE_LIMIT`（导入与预览使用独立桶）；面板数据读接口（会话列表 / 状态 / 导出 / 台账）按 `READ_RATE_LIMIT`（默认 600 次 / 分钟）限流；并发会话数受进程内会话缓存上限约束（全部在用时的超额对话返回 503，避免打爆数据库连接数）
- 评估 Milvus Lite 单进程文件锁与本地 JSON 存储在并发 / 多实例场景下的限制（见 README「已知限制」）；共享向量库需替换为 Milvus 服务端部署

## 支持范围

以 `main` 分支为最新版本，安全问题的修复会直接进入 `main` 并在 CHANGELOG 中记录。
