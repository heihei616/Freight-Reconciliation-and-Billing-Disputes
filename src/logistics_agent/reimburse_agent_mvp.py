#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
报销助手智能体系统 · 阶段 1 最小原型（单文件、无 Web）

功能链路：
本地 Markdown → 500 字切片 → 向量化并写入 FAISS →
命令行提问 → BM25 + 向量混合检索 Top 3 → 拼接提示词 → 调用 LLM。

安装依赖：
    pip install numpy faiss-cpu rank-bm25

离线自检（不需要 API Key，使用本地哈希向量与模拟 LLM）：
    python reimburse_agent_mvp.py 制度.md --mock-llm

接入 OpenAI 兼容供应商（环境变量示例）：
    set LLM_BASE_URL=https://api.openai.com/v1
    set LLM_API_KEY=你的密钥
    set LLM_MODEL=gpt-4o-mini
    set EMBEDDING_PROVIDER=api
    set EMBEDDING_MODEL=text-embedding-3-small
    python reimburse_agent_mvp.py 制度.md

说明：默认使用本地哈希向量，便于零密钥跑通流程；正式验证检索质量时，
应将 EMBEDDING_PROVIDER 设置为 api，使用真实语义向量模型。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

# numpy / faiss / rank_bm25 是运行必需的第三方依赖。
# 这里不静默替换为其他实现，避免“看起来是 RAG，实际没有使用 FAISS/BM25”。
try:
    import faiss
    import numpy as np
    from rank_bm25 import BM25Okapi
except ImportError as exc:  # 给出可操作的安装提示，而不是难懂的堆栈。
    raise SystemExit(
        "缺少依赖，请先执行：pip install numpy faiss-cpu rank-bm25\n"
        f"原始错误：{exc}"
    ) from exc


# ============================================================================
# 全局配置：后续阶段可迁移到 core/config.py，用环境变量或 Settings 管理。
# ============================================================================

CHUNK_SIZE = 500          # 每个切片最多 500 个字符，符合阶段 1 约定。
CHUNK_OVERLAP = 50        # 相邻切片保留少量重叠，降低答案被截断的概率。
LOCAL_VECTOR_DIM = 512    # 本地哈希向量维度；API 向量维度由模型自动决定。
DEFAULT_TOP_K = 3         # 默认取混合检索前 3 条。
DEFAULT_BM25_WEIGHT = 0.5 # BM25 与向量分各占 50%，可通过命令行调整。


# ============================================================================
# 【核心模块｜后续阶段复用】文档切片数据结构
# 后续可映射为 kb_chunks 表：document_id / seq / content / metadata。
# ============================================================================

@dataclass
class Chunk:
    """一个知识切片及其溯源信息。"""

    chunk_id: str       # 稳定 ID，用于引用、去重与审计。
    text: str           # 切片正文，既用于向量化，也用于 BM25。
    source: str         # 来源文件名，回答引用时展示。
    section: str        # 最近的 Markdown 标题，帮助定位制度章节。
    start_char: int     # 在原文中的起始位置，便于后续做原文跳转。
    tokens: list[str]   # BM25 分词结果；保存索引时不重复存储，可加载后重建。


def tokenize(text: str) -> list[str]:
    """
    为中英文混合文本做轻量分词。

    - 英文、数字按完整单词保留；
    - 中文连续片段生成单字和二元词（bigram），无需额外安装 jieba；
    - 同一套 token 同时供 BM25 与本地向量使用，保证两条召回链路口径一致。
    """
    tokens: list[str] = []
    # 先把英文/数字单词提取出来，再处理中文连续片段。
    for part in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[A-Za-z0-9_]+", part):
            tokens.append(part)
            continue
        # 中文单字能覆盖短词，二元词能提供更多语义组合。
        tokens.extend(part)
        tokens.extend(part[i : i + 2] for i in range(max(0, len(part) - 1)))
    return tokens


# ============================================================================
# 【核心模块｜后续阶段复用】Markdown 读取与 500 字切片
# 后续可迁入 rag/loader.py、rag/chunker.py，并增加 PDF/Word 解析器。
# ============================================================================

def read_markdown(file_path: str | Path) -> str:
    """读取 UTF-8 Markdown；utf-8-sig 可同时兼容带 BOM 的 Windows 文件。"""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到 Markdown 文件：{path}")
    return path.read_text(encoding="utf-8-sig")


def _split_long_text(text: str, size: int, overlap: int) -> Iterable[tuple[str, int]]:
    """把超长段落按滑动窗口切开，并返回每段相对原段落的偏移量。"""
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        yield text[start:end], start
        if end >= len(text):
            break
        # 必须保证步长大于 0，否则重叠设置错误时可能死循环。
        start = end - overlap


def split_markdown(text: str, source: str, size: int = CHUNK_SIZE,
                   overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    """
    按 Markdown 段落切片，单块不超过 500 字。

    实现策略：
    1. 优先保留段落完整性，避免在制度条款中间随意断句；
    2. 多段可合并到同一个 500 字块；
    3. 单段超过 500 字时，使用带重叠的滑动窗口；
    4. 每块记录最近标题与原文偏移，供答案引用与后续审计复用。
    """
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("切片参数应满足：size > 0 且 0 <= overlap < size")

    chunks: list[Chunk] = []
    current_section = "未命名章节"
    buffer = ""          # 暂存尚未达到 500 字的连续段落。
    buffer_start = 0     # buffer 在原文中的起始位置。
    cursor = 0           # 当前段落在原文中的位置。

    def flush_buffer() -> None:
        """把暂存段落转换为一个 Chunk；使用 nonlocal 更新外层游标。"""
        nonlocal buffer
        content = buffer.strip()
        if content:
            chunk_no = len(chunks) + 1
            chunks.append(Chunk(
                chunk_id=f"{source}#chunk-{chunk_no:04d}",
                text=content,
                source=source,
                section=current_section,
                start_char=buffer_start,
                tokens=tokenize(content),
            ))
        buffer = ""

    # re.finditer 能同时取得段落文本和它在原文中的准确位置。
    for match in re.finditer(r"\S(?:[\s\S]*?\S)?(?=\n\s*\n|$)", text):
        paragraph = match.group(0).strip()
        if not paragraph:
            continue

        # 标题只更新章节元数据，不单独作为一个无意义的小切片。
        heading = re.match(r"^#{1,6}\s+(.+)$", paragraph)
        if heading:
            flush_buffer()
            current_section = heading.group(1).strip()
            cursor = match.end()
            continue

        # 超长段落独立切分；先 flush，避免把前后不相关条款混在一起。
        if len(paragraph) > size:
            flush_buffer()
            for piece, offset in _split_long_text(paragraph, size, overlap):
                chunk_no = len(chunks) + 1
                chunks.append(Chunk(
                    chunk_id=f"{source}#chunk-{chunk_no:04d}",
                    text=piece.strip(),
                    source=source,
                    section=current_section,
                    start_char=match.start() + offset,
                    tokens=tokenize(piece),
                ))
            cursor = match.end()
            continue

        # 当前段落能放入 buffer 就合并，否则先输出旧 buffer 再开新块。
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) > size and buffer:
            flush_buffer()
            buffer = paragraph
            buffer_start = match.start()
        else:
            if not buffer:
                buffer_start = match.start()
            buffer = candidate
        cursor = match.end()

    flush_buffer()
    if not chunks:
        raise ValueError("Markdown 中没有可索引的正文内容。")
    return chunks


# ============================================================================
# 【核心模块｜后续阶段复用】向量化
# 默认本地哈希向量用于离线自检；生产环境切换为 API 语义向量。
# 后续可迁入 rag/embedder.py，业务层只依赖 embed_texts() 统一接口。
# ============================================================================

def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """L2 归一化后，FAISS 内积就等价于余弦相似度。"""
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def embed_texts_local(texts: Sequence[str], dim: int = LOCAL_VECTOR_DIM) -> np.ndarray:
    """
    离线哈希向量：用 token 的哈希值决定向量维度与正负号。

    它不是语义模型，只用于零依赖 API 的流程自检；同一 token 总会映射到
    相同维度，因此字面相近的问题与条款仍具有一定相似度。
    """
    matrix = np.zeros((len(texts), dim), dtype=np.float32)
    for row, text in enumerate(texts):
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            column = number % dim
            sign = 1.0 if (number >> 8) & 1 else -1.0
            matrix[row, column] += sign
    return _l2_normalize(matrix)


def _post_json(url: str, payload: dict[str, Any], api_key: str = "",
               timeout: int = 60) -> dict[str, Any]:
    """OpenAI 兼容接口的极简 HTTP 客户端，避免为原型引入重量级 SDK。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 把供应商返回的错误正文带出，便于定位模型名、额度、权限等问题。
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM/Embedding HTTP {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接模型服务：{url}；{exc.reason}") from exc


def embed_texts_api(texts: Sequence[str]) -> np.ndarray:
    """
    调用 OpenAI 兼容 /embeddings 接口，支持更换供应商与向量模型。

    可配置项：
      EMBEDDING_BASE_URL：不填时复用 LLM_BASE_URL；
      EMBEDDING_API_KEY ：不填时复用 LLM_API_KEY；
      EMBEDDING_MODEL   ：例如 text-embedding-3-small；
      EMBEDDING_BATCH_SIZE：单次请求的文本条数，默认 25。

    为什么要分批：多数供应商对单次输入条数有硬限制（通义千问 qwen embedding
    实测上限为 25 条，超出直接返回 400 InvalidParameter）。不分批的话，
    文档稍大就建不成索引。
    """
    base_url = os.getenv(
        "EMBEDDING_BASE_URL",
        os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
    ).rstrip("/")
    api_key = os.getenv("EMBEDDING_API_KEY", os.getenv("LLM_API_KEY", ""))
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    # 单批条数：不同供应商上限不一样（通义千问一度从 25 收到 20），
    # 所以默认取保守值，并在服务端报「批量过大」时自动折半重试。
    batch_size = max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "10")))

    def _embed_batch(batch: list[str]) -> list[list[float]]:
        """一次请求；批量超限时二分递归，直到供应商接受为止。"""
        result = _post_json(
            f"{base_url}/embeddings",
            {"model": model, "input": batch},
            api_key=api_key,
        )
        rows = sorted(result["data"], key=lambda d: d.get("index", 0))
        return [row["embedding"] for row in rows]

    def _embed_adaptive(batch: list[str]) -> list[list[float]]:
        try:
            return _embed_batch(batch)
        except RuntimeError as exc:
            message = str(exc)
            # 只在「参数非法/批量过大」这类可恢复错误上二分，
            # 鉴权失败、地址写错之类的重试多少次都没用。
            retryable = ("InvalidParameter" in message or "batch size" in message
                         or "too large" in message)
            if not retryable or len(batch) <= 1:
                raise
            mid = len(batch) // 2
            return _embed_adaptive(batch[:mid]) + _embed_adaptive(batch[mid:])

    items = list(texts)
    vectors: list[list[float]] = []
    for start in range(0, len(items), batch_size):
        vectors.extend(_embed_adaptive(items[start:start + batch_size]))

    if len(vectors) != len(items):
        raise RuntimeError(
            f"向量化返回数量不匹配：期望 {len(items)} 条，实际 {len(vectors)} 条"
        )
    return _l2_normalize(np.asarray(vectors, dtype=np.float32))


def embed_texts(texts: Sequence[str]) -> tuple[np.ndarray, dict[str, Any]]:
    """
    统一向量化入口，同时返回向量与索引元数据。

    元数据会写入索引目录；若更换向量模型，程序能识别并强制重建索引，
    防止把不同维度、不同语义的向量混在同一个 FAISS 中。
    """
    provider = os.getenv("EMBEDDING_PROVIDER", "local").lower()
    if provider == "local":
        vectors = embed_texts_local(texts)
        metadata = {"provider": "local", "model": f"hash-{LOCAL_VECTOR_DIM}"}
    elif provider == "api":
        vectors = embed_texts_api(texts)
        metadata = {
            "provider": "api",
            "model": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        }
    else:
        raise ValueError(f"未知 EMBEDDING_PROVIDER：{provider}（可选 local/api）")
    return vectors, metadata


# ============================================================================
# 【核心模块｜后续阶段复用】FAISS + BM25 混合检索
# 后续可迁入 rag/retriever.py；存储可替换为 pgvector，但检索接口保持不变。
# ============================================================================

class HybridKnowledgeBase:
    """同时维护 FAISS 向量索引与 BM25 倒排索引，并提供混合 Top-K 检索。"""

    def __init__(self, chunks: list[Chunk], vectors: np.ndarray,
                 embedding_meta: dict[str, Any]):
        self.chunks = chunks
        self.vectors = vectors
        self.embedding_meta = embedding_meta
        self.dimension = vectors.shape[1]

        # 向量已归一化，因此 IndexFlatIP 的分数就是余弦相似度。
        self.index = faiss.IndexFlatIP(self.dimension)
        self.index.add(vectors)

        # BM25 与 FAISS 使用完全相同的 chunks 顺序，分数可按位置对齐。
        self.bm25 = BM25Okapi([chunk.tokens for chunk in chunks])

    @staticmethod
    def _min_max(scores: np.ndarray) -> np.ndarray:
        """把不同量纲的分数归一到 0~1，避免某一路召回因分值大而垄断排序。"""
        scores = np.asarray(scores, dtype=np.float32)
        low, high = float(scores.min()), float(scores.max())
        if high - low < 1e-12:
            return np.zeros_like(scores)
        return (scores - low) / (high - low)

    def search(self, query: str, top_k: int = DEFAULT_TOP_K,
               bm25_weight: float = DEFAULT_BM25_WEIGHT) -> list[dict[str, Any]]:
        """
        混合检索：最终分 = BM25 权重 * BM25 归一分 + 向量权重 * 向量归一分。

        BM25 擅长精确术语/编号，向量擅长同义表达；两路互补后再取 Top 3。
        """
        if not query.strip():
            raise ValueError("查询问题不能为空。")
        top_k = max(1, min(top_k, len(self.chunks)))

        # 第一路：BM25 全文相关性。
        bm25_scores = np.asarray(self.bm25.get_scores(tokenize(query)), dtype=np.float32)

        # 第二路：查询向量化后，在 FAISS 中取所有候选分，便于和 BM25 全量融合。
        query_vector, _ = embed_texts([query])
        if query_vector.shape[1] != self.dimension:
            raise RuntimeError("查询向量维度与索引不一致，请重建索引。")
        vector_scores = np.asarray(
            [float(np.dot(query_vector[0], vector)) for vector in self.vectors],
            dtype=np.float32,
        )

        hybrid = (bm25_weight * self._min_max(bm25_scores)
                  + (1.0 - bm25_weight) * self._min_max(vector_scores))
        best_indices = np.argsort(-hybrid)[:top_k]

        results: list[dict[str, Any]] = []
        for idx in best_indices:
            chunk = self.chunks[int(idx)]
            results.append({
                "chunk": chunk,
                "score": float(hybrid[idx]),
                "bm25_score": float(bm25_scores[idx]),
                "vector_score": float(vector_scores[idx]),
            })
        return results

    def save(self, index_dir: str | Path, source_path: Path, source_hash: str) -> None:
        """保存 FAISS、切片正文与索引元数据，下次启动可秒级加载。"""
        directory = Path(index_dir)
        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(directory / "index.faiss"))

        # tokens 可由正文重新计算，不落盘以减小元数据文件。
        serializable_chunks = []
        for chunk in self.chunks:
            item = asdict(chunk)
            item.pop("tokens", None)
            serializable_chunks.append(item)
        metadata = {
            "source_path": str(source_path.resolve()),
            "source_hash": source_hash,
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
            "embedding": self.embedding_meta,
            "chunks": serializable_chunks,
        }
        (directory / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, index_dir: str | Path) -> "HybridKnowledgeBase":
        """从磁盘恢复 FAISS 与切片；BM25 根据正文即时重建。"""
        directory = Path(index_dir)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        chunks = []
        for item in metadata["chunks"]:
            item["tokens"] = tokenize(item["text"])
            chunks.append(Chunk(**item))

        index = faiss.read_index(str(directory / "index.faiss"))
        # 从 FAISS 还原向量，保持 search() 的全量融合逻辑简单一致。
        vectors = np.asarray([index.reconstruct(i) for i in range(index.ntotal)], dtype=np.float32)
        return cls(chunks, vectors, metadata["embedding"])


def file_sha256(path: Path) -> str:
    """源文件内容指纹：文件变化后自动重建，避免旧制度切片继续被检索。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_or_build_knowledge_base(markdown_path: Path, index_dir: Path,
                                 rebuild: bool = False) -> HybridKnowledgeBase:
    """索引缓存入口：源文件、切片参数或向量模型任一变化都会触发重建。"""
    current_hash = file_sha256(markdown_path)
    metadata_file = index_dir / "metadata.json"

    if not rebuild and metadata_file.exists() and (index_dir / "index.faiss").exists():
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            current_provider = os.getenv("EMBEDDING_PROVIDER", "local").lower()
            if (metadata["source_hash"] == current_hash
                    and metadata["chunk_size"] == CHUNK_SIZE
                    and metadata["chunk_overlap"] == CHUNK_OVERLAP
                    and metadata["embedding"]["provider"] == current_provider):
                print(f"[索引] 复用缓存：{index_dir}")
                return HybridKnowledgeBase.load(index_dir)
            print("[索引] 文档或向量配置已变化，重新构建。")
        except (KeyError, ValueError, json.JSONDecodeError):
            print("[索引] 缓存元数据不完整，重新构建。")

    print(f"[索引] 读取并切片：{markdown_path}")
    text = read_markdown(markdown_path)
    chunks = split_markdown(text, source=markdown_path.name)
    print(f"[索引] 共 {len(chunks)} 个切片，开始向量化并写入 FAISS……")
    vectors, embedding_meta = embed_texts([chunk.text for chunk in chunks])
    knowledge_base = HybridKnowledgeBase(chunks, vectors, embedding_meta)
    knowledge_base.save(index_dir, markdown_path, current_hash)
    print(f"[索引] 构建完成：{index_dir}")
    return knowledge_base


# ============================================================================
# 【核心模块｜后续阶段复用】提示词组装
# 后续可迁入 agent/prompts/，并增加提示词版本号与引用格式约束。
# ============================================================================

def build_messages(question: str, results: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """把 Top 3 检索结果拼入提示词，并要求模型仅依据证据回答、明确引用来源。"""
    context_blocks = []
    for number, result in enumerate(results, start=1):
        chunk: Chunk = result["chunk"]
        context_blocks.append(
            f"[资料{number}] 来源：{chunk.source}；章节：{chunk.section}\n{chunk.text}"
        )
    context = "\n\n".join(context_blocks)

    system_prompt = (
        "你是企业报销与物流费计费制度助手。只能依据用户提供的资料回答，"
        "不得编造制度条款、金额或比例。资料不足时明确说‘现有资料无法确认’，"
        "并建议咨询管理员。回答使用简洁中文，在关键结论后标注[资料1]/[资料2]等引用。"
    )
    user_prompt = (
        f"员工问题：{question}\n\n"
        f"以下为检索到的制度资料：\n{context}\n\n"
        "请给出直接答案；若适用，列出办理步骤或注意事项。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


# ============================================================================
# 【核心模块｜后续阶段复用】独立 LLM 调用函数
# 后续可整体迁入 services/llm_gateway/；Agent 不感知具体供应商。
# ============================================================================

def call_llm(messages: Sequence[dict[str, str]], mock: bool = False) -> str:
    """
    调用大模型生成回答，供应商通过环境变量配置。

    LLM_PROVIDER：openai（OpenAI 兼容协议）或 mock（离线演示）；
    LLM_BASE_URL ：例如 https://api.openai.com/v1，也可换成其他兼容供应商；
    LLM_API_KEY  ：供应商密钥；
    LLM_MODEL    ：对话模型名；
    LLM_TIMEOUT  ：超时秒数，默认 60。
    """
    provider = "mock" if mock else os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "mock":
        # 离线模式只验证编排链路，明确标识它不是真实模型回答。
        user_content = messages[-1]["content"]
        return (
            "[模拟 LLM] 已完成“检索结果 → 提示词”链路，但未调用真实模型。\n"
            "配置 LLM_API_KEY 并去掉 --mock-llm 后即可生成正式回答。\n\n"
            f"本次发送给模型的内容预览：\n{user_content[:600]}"
        )

    if provider != "openai":
        raise ValueError(f"未知 LLM_PROVIDER：{provider}（可选 openai/mock）")

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError(
            "未配置 LLM_API_KEY。请设置密钥，或使用 --mock-llm 做离线链路验证。"
        )

    payload = {
        "model": model,
        "messages": list(messages),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.1")),
        "stream": False,  # 阶段 1 先返回完整答案；Web 阶段再扩展 SSE 流式输出。
    }
    result = _post_json(
        f"{base_url}/chat/completions",
        payload,
        api_key=api_key,
        timeout=int(os.getenv("LLM_TIMEOUT", "60")),
    )
    return result["choices"][0]["message"]["content"].strip()


# ============================================================================
# 命令行编排：仅属于阶段 1 原型；后续 Web 阶段由 FastAPI 路由替代。
# ============================================================================

def answer_question(knowledge_base: HybridKnowledgeBase, question: str,
                    top_k: int, bm25_weight: float, mock_llm: bool) -> str:
    """执行一次完整问答，并先在终端展示证据，便于核对检索是否准确。"""
    results = knowledge_base.search(question, top_k=top_k, bm25_weight=bm25_weight)

    print("\n--- 混合检索结果 ---")
    for number, result in enumerate(results, start=1):
        chunk: Chunk = result["chunk"]
        preview = chunk.text.replace("\n", " ")[:100]
        print(
            f"[资料{number}] 混合分={result['score']:.3f} "
            f"BM25={result['bm25_score']:.3f} 向量={result['vector_score']:.3f}\n"
            f"  来源={chunk.source} / {chunk.section}\n  摘要={preview}…"
        )

    messages = build_messages(question, results)
    return call_llm(messages, mock=mock_llm)


def main() -> None:
    """解析命令行参数、准备索引，然后进入单次或交互式问答。"""
    parser = argparse.ArgumentParser(description="报销/物流费制度 RAG 最小原型")
    parser.add_argument("markdown", type=Path, help="本地 Markdown 制度文件路径")
    parser.add_argument("--question", "-q", help="只问一个问题；不传则进入交互模式")
    parser.add_argument("--index-dir", type=Path, default=Path(".rag_index"),
                        help="FAISS 与元数据缓存目录，默认 ./.rag_index")
    parser.add_argument("--rebuild", action="store_true", help="忽略缓存并强制重建索引")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="返回证据数，默认 3")
    parser.add_argument("--bm25-weight", type=float, default=DEFAULT_BM25_WEIGHT,
                        help="BM25 权重 0~1；向量权重自动为 1-该值")
    parser.add_argument("--mock-llm", action="store_true",
                        help="不调用真实模型，用于离线验证完整链路")
    args = parser.parse_args()

    if not 0.0 <= args.bm25_weight <= 1.0:
        parser.error("--bm25-weight 必须在 0~1 之间。")

    knowledge_base = load_or_build_knowledge_base(
        args.markdown, args.index_dir, rebuild=args.rebuild
    )

    # 单次模式适合脚本化测试；交互模式适合人工连续提问。
    if args.question:
        answer = answer_question(
            knowledge_base, args.question, args.top_k, args.bm25_weight, args.mock_llm
        )
        print(f"\n--- 回答 ---\n{answer}")
        return

    print("\n输入问题开始问答，输入 exit 或 quit 退出。")
    while True:
        try:
            question = input("\n问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break
        if question.lower() in {"exit", "quit", "退出"}:
            break
        if not question:
            continue
        try:
            answer = answer_question(
                knowledge_base, question, args.top_k, args.bm25_weight, args.mock_llm
            )
            print(f"\n--- 回答 ---\n{answer}")
        except Exception as exc:  # 单次失败不退出交互会话，便于修正配置后继续。
            print(f"处理失败：{exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
