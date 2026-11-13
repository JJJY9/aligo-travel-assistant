"""日志与错误文本脱敏（DSN / URL 凭据不落日志）。

psycopg / redis-py 的连接异常经常把完整 URI 拼进异常文本（含密码、或未编码
密码片段）；这些文本会进入应用日志、甚至经 503 详情回流给客户端。存储层
与调用方在记录 / 抛出之前统一用 :func:`redact_secret` 处理。
"""
import re

# userinfo 允许出现 "/" ":" ";" "," 与多个 "@"（密码未编码 / 多 @）：
# 贪婪吃到 "?" "#" 前的最后一个 "@"，避免只遮住第一段密码片段
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^\s?#]*@")
_URL_QUERY = re.compile(r"(?P<base>[a-zA-Z][\w+.-]*://[^\s?#]+)[?#][^\s]*")
# 没有 scheme 的 user:pass@host（常见粘贴错误：DATABASE_URL 少了 postgresql://）
_BARE_USERINFO = re.compile(
    r"(?<![\w+.-])([A-Za-z0-9_.%+-]+):(?!//)([^\s?#]*@)"
)
# libpq 关键字形式（host=… password=…）与常见密钥字段
_KEY_VALUE = re.compile(
    r"(?i)((?:password|pwd|api[_-]?key|secret|token)\s*=\s*)[^\s;]+"
)
# 本机绝对路径与上游 URL：技能错误文本会经编排层渲染给用户（Web / CLI），
# 嵌入模型缓存目录、Milvus 库文件路径、下载地址都不应出现在回答里
_URL_BODY = re.compile(r"https?://[^\s'\"<>）)]+")
# 路径片段里允许空格：每段空格后的词必须由分隔符收尾（``Program Files\``、
# ``John Doe\``、``Files (x86)\``），否则匹配会横穿自然语言句子、把
# ``Connection refused`` 这类重试关键词一起吞掉
_WINDOWS_PATH = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)"
    r"[^\s'\"<>）)]*"
    r"(?: (?:"
    r"[^\s\\/'\"<>）)]+(?:"
    r"[\\/]"
    r"| \([^()\s'\"<>）]+\)[\\/]"
    r"| [^\s\\/'\"<>）)]+[\\/]"
    r")"
    r"[^\s'\"<>）)]*"
    r"))*"
)
# 引号内的路径整体遮蔽：引号给了明确边界，可以跨空格一直吃到收尾引号
# （``'C:\Users\John Doe\My File.txt'``）
_QUOTED_WINDOWS_PATH = re.compile(
    r"(['\"`])(?:[A-Za-z]:[\\/]|\\\\)[^'\"`\r\n]*?\1"
)
_UNIX_ROOTS = (
    r"home|Users|root|tmp|var|opt|srv|mnt|data|workspace"
    r"|proc|etc|app|code|build|usr|private"
)
_QUOTED_UNIX_PATH = re.compile(
    r"(['\"`])/(" + _UNIX_ROOTS + r")/[^'\"`\r\n]*?\1"
)
_UNIX_PATH = re.compile(
    r"(?<![\w:/])/(" + _UNIX_ROOTS + r")(?:/[^\s'\"<>）)]+)+"
)


def redact_local_paths(text: object) -> str:
    """只遮蔽本机绝对路径（Windows 盘符 / UNC / 常见 Unix 根），其余原样保留。

    诊断报告（/api/doctor）要保留协议与主机便于排查，不能像
    :func:`redact_paths` 那样把整条 URL 抹成 ``http(s)://***``，但又不能
    回显 ``C:\\venv\\...`` / ``/usr/lib/...`` 这类目录布局。
    """
    cleaned = _QUOTED_WINDOWS_PATH.sub(
        lambda match: f"{match.group(1)}<路径>{match.group(1)}", str(text)
    )
    cleaned = _QUOTED_UNIX_PATH.sub(
        lambda match: f"{match.group(1)}<路径>{match.group(1)}", cleaned
    )
    cleaned = _WINDOWS_PATH.sub("<路径>", cleaned)
    return _UNIX_PATH.sub("<路径>", cleaned)


def redact_paths(text: object) -> str:
    """遮蔽内部绝对路径、上游 URL 与凭据（错误信息可能回流给终端用户）。

    只替换路径 / 链接本身，保留异常的其余文字（如 Connection refused /
    timeout）：编排层按这些关键词判断瞬时错误并重试，不能一起抹掉。
    路径先于 URL 处理：``http(s)://***`` 占位符里的 ``s:/`` 会被正斜杠
    盘符规则误认成 ``C:/`` 形态；原样 URL 中 ``字母:/`` 前是另一字母，
    路径规则的前瞻守护不会误伤。
    """
    cleaned = redact_local_paths(str(text))
    cleaned = _URL_BODY.sub("http(s)://***", cleaned)
    return redact_secret(cleaned)


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
