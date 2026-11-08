"""会话 ID 的公共约定（CLI 与 Web 共用）。"""

# 与静态路由冲突的会话 ID：GET /api/sessions/export 永远命中导出接口，这类
# 会话无法在面板查看 / 继续（聊天请求与备份恢复都必须拒绝）。CLI 的备份恢复
# 与 Web 的 /api/import 使用同一份清单。
RESERVED_SESSION_IDS = frozenset({"export"})
