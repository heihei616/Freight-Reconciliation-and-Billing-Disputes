"""
rag_service.py —— 知识库服务：多文档管理 + 混合检索

职责
----
1. 把阶段 1 原型（reimburse_agent_mvp.py）的切片 / 向量化 / 混合检索能力
   包装成一个可在 Web 服务里长期存在的单例，**不重写任何检索逻辑**。
2. 支持“多文档 + 多格式”：docs/ 目录下的 .md / .markdown / .txt / .pdf
   会合并为同一个知识库，每个切片仍保留自己的 source 文件名，引用时能准确区分来源。
3. 支持上传新文档后重建索引（重建是 CPU 密集操作，必须丢线程池）。

为什么要处理 PDF
----------------
现实里的制度文件大多是 PDF。但检索需要的是**文字**，不是二进制，
所以 PDF 在入库前先抽文本（pypdf，纯文本层抽取，不做 OCR）：

  可复制文字的 PDF  → 抽出文本 → 和 .md 一样切片入库
  扫描件 / 图片型 PDF → 抽不出字 → 明确报错，让管理员去换文件或先做 OCR

宁可上传时报一句「疑似扫描件」，也不要让它悄悄入库、
最后在员工提问时给出「制度中未找到」——那才是真的坑人。

关键复用点（均来自阶段 1）
--------------------------
split_markdown          → 500 字切片 + 章节/偏移溯源
embed_texts             → 本地哈希向量或 API 向量，自动按环境变量选择
HybridKnowledgeBase     → FAISS + BM25 双路召回，分数归一化加权
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

# 直接复用阶段 1 的核心模块，避免两套实现产生检索口径差异。
from reimburse_agent_mvp import (
    DEFAULT_BM25_WEIGHT,
    DEFAULT_TOP_K,
    LOCAL_VECTOR_DIM,
    HybridKnowledgeBase,
    embed_texts,
    split_markdown,
)

BASE_DIR = Path(__file__).parent
# 文档目录：放这里的所有受支持格式文件都会被纳入知识库。
DOCS_DIR = BASE_DIR / "docs"
# 索引缓存目录。
# 注意：faiss 的 C++ 实现使用窄字符路径，在 Windows 上遇到中文路径会直接报
# "could not open ... for writing"。本项目位于「物流」中文目录下，因此索引
# 默认放到用户主目录（纯 ASCII），可用环境变量 RAG_INDEX_DIR 覆盖。
INDEX_DIR = Path(os.getenv("RAG_INDEX_DIR", Path.home() / ".logistics_rag_web_index"))

# 允许入库的扩展名。新增格式时只需要在这里登记 + 在 read_document_text 里加分支。
SUPPORTED_EXTS = (".md", ".markdown", ".txt", ".pdf")

# 上传上限 5MB：再大的制度文件已经不合常理，且解析耗时会明显拖慢上传响应。
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# 扫描件判定阈值：平均每页有效字符少于这个数，基本可以断定是图片型 PDF。
# 正常排版的中文 A4 页大约 700~1200 字，30 是很宽松的下限了。
MIN_CHARS_PER_PAGE = 30


# ---------------------------------------------------------------------------
# 文件扫描与文本抽取
# ---------------------------------------------------------------------------

def _list_documents() -> list[Path]:
    """列出参与索引的文件（忽略隐藏文件、Office 临时文件与不支持的类型）。"""
    if not DOCS_DIR.exists():
        return []
    return sorted(
        (
            p for p in DOCS_DIR.iterdir()
            if p.is_file()
            and not p.name.startswith((".", "~$"))
            and p.suffix.lower() in SUPPORTED_EXTS
        ),
        key=lambda p: p.name,
    )


def _decode_bytes(data: bytes) -> str:
    """
    纯文本解码：优先 UTF-8，退 GB18030。

    Windows 上导出的制度文件常是 GBK，直接按 UTF-8 读会整段乱码，
    而乱码既不报错又检索不到，比抛异常更难排查——所以这里显式尝试。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def extract_pdf_text(data: bytes, filename: str) -> tuple[str, int]:
    """
    抽取 PDF 的文字层，返回 (文本, 页数)。

    只做文字层抽取，不做 OCR。抽不到字就抛 ValueError，由调用方转成
    中文提示展示给管理员。
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 依赖缺失要说清楚，而不是抛个 ImportError 了事
        raise ValueError("服务端缺少 pypdf 依赖，无法解析 PDF，请让运维安装 pypdf") from exc

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            # 空密码尝试一次：不少制度 PDF 只设了权限密码，正文其实可读。
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError(f"《{filename}》已加密，无法读取，请提供未加密的版本") from exc

        pages = reader.pages
        page_count = len(pages)
        texts: list[str] = []
        for page in pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                # 单页解析失败不该让整份文档报废，跳过并继续
                texts.append("")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"《{filename}》无法解析：{exc}") from exc

    text = "\n\n".join(t.strip() for t in texts if t.strip())
    visible = sum(1 for ch in text if not ch.isspace())

    if page_count and visible < MIN_CHARS_PER_PAGE * page_count:
        raise ValueError(
            f"《{filename}》疑似扫描件：共 {page_count} 页，仅提取到 {visible} 个字符。"
            "本系统只识别 PDF 的文字层，请上传可复制文字的版本，或先做 OCR 后再入库。"
        )
    if not text.strip():
        raise ValueError(f"《{filename}》未提取到任何文字，请确认文件是否正常")
    return text, page_count


def read_document_text(path: Path) -> str:
    """
    按扩展名分派到对应的抽取逻辑，统一返回纯文本。

    统一把换行规范成 \\n：Windows 下保存的 .md/.txt 是 CRLF，
    而切片函数按 “空行分隔” 找段落（\\n\\s*\\n），CRLF 会让它一段都匹配不到，
    最终报“没有可索引的正文内容”——一个看上去毫无线索的错误。
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text, _ = extract_pdf_text(path.read_bytes(), path.name)
        return _normalize_newlines(text)
    if suffix in (".md", ".markdown", ".txt"):
        # 不用 read_markdown 的严格 UTF-8：Windows 下导出的 .md/.txt 常是 GBK，
        # 严格解码会直接抛异常，整个知识库都建不起来。
        return _normalize_newlines(_decode_bytes(path.read_bytes()))
    raise ValueError(f"不支持的文件类型：{suffix}")


def _normalize_newlines(text: str) -> str:
    """CRLF / CR 统一成 LF，避免影响按空行切段的逻辑。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _kind_label(suffix: str) -> str:
    """给界面用的格式标签。"""
    return {
        ".pdf": "PDF",
        ".md": "Markdown",
        ".markdown": "Markdown",
        ".txt": "纯文本",
    }.get(suffix.lower(), suffix.lstrip(".").upper())


def _human_size(size: int) -> str:
    """字节数转成人看的大小，避免界面上出现 10485760 这种数字。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _pdf_page_count(path: Path) -> int | None:
    """取 PDF 页数，失败返回 None（页数只是展示用信息，不值得为它报错）。"""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def _sha256_of_files(paths: list[Path]) -> str:
    """把多个文件的指纹合成一个整体指纹，任一文件变化都会触发重建。"""
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# 旧名字保留：外部代码（含阶段 1 脚本）可能还在引用 _list_markdown。
_list_markdown = _list_documents


class KnowledgeBaseService:
    """
    知识库单例。

    索引常驻内存，只在文档变化或收到上传请求时才重建；
    search() 是同步阻塞方法，调用方需用 `await asyncio.to_thread(...)` 包一层。
    """

    def __init__(self) -> None:
        self._kb: HybridKnowledgeBase | None = None
        self._build_lock = threading.Lock()   # 防止并发上传触发两次重建
        self._doc_hash: str = ""
        self._chunk_by_source: Counter[str] = Counter()  # 每个文件贡献了多少切片
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # 初始化与重建
    # ------------------------------------------------------------------

    def _build_sync(self, force: bool = False) -> None:
        """同步重建索引：扫描 docs/ → 逐文件抽文本切片 → 合并向量化 → 落盘缓存。"""
        if not self._build_lock.acquire(blocking=False):
            # 已有线程在重建，直接等它结束并复用其结果，避免重复计算。
            self._build_lock.acquire()
            self._build_lock.release()
            return
        try:
            DOCS_DIR.mkdir(parents=True, exist_ok=True)
            files = _list_documents()

            if not files:
                self._kb = None
                self._chunk_by_source = Counter()
                self.last_error = "docs 目录下没有任何可入库的文档"
                return

            current_hash = _sha256_of_files(files)
            meta_file = INDEX_DIR / "metadata.json"

            # 缓存命中：文档内容与向量模型都没变，直接读盘，秒级启动。
            if not force and meta_file.exists() and current_hash == self._doc_hash:
                self._kb = HybridKnowledgeBase.load(INDEX_DIR)
                self._refresh_chunk_counts()
                return

            if not force and meta_file.exists():
                try:
                    cached = json.loads(meta_file.read_text(encoding="utf-8"))
                    provider = os.getenv("EMBEDDING_PROVIDER", "local").lower()
                    # 必须同时比对 provider 与 model：只比 provider 会在换向量模型时
                    # 复用旧维度的 FAISS 索引，导致维度冲突或语义错配。
                    if provider == "local":
                        expect_model = f"hash-{LOCAL_VECTOR_DIM}"
                    else:
                        expect_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
                    cached_emb = cached.get("embedding", {})
                    if (cached.get("source_hash") == current_hash
                            and cached_emb.get("provider") == provider
                            and cached_emb.get("model") == expect_model):
                        self._kb = HybridKnowledgeBase.load(INDEX_DIR)
                        self._doc_hash = current_hash
                        self._refresh_chunk_counts()
                        self.last_error = None
                        return
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass  # 缓存损坏则静默重建

            # 多文档合并：每个切片保留自身 source，引用时可区分文件。
            chunks = []
            skipped: list[str] = []
            for path in files:
                try:
                    text = read_document_text(path)
                    # split_markdown 也在 try 内：只有标题、没有正文的文件会抛
                    # ValueError，同样属于「单个文件的问题」，不该让整库重建失败。
                    chunks.extend(split_markdown(text, source=path.name))
                except (ValueError, OSError) as exc:
                    # 单个坏文件不该让整个知识库起不来：记下来，其余照常入库。
                    skipped.append(str(exc))
                    print(f"[知识库] 跳过 {path.name}：{exc}")
                    continue

            if not chunks:
                self._kb = None
                self._chunk_by_source = Counter()
                self.last_error = skipped[0] if skipped else "文档内容为空，无法建立索引"
                return

            vectors, embedding_meta = embed_texts([c.text for c in chunks])
            kb = HybridKnowledgeBase(chunks, vectors, embedding_meta)
            # source_path 传目录，表示索引由多个文件聚合而成。
            kb.save(INDEX_DIR, DOCS_DIR, current_hash)

            self._kb = kb
            self._doc_hash = current_hash
            self._refresh_chunk_counts()
            self.last_error = "；".join(skipped) if skipped else None
        finally:
            self._build_lock.release()

    def _refresh_chunk_counts(self) -> None:
        """重建后统计每个文件贡献的切片数，供知识库页展示。"""
        if self._kb is None:
            self._chunk_by_source = Counter()
            return
        self._chunk_by_source = Counter(chunk.source for chunk in self._kb.chunks)

    async def ensure_ready(self) -> None:
        """服务启动时调用：没有索引就建，有就加载。"""
        await asyncio.to_thread(self._build_sync, False)

    async def rebuild(self) -> int:
        """强制重建索引，返回切片总数，供上传接口反馈。"""
        await asyncio.to_thread(self._build_sync, True)
        return len(self._kb.chunks) if self._kb else 0

    async def reindex(self) -> dict[str, Any]:
        """后台「重建索引」按钮：强制全量重建，返回重建后的概况。"""
        await asyncio.to_thread(self._build_sync, True)
        return self.stats()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._kb is not None

    def stats(self) -> dict[str, Any]:
        """健康检查/总览用：切片数、文档数、向量来源。"""
        files = _list_documents()
        return {
            "ready": self.ready,
            "documents": [p.name for p in files],
            "document_count": len(files),
            "chunk_count": len(self._kb.chunks) if self._kb else 0,
            "embedding": self._kb.embedding_meta if self._kb else None,
            "error": self.last_error,
        }

    def documents_detail(self) -> list[dict[str, Any]]:
        """
        知识库管理页的列表数据：每个文件的格式、大小、时间、切片数。

        切片数来自内存索引（按 source 聚合），所以能反映「这份文件真正被切成了几段」，
        而不是按文件大小估算。
        """
        items: list[dict[str, Any]] = []
        for path in _list_documents():
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({
                "name": path.name,
                "kind": _kind_label(path.suffix),
                "size": stat.st_size,
                "size_text": _human_size(stat.st_size),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "chunk_count": self._chunk_by_source.get(path.name, 0),
                "pages": _pdf_page_count(path),
                "indexed": self._chunk_by_source.get(path.name, 0) > 0,
            })
        return items

    def search_sync(self, question: str, top_k: int = DEFAULT_TOP_K,
                    bm25_weight: float = DEFAULT_BM25_WEIGHT) -> list[dict[str, Any]]:
        """同步检索，供线程池调用。"""
        if self._kb is None:
            raise RuntimeError("知识库尚未就绪")
        return self._kb.search(question, top_k=top_k, bm25_weight=bm25_weight)

    async def search(self, question: str, top_k: int = DEFAULT_TOP_K,
                     bm25_weight: float = DEFAULT_BM25_WEIGHT) -> list[dict[str, Any]]:
        """异步检索：FAISS/BM25 都是 CPU 操作，必须丢到线程池，否则阻塞 SSE。"""
        return await asyncio.to_thread(self.search_sync, question, top_k, bm25_weight)

    # ------------------------------------------------------------------
    # 文档上传 / 删除
    # ------------------------------------------------------------------

    async def save_document(self, filename: str, content: bytes) -> dict[str, Any]:
        """
        保存上传的文档并重建索引。

        流程刻意分成「先校验、后落盘、再重建」：
        1. 扩展名与大小先挡掉（省得白解析一遍）；
        2. PDF 先在内存里试抽一次文本——扫描件在这一步就被拦住，
           不会先写进 docs/ 再在重建时失败，留下一个永远建不好索引的坏文件；
        3. 落盘后统一重建，返回本次的切片数。
        """
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_EXTS:
            raise ValueError(
                f"不支持的文件类型：{suffix or '（无扩展名）'}，"
                "仅接受 .md / .markdown / .txt / .pdf"
            )
        if not content.strip():
            raise ValueError("文件内容为空")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("上传文件过大，请控制在 5MB 以内")

        # PDF 预检：抽不出文字 / 是扫描件，直接抛中文错误，不落盘。
        if suffix == ".pdf":
            text, _ = extract_pdf_text(content, safe_name)
            text = _normalize_newlines(text)
        else:
            text = _normalize_newlines(_decode_bytes(content))

        # 正文预检：能读出来但切不出段落（例如整份文件只有一个标题），
        # 入库后对检索毫无贡献，不如当场说清楚。
        try:
            split_markdown(text, source=safe_name)
        except ValueError as exc:
            raise ValueError(
                f"《{safe_name}》没有可索引的正文内容：{exc}"
                " 请确认文件里是制度条文，而不是只有标题、目录或扫描图片。"
            ) from exc

        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        target = DOCS_DIR / safe_name
        replaced = target.exists()
        target.write_bytes(content)

        chunk_count = await self.rebuild()
        return {
            "filename": safe_name,
            "size": len(content),
            "size_text": _human_size(len(content)),
            "kind": _kind_label(suffix),
            "replaced": replaced,
            "chunk_count": chunk_count,
            "document_count": len(_list_documents()),
        }

    async def delete_document(self, filename: str) -> dict[str, Any]:
        """删除文档并重建索引；用于清理上传错的资料。"""
        safe_name = Path(filename).name
        target = DOCS_DIR / safe_name
        if not target.exists():
            raise FileNotFoundError(f"文档不存在：{safe_name}")
        target.unlink()
        chunk_count = await self.rebuild()
        return {"filename": safe_name, "chunk_count": chunk_count}


def to_citations(results: list[dict[str, Any]], snippet_len: int = 120) -> list[dict[str, Any]]:
    """
    把检索结果转成前端引用卡片。

    字段与接口设计一致：文件、章节、片段、分数、原文偏移。
    片段截断到 120 字，避免前端卡片过长。
    """
    citations: list[dict[str, Any]] = []
    for index, result in enumerate(results, start=1):
        chunk = result["chunk"]
        snippet = chunk.text.replace("\n", " ").strip()
        if len(snippet) > snippet_len:
            snippet = snippet[:snippet_len] + "…"
        citations.append({
            "index": index,
            "file": chunk.source,
            "section": chunk.section or "（无章节）",
            "snippet": snippet,
            "score": round(float(result["score"]), 4),
            "bm25": round(float(result["bm25_score"]), 2),
            "vector": round(float(result["vector_score"]), 4),
            "offset": int(chunk.start_char),
        })
    return citations


# 全局单例：整个进程共用一份内存索引。
kb_service = KnowledgeBaseService()


def seed_default_document(source: Path) -> None:
    """
    首次启动时把阶段 1 使用的 discovery-report.md 复制进 docs/，
    这样界面一打开就有内容可问，不需要先上传。
    """
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    target = DOCS_DIR / source.name
    if source.exists() and not target.exists():
        shutil.copy2(source, target)
