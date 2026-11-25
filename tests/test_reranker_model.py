"""共享重排模型加载器测试（不依赖 sentence-transformers / 网络）。

覆盖：并发首载单飞（只构造一份模型）、加载失败冷却、同名模型复用。
"""
import os
import sys
import threading
import types

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import reranker_model


class _FakeCrossEncoder:
    instances: list[str] = []

    def __init__(self, name):
        _FakeCrossEncoder.instances.append(name)


def _install_fake_backend(monkeypatch, factory):
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(CrossEncoder=factory),
    )


def test_concurrent_first_load_constructs_once(monkeypatch):
    """并发首载只构造一份模型：其它线程等锁复用缓存。"""
    reranker_model._MODELS.clear()
    reranker_model._FAILURES.clear()
    _FakeCrossEncoder.instances = []
    started = threading.Event()

    class _SlowCrossEncoder:
        def __init__(self, name):
            _FakeCrossEncoder.instances.append(name)
            started.set()
            # 拉长加载窗口，让并发线程都进入等待而不是各自构造
            import time

            time.sleep(0.15)

    _install_fake_backend(monkeypatch, _SlowCrossEncoder)
    results = []
    errors = []

    def _load():
        try:
            results.append(reranker_model.get_shared_cross_encoder("fake"))
        except Exception as exc:  # pragma: no cover - 失败会由断言暴露
            errors.append(exc)

    threads = [threading.Thread(target=_load) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors, errors
    assert len(_FakeCrossEncoder.instances) == 1, _FakeCrossEncoder.instances
    assert all(model is results[0] for model in results)
    print("✓ 并发首载只构造一份重排模型")


def test_load_failure_enters_cooldown(monkeypatch):
    """加载失败抛 RuntimeError 并进入冷却，冷却期内不再尝试构造。"""
    reranker_model._MODELS.clear()
    reranker_model._FAILURES.clear()
    calls = []

    class _BoomCrossEncoder:
        def __init__(self, name):
            calls.append(name)
            raise OSError("model not found")

    _install_fake_backend(monkeypatch, _BoomCrossEncoder)
    for _ in range(2):
        try:
            reranker_model.get_shared_cross_encoder("missing")
        except (OSError, RuntimeError):
            pass
        else:  # pragma: no cover
            raise AssertionError("加载失败应抛出")
    assert calls == ["missing"], "冷却期内不得重复构造"
    print("✓ 重排模型加载失败进入冷却")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    import pytest

    mp = pytest.MonkeyPatch()
    try:
        test_concurrent_first_load_constructs_once(mp)
        mp.undo()
        mp = pytest.MonkeyPatch()
        test_load_failure_enters_cooldown(mp)
    finally:
        mp.undo()
    print("\n全部通过")
