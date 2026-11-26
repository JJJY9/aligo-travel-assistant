"""共享重排模型加载器测试（不依赖 sentence-transformers / 网络）。

覆盖：并发首载单飞（只构造一份模型）、加载失败冷却、同名模型复用。
"""
import os
import sys
import threading
import types

import pytest

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
    """加载失败抛 RuntimeError 并进入冷却，冷却期内不再尝试构造。

    冷却命中抛 ``RerankerCooldownError``：调用方不能把他人失败的冷却
    当成自己的永久失败。
    """
    reranker_model._MODELS.clear()
    reranker_model._FAILURES.clear()
    calls = []

    class _BoomCrossEncoder:
        def __init__(self, name):
            calls.append(name)
            raise OSError("model not found")

    _install_fake_backend(monkeypatch, _BoomCrossEncoder)
    with pytest.raises((OSError, RuntimeError)):
        reranker_model.get_shared_cross_encoder("missing")
    with pytest.raises(reranker_model.RerankerCooldownError):
        reranker_model.get_shared_cross_encoder("missing")
    assert calls == ["missing"], "冷却期内不得重复构造"
    print("✓ 重排模型加载失败进入冷却（独立异常类型）")


def test_loading_wait_is_bounded(monkeypatch):
    """加载进行中：其它调用有限等待后按「加载中」降级，不计入失败冷却。"""
    import threading
    import time

    reranker_model._MODELS.clear()
    reranker_model._FAILURES.clear()
    monkeypatch.setattr(reranker_model, "_LOAD_WAIT_SEC", 0.05)
    started = threading.Event()

    class _SlowCrossEncoder:
        def __init__(self, name):
            started.set()
            time.sleep(0.4)

    _install_fake_backend(monkeypatch, _SlowCrossEncoder)
    loaded = []
    loader = threading.Thread(
        target=lambda: loaded.append(
            reranker_model.get_shared_cross_encoder("slow")
        )
    )
    loader.start()
    assert started.wait(timeout=2)
    began = time.monotonic()
    with pytest.raises(reranker_model.RerankerLoadingError):
        reranker_model.get_shared_cross_encoder("slow")
    assert time.monotonic() - began < 0.3, "等待必须有界，不能钉住默认线程池"
    assert "slow" not in reranker_model._FAILURES, "加载中不是失败"
    loader.join(timeout=5)
    assert loaded and reranker_model._MODELS.get("slow") is loaded[0]
    print("✓ 并发加载有界等待，超时按 RRF 顺序降级且不冷却")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    import pytest

    cases = (
        test_concurrent_first_load_constructs_once,
        test_load_failure_enters_cooldown,
        test_loading_wait_is_bounded,
    )
    for case in cases:
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
