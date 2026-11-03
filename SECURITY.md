# 安全策略

## 报告安全问题

如果发现安全问题（例如密钥泄露、注入、越权访问等），请不要公开提交 Issue，优先通过仓库 **Security → Report a vulnerability** 私下联系维护者；不方便使用该入口时，可先提交一个不含利用细节的 Issue 说明情况，维护者会跟进。

## 仓库约定

- 仓库**不包含任何真实密钥**：大模型 API Key 通过环境变量 `LLM_API_KEY` 或本地 `config_local.py`（已 gitignore）提供，示例值见 `.env.example`
- `data/memory/`、`data/models/`、`rag_knowledge/`、`.env` 等运行时产物与本地配置不入库
- Web 服务默认仅监听 `127.0.0.1`；用户 / 会话 ID 会做字符集校验，接口带基础安全响应头（CSP / X-Frame-Options 等）与请求频率限制

## 部署提示

本项目定位为**本地演示实现**。如需对外部署，请自行补充：身份认证与授权、HTTPS、反向代理、独立限流（如 Redis），并评估 Milvus Lite / 本地 JSON 存储在并发与多实例场景下的限制（见 README「已知限制」）。

## 支持范围

以 `main` 分支为最新版本，安全问题的修复会直接进入 `main` 并在 CHANGELOG 中记录。
