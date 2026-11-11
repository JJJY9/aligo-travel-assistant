"""
JSON 解析辅助工具
提供更鲁棒的 JSON 解析功能，处理常见的格式问题
"""
import json
import logging
import re

from utils.access_log import MAX_LOG_TEXT, _safe_log_text

logger = logging.getLogger(__name__)


def reject_json_constant(value: str):
    """拒绝 NaN / Infinity（Python 默认允许，会让持久化的 JSON 无法再被标准解析）。"""
    raise ValueError(f"非法 JSON 数值: {value}")


def _strip_trailing_commas(text: str) -> str:
    """移除对象 / 数组的尾部逗号，只处理字符串之外的逗号。

    逐字符扫描并跟踪字符串 / 转义状态：正则 ``,(\\s*[}\\]])`` 会把字符串值
    里的 ",}"/",]"（例如引用 JSON 片段的行程序内容）一起删掉，静默篡改
    解析结果；字符串内的字节原样保留。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                # 丢弃尾逗号；其后的空白由后续循环原样输出
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _top_level_object_starts(text: str):
    """产出文本中处于「顶层」的 ``{`` 位置（不在任何未闭合的 {}/[] 内）。

    追踪括号深度与双引号字符串（含转义）：嵌套在外层畸形对象 / 数组里的
    子对象不再是候选——``extract_first_json_object`` 此前会把它当作完整
    结果返回（尾逗号 / 截断的外层对象 → 只拿到其中一个 preferences 片段）。
    说明文字里的花括号通常成对出现，深度回到 0 后继续产出后面的候选。
    """
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                yield index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
        elif char == "[":
            depth += 1
        elif char == "]":
            if depth > 0:
                depth -= 1


def extract_first_json_object(text: str) -> dict:
    """返回文本中第一个完整解析出的**顶层** JSON 对象，忽略说明文字。

    ``find('{')`` / ``rfind('}')`` 会把结尾说明里的花括号（例如「如 {日期}
    未提供可默认明天」）一起截进 JSON，导致合法输出解析失败。这里对顶层
    每个 ``{`` 尝试 ``raw_decode``，只接受完整解析出的 dict：

    - 嵌套在外层畸形对象 / 数组里的子对象不算（否则尾逗号 / 截断的外层
      对象会返回其中一个片段，调用方误当完整结果）；
    - NaN / Infinity（``reject_json_constant`` 抛 ValueError）只跳过当前
      候选，不能中止整个扫描——后面可能还有合法的结果对象。
    """
    if not text:
        raise ValueError("Empty text provided")
    decoder = json.JSONDecoder(parse_constant=reject_json_constant)
    for index in _top_level_object_starts(text):
        try:
            value, _ = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("No JSON object found in response")


def iter_json_objects(text: str):
    """按出现顺序产出文本中所有能完整解析的 JSON 对象。

    说明文字或推理过程中可能先出现一个合法的小对象（如单个
    ``key_entities`` 片段），只取第一个会丢弃后面真正完整的结构化结果；
    调用方需要自行挑选（例如最后一个带 ``agent_schedule`` 的对象）。
    """
    if not text:
        return
    decoder = json.JSONDecoder(parse_constant=reject_json_constant)
    index = 0
    while True:
        index = text.find("{", index)
        if index < 0:
            return
        try:
            value, end = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            index += 1
            continue
        if isinstance(value, dict):
            yield value
        index = max(end, index + 1)


def _escape_control_chars_in_strings(fragment: str) -> str:
    """把字符串值内部的裸控制字符转义成合法的 JSON 转义文本。

    模型输出常在 summary / 正文里直接换行（未写成 ``\\n``）：严格解析失败，
    但对象本身完整。与 ``robust_json_parse`` 的控制字符修复同一口径，作用
    域限定在字符串内部（结构中的空白原样保留）。
    """
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}
    out: list[str] = []
    in_string = False
    escaped = False
    for char in fragment:
        if in_string:
            if escaped:
                out.append(char)
                escaped = False
                continue
            if char == "\\":
                out.append(char)
                escaped = True
                continue
            if char == '"':
                out.append(char)
                in_string = False
                continue
            code = ord(char)
            if code < 0x20 or 0x7F <= code <= 0x9F:
                out.append(escapes.get(char) or f"\\u{code:04x}")
                continue
            out.append(char)
            continue
        if char == '"':
            in_string = True
        out.append(char)
    return "".join(out)


def _repair_single_quotes(fragment: str) -> str:
    """复用 ``robust_json_parse`` 的单引号修复（键名 / 字符串值）。"""
    fixed = re.sub(r"'([^']*)'(\s*:\s*)", r'"\1"\2', fragment)
    return re.sub(r":\s*'([^']*)'", r': "\1"', fixed)


def _repair_object_fragment(fragment: str) -> "dict | None":
    """按机械小瑕疵修复候选对象：尾逗号 / 字符串内裸控制字符 / 单引号。"""
    decoder = json.JSONDecoder(parse_constant=reject_json_constant)
    no_commas = _strip_trailing_commas(fragment)
    escaped = _escape_control_chars_in_strings(fragment)
    combined = _escape_control_chars_in_strings(no_commas)
    candidates = [
        no_commas,
        escaped,
        combined,
        _repair_single_quotes(combined),
    ]
    for candidate in candidates:
        try:
            value, end = decoder.raw_decode(candidate, 0)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict) and not candidate[end:].strip():
            return value
    return None


def _object_span_end(
    text: str, start: int, open_char: str, close_char: str
) -> "int | None":
    """从 ``start`` 起按字符串感知扫描，返回匹配结束符之后的位置。"""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


_RESULT_KEY_RE = re.compile(r'"\s*[^"\\\n]{1,80}"\s*:')
_RESULT_VALUE_RE = re.compile(
    r'"\s*:\s*(?:"|\d|\{|\[|true|false|null|-)'
)


def _looks_like_result_object(fragment: str) -> bool:
    """粗略判断无法解析的片段是否「像被破坏的结果对象」。

    仅用于「无法修复」的片段：含 NaN / Infinity（确定性非法）或至少一个
    JSON 形态的值（引号字符串 / 数字 / 容器 / 字面量）时，视为正式结果
    损坏或截断，拒绝让更早的示例对象顶替；说明文字里的 ``{"destination":
    城市名}`` 这类平衡片段不满足条件，仍按普通说明忽略。
    """
    window = fragment[:2000]
    if "NaN" in window or "Infinity" in window:
        return True
    if not _RESULT_KEY_RE.search(window):
        return False
    return bool(_RESULT_VALUE_RE.search(window))


def _iter_object_spans(text: str):
    """按位置产出 ``(start, value | None, end, looks_like)``。

    ``value is None`` 表示找到了一个无法解析的 ``{`` 片段：调用方据此区分
    「说明文字里的花括号」（looks_like=False）与「结果对象被截断 / 含无法
    修复的脏字符」（looks_like=True，不能让更早的示例对象顶替）。
    """
    index = 0
    decoder = json.JSONDecoder(parse_constant=reject_json_constant)
    while True:
        index = text.find("{", index)
        if index < 0:
            return
        end = _object_span_end(text, index, "{", "}")
        fragment = text[index:end] if end is not None else text[index:]
        value: "dict | None" = None
        if end is not None:
            try:
                parsed, _ = decoder.raw_decode(fragment, 0)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                value = parsed
            else:
                value = _repair_object_fragment(fragment)
        if value is not None:
            yield index, value, end, False
            index = end if end is not None else index + 1
            continue
        yield index, None, end, _looks_like_result_object(fragment)
        index = end if end is not None else len(text)


def iter_json_objects_with_repair(text: str):
    """按出现顺序产出 ``(对象, 结束位置)``（含小瑕疵修复后解析的对象）。"""
    for _start, value, end, _looks in _iter_object_spans(text):
        if value is not None and end is not None:
            yield value, end


def _iter_objects_with_repair(text: str):
    """兼容旧调用：产出 ``(value, end)``（修复见 :func:`_repair_object_fragment`）。"""
    yield from iter_json_objects_with_repair(text)


def has_unclosed_json_object(text: str) -> bool:
    """公共包装：文本中是否还有未闭合的 ``{``（字符串感知）。"""
    return _has_unclosed_object(text)
def json_array_start(text: str) -> "int | None":
    """文本中第一个「能整体解析为合法顶层数组」的起始下标；没有返回 None。

    说明文字里的 ``[注]`` / ``[1]`` 等无法整体解析为 JSON 的方括号会被跳过，
    调用方必须用本函数返回的下标定位载荷（用 ``text.find("[")`` 会指到前言
    里的方括号，让合法数组被误判解析失败）。
    """
    decoder = json.JSONDecoder()
    first_object = text.find("{")
    index = text.find("[")
    while index != -1:
        if first_object != -1 and first_object < index:
            return None
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("[", index + 1)
            continue
        if not isinstance(value, list):
            return None
        # 前言里可整体解析的小数组（引用标记 [1]、Markdown 待办「- [ ]」）
        # 后面若还跟着可解析的 JSON 对象（中间可能隔着说明文字），那是
        # 前言，不能当成顶层输出、把紧随其后的合法结果丢掉
        rest = text[end:]
        probe = rest.find("{")
        while probe != -1:
            try:
                nested, _ = decoder.raw_decode(rest, probe)
            except json.JSONDecodeError:
                probe = rest.find("{", probe + 1)
                continue
            if isinstance(nested, dict):
                return None
            probe = rest.find("{", probe + 1)
        return index
    return None


def starts_with_json_array(text: str) -> bool:
    """文本中第一个完整的 JSON 值是数组（而不是对象）；口径见 :func:`json_array_start`。"""
    return json_array_start(text) is not None


def first_json_array_with_keys(
    text: str, keys: "tuple[str, ...]"
) -> "list | None":
    """返回第一个「元素为含目标键的对象」的顶层数组（没有则 None）。

    用于裸数组输出前面带引用标记（``参考[1]``）或说明方括号的场景：
    :func:`starts_with_json_array` 会因前言小数组返回 False，对象扫描也
    找不到 ``preferences`` 键，合法载荷会被整段丢弃。
    """
    decoder = json.JSONDecoder()
    index = 0
    while True:
        index = text.find("[", index)
        if index < 0:
            return None
        end = _object_span_end(text, index, "[", "]")
        if end is not None:
            try:
                value, _ = decoder.raw_decode(text[index:end], 0)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, list) and any(
                isinstance(item, dict) and any(key in item for key in keys)
                for item in value
            ):
                return value
            index = end
            continue
        index += 1


def _has_unclosed_object(text: str) -> bool:
    """文本里是否还有未闭合的 ``{``（忽略字符串内的花括号与转义）。

    比「尾部出现带引号键名」更精确：正式结果在写出任何键之前被截断
    （``{"summary": "下周一…`` / ``{"notes``）时尾部没有任何键名，旧判定
    会把更早的 prompt 回显示例当成结果；反过来，完整结果后的说明文字里
    出现 ``{"destination": 城市名}`` 这类带键名的平衡片段，旧判定会误报
    截断、把有效结果整段丢弃。
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
    return depth > 0


def _scan_last_json_object_with_keys(
    text: str, keys: "tuple[str, ...]", *, warn: bool = True
) -> "tuple[dict | None, bool]":
    """扫描最后一个含目标键的顶层对象，返回 ``(对象, 是否因末尾截断放弃)``。"""
    selected: dict | None = None
    selected_end = -1
    failed_tail = False
    for start, value, end, looks_like in _iter_object_spans(text):
        if value is not None:
            if any(key in value for key in keys):
                selected = value
                selected_end = end
                failed_tail = False
            continue
        if looks_like and start >= selected_end:
            # 结果位置出现「像结果对象但无法解析」的片段：可能是被截断的
            # 正式结果，也可能是含裸控制字符 / NaN 等修复不了的脏数据——
            # 都不能让更早的 prompt 回显示例顶替
            failed_tail = True
    if selected is None:
        if failed_tail:
            if warn:
                logger.warning(
                    "存在无法解析的 JSON 结果对象（可能被截断），拒绝回退到更早的候选"
                )
            return None, True
        return None, False
    if failed_tail or _has_unclosed_object(text[selected_end:]):
        if warn:
            logger.warning(
                "末尾仍有无法解析 / 未闭合的 JSON 对象（可能被截断），放弃更早的候选"
            )
        return None, True
    return selected, False


def last_json_object_prefer_keys_strict(
    text: str,
    strong_keys: "tuple[str, ...]",
    weak_keys: "tuple[str, ...]",
) -> "dict | None":
    """优先取最后一个含强键的对象；没有强键时才回退到最后一个弱键对象。

    行程规划的输出可以是完整包装（含 ``itinerary`` / ``daily_plans``）或
    裸行程对象（只有 ``title`` / ``duration``）。说明性尾注里的片段
    （``{"duration": "3天", "note": ...}``）也是弱键对象：与完整行程一起
    出现时不能顶掉正式结果。末尾出现未闭合对象（截断）时抛 ``ValueError``。
    """
    strong: dict | None = None
    strong_end = -1
    weak: dict | None = None
    weak_end = -1
    for value, end in _iter_objects_with_repair(text):
        if any(key in value for key in strong_keys):
            strong, strong_end = value, end
        elif any(key in value for key in weak_keys):
            weak, weak_end = value, end
    if strong is not None:
        selected, selected_end = strong, strong_end
    elif weak is not None:
        selected, selected_end = weak, weak_end
    else:
        return None
    if _has_unclosed_object(text[selected_end:]):
        raise ValueError("模型输出在结果对象处被截断，拒绝回退到更早的候选")
    return selected


def last_json_object_with_keys(
    text: str, keys: "tuple[str, ...]"
) -> "dict | None":
    """返回文本中**最后一个**包含任一指定键的顶层对象（没有则 None）。

    模型常在正式结果前复述 prompt 里的输出示例 / 当前数据：只取第一个
    对象会把回显当成结果（示例行程 / 示例偏好被写库）。正式结果通常在
    响应末尾，按「最后一个形如结果的对象」挑选更符合实际输出习惯。

    候选按小瑕疵修复后再解码（尾逗号等），避免「正式结果带尾逗号 → 严格
    扫描跳过 → 更早的示例对象被当成结果」污染长期记忆。若正式结果之后的
    文本仍出现目标键（说明末尾还有一个无法解析的结果对象，如被截断），
    返回 None 让调用方走错误降级，而不是把示例当真实数据。
    """
    return _scan_last_json_object_with_keys(text, keys)[0]


def last_json_object_with_keys_strict(
    text: str, keys: "tuple[str, ...]"
) -> "dict | None":
    """同 :func:`last_json_object_with_keys`，但末尾截断时抛 ``ValueError``。

    调用方用它把「没有任何含目标键的对象」与「正式结果被截断」区分开：
    前者可以回退到第一个含目标键的对象，后者必须错误降级——回退会取回
    prompt 回显的示例对象，把示例偏好 / 示例行程当成真实结果写库。
    """
    selected, truncated = _scan_last_json_object_with_keys(text, keys)
    if truncated:
        raise ValueError("模型输出在结果对象处被截断，拒绝回退到更早的候选")
    return selected


def first_json_object_with_keys(
    text: str, keys: "tuple[str, ...]"
) -> "dict | None":
    """返回文本中第一个完整解析且含任一目标键的顶层对象（没有则 None）。

    只用于「不存在任何含目标键的完整对象」时的兜底扫描；若模型回显了
    prompt 示例（通常含全部目标键），它同样命中——调用方必须先用
    :func:`last_json_object_with_keys_strict` 排除截断场景。
    """
    for value, _ in _iter_objects_with_repair(text):
        if any(key in value for key in keys):
            return value
    return None


def robust_first_json_object(text: str) -> dict:
    """解析模型输出里的第一个完整 JSON 对象；严格扫描失败时尝试修复。

    ``extract_first_json_object`` 只接受无瑕疵的顶层对象：模型偶尔输出尾逗号
    （``"agent_schedule": [...],}``）或裸控制字符时整段放弃，调用方会把整轮
    提取降级 / 丢结果。修复交给 ``robust_json_parse``（代码块 / 控制字符 /
    尾逗号 / 单引号），仍不是对象才报错。
    """
    try:
        return extract_first_json_object(text)
    except ValueError:
        pass
    repaired = robust_json_parse(text, fallback=None)
    if not isinstance(repaired, dict):
        raise ValueError("No JSON object found in response")
    return repaired


def robust_json_parse(text: str, fallback=None) -> dict:
    """
    鲁棒的 JSON 解析函数

    Args:
        text: 要解析的文本（可能包含 JSON）
        fallback: 解析失败时的默认返回值

    Returns:
        解析后的字典，或 fallback 值

    Raises:
        ValueError: 如果所有解析尝试都失败且没有提供 fallback
    """
    # 如果已经是字典，直接返回（空字典 {} 也要原样返回，不能当作空文本）
    if isinstance(text, dict):
        return text
    if not text:
        if fallback is not None:
            return fallback
        raise ValueError("Empty text provided")

    # 清理文本，移除 markdown 代码块标记
    text = text.strip()
    if text.startswith('```json'):
        text = text[7:]
    elif text.startswith('```'):
        text = text[3:]
    text = text.removesuffix('```')
    text = text.strip()

    # 提取 JSON 部分
    start_idx = text.find('{')
    end_idx = text.rfind('}')

    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        if fallback is not None:
            logger.warning("No JSON found in text, using fallback")
            return fallback
        raise ValueError("No JSON found in response")

    json_str = text[start_idx:end_idx+1]

    # 记录最后一次解析错误：Python 3 会回收 except 变量，
    # 不显式保存的话最终兜底处引用 e 会抛 NameError（Issue #6）
    last_error: Exception | None = None

    # 尝试1: 直接解析
    try:
        return json.loads(json_str, parse_constant=reject_json_constant)
    except json.JSONDecodeError as e:
        last_error = e
        logger.warning(f"Direct JSON parse failed: {e}")
        # 输出出错位置附近的内容
        error_pos = getattr(e, 'pos', 0)
        start = max(0, error_pos - 50)
        end = min(len(json_str), error_pos + 50)
        logger.warning(
            "Error context: ...%s...",
            _safe_log_text(json_str[start:end])[:MAX_LOG_TEXT],
        )

    # 尝试2: 移除控制字符后解析
    try:
        json_str_cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', json_str)
        result = json.loads(
            json_str_cleaned, parse_constant=reject_json_constant
        )
        logger.info("JSON parsed successfully after removing control characters")
        return result
    except json.JSONDecodeError as e:
        last_error = e
        logger.warning(f"JSON parse failed after cleaning: {e}")

    # 尝试3: 修复引号问题
    try:
        # 只在必要时替换单引号（避免破坏字符串内容）
        # 这个正则会匹配键名和值周围的单引号
        json_str_fixed = re.sub(r"'([^']*)'(\s*:\s*)", r'"\1"\2', json_str)  # 键名
        json_str_fixed = re.sub(r':\s*\'([^\']*)\'', r': "\1"', json_str_fixed)  # 字符串值
        result = json.loads(
            json_str_fixed, parse_constant=reject_json_constant
        )
        logger.info("JSON parsed successfully after fixing quotes")
        return result
    except json.JSONDecodeError as e:
        last_error = e
        logger.warning(f"JSON parse failed after fixing quotes: {e}")

    # 尝试4: 修复常见的尾部逗号问题
    try:
        # 移除对象和数组中的尾部逗号（字符串值内的 ",}"/",]" 原样保留）
        json_str_fixed = _strip_trailing_commas(json_str)
        result = json.loads(
            json_str_fixed, parse_constant=reject_json_constant
        )
        logger.info("JSON parsed successfully after removing trailing commas")
        return result
    except json.JSONDecodeError as e:
        last_error = e
        logger.warning(f"JSON parse failed after removing trailing commas: {e}")

    # 尝试5: 修复未转义的换行符（只在字符串值内部）
    try:
        # 使用更智能的方式处理换行符，避免破坏 JSON 结构
        def escape_newlines_in_strings(s):
            """只转义字符串值中的换行符，不影响 JSON 结构"""
            result = []
            in_string = False
            escape_next = False

            for i, char in enumerate(s):
                if escape_next:
                    result.append(char)
                    escape_next = False
                    continue

                if char == '\\':
                    result.append(char)
                    escape_next = True
                    continue

                if char == '"':
                    in_string = not in_string
                    result.append(char)
                    continue

                if in_string and char in ('\n', '\r', '\t'):
                    # 在字符串内部，转义特殊字符
                    if char == '\n':
                        result.append('\\n')
                    elif char == '\r':
                        result.append('\\r')
                    elif char == '\t':
                        result.append('\\t')
                else:
                    result.append(char)

            return ''.join(result)

        json_str_fixed = escape_newlines_in_strings(json_str)
        result = json.loads(
            json_str_fixed, parse_constant=reject_json_constant
        )
        logger.info("JSON parsed successfully after smart escaping")
        return result
    except json.JSONDecodeError as e:
        last_error = e
        logger.warning(f"JSON parse failed after smart escaping: {e}")

    # 尝试6: 使用 json5 或其他宽松解析器（如果可用）
    try:
        import json5
        result = json5.loads(json_str)
        logger.info("JSON parsed successfully using json5")
        return result
    except ImportError as e:
        last_error = e
        logger.debug("json5 not available")
    except Exception as e:
        last_error = e
        logger.warning(f"JSON5 parse failed: {e}")

    # 尝试7: 文本前后夹带说明文字、或结尾说明里还有花括号时，取第一个
    # 能完整解析的对象（与 extract_first_json_object 相同的策略）
    try:
        value = extract_first_json_object(text)
        logger.info("JSON parsed successfully by extracting the first object")
        return value
    except ValueError as e:
        last_error = e
        logger.warning(f"First-object extraction failed: {e}")

    # 所有尝试都失败
    logger.error(
        "All JSON parsing attempts failed. Full JSON: %s",
        _safe_log_text(json_str)[:MAX_LOG_TEXT],
    )

    if fallback is not None:
        logger.warning("Using fallback value")
        return fallback

    raise ValueError(f"Failed to parse JSON after all attempts. Last error: {last_error}")


def _extract_text_from_block(block) -> str:
    """从 agentscope 内容块中提取文本（TextBlock 形如 {'type': 'text', 'text': ...}）。"""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        if block.get("type") == "text":
            return block.get("text") or ""
        return ""
    # 其他实现：对象形式的文本块
    if getattr(block, "type", None) == "text":
        return getattr(block, "text", "") or ""
    return ""


def extract_response_text(response) -> str:
    """从模型返回中提取纯文本，兼容以下形态：

    - **非流式 ChatResponse**：agentscope 1.0.16 的 `dict` 子类，形如
      `{"content": [{"type": "text", "text": "..."}, ...]}`。
      注意它重写了 `__getattr__`，访问不存在的属性会抛 `KeyError` 而不是
      `AttributeError`，因此必须先按 dict 处理，不能使用 `hasattr`。
    - 字符串，或带 `.text` / `.content` 属性的简单对象（测试 mock、其他模型封装）；
    - 其他对象，兜底转字符串。
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        content = response.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(_extract_text_from_block(block) for block in content)
        if content is None:
            return ""
        return str(content)
    # 非 dict 对象：常规属性访问，hasattr 安全
    text = getattr(response, "text", None)
    if text is not None:
        return str(text)
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_extract_text_from_block(block) for block in content)
    if content is not None:
        return str(content)
    return str(response)


def extract_json_from_response(response, field_name="content") -> str:
    """
    从各种响应格式中提取文本（兼容旧接口，field_name 参数已废弃）

    Args:
        response: 模型响应（ChatResponse / 字典 / 字符串等）
        field_name: 保留以兼容旧调用方，无实际作用

    Returns:
        提取的文本内容
    """
    return extract_response_text(response)


async def extract_json_from_async_response(response, field_name="content") -> str:
    """
    从（可能是异步生成器的）模型响应中提取文本。

    流式返回时取最后一个非空文本块的累积结果（agentscope 的流式块
    携带逐次累积的 content）；非流式响应直接走同步解析。

    Args:
        response: 模型响应（可能是异步生成器 / ChatResponse / 字符串等）
        field_name: 保留以兼容旧调用方，无实际作用

    Returns:
        提取的文本内容
    """
    # ChatResponse 是 dict 子类，必须先排除再判断 __aiter__（其 __getattr__ 会抛 KeyError）
    if not isinstance(response, dict) and hasattr(response, "__aiter__"):
        text = ""
        async for chunk in response:
            chunk_text = extract_response_text(chunk)
            if chunk_text:
                text = chunk_text
        return text
    return extract_json_from_response(response)

