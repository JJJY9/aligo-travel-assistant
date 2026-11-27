"""导出文件的原子写入（临时文件 + os.replace）。

导出命令支持 ``--force`` 覆盖旧备份；直接 ``Path.write_text`` 会先截断
目标再编码：磁盘满、进程中断或内容含无法编码的字符（孤立代理项）时，
旧备份被清成 0 字节并留下半截新文件。这里在目标目录写临时文件、fsync
后原子替换；失败时清理临时文件，原文件保持不变。
"""
import errno
import hashlib
import os
import tempfile
import time
from pathlib import Path

# Windows 上目标文件被其它进程短暂打开（杀软扫描 / 同步盘 / 读者句柄）时
# os.replace 会抛 PermissionError / WinError 5；重试预算与长期记忆 _save 对齐
_REPLACE_RETRY_WINDOW_SEC = 1.5
# 崩溃 / 断电可能在 mkstemp 与 replace 之间留下临时文件：只清理超过该
# 年龄的条目，避免误删并发写者的活跃临时文件
_STALE_TEMP_AGE_SEC = 600.0
# 只有「目标被短暂占用」类 errno 值得重试：EACCES / WinError 5（读者 /
# 杀软 / 同步盘，与只读目标同一形态无法再细分）、EBUSY / ENOTEMPTY。
# ENOENT（临时文件被并发清理清走）、ENOSPC / EDQUOT（磁盘满）、EISDIR、
# ENAMETOOLONG、EROFS 等重试不会自愈，继续轮询只会让每次失败多付 1.5 秒。
# errno 为 None 的裸 OSError 仍按可重试处理（与 utils/llm_resilience.py 同口径）。
_REPLACE_RETRY_ERRNOS = frozenset(
    {errno.EACCES, errno.EBUSY, getattr(errno, "ENOTEMPTY", errno.EACCES)}
)


def _temp_prefix(target: Path) -> str:
    """临时文件名前缀与目标名解耦：``.<目标名>.`` + 随机 + ``.tmp`` 比目标名
    长 14 字节，目标名接近 NAME_MAX（255）时 mkstemp 会 ENAMETOOLONG——
    原名本身合法、占名（claim_export_path）也已成功，写盘时才失败。"""
    digest = hashlib.sha1(os.fsencode(target.name)).hexdigest()[:16]
    return f".tmp-{digest}."


def fsync_parent_dir(path: str | os.PathLike[str]) -> None:
    """尽力让目标目录项（rename / 新建）落盘。

    ``fsync`` 临时文件只保证内容落盘，目录项（重命名）仍可能在掉电后
    丢失，重启回退到旧版本。Windows 不能以普通方式打开目录，静默跳过
    （NTFS 的重命名走日志，受影响较小）。网络盘 / FUSE 不支持目录
    fsync 时同样保持原有的 best-effort 语义，不能让保存变失败。
    """
    if os.name != "posix":
        return
    try:
        parent = os.path.dirname(os.path.abspath(os.fspath(path))) or "."
        fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def replace_with_retry(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    timeout: float = _REPLACE_RETRY_WINDOW_SEC,
) -> None:
    """``os.replace`` 有界重试：Windows 上目标被其它进程短暂打开时
    会报 PermissionError / WinError 5（与长期记忆 ``_save`` 同口径）。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if (
                exc.errno is not None and exc.errno not in _REPLACE_RETRY_ERRNOS
            ) or time.monotonic() >= deadline:
                raise
            time.sleep(0.03)


def cleanup_stale_temp_files(
    directory: str | os.PathLike[str],
    name_prefix: str | tuple[str, ...],
    older_than_sec: float = _STALE_TEMP_AGE_SEC,
) -> int:
    """清理目录下 ``name_prefix`` 开头且以 ``.tmp`` 结尾、超过年龄阈值的
    遗留临时文件。

    原子写崩溃残留的 ``.tmp-<哈希>.<随机>.tmp`` 可能含完整用户数据（记忆
    快照 / 导出内容），不能永久累积。只删超过阈值的条目：并发写者的
    临时文件刚创建，mtime 新鲜，不会被误删。必须带 ``.tmp`` 后缀守卫：
    前缀匹配还会命中用户自建的同名隐藏文件（如 ``.backup.json.keep``），
    那些不是本模块的临时文件。返回删除条数，失败静默。
    """
    prefixes = (name_prefix,) if isinstance(name_prefix, str) else name_prefix
    cutoff = time.time() - max(0.0, older_than_sec)
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.endswith(".tmp"):
            continue
        if not any(entry.name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            os.remove(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


def write_text_atomic(
    path: str | os.PathLike[str],
    text: str,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Path:
    """原子写入文本文件（``newline`` 语义同 ``open``）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = _temp_prefix(target)
    # 旧版本的临时前缀含完整目标名，升级后仍需清理其遗留
    cleanup_stale_temp_files(target.parent, (prefix, f".{target.name}."))
    fd, temp_path = tempfile.mkstemp(
        prefix=prefix, suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp_path, target)
        fsync_parent_dir(target)
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
    prefix = _temp_prefix(target)
    # 旧版本的临时前缀含完整目标名，升级后仍需清理其遗留
    cleanup_stale_temp_files(target.parent, (prefix, f".{target.name}."))
    fd, temp_path = tempfile.mkstemp(
        prefix=prefix, suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp_path, target)
        fsync_parent_dir(target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return target
