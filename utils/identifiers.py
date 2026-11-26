"""会话 / 用户 ID 的校验口径（与 Web 路径参数共用同一字符集）。

Web 的 ``_ID_PATTERN``（``^[\\w-]{1,64}$``）由 pydantic-core 用 Rust 正则
执行：Rust 的 ``\\w`` 是「Alphabetic + Mark + Nd + Pc + Join_Control」。
Python ``re`` 的 ``\\w`` 是 ``str.isalnum()``（含上标数字这类 No），两者在
组合符 / 零宽连接符与上标数字上相反——Web 可创建的会话（含零宽连接符）
会被备份恢复判为「会话 ID 非法」而丢消息，导入可接受的会话（含上标数字）
在 Web 全部端点 422、无法打开也无法删除。这里用 unicodedata 复刻 Rust
语义，供 CLI 导入 / 脚本复用。
"""
import functools
import unicodedata

try:  # regex 是 transformers 的传递依赖；缺失时退回 unicodedata 近似
    import regex as _regex
except ImportError:  # pragma: no cover - 轻量环境
    _regex = None

# 预编译单字符匹配：is_web_word_char 对每个字符各调一次 fullmatch，导入
# 数万消息的会话 ID 校验曾把一半以上解析时间花在逐字符重新编译正则上
_WORD_RE = _regex.compile(r"\w") if _regex is not None else None

# 零宽连接符（ZWNJ / ZWJ）：Rust \w 把 Join_Control 计为单词字符
_JOIN_CONTROLS = (chr(0x200C), chr(0x200D))


@functools.lru_cache(maxsize=4096)
def is_web_word_char(char: str) -> bool:
    """Rust 正则 ``\\w`` 的字符判定（pydantic-core 校验 ID 的同一集合）。

    Rust（regex crate）的 ``\\w`` 是 Unicode 「Alphabetic + Mark + Nd + Pc +
    Join_Control」复合集合：``str.isalpha()`` 只覆盖类别 L*，会漏掉
    Other_Alphabetic（如带圈字母 Ⓐ）。有 ``regex`` 模块时直接用它的
    Unicode ``\\w``（与 Rust 实测逐字符一致）；没有时退回 unicodedata 近似
    （覆盖 L/M/Nd/Nl/全部 Pc/Join_Control）。
    """
    if not char:
        return False
    if _regex is not None:
        # regex 模块打包的 Unicode 表可能新于 pydantic-core 的 Rust 表
        # （实测 regex Unicode 18 与 Rust ≈15.1 有上万字符判定相反，全部是
        # 「regex 接受而 Web 拒绝」）：未在 Python 自带表（随解释器发布，
        # 通常不新于 Rust 表）中的字符一律拒绝——CLI / 导入绝不能产出
        # Web 无法寻址（所有端点 422）的 ID。反向缺口（Rust 新增字符）只会
        # 让 Web 更宽，不会造成不可打开的账号 / 会话
        if unicodedata.category(char) == "Cn":
            return False
        assert _WORD_RE is not None
        return _WORD_RE.fullmatch(char) is not None
    if char in _JOIN_CONTROLS:
        return True
    if char.isalpha():
        return True
    category = unicodedata.category(char)
    return category in ("Mn", "Mc", "Me", "Nd", "Nl", "Pc")


# Windows 设备保留名：``<保留名>`` / ``<保留名>.<任意扩展名>`` 指向设备而不是
# 普通文件。用户 / 会话 ID 会直接成为记忆库文件名（``NUL.json``），写入会
# 静默丢弃、读取恒为空；COM1 等串口设备还可能阻塞。大小写不敏感。
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


# ID 会直接拼成 <user_id>.json 以及 .lock / 隔离备份 / 迁移备份 / 导入锁等
# 派生文件名：POSIX 的单文件名上限是 NAME_MAX=255 字节（ext4 / XFS / APFS）。
# 不能只按主文件预算——最长的派生名是损坏隔离备份
# `«id».json.corrupt-«20 位时间戳»-«pid»`（id + 42 字节，pid 按 7 位计），
# 240 字节的合法 ID 在 POSIX 上会让导入锁 / 隔离 / 迁移全部 ENAMETOOLONG。
# 取 210 = 255 - 42 - 3 字节余量，覆盖全族派生名。
_MAX_ID_UTF8_BYTES = 210


def is_valid_web_id(value: object, max_length: int = 64) -> bool:
    """是否为 Web 端点可寻址的 ID（``^[\\w-]{1,64}$``，Rust 语义）。

    Windows 设备保留名一律拒绝：ID 会被拼成记忆库文件名，``NUL.json``
    在 Windows 上打开的是空设备，数据静默丢失。
    """
    text = str(value or "")
    if not text or len(text) > max_length:
        return False
    if len(text.encode("utf-8")) > _MAX_ID_UTF8_BYTES:
        # 字符数达标但字节数超限：会成为超过 NAME_MAX 的文件名（见常量注释）
        return False
    if text.lower() in _WINDOWS_RESERVED_NAMES:
        return False
    return all(char == "-" or is_web_word_char(char) for char in text)


def is_valid_web_session_id(value: object, max_length: int = 64) -> bool:
    """会话 ID 的校验口径（字符集与 Web 相同，但不套用设备保留名）。

    会话 ID 只作为消息元数据与 Redis 会话名的组成部分，不会变成记忆库
    文件名；Web 的 ``ChatRequest.session_id`` 允许 ``nul`` / ``con`` 等
    名字。CLI 若按用户 ID 的口径拒绝，Web 能创建的会话在 CLI 无法查看 /
    删除，备份恢复时其消息还会被当作非法 ID 丢弃。
    """
    text = str(value or "")
    if not text or len(text) > max_length:
        return False
    return all(char == "-" or is_web_word_char(char) for char in text)
