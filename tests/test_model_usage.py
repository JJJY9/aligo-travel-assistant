"""
模型用量统计测试（无需 API Key、不访问网络）

覆盖：
1. UsageTrackingModel 透传调用并累计 token 用量与耗时；
2. 底层模型没有 usage 时只计数、不累计 token；
3. stream / generate_kwargs 等属性透明代理；
4. create_chat_model 返回带用量统计的模型。

运行：python tests/test_model_usage.py
"""
import asyncio
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from config_agentscope import UsageTrackingModel, create_chat_model


class _FakeModel:
    """返回固定响应与 usage 的假模型"""

    stream = False
    generate_kwargs = {"temperature": 0.7, "max_tokens": 100}

    def __init__(self, usage=None):
        self._usage = usage

    async def __call__(self, messages, **kwargs):
        return {"content": [{"type": "text", "text": "ok"}], "usage": self._usage}


def test_tracks_usage():
    model = UsageTrackingModel(_FakeModel({"input_tokens": 10, "output_tokens": 5, "time": 0.2}))
    asyncio.run(model([{"role": "user", "content": "hi"}]))
    assert model.usage.calls == 1
    assert model.usage.input_tokens == 10
    assert model.usage.output_tokens == 5
    assert model.usage.total_tokens == 15
    assert abs(model.usage.time - 0.2) < 1e-9
    print("✓ token 用量与耗时累计正常")


def test_missing_usage_counts_call_only():
    model = UsageTrackingModel(_FakeModel(None))
    asyncio.run(model([{"role": "user", "content": "hi"}]))
    assert model.usage.calls == 1
    assert model.usage.total_tokens == 0
    print("✓ 无 usage 时只计数不累计 token")


def test_attribute_delegation():
    model = UsageTrackingModel(_FakeModel())
    assert model.stream is False
    assert model.generate_kwargs["max_tokens"] == 100
    print("✓ 底层属性透明代理")


def test_factory_returns_tracked_model():
    model = create_chat_model(api_key="sk-test-dummy")
    assert isinstance(model, UsageTrackingModel)
    assert model.usage.calls == 0
    assert model.usage.summary_text().startswith("0 次调用")
    print("✓ create_chat_model 返回带用量统计的模型")


def test_snapshot_delta():
    model = UsageTrackingModel(_FakeModel({"input_tokens": 3, "output_tokens": 2, "time": 0.1}))
    before = model.usage.snapshot()
    asyncio.run(model([{"role": "user", "content": "a"}]))
    after = model.usage.snapshot()
    assert after["total_tokens"] - before["total_tokens"] == 5
    print("✓ 快照增量计算正常")


if __name__ == "__main__":
    test_tracks_usage()
    test_missing_usage_counts_call_only()
    test_attribute_delegation()
    test_factory_returns_tracked_model()
    test_snapshot_delta()
    print("\n全部通过")
