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


_TITLE_PREFIX = re.compile(
    r"^(?:#+\s*|\d+[.、)）]\s*|[（(]\d+[)）]\s*|[一二三四五六七八九十]+[、.]\s*)"
)


def _is_title_like(chunk: str, min_chars: int = 30) -> bool:
    """判断块是否像「孤立标题」：单行、较短、且没有句读符号。

    允许「1. 住宿标准：」这类带编号 / 结尾冒号的标题：去掉编号前缀与
    结尾冒号后再看句读，否则它们会被判成正文、作为纯标题块留在索引里。
    """
    stripped = chunk.strip()
    if "\n" in stripped or len(stripped) >= min_chars:
        return False
    body = _TITLE_PREFIX.sub("", stripped).rstrip("：: ")
    if "：" in body or ":" in body:
        # 内部冒号说明是「标签：内容」的正文行，不是纯标题
        return False
    return not re.search(r"[。；！？!?.;]", body)


# 多行标题块里单行的长度上限：目录式标题很短；章节正文里的短清单行
# （同样没有句读）通常更长或以项目符号开头
_TITLE_LINE_MAX = 16


def _is_title_block(chunk: str, min_chars: int = 30) -> bool:
    """判断块是否只由标题行组成（多行连续标题也算）。

    多行块对「标题行」更严格：每条都很短且不以项目符号开头。否则章节
    正文里的短清单行（如「- 本标准自发布之日起施行」没有句读）会整节
    被当成标题块，_merge_short_chunks 随后把相邻两个章节拼进同一块，
    产出混主题向量。
    """
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) == 1:
        return _is_title_like(lines[0], min_chars)
    return all(
        len(line) <= _TITLE_LINE_MAX
        and not line.startswith(("-", "*", "•", "·"))
        and _is_title_like(line, min_chars)
        for line in lines
    )


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
                short_heading = "\n" not in pending and len(pending) <= min_chars
                if short_heading:
                    # 单行短标题（≤30 字）并入正文第一段，最多超出标题长度；
                    # 保留正文的句子边界，也不会产生无内容的标题块
                    body_pieces = _split_long_paragraph(chunk, max_chars)
                    body_pieces[0] = f"{pending}\n{body_pieces[0]}"
                    merged.extend(body_pieces)
                else:
                    # 多行目录式标题：按上限切分，不能整体并入正文
                    merged.extend(_split_long_paragraph(candidate, max_chars))
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
    merged = _merge_short_chunks(chunks, max_chars=max_chars)
    # 定长硬切可能产生纯空白分片（整块空格或单独的换行）：没有内容就不该
    # 进向量库——空白片段同样会编码成向量并占用检索名额；与
    # knowledge_admin._chunk_documents 的过滤口径保持一致
    return [chunk for chunk in merged if chunk.strip()]
