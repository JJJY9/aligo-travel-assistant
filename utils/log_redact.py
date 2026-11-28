"""日志与错误文本脱敏（DSN / URL 凭据不落日志）。

psycopg / redis-py 的连接异常经常把完整 URI 拼进异常文本（含密码、或未编码
密码片段）；这些文本会进入应用日志、甚至经 503 详情回流给客户端。存储层
与调用方在记录 / 抛出之前统一用 :func:`redact_secret` 处理。
"""
import re

# userinfo 允许出现 "/" ":" ";" "," 与多个 "@"（密码未编码 / 多 @）：
# 处理时吃到 "?" "#" 前的最后一个 "@"，避免只遮住第一段密码片段。
# scheme 限长 63（RFC 3986 事实上限）。尾部用无失败条件的 [^\s?#]* 再由
# 回调判断：带终止条件的贪心类（如 [^\s?#]*@）会在每个起点向后扫到空白
# 才失败，前缀密集的长文本（模型输出 / base64 错误体）呈 O(n²)。
_URL_USERINFO = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.-]{0,63}://)(?P<rest>[^\s?#]*)"
)
# 密码里的 # / ? 未编码时 URL 语法把它们当 fragment/query 分隔符：
# [^\s?#]*@ 失配、查询串规则又从 #/? 起截断，只剩 scheme://用户名:密码前缀。
# 只在「authority 内出现 ':' 且其后无 '/'、紧跟 #/? 再出现 '@'」的形态上
# 放宽（中段有界，避免重复前缀输入回退成 O(n²)），避免把正常查询串
# （?to=a@b.com）当凭据。
_URL_MALFORMED_USERINFO = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.-]{0,63}://)[^\s/?#:]{0,512}:"
    r"[^\s/?#]{0,512}[?#][^\s/]{0,2048}@"
)
_URL_QUERY = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.-]{0,63}://)(?P<rest>[^\s]*)"
)
# 没有 scheme 的 user:pass@host（常见粘贴错误：DATABASE_URL 少了 postgresql://）
_BARE_USERINFO = re.compile(
    r"(?<![\w+.-])(?P<name>[A-Za-z0-9_.%+-]+):(?!//)(?P<rest>[^\s?#]*)"
)
# libpq / psycopg 在 DSN 解析失败时把原始参数值整段回显在引号里：
# ``unexpected spaces found in "pa ss"``、``missing "=" after "alice:pw"``。
# 这些 token 无 scheme / @ / 键名 / 密钥前缀，上面的规则全不命中；只遮蔽
# 引号内 token，保留诊断措辞（便于确认是空格未编码还是缺等号）
_LIBPQ_QUOTED_TOKEN = re.compile(
    r'(?i)((?:unexpected spaces found in|missing "=" after) )"[^"\r\n]*"'
)
# libpq 关键字形式（host=… password=…）、冒号分隔的 YAML / 请求头形态与
# 常见密钥字段。引号允许出现在键名后 / 值前（JSON / dict repr）：
# ``{"api_key": "…"}``、``'api_key': '…'``、``api-key: …`` 都要遮蔽
_KEY_VALUE = re.compile(
    r"(?i)((?:password|pwd|api[_-]?key|secret|token)\s*['\"]?\s*[:=]\s*['\"]?)"
    r"[^\s;,'\"}]+"
)
# 引号形态值里的空格 / 逗号是值的一部分（``{'password': 'pa ss'}``、
# ``{"password": "pa,ss"}``）：引号已给出明确边界，整段遮蔽。先于
# _KEY_VALUE 应用，未加引号的形态仍走上一条的窄终止符。
_KEY_VALUE_QUOTED = re.compile(
    r"(?i)((?:password|pwd|api[_-]?key|secret|token)\s*['\"]?\s*[:=]\s*)"
    r"(['\"])(?:\\.|(?!\2)[^\\\r\n])*\2"
)
# 请求头形态：Authorization: Bearer/Basic/Token <凭据>（网关 4xx 常原样
# 回显请求头）。裸 Bearer 不遮蔽——误伤面太大
_AUTH_HEADER = re.compile(
    r"(?i)((?:proxy-)?authorization\s*['\"]?\s*[:=]\s*['\"]?"
    r"(?:bearer|basic|token)\s+)[^\s'\"},;]+"
)
# httpx / httpcore 发送前校验头值，失败时把整段头值 repr 进异常：
# ``Illegal header value b'Bearer sk-…\n…'``。折行密钥的 \n 是字面转义，
# 裸 Bearer 规则在空白处截断、sk- 规则在反斜杠处截断，都只遮一半；
# 引号内整段就是凭据，整体遮蔽
_ILLEGAL_HEADER_VALUE = re.compile(
    r"(?i)(Illegal header value b?['\"])([^'\"\r\n]*)(['\"])"
)
# 裸密钥：网关错误体常原样引用请求密钥（如「Incorrect API key provided:
# sk-…」）；健康检查 / doctor 把异常原文回显给调用方前必须遮蔽
_BARE_API_KEY = re.compile(r"(?i)\b(?:sk|pk|rk|ak)-[A-Za-z0-9_\-]{12,}")
# 本机绝对路径与上游 URL：技能错误文本会经编排层渲染给用户（Web / CLI），
# 嵌入模型缓存目录、Milvus 库文件路径、下载地址都不应出现在回答里
_URL_BODY = re.compile(r"https?://[^\s'\"<>）)]+")
# 路径片段里允许空格：每段空格后的词必须由分隔符收尾（``Program Files\``、
# ``John Doe\``、``Files (x86)\``），否则匹配会横穿自然语言句子、把
# ``Connection refused`` 这类重试关键词一起吞掉。片段字符排除冒号：
# 否则「路径 与 https://…」会把 URL 的 scheme 当成下一段路径一起吃掉
_WINDOWS_PATH = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)"
    r"[^\s'\"<>）)]*"
    r"(?: (?:"
    r"[^\s\\/:'\"<>）)]+(?:"
    r"[\\/]"
    r"| \([^()\s'\"<>）]+\)[\\/]"
    r"| [^\s\\/:'\"<>）)]+[\\/]"
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
    # macOS python.org 框架安装（/Library/Frameworks/...）、外置卷
    # （/Volumes/...）与 NixOS 的 /nix/store 同属常见部署目录
    r"|Library|Volumes|nix"
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


def _mask_url_userinfo(match: re.Match[str]) -> str:
    scheme = match.group("scheme")
    rest = match.group("rest")
    last_at = rest.rfind("@")
    if last_at < 0:
        return match.group(0)
    authority, sep, _tail = rest.partition("/")
    # authority 里没有 @ / 凭据冒号、且形如域名时，@ 属于路径而非 userinfo
    # （Google Maps / 邮箱路径链接）：保留主机，不伪造 ***@
    if sep and "@" not in authority and ":" not in authority and "." in authority:
        return match.group(0)
    # 贪婪吃到最后一个 @，避免只遮住第一段密码片段（未编码 / 多 @）
    return f"{scheme}***@{rest[last_at + 1:]}"


def _strip_url_query(match: re.Match[str]) -> str:
    scheme = match.group("scheme")
    rest = match.group("rest")
    cut = min((i for i in (rest.find("?"), rest.find("#")) if i >= 0), default=-1)
    if cut <= 0:
        return match.group(0)
    return scheme + rest[:cut]


def _mask_bare_userinfo(match: re.Match[str]) -> str:
    rest = match.group("rest")
    last_at = rest.rfind("@")
    if last_at < 0:
        return match.group(0)
    return f"{match.group('name')}:***@{rest[last_at + 1:]}"


def redact_secret(text: object) -> str:
    """去掉文本中 URL / DSN 的 userinfo 与查询串（保留协议与主机便于排查）。

    psycopg 的报错会把整条连接串拼进异常文本：URI 形态、缺 scheme 的
    ``user:pass@host`` 粘贴、``password=`` 关键字形态都要遮蔽。
    """
    cleaned = str(text)
    # 三条 scheme 规则的匹配体都必含 "://"：文本里完全没有该子串时（网关
    # 数百 KB~MB 级的错误体 / base64）跳过，省掉逐位置最多 63 字符的失配
    # 扫描（1MB 无 URL 文本 1.4s -> 0.13s），输出逐字节不变
    if "://" in cleaned:
        cleaned = _URL_USERINFO.sub(_mask_url_userinfo, cleaned)
        cleaned = _URL_MALFORMED_USERINFO.sub(r"\g<scheme>***@", cleaned)
        cleaned = _URL_QUERY.sub(_strip_url_query, cleaned)
    cleaned = _LIBPQ_QUOTED_TOKEN.sub(r'\1"***"', cleaned)
    # _BARE_USERINFO 的回调只在匹配片段含 "@" 时改动输出：整段文本没有
    # "@" 时全部匹配都是原样返回，直接跳过不改变结果
    if "@" in cleaned:
        cleaned = _BARE_USERINFO.sub(_mask_bare_userinfo, cleaned)
    cleaned = _AUTH_HEADER.sub(r"\1***", cleaned)
    cleaned = _ILLEGAL_HEADER_VALUE.sub(r"\1***\3", cleaned)
    cleaned = _KEY_VALUE_QUOTED.sub(r"\1\2***\2", cleaned)
    cleaned = _KEY_VALUE.sub(r"\1***", cleaned)
    cleaned = _BARE_API_KEY.sub("***", cleaned)
    return cleaned


def redacted_exc_text(exc: BaseException) -> str:
    """异常对象 → 可安全写入日志的单段 traceback 文本（含 __cause__ 链）。

    ``logger.…(..., exc_info=True)`` 会让日志 Formatter 从原始异常对象重新
    格式化 traceback，绕过 :func:`redact_secret`；需要保留 traceback 的诊断
    价值时，调用方改用本函数把脱敏后的文本作为普通参数记录。
    """
    import traceback

    return redact_secret(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    )
