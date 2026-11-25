"""会话标题派生（Web 列表 / CLI sessions / 会话导出共用）。

导入允许 50MB 级单条消息；对整条大消息做 ``" ".join(content.split())``
会生成数千万项中间列表（数百 MB~GB 内存、秒级 CPU）。标题只需要前 40 个
归一化字符，按 ``\\S+`` 惰性扫描到够长即停止，内存与耗时只和标题前缀
有关；输出与 ``" ".join(str(content).split())[:40]`` 完全一致。
"""
import re

_TITLE_MAX_CHARS = 40


def session_title_from_content(content: object) -> str:
    """折叠空白后取前 40 字符，只扫描到足够长度即停止。"""
    result = ""
    for match in re.finditer(r"\S+", str(content or "")):
        piece = (" " if result else "") + match.group(0)
        if len(result) + len(piece) >= _TITLE_MAX_CHARS:
            return (result + piece)[:_TITLE_MAX_CHARS]
        result += piece
    return result[:_TITLE_MAX_CHARS]


def normalized_preview_from_content(content: object, limit: int) -> str:
    """折叠空白后取前 ``limit`` 字符（超出时追加省略号），惰性前缀扫描。

    输出与 ``" ".join(str(content).split())[:limit]`` 加省略号完全一致，
    但只扫描到 limit+1 个字符即停止：50MB 级单条消息的整句 ``split()``
    会生成数千万项中间列表（数百 MB 内存），会话预览只需要前 80 字。
    """
    result = ""
    for match in re.finditer(r"\S+", str(content or "")):
        piece = (" " if result else "") + match.group(0)
        if len(result) + len(piece) > limit:
            return (result + piece)[:limit] + "…"
        result += piece
    return result[:limit]
