"""
长期记忆消息渲染

助手消息存的是编排结果 JSON（`results` 或 `status`），展示前需要重新渲染为
Markdown；用户消息原样返回。Web API 与 CLI 导出共用本模块，避免两套逻辑漂移。

渲染结果只取决于 (role, content)，但会话搜索、会话详情、导出与 CLI 会反复
渲染同一批消息：37MB / 12 万条的真实账号上一次未命中搜索就要把全部助手
消息重渲染一遍（实测约 2.4s，其中过半是渲染）。这里用有界 LRU 缓存按内容
记忆化，搜索换关键字仍按内容命中，无需失效。
"""
import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any

from utils.json_safe import sanitize_json
from utils.result_formatter import result_to_markdown
from utils.turn_meta import META_KEY, extract_turn_meta

# 以已缓存渲染文本的字符总数为预算（≈ 48M 字符，UTF-8 中文约占 144MB；
# 实际账号历史 12 万条、渲染后约 90M 字符，单条平均 1KB 级，逐出按 LRU）。
_CACHE_BUDGET_CHARS = 48_000_000
# 单条超过预算 1/8 的大消息不入缓存，避免一条巨消息冲掉整批常用条目
_CACHE_MAX_ENTRY_CHARS = _CACHE_BUDGET_CHARS // 8

_CACHE: "OrderedDict[tuple[str, bytes], str]" = OrderedDict()
_CACHE_CHARS = 0
_CACHE_LOCK = threading.Lock()


def _cache_key(role: str, content: str) -> tuple[str, bytes]:
    digest = hashlib.blake2b(
        content.encode("utf-8", "surrogatepass"), digest_size=16
    ).digest()
    return role, digest


def _cache_get(key: tuple[str, bytes]) -> str | None:
    with _CACHE_LOCK:
        value = _CACHE.get(key)
        if value is not None:
            _CACHE.move_to_end(key)
        return value


def _cache_put(key: tuple[str, bytes], value: str) -> None:
    global _CACHE_CHARS
    if len(value) > _CACHE_MAX_ENTRY_CHARS:
        return
    with _CACHE_LOCK:
        previous = _CACHE.pop(key, None)
        if previous is not None:
            _CACHE_CHARS -= len(previous)
        _CACHE[key] = value
        _CACHE_CHARS += len(value)
        while _CACHE_CHARS > _CACHE_BUDGET_CHARS and _CACHE:
            _, evicted = _CACHE.popitem(last=False)
            _CACHE_CHARS -= len(evicted)


def _safe_output(text: str) -> str:
    """渲染结果统一转义孤立代理项。

    旧记忆 / 手写文件里的 assistant 内容是 JSON 文本，``json.loads`` 会把
    ``\\ud800`` 还原成真代理项：直接放进 JSON / HTML / Markdown 响应会在
    UTF-8 编码时抛 UnicodeEncodeError，让该会话每次打开 / 导出都 500。
    """
    return sanitize_json(text)


def _render_uncached(role: str, content: str) -> tuple[str, dict[str, Any] | None, bool]:
    """渲染单条消息：返回 (文本, 执行元数据, 是否可缓存)。

    ``cacheable=False`` 只用于渲染器临时异常后的原文回退（依赖状态可能恢复，
    缓存会把一次异常固化成永久结果）。
    """
    if role != "assistant":
        return _safe_output(content), None, True
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        # 深度嵌套（RecursionError）与超 4300 位整数字面量（普通 ValueError，
        # 不是 JSONDecodeError）的历史内容会让会话查看 / 搜索 / 导出永久
        # 500：一律按旧格式原样返回
        return _safe_output(content), None, True
    if not isinstance(data, dict):
        return _safe_output(content), None, True
    raw_meta = data.get(META_KEY)
    meta = raw_meta if isinstance(raw_meta, dict) else None
    status = data.get("status")
    if "results" in data or (
        isinstance(status, str)
        and status in {"completed", "partial_failure", "no_agents"}
    ):
        try:
            return _safe_output(result_to_markdown(data)), meta, True
        except Exception:  # noqa: BLE001 - 渲染失败时退回原始内容
            return _safe_output(content), meta, False
    return _safe_output(content), meta, True


def render_stored_message(message: dict[str, Any]) -> str:
    """把历史消息转换为可展示文本：助手消息为编排 JSON 时重新渲染为 Markdown。"""
    role = message.get("role", "assistant")
    content = str(message.get("content", ""))
    key = _cache_key(role, content)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    rendered, _meta, cacheable = _render_uncached(role, content)
    if cacheable:
        _cache_put(key, rendered)
    return rendered


def render_stored_message_with_meta(
    message: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """渲染消息并同时取出执行元数据（助手内容只 ``json.loads`` 一次）。

    会话详情此前对每条助手消息先渲染（解析一次）再 ``extract_turn_meta``
    （再解析一次）；这里共享同一次解析。渲染文本命中缓存时仍只解析一次
    （元数据量小、不单独入缓存）。
    """
    role = message.get("role", "assistant")
    content = str(message.get("content", ""))
    key = _cache_key(role, content)
    cached = _cache_get(key)
    if cached is not None:
        if role != "assistant":
            return cached, None
        # 渲染文本命中缓存时只需为元数据解析一次，不再重渲染
        return cached, extract_turn_meta(content)
    rendered, meta, cacheable = _render_uncached(role, content)
    if cacheable:
        _cache_put(key, rendered)
    return rendered, meta
