"""日志与错误文本脱敏（DSN / URL 凭据不落日志）。

psycopg / redis-py 的连接异常经常把完整 URI 拼进异常文本（含密码、或未编码
密码片段）；这些文本会进入应用日志、甚至经 503 详情回流给客户端。存储层
与调用方在记录 / 抛出之前统一用 :func:`redact_secret` 处理。
"""
import re

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^\s/@]+@")
_URL_QUERY = re.compile(r"(?P<base>[a-zA-Z][\w+.-]*://[^\s?#]+)[?#][^\s]*")


def redact_secret(text: object) -> str:
    """去掉文本中 URL / DSN 的 userinfo 与查询串（保留协议与主机便于排查）。"""
    cleaned = _URL_USERINFO.sub(r"\g<scheme>***@", str(text))
    cleaned = _URL_QUERY.sub(r"\g<base>", cleaned)
    return cleaned
