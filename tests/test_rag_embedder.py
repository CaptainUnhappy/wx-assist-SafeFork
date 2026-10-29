"""Embedder batching/serialization guards (memory and ordering)."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

np = pytest.importorskip("numpy")

from src.assistant.rag.embedder import FastEmbedder


def test_encode_is_serialized_and_uses_bounded_batches():
    class Model:
        active = 0
        max_active = 0

        def embed(self, texts, batch_size):
            assert batch_size == 16
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.01)
                for text in texts:
                    yield [float(text), 0.0]
            finally:
                self.active -= 1

    model = Model()
    embedder = FastEmbedder(batch_size=16, threads=2)
    embedder._model = model

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(embedder.encode, [["1", "2"], ["3"], ["4", "5"]]))

    assert model.max_active == 1
    assert [r[:, 0].tolist() for r in results] == [[1, 2], [3], [4, 5]]
    assert embedder._threads == 2


def test_invalid_batch_or_threads_rejected():
    with pytest.raises(ValueError):
        FastEmbedder(batch_size=0)
    with pytest.raises(ValueError):
        FastEmbedder(threads=0)


def test_encode_returns_vectors_in_input_order_after_length_sorting():
    class Model:
        def embed(self, texts, batch_size):
            for text in texts:
                yield [float(len(text)), float(text[0] == "L")]

    embedder = FastEmbedder(batch_size=32, threads=2)
    embedder._model = Model()
    texts = ["longest-text-here", "a", "medium-one", "bb"]

    vectors = embedder.encode(texts)

    assert [int(row[0]) for row in vectors] == [len(t) for t in texts]
    assert embedder.encode([]).shape == (0, 0)
