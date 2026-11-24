"""utils.identifiers 与 Web（pydantic-core Rust \\w）口径一致性测试。

无需 API Key、不访问网络。运行：python tests/test_identifiers.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from typing import Annotated

from pydantic import Field, TypeAdapter

from utils.identifiers import is_valid_web_id, is_valid_web_session_id

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


def test_session_ids_allow_windows_device_names():
    """会话 ID 不拼记忆库文件名：Web 允许的 nul / con 在会话维度合法。"""
    for value in ("nul", "CON", "com1", "lpt9"):
        assert is_valid_web_session_id(value) is True, value
        assert is_valid_web_id(value) is False, value
    # 保留字（export）由调用方按 RESERVED_SESSION_IDS 另行拒绝
    assert is_valid_web_session_id("export") is True
    assert is_valid_web_session_id("has space") is False
    assert is_valid_web_session_id(None) is False
    print("✓ 会话 ID 允许设备保留名（用户 ID 仍拒绝）")

def test_unassigned_codepoints_never_accepted():
    """regex 模块的 Unicode 表可能新于 pydantic-core 的 Rust 表。

    未在 Python 自带表（随解释器发布，通常不新于 Rust 表）中的字符一律
    拒绝：CLI / 导入绝不能产出 Web 全部端点 422 的 ID（实测 Unicode 18
    与 Rust ≈15.1 有上万字符判定相反，全部是「regex 接受而 Web 拒绝」）。
    """
    import unicodedata

    probe = None
    for codepoint in range(0x323B0, 0x32400):
        char = chr(codepoint)
        if unicodedata.category(char) == "Cn":
            probe = char
            break
    if probe is None:
        # 解释器的 Unicode 表已覆盖探测区间（未来 Python）：本用例无字符可测
        print("— 解释器 Unicode 表已覆盖探测区间，跳过未分配码位用例")
        return
    assert is_valid_web_session_id("s" + probe) is False
    assert is_valid_web_id("u" + probe) is False
    print("✓ 未分配码位（regex 新表 / Rust 旧表分歧区）一律拒绝")


def test_oversized_filename_ids_rejected():
    """64 个 4 字节字符会拼出 261 字节文件名（POSIX NAME_MAX=255）。"""
    deseret = chr(0x10400)
    assert is_valid_web_id(deseret * 60) is True  # 240 字节
    assert is_valid_web_id(deseret * 61) is False  # 244 字节
    assert is_valid_web_id(deseret * 64) is False
    # 会话 ID 不拼记忆库文件名，不受字节上限约束
    assert is_valid_web_session_id(deseret * 64) is True
    print("✓ 超长文件名的用户 ID 被拒绝（会话 ID 不受影响）")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_ids_match_web_rust_word_semantics()
    test_prefixed_and_suffixed_fragments()
    test_windows_reserved_names_rejected()
    test_session_ids_allow_windows_device_names()
    test_unassigned_codepoints_never_accepted()
    test_oversized_filename_ids_rejected()
    print("\n全部通过")
