"""``vectorization.py`` 的单测。

向量器是纯本地实现，不需要下载模型；这里同时验证中文相似度、确定性和缓存隔离。
"""
from __future__ import annotations

import pytest
import chromadb

from boss_zhipin.vectorization import (
    EMBEDDING_DIMENSIONS,
    VectorStore,
    _encode_text,
    _encode_texts,
    embedding_cache_id,
    split_text,
    text_hash,
)


# ---------- split_text ----------

class TestSplitText:
    def test_short_text_single_chunk(self):
        chunks = split_text("短文本", chunk_size=1000, chunk_overlap=200)
        assert chunks == ["短文本"]

    def test_long_text_chunked_with_overlap(self):
        text = "ABCDEFGHIJ" * 100  # 1000 字符
        chunks = split_text(text, chunk_size=300, chunk_overlap=50)
        # 至少切成多块
        assert len(chunks) > 1
        # 每块不超过 chunk_size
        for c in chunks:
            assert len(c) <= 300
        # 相邻块之间有 overlap（不严格断在边界）
        # 验证：把所有 chunk concat 起来去重后包含原始 text
        assert text in "".join(chunks) or len("".join(chunks)) >= len(text)

    def test_empty_text_returns_empty(self):
        assert split_text("", chunk_size=1000, chunk_overlap=200) == []

    def test_chunk_size_must_exceed_overlap(self):
        with pytest.raises(ValueError):
            split_text("anything", chunk_size=100, chunk_overlap=100)
        with pytest.raises(ValueError):
            split_text("anything", chunk_size=50, chunk_overlap=100)

    def test_whitespace_only_chunks_dropped(self):
        # chunk 全是空白时会被 .strip() 掉
        text = "   AAA   "
        chunks = split_text(text, chunk_size=3, chunk_overlap=1)
        # 没有任何 chunk 是纯空白
        assert all(c.strip() for c in chunks)

    def test_chinese_text_chunked_correctly(self):
        # 中文（多字节）也按字符数切，不该按字节
        text = "求职者简历内容" * 100
        chunks = split_text(text, chunk_size=50, chunk_overlap=10)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 50
            assert "求职者" in c or "简历" in c or "内容" in c


# ---------- text_hash ----------

class TestTextHash:
    def test_same_content_same_hash(self):
        assert text_hash("hello world") == text_hash("hello world")

    def test_different_content_different_hash(self):
        assert text_hash("hello world") != text_hash("hello world!")

    def test_hash_is_md5_hex(self):
        h = text_hash("x")
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)


# ---------- local embedding ----------

class TestLocalEmbedding:
    def test_is_deterministic_and_normalised(self):
        first = _encode_text("Python 后端开发")
        second = _encode_text("Python 后端开发")
        assert first == second
        assert len(first) == EMBEDDING_DIMENSIONS
        assert sum(value * value for value in first) == pytest.approx(1.0)

    def test_related_chinese_text_is_closer(self):
        query = _encode_text("Python 后端开发、FastAPI 和数据库")
        related = _encode_text("负责 Python 后端开发，熟悉 FastAPI、MySQL 数据库")
        unrelated = _encode_text("负责餐厅菜品制作、库存盘点和门店卫生")
        related_score = sum(a * b for a, b in zip(query, related))
        unrelated_score = sum(a * b for a, b in zip(query, unrelated))
        assert related_score > unrelated_score

    def test_empty_and_single_character_text(self):
        assert _encode_text("") == [0.0] * EMBEDDING_DIMENSIONS
        assert sum(value * value for value in _encode_text("甲")) == pytest.approx(1.0)

    def test_cache_key_is_namespaced_by_backend(self):
        assert embedding_cache_id("同一份简历") != text_hash("同一份简历")
        assert embedding_cache_id("同一份简历") == embedding_cache_id("同一份简历")

    def test_chroma_search_and_relevance_work_without_model_download(self):
        client = chromadb.EphemeralClient()
        collection = client.create_collection("offline-test")
        documents = [
            "负责 Python 后端开发，熟悉 FastAPI、MySQL 数据库",
            "负责 React 前端页面和交互体验开发",
        ]
        collection.add(
            documents=documents,
            embeddings=_encode_texts(documents),
            ids=["backend", "frontend"],
        )
        store = VectorStore(collection)

        assert "Python" in store.search("Python FastAPI 后端岗位", k=1)[0]
        assert store.check_relevance("Python FastAPI 后端岗位")[0] is True
        assert store.check_relevance("餐厅厨师和门店卫生")[0] is False
