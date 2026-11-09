"""嵌入模型维度探测的版本兼容（无网络 / 不加载真实模型）。

sentence-transformers 6.x 把 ``get_sentence_embedding_dimension`` 更名为
``get_embedding_dimension``（旧名仍可用但会告警）：helper 必须新名优先、
旧名回退，升级依赖后不产生 FutureWarning。
运行：python tests/test_embedding_model.py
"""
import os
import sys
import types

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import embedding_model


def test_dimension_prefers_new_method():
    """同时存在新旧方法时用新名（6.x），不再触发旧名告警。"""

    class Both:
        def get_embedding_dimension(self):
            return 512

        def get_sentence_embedding_dimension(self):
            return 999

    assert embedding_model.embedding_dimension(Both()) == 512
    print("✓ 维度探测优先新方法名")


def test_dimension_falls_back_to_old_method():
    """只有旧方法（sentence-transformers 5.x / 历史假件）时回退。"""

    class OldOnly:
        def get_sentence_embedding_dimension(self):
            return 512

    assert embedding_model.embedding_dimension(OldOnly()) == 512
    print("✓ 旧方法名回退")


def test_dimension_unknown_or_broken_returns_none():
    """方法缺失 / 抛异常 / 返回值不可转 int 都按未知处理。"""

    class Neither:
        pass

    class Boom:
        def get_embedding_dimension(self):
            raise RuntimeError("boom")

    class BadValue:
        def get_embedding_dimension(self):
            return "not-a-number"

    assert embedding_model.embedding_dimension(Neither()) is None
    assert embedding_model.embedding_dimension(Boom()) is None
    assert embedding_model.embedding_dimension(BadValue()) is None
    print("✓ 缺失 / 异常 / 非法返回值按未知处理")


def test_peek_uses_new_method(monkeypatch):
    """peek_embedding_dim 同样走兼容 helper（只提供新名也能探测）。"""
    embedding_model.reset_embedding_models()

    class NewStyle:
        def __init__(self, path):
            self.path = path

        def get_embedding_dimension(self):
            return 384

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = NewStyle  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    try:
        embedding_model.get_embedding_model("new-style-model")
        assert embedding_model.peek_embedding_dim() == 384
    finally:
        embedding_model.reset_embedding_models()
    print("✓ peek_embedding_dim 兼容新方法名")


if __name__ == "__main__":
    test_dimension_prefers_new_method()
    test_dimension_falls_back_to_old_method()
    test_dimension_unknown_or_broken_returns_none()
    mp = pytest.MonkeyPatch()
    try:
        test_peek_uses_new_method(mp)
    finally:
        mp.undo()
    print("\n全部通过")
