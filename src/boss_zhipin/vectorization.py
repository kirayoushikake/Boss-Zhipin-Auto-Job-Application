"""
用户上传 resume.pdf
      ↓
→ 生成文件 hash（MD5）
      ↓
→ 查本地向量库是否已有对应 hash 的目录
      ↓
   ↙                ↘
已存在             不存在 or hash 变了
  ↓                        ↓
加载已有向量库      → 重新向量化 → 保存
"""

import hashlib
import math
import re
import unicodedata
from pathlib import Path

import chromadb

# 内置字符 n-gram 哈希向量器：不下载模型、不访问 Hugging Face，国内网络和
# 完全离线环境都能直接加载。版本号进入缓存键；以后调整算法/维度时改版本号，
# 就不会误读旧 sentence-transformers 向量库或不同维度的 Chroma collection。
EMBEDDING_BACKEND = "local-char-ngram-v1"
EMBEDDING_DIMENSIONS = 2048
COLLECTION_NAME = "resume"

_WHITESPACE_RE = re.compile(r"\s+")
_HASH_PERSON = b"boss-vec-v1"


def _normalise_text(text: str) -> str:
    """统一全/半角、大小写和空白，保证同一文本的向量跨机器稳定。"""
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).lower()).strip()


def _encode_text(text: str) -> list[float]:
    """把中英文文本编码为定长单位向量，全程只用 Python 标准库。"""
    normalised = _normalise_text(text)
    vector = [0.0] * EMBEDDING_DIMENSIONS
    if not normalised:
        return vector

    feature_count = 0
    for size in (2, 3, 4):
        if len(normalised) < size:
            continue
        for start in range(len(normalised) - size + 1):
            gram = normalised[start:start + size]
            digest = hashlib.blake2b(
                gram.encode("utf-8"), digest_size=8, person=_HASH_PERSON,
            ).digest()
            value = int.from_bytes(digest, "big")
            index = value % EMBEDDING_DIMENSIONS
            vector[index] += 1.0 if value & (1 << 63) else -1.0
            feature_count += 1

    # 极短文本（只有一个字符）也要有可查询的向量。
    if feature_count == 0:
        digest = hashlib.blake2b(
            normalised.encode("utf-8"), digest_size=8, person=_HASH_PERSON,
        ).digest()
        value = int.from_bytes(digest, "big")
        vector[value % EMBEDDING_DIMENSIONS] = 1.0

    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _encode_texts(texts: list[str]) -> list[list[float]]:
    return [_encode_text(text) for text in texts]


def embedding_cache_id(text: str) -> str:
    """缓存键同时包含向量器版本，防止复用旧模型生成的向量库。"""
    return text_hash(f"{EMBEDDING_BACKEND}\0{text}")


def text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def split_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> list[str]:
    if chunk_size <= chunk_overlap:
        raise ValueError("chunk_size must exceed chunk_overlap")
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        start = end - chunk_overlap
    return chunks


class VectorStore:
    """Chroma collection wrapper with local similarity search over PDF chunks."""

    def __init__(self, collection: chromadb.Collection):
        self._collection = collection

    def search(self, query: str, k: int = 4) -> list[str]:
        query_embedding = _encode_texts([query])
        results = self._collection.query(query_embeddings=query_embedding, n_results=k)
        documents = results.get("documents") or []
        return documents[0] if documents else []

    def check_relevance(self, query: str, distance_threshold: float = 1.3) -> tuple[bool, float]:
        """检查查询（如JD）与集合的最短距离。
        distance_threshold 默认1.3（经验值，基于 L2 距离，越小越相似）。
        如果所有 chunk 的距离都大于阈值，说明完全不相关，返回 False。
        """
        query_embedding = _encode_texts([query])
        results = self._collection.query(query_embeddings=query_embedding, n_results=1)
        distances = results.get("distances")
        if not distances or not distances[0]:
            return True, 0.0  # 没提取到 distance 就放行
        
        min_distance = distances[0][0]
        return min_distance <= distance_threshold, min_distance


def embed_resume(resume_text: str, base_dir: str = "./vectorstores") -> VectorStore:
    file_id = embedding_cache_id(resume_text)
    persist_dir = Path(base_dir) / file_id
    persist_dir.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(persist_dir))
    existing = {c.name for c in client.list_collections()}

    if COLLECTION_NAME in existing:
        print("✅ 加载已存在向量库")
        collection = client.get_collection(COLLECTION_NAME)
    else:
        print("❌ 不存在向量库，重新向量化")
        chunks = split_text(resume_text)
        if not chunks:
            raise ValueError("No text extracted from resume")

        print("✅ 使用内置本地向量器，无需下载或访问 Hugging Face")
        embeddings = _encode_texts(chunks)
        collection = client.create_collection(COLLECTION_NAME)
        collection.add(
            documents=chunks,
            embeddings=embeddings,
            ids=[f"chunk-{i}" for i in range(len(chunks))],
        )
        print(f"✅ 已保存向量库到：{persist_dir}")

    return VectorStore(collection)
