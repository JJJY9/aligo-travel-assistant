"""
JSON 解析辅助工具
提供更鲁棒的 JSON 解析功能，处理常见的格式问题
"""
import json
import logging
import re

logger = logging.getLogger(__name__)


def reject_json_constant(value: str):
    """拒绝 NaN / Infinity（Python 默认允许，会让持久化的 JSON 无法再被标准解析）。"""
    raise ValueError(f"非法 JSON 数值: {value}")


def extract_first_json_object(text: str) -> dict:
    """返回文本中第一个能完整解析的 JSON 对象，忽略前后的说明文字。

    ``find('{')`` / ``rfind('}')`` 会把结尾说明里的花括号（例如「如 {日期}
    未提供可默认明天」）一起截进 JSON，导致合法输出解析失败。这里逐个尝试
    每个 ``{`` 的 ``raw_decode``，只接受完整解析出的 dict。
    """
    if not text:
        raise ValueError("Empty text provided")
    decoder = json.JSONDecoder(parse_constant=reject_json_constant)
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("No JSON object found in response")


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
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        last_error = e
        logger.warning(f"Direct JSON parse failed: {e}")
        # 输出出错位置附近的内容
        error_pos = getattr(e, 'pos', 0)
        start = max(0, error_pos - 50)
        end = min(len(json_str), error_pos + 50)
        logger.warning(f"Error context: ...{json_str[start:end]}...")

    # 尝试2: 移除控制字符后解析
    try:
        json_str_cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', json_str)
        result = json.loads(json_str_cleaned)
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
        result = json.loads(json_str_fixed)
        logger.info("JSON parsed successfully after fixing quotes")
        return result
    except json.JSONDecodeError as e:
        last_error = e
        logger.warning(f"JSON parse failed after fixing quotes: {e}")

    # 尝试4: 修复常见的尾部逗号问题
    try:
        # 移除对象和数组中的尾部逗号
        json_str_fixed = re.sub(r',(\s*[}\]])', r'\1', json_str)
        result = json.loads(json_str_fixed)
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
        result = json.loads(json_str_fixed)
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
    logger.error(f"All JSON parsing attempts failed. Full JSON:\n{json_str}")

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

