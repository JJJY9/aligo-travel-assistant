"""会话 / 用户 ID 的校验口径（与 Web 路径参数共用同一字符集）。

Web 的 ``_ID_PATTERN``（``^[\\w-]{1,64}$``）由 pydantic-core 用 Rust 正则
执行：Rust 的 ``\\w`` 是「Alphabetic + Mark + Nd + Pc + Join_Control」。
Python ``re`` 的 ``\\w`` 是 ``str.isalnum()``（含上标数字这类 No），两者在
组合符 / 零宽连接符与上标数字上相反——Web 可创建的会话（含零宽连接符）
会被备份恢复判为「会话 ID 非法」而丢消息，导入可接受的会话（含上标数字）
在 Web 全部端点 422、无法打开也无法删除。这里用 unicodedata 复刻 Rust
语义，供 CLI 导入 / 脚本复用。
"""
import unicodedata

try:  # regex 是 transformers 的传递依赖；缺失时退回 unicodedata 近似
    import regex as _regex
except ImportError:  # pragma: no cover - 轻量环境
    _regex = None

# 零宽连接符（ZWNJ / ZWJ）：Rust \w 把 Join_Control 计为单词字符
_JOIN_CONTROLS = (chr(0x200C), chr(0x200D))


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
        return _regex.fullmatch(r"\w", char) is not None
    if char in _JOIN_CONTROLS:
        return True
    if char.isalpha():
        return True
    category = unicodedata.category(char)
    return category in ("Mn", "Mc", "Me", "Nd", "Nl", "Pc")


def is_valid_web_id(value: object, max_length: int = 64) -> bool:
    """是否为 Web 端点可寻址的 ID（``^[\\w-]{1,64}$``，Rust 语义）。"""
    text = str(value or "")
    if not text or len(text) > max_length:
        return False
    return all(char == "-" or is_web_word_char(char) for char in text)
