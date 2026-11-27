"""Web 静态外壳契约回归（无需 Node / 浏览器 / 网络）。

防止三类「测试全绿但浏览器坏掉」的漂移：

- app.js 通过 ``byId("...")`` 引用的 id 必须在 index.html 中存在：缺一个
  就会让 bindEvents 在首个 null 上抛错，其后的绑定全部中止；
- index.html 里静态资源的 ``?v=`` 必须等于 ``APP_VERSION``（发版漏改会
  让旧 CSS / app.js 继续被缓存）；
- theme.js 与 app.js 必须共用同一个主题存储键（改单侧会让主题选择失效，
  首屏闪烁复发），且启动脚本同步 color-scheme。

运行：python -m pytest tests/test_static_shell_contract.py -q
"""
import os
import re
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.version import APP_VERSION  # noqa: E402

STATIC_DIR = Path(project_root, "webapp", "static")


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _literal_ids(source: str) -> set[str]:
    return set(re.findall(r'byId\(\s*"([^"]+)"', source))


def test_app_js_by_id_targets_exist_in_index():
    wanted = _literal_ids(_read("app.js"))
    defined = set(re.findall(r'\bid="([^"]+)"', _read("index.html")))
    assert wanted, "未从 app.js 提取到任何 byId 字面量，契约测试失效"
    missing = sorted(wanted - defined)
    assert not missing, f"app.js 引用了 index.html 中不存在的 id：{missing}"


def test_static_asset_versions_match_app_version():
    html = _read("index.html")
    versions = set(
        re.findall(r'/static/[A-Za-z0-9_.-]+\.(?:js|css)\?v=([^"&\s]+)', html)
    )
    assert versions == {APP_VERSION}, (
        f"index.html 的 ?v= 为 {sorted(versions)}，APP_VERSION 为 {APP_VERSION}"
    )
    for name in ("theme.js", "style.css", "app.js"):
        assert f"/static/{name}?v={APP_VERSION}" in html, name


def test_theme_storage_key_shared_and_color_scheme_synced():
    theme_js = _read("theme.js")
    app_js = _read("app.js")
    assert '"aligo.theme"' in theme_js
    assert '"aligo.theme"' in app_js
    # 首帧 color-scheme 必须由 theme.js 写入，否则深色系统 + 浅色偏好闪帧
    assert "style.colorScheme" in theme_js
    assert "style.colorScheme" in app_js
