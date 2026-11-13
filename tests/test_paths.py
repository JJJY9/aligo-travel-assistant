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


if __name__ == "__main__":
    import pytest

    for case in (
        test_env_override_wins,
        test_source_checkout_uses_repo_data,
        test_wheel_install_falls_back_to_user_dir,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
