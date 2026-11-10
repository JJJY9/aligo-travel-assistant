"""日志与错误文本脱敏（DSN / URL 凭据不落日志）。

psycopg / redis-py 的连接异常经常把完整 URI 拼进异常文本（含密码、或未编码
密码片段）；这些文本会进入应用日志、甚至经 503 详情回流给客户端。存储层
与调用方在记录 / 抛出之前统一用 :func:`redact_secret` 处理。
"""
import re

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^\s/@]+@")
_URL_QUERY = re.compile(r"(?P<base>[a-zA-Z][\w+.-]*://[^\s?#]+)[?#][^\s]*")
# 没有 scheme 的 user:pass@host（常见粘贴错误：DATABASE_URL 少了 postgresql://）
_BARE_USERINFO = re.compile(r"(?<![\w+.-])([A-Za-z0-9_.%+-]+):([^\s@/:;,]+)@")
# libpq 关键字形式（host=… password=…）与常见密钥字段
_KEY_VALUE = re.compile(
    r"(?i)((?:password|pwd|api[_-]?key|secret|token)\s*=\s*)[^\s;]+"
)


def redact_secret(text: object) -> str:
    """去掉文本中 URL / DSN 的 userinfo 与查询串（保留协议与主机便于排查）。

    psycopg 的报错会把整条连接串拼进异常文本：URI 形态、缺 scheme 的
    ``user:pass@host`` 粘贴、``password=`` 关键字形态都要遮蔽。
    """
    cleaned = _URL_USERINFO.sub(r"\g<scheme>***@", str(text))
    cleaned = _URL_QUERY.sub(r"\g<base>", cleaned)
    cleaned = _BARE_USERINFO.sub(r"\1:***@", cleaned)
    cleaned = _KEY_VALUE.sub(r"\1***", cleaned)
    return cleaned
