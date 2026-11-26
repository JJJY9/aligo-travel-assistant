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

def test_word_chars_match_web_engine():
    """与 Web 的 Rust 正则在 Unicode 新增区逐字符一致（双向闭合）。

    旧实现按 Python 自带 Unicode 表（3.12 = Unicode 15.0）拒绝「未分配」
    字符，但 pydantic-core 的 Rust 表更新（15.1 起新增字母）：Web 能创建
    的会话 ID 在备份恢复时被判非法、整会话消息被丢弃。这里以 Web 引擎
    为准逐字符对比，两个方向的缺口都不允许存在。
    """
    checked = 0
    accepted = 0
    for start, end in (
        (0x2EBF0, 0x2EE70),  # CJK 扩展 I（Unicode 15.1 新增）
        (0x323B0, 0x32400),  # 原未分配探测区
        (0x1C80, 0x1CC0),  # 西里尔扩展增补
        (0x0890, 0x08A0),  # 阿拉伯扩展增补
    ):
        for codepoint in range(start, end):
            char = chr(codepoint)
            expected = _web_accepts(char)
            assert is_valid_web_session_id(char) is expected, hex(codepoint)
            checked += 1
            accepted += int(expected)
    assert checked > 0
    print(
        f"✓ {checked} 个新增/边缘码位与 Web 引擎逐字符一致"
        f"（{accepted} 个接受）"
    )


def test_oversized_filename_ids_rejected():
    """按最长派生名（损坏隔离备份 = id + 41 字节）给 NAME_MAX=255 留余量。"""
    deseret = chr(0x10400)
    assert is_valid_web_id(deseret * 52) is True  # 208 字节
    assert is_valid_web_id(deseret * 53) is False  # 212 字节
    assert is_valid_web_id(deseret * 60) is False  # 240 字节（旧上限）
    assert is_valid_web_id(deseret * 64) is False
    # 会话 ID 不拼记忆库文件名，不受字节上限约束
    assert is_valid_web_session_id(deseret * 64) is True
    print("✓ 超长文件名的用户 ID 被拒绝（会话 ID 不受影响）")


def test_derived_filenames_fit_name_max():
    """最大合法 ID 的全部派生文件名（含崩溃/隔离/迁移/导入锁）≤ NAME_MAX。"""
    from utils.identifiers import _MAX_ID_UTF8_BYTES

    # 用真实上限构造最长的合法 ID（4 字节字符数向下取整）
    unit = "𝔸"  # U+1D538，4 字节
    count = _MAX_ID_UTF8_BYTES // len(unit.encode("utf-8"))
    user_id = unit * count
    assert is_valid_web_id(user_id) is True
    suffixes = (
        ".json",
        ".json.lock",
        f".{user_id}.{ 'x' * 8 }.tmp",  # _save 的 mkstemp 名
        ".json.quarantine",
        f".{user_id}.{ 'x' * 8 }.quarantine.tmp",
        ".json.corrupt-20261125191225123456-1234567",
        ".json.migrate-backup",
        ".json.import.lock",
        ".json.deleted",
    )
    for suffix in suffixes:
        name = f"{user_id}{suffix}" if not suffix.startswith(f".{user_id}") else suffix
        size = len(name.encode("utf-8"))
        assert size <= 255, f"派生文件名 {size} 字节超过 NAME_MAX：{name!r}"
    print("✓ 最大合法 ID 的全部派生文件名均在 NAME_MAX 内")


def test_word_char_cache_reuses_results():
    """逐字符判定按字符缓存：同批会话的重复字符不再逐次跑正则。"""
    from utils import identifiers

    identifiers.is_web_word_char.cache_clear()
    for _ in range(3):
        assert is_valid_web_id("abc-123") is True
    info = identifiers.is_web_word_char.cache_info()
    assert info.misses == len(set("abc123")), info
    assert info.hits > 0, info
    print("✓ ID 字符判定缓存命中")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_ids_match_web_rust_word_semantics()
    test_prefixed_and_suffixed_fragments()
    test_windows_reserved_names_rejected()
    test_session_ids_allow_windows_device_names()
    test_word_chars_match_web_engine()
    test_oversized_filename_ids_rejected()
    test_derived_filenames_fit_name_max()
    test_word_char_cache_reuses_results()
    print("\n全部通过")
