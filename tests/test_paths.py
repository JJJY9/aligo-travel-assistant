"""utils.paths 运行期数据目录测试（无需 API Key / 网络）。

运行：python tests/test_paths.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.paths import default_data_root, is_source_checkout, package_root


def test_env_override_wins(monkeypatch, tmp_path=None):
    import tempfile

    target = tmp_path or tempfile.mkdtemp(prefix="chailv_paths_")
    monkeypatch.setenv("ALIGO_DATA_DIR", str(target))
    from pathlib import Path as _Path

    assert default_data_root() == _Path(target)
    print("✓ ALIGO_DATA_DIR 覆盖数据目录")


def test_source_checkout_uses_repo_data(monkeypatch):
    monkeypatch.delenv("ALIGO_DATA_DIR", raising=False)
    assert is_source_checkout(), "当前仓库运行应识别为源码 checkout"
    assert default_data_root() == package_root() / "data"
    print("✓ 源码 checkout 使用 <仓库>/data")


def test_wheel_install_falls_back_to_user_dir(monkeypatch, tmp_path=None):
    import tempfile
    from pathlib import Path

    from utils import paths

    base = Path(tmp_path or tempfile.mkdtemp(prefix="chailv_paths_wheel_"))
    fake_site = base / "site-packages"
    (fake_site / "webapp").mkdir(parents=True)
    monkeypatch.setattr(paths, "package_root", lambda: fake_site)
    monkeypatch.delenv("ALIGO_DATA_DIR", raising=False)
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(base / "local"))
        expected = base / "local" / "aligo" / "data"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(base / "xdg"))
        expected = base / "xdg" / "aligo" / "data"
    assert paths.default_data_root() == expected
    print("✓ wheel 安装（无 pyproject）回退用户数据目录")


def test_env_override_strips_bom_and_quotes(monkeypatch, tmp_path=None):
    """ALIGO_DATA_DIR 带 BOM / 成对引号（文件注入形态）仍指向目标目录。"""
    import tempfile
    from pathlib import Path

    target = Path(tmp_path or tempfile.mkdtemp(prefix="chailv_paths_clean_"))
    monkeypatch.setenv("ALIGO_DATA_DIR", '"' + str(target) + '"')
    assert default_data_root() == target
    monkeypatch.setenv("ALIGO_DATA_DIR", "'" + str(target) + "'")
    assert default_data_root() == target
    monkeypatch.setenv("ALIGO_DATA_DIR", chr(0xFEFF) + str(target))
    assert default_data_root() == target
    print("✓ ALIGO_DATA_DIR 去 BOM / 成对引号")


def test_relative_override_resolved_against_cwd(monkeypatch, tmp_path=None):
    """相对 ALIGO_DATA_DIR 统一按进程 CWD 归一为绝对路径。"""
    import tempfile
    from pathlib import Path

    base = Path(tmp_path or tempfile.mkdtemp(prefix="chailv_paths_rel_"))
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(base)
    monkeypatch.setenv("ALIGO_DATA_DIR", "rel-data")
    root = default_data_root()
    assert root.is_absolute(), root
    assert root == (base / "rel-data").resolve(), root
    print("✓ 相对 ALIGO_DATA_DIR 按 CWD 归一为绝对路径")


def test_wheel_fallback_strips_quoted_base_dir(monkeypatch, tmp_path=None):
    """LOCALAPPDATA / XDG_DATA_HOME 带成对引号 / BOM 时仍回退到正确目录。"""
    import tempfile
    from pathlib import Path

    from utils import paths

    base = Path(tmp_path or tempfile.mkdtemp(prefix="chailv_paths_wheelq_"))
    fake_site = base / "site-packages"
    (fake_site / "webapp").mkdir(parents=True)
    monkeypatch.setattr(paths, "package_root", lambda: fake_site)
    monkeypatch.delenv("ALIGO_DATA_DIR", raising=False)
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", '"' + str(base / "local") + '"')
        expected = base / "local" / "aligo" / "data"
    else:
        monkeypatch.setenv(
            "XDG_DATA_HOME", chr(0xFEFF) + str(base / "xdg")
        )
        expected = base / "xdg" / "aligo" / "data"
    assert paths.default_data_root() == expected
    print("✓ wheel 回退基目录去成对引号 / BOM")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    import pytest

    for case in (
        test_env_override_wins,
        test_env_override_strips_bom_and_quotes,
        test_relative_override_resolved_against_cwd,
        test_wheel_fallback_strips_quoted_base_dir,
        test_source_checkout_uses_repo_data,
        test_wheel_install_falls_back_to_user_dir,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
