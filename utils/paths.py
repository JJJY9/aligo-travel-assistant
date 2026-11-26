"""运行期数据目录（源码 checkout 与 wheel 安装的行为差异）。

源码运行时 ``data/`` 就在仓库根；wheel 安装后包目录是 site-packages，
把用户记忆 / 知识库写进安装前缀会在重建 venv / pipx reinstall 时整体
丢失，root 属主安装还会让普通用户拿到只读目录（长期记忆 503）。这里
按运行形态选择可写的用户数据目录，并允许 ``ALIGO_DATA_DIR`` 覆盖。
"""
import os
from pathlib import Path


def clean_env_path(name: str) -> str:
    """读取路径类环境变量并清洗文件注入残留（BOM / 成对引号 / 空白）。

    K8s ConfigMap ``--from-file`` 会带 BOM 与尾随换行，docker ``--env-file``
    / Windows ``set VAR="D:\\data"`` 会把引号连值一起注入：Windows 路径
    含引号 / BOM 时 ``Path`` 操作直接 WinError 123，且没有任何提示。
    与 config.py 的字符串型环境变量同一清洗口径。
    """
    value = os.environ.get(name, "").replace("\ufeff", "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def package_root() -> Path:
    """仓库根（源码运行）或 site-packages（wheel 安装）。"""
    return Path(__file__).resolve().parent.parent


def is_source_checkout(root: Path | None = None) -> bool:
    """包目录是否位于源码仓库（pyproject.toml 不会随 wheel 安装）。"""
    base = root if root is not None else package_root()
    return (base / "pyproject.toml").is_file() and (base / "webapp").is_dir()


def default_data_root() -> Path:
    """长期数据根目录。

    - ``ALIGO_DATA_DIR`` 优先（任意部署形态都可用）；
    - 源码 checkout：``<仓库根>/data``（与既有行为一致）；
    - wheel 安装：site-packages 不可作为数据目录，改用用户数据目录
      （Windows: ``%LOCALAPPDATA%\\aligo\\data``；其它:
      ``$XDG_DATA_HOME/aligo/data`` 或 ``~/.local/share/aligo/data``）。
    """
    override = clean_env_path("ALIGO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    root = package_root()
    if is_source_checkout(root):
        return root / "data"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        base_path = Path(base) if base else Path.home() / "AppData" / "Local"
        return base_path / "aligo" / "data"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base_path = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base_path / "aligo" / "data"
