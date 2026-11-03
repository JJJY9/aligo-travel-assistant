"""
模型响应解析与模型工厂测试（无需 API Key）

覆盖：
1. extract_response_text / extract_json_from_async_response 兼容
   agentscope 非流式 ChatResponse（dict 子类）、流式生成器、普通对象与字符串；
2. create_chat_model 统一使用 stream=False，且 temperature / max_tokens
   通过 generate_kwargs 传递（agentscope 1.0.16 的流式解析与 DeepSeek 不兼容，
   顶层参数会被静默忽略——见 README「兼容性说明」）。

运行：python tests/test_model_response.py
"""
import asyncio
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from config_agentscope import create_chat_model
from utils.json_parser import extract_json_from_async_response, extract_response_text


def test_chat_response_dict():
    """非流式 ChatResponse：content 为文本块列表（dict 形态）"""
    response = {
        "content": [
            {"type": "text", "text": "你好，"},
            {"type": "thinking", "text": "（思考块应被忽略）"},
            {"type": "text", "text": "世界"},
        ]
    }
    assert extract_response_text(response) == "你好，世界"
    print("✓ ChatResponse 文本块提取正常")


def test_string_and_plain_object():
    assert extract_response_text("纯文本") == "纯文本"

    class _Resp:
        text = "对象文本"

    assert extract_response_text(_Resp()) == "对象文本"
    print("✓ 字符串 / 普通对象兼容")


def test_async_stream_chunks():
    """流式异步生成器：取最后一个非空文本"""

    async def _gen():
        yield {"content": [{"type": "text", "text": "中间结果"}]}
        yield {"content": [{"type": "text", "text": "最终答案"}]}

    assert asyncio.run(extract_json_from_async_response(_gen())) == "最终答案"
    print("✓ 流式生成器兼容")


def test_factory_uses_nonstream_and_generate_kwargs():
    """工厂必须使用非流式，并把生成参数放入 generate_kwargs"""
    model = create_chat_model(api_key="sk-test-dummy")
    assert getattr(model, "stream", None) is False, "agentscope 1.0.16 流式与 DeepSeek 不兼容"
    generate_kwargs = getattr(model, "generate_kwargs", {})
    assert "temperature" in generate_kwargs and "max_tokens" in generate_kwargs
    print("✓ create_chat_model 使用非流式 + generate_kwargs")


if __name__ == "__main__":
    test_chat_response_dict()
    test_string_and_plain_object()
    test_async_stream_chunks()
    test_factory_uses_nonstream_and_generate_kwargs()
    print("\n全部通过")
