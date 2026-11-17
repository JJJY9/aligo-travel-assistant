"""直跑清单的临时目录回收（pytest 侧由 tests/conftest.py 的 autouse 夹具处理）。

许多用例直接用 ``tempfile.mkdtemp()`` 自建记忆目录（里面是含用户数据
样本的 JSON），pytest 只回收 ``tmp_path`` 夹具目录，直跑清单也只清自己
注入的夹具目录——这些目录因此长期堆在系统临时目录。``install()`` 把
``tempfile.mkdtemp`` 包一层、登记所有自建目录，进程退出时统一删除。
"""
import atexit
import shutil
import tempfile

_created: list[str] = []


def install() -> None:
    """幂等安装跟踪器：登记本进程内 ``mkdtemp`` 创建的目录并在退出时清理。"""
    real = tempfile.mkdtemp
    if getattr(real, "_aligo_tracked", False):
        return

    def tracked_mkdtemp(*args, **kwargs):
        path = real(*args, **kwargs)
        _created.append(path)
        return path

    tracked_mkdtemp._aligo_tracked = True  # type: ignore[attr-defined]
    tempfile.mkdtemp = tracked_mkdtemp
    atexit.register(_cleanup)


def _cleanup() -> None:
    while _created:
        shutil.rmtree(_created.pop(), ignore_errors=True)
