"""utils.identifiers 与 Web（pydantic-core Rust \\w）口径一致性测试。

无需 API Key、不访问网络。运行：python tests/test_identifiers.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from typing import Annotated

from pydantic import Field, TypeAdapter

from utils.identifiers import is_valid_web_id

_WEB_ID = TypeAdapter(Annotated[str, Field(pattern=r"^[\w-]{1,64}$")])


def _web_accepts(value: str) -> bool:
    try:
        _WEB_ID.validate_python(value)
    except Exception:  # noqa: BLE001 - 只关心是否通过
        return False
    return True


def test_ids_match_web_rust_word_semantics():
    """CLI / 导入侧的 ID 判定必须与 Web 端点（Rust \\w）完全一致。"""
    connectors = [
        chr(0x203F),
        chr(0x2040),
        chr(0x2054),
        chr(0xFE33),
        chr(0xFE34),
        chr(0xFE4D),
        chr(0xFE4E),
        chr(0xFE4F),
        chr(0xFF3F),
    ]
    samples = [
        "abc",
        "张三",
        "a_b-1",
        "a" + chr(0x24B6) + "b",  # Other_Alphabetic（带圈 A）
        "a" + chr(0x0301) + "b",  # 组合符 Mn
        "a" + chr(0x200C) + "b",  # ZWNJ（Join_Control）
        "a" + chr(0x200D) + "b",  # ZWJ
        "²abc",  # No：Web 拒绝、Python re \w 接受
        "a b",
        "a/b",
        "-lead",
        "",
        "x" * 64,
        "x" * 65,
    ]
    samples += ["a" + connector + "b" for connector in connectors]
    for value in samples:
        expected = _web_accepts(value)
        assert is_valid_web_id(value) is expected, (value, expected)
    print("✓ ID 字符集与 Web（Rust \\w）一致")


def test_prefixed_and_suffixed_fragments():
    """前后缀片段不影响判定（组合字符不能拆开计数）。"""
    assert is_valid_web_id("trip_1") is True
    assert is_valid_web_id("session-1") is True
    assert is_valid_web_id("has space") is False
    assert is_valid_web_id(None) is False
    print("✓ ID 前后缀与非法输入判定")


def test_windows_reserved_names_rejected():
    """Windows 设备保留名不能作 ID：会拼成记忆库文件名并指向设备。

    这是相对 Web 模式的**有意收窄**：Web 的 pydantic 正则（Rust regex）
    不支持 lookaround，无法表达保留名否定；CLI / 导入 / Web 打开存储的
    路径统一在 :func:`is_valid_web_id` 拒绝。
    """
    for value in (
        "CON",
        "con",
        "Prn",
        "aux",
        "NUL",
        "nul",
        "COM1",
        "com9",
        "LPT1",
        "LPT9",
    ):
        assert is_valid_web_id(value) is False, value
    # 仅精确命中保留名才拒绝：前缀 / 后缀 / 扩展形态都是普通文件名
    for value in ("console", "con-1", "con_1", "xcon", "com10", "lpt0", "nul_2"):
        assert is_valid_web_id(value) is True, value
    print("✓ Windows 设备保留名被拒绝")


if __name__ == "__main__":
    test_ids_match_web_rust_word_semantics()
    test_prefixed_and_suffixed_fragments()
    test_windows_reserved_names_rejected()
    print("\n全部通过")
