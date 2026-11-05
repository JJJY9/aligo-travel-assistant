"""
文本切分（RAG Chunking）

针对知识库文档的结构化切分：按「一、二、三」等章节标题与 FAQ 的 Q 标题作为
语义边界，避免把多个主题混进同一个向量（会稀释嵌入、导致检索排不上）。

旧实现按 600 字合并段落且不看章节边界，实测会把「交通标准 + 住宿标准」揉成
一块，查询「住宿标准」时目标片段排不进 Top-5。
"""
import re

# 语义边界：一级章节标题（一、二、三…）、FAQ 题号（Q1:）、Markdown 标题
_SECTION_RE = re.compile(r"^\s*(?:[一二三四五六七八九十]+、|Q\d+\s*[:：]|#{1,3}\s)")


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """超长段落按句子/换行切分，保证不丢内容。"""
    pieces: list[str] = []
    buffer = ""
    units = re.split(r"(?<=[。；！？\n])", paragraph)
    for unit in units:
        if not unit:
            continue
        if len(unit) > max_chars:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            for i in range(0, len(unit), max_chars):
                pieces.append(unit[i:i + max_chars])
            continue
        if buffer and len(buffer) + len(unit) > max_chars:
            pieces.append(buffer)
            buffer = ""
        buffer += unit
    if buffer:
        pieces.append(buffer)
    return pieces


def _is_title_like(chunk: str, min_chars: int = 30) -> bool:
    """判断块是否像「孤立标题」：单行、较短、且没有句读符号。"""
    stripped = chunk.strip()
    if "\n" in stripped or len(stripped) >= min_chars:
        return False
    return not re.search(r"[。；！？!?.;:：]", stripped)


def _is_title_block(chunk: str, min_chars: int = 30) -> bool:
    """判断块是否只由标题行组成（多行连续标题也算）。"""
    lines = [line for line in chunk.splitlines() if line.strip()]
    return bool(lines) and all(_is_title_like(line, min_chars) for line in lines)


def _merge_short_chunks(
    chunks: list[str], min_chars: int = 30, max_chars: int | None = None
) -> list[str]:
    """把孤立的标题块并入相邻块（``max_chars`` 给定时保证合并后不超长）。

    纯标题块本身没有知识内容，却会因为与查询字面高度相似而挤占检索名额。
    注意只合并「标题形态」的块（单行、无句读），避免误合并短问答段落；
    连续多行标题（如总标题 + 节标题）要一直挂起到遇到正文为止。
    超长上限必须兜住：连续几十行目录式标题累计起来会超过嵌入窗口，
    尾部内容会再次变得不可检索。
    """
    merged: list[str] = []
    pending = ""
    for chunk in chunks:
        if _is_title_block(chunk, min_chars):
            if max_chars is not None and len(chunk) > max_chars:
                # 单个标题块本身超长（如几十行目录）：按行切开，不能整块挂起
                if pending:
                    merged.append(pending)
                    pending = ""
                merged.extend(_split_long_paragraph(chunk, max_chars))
                continue
            candidate = f"{pending}\n{chunk}" if pending else chunk
            if max_chars is not None and len(candidate) > max_chars:
                merged.append(pending)
                pending = chunk
            else:
                pending = candidate
            continue
        if pending:
            candidate = f"{pending}\n{chunk}"
            if max_chars is not None and len(candidate) > max_chars:
                # 标题 + 正文超过上限：把标题接到正文第一段开头，按剩余空间
                # 切分正文（无句读的长段落 hard-split 后第一段也可能超长，
                # 直接整体切分会让标题又单独成块）
                body_pieces = _split_long_paragraph(chunk, max_chars)
                room = max_chars - len(pending) - 1
                if room <= 0:
                    merged.append(pending)
                    merged.extend(body_pieces)
                else:
                    first = body_pieces[0]
                    head, tail = first[:room], first[room:]
                    body_pieces[0] = f"{pending}\n{head}"
                    if tail:
                        body_pieces.insert(1, tail)
                    merged.extend(body_pieces)
            else:
                merged.append(candidate)
            pending = ""
        else:
            merged.append(chunk)
    if pending:
        if merged and (
            max_chars is None or len(merged[-1]) + 1 + len(pending) <= max_chars
        ):
            merged[-1] = f"{merged[-1]}\n{pending}"
        else:
            merged.append(pending)
    return merged


def split_text(text: str, max_chars: int = 400) -> list[str]:
    """按段落与章节边界切分文本。

    规则：
    - 空行分隔段落；
    - 以「一、」「Q1:」「# 标题」开头的段落视为新章节起点，强制开启新块；
    - 其余段落合并到不超过 max_chars；超长段落按句子切分；
    - 返回的块按原文顺序排列，不丢内容。
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in paragraphs:
        first_line = paragraph.split("\n", 1)[0]
        if _SECTION_RE.match(first_line) and current:
            flush()

        if len(paragraph) > max_chars:
            flush()
            chunks.extend(_split_long_paragraph(paragraph, max_chars))
            continue

        if current and len(current) + len(paragraph) + 1 > max_chars:
            flush()
        current = f"{current}\n{paragraph}" if current else paragraph

    flush()
    return _merge_short_chunks(chunks, max_chars=max_chars)
