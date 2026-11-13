"""导出文件的原子写入（临时文件 + os.replace）。

导出命令支持 ``--force`` 覆盖旧备份；直接 ``Path.write_text`` 会先截断
目标再编码：磁盘满、进程中断或内容含无法编码的字符（孤立代理项）时，
旧备份被清成 0 字节并留下半截新文件。这里在目标目录写临时文件、fsync
后原子替换；失败时清理临时文件，原文件保持不变。
"""
import os
import tempfile
from pathlib import Path


def write_text_atomic(
    path: str | os.PathLike[str],
    text: str,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Path:
    """原子写入文本文件（``newline`` 语义同 ``open``）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return target


def write_bytes_atomic(
    path: str | os.PathLike[str], data: bytes
) -> Path:
    """原子写入二进制文件（iCalendar 等固定行尾的输出）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return target
