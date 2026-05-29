"""PDF 性能基准测试

针对 "证据1：转账记录_29.pdf" 文件，测试：
1. PDF 加载性能
2. 分片（chunking）性能 + 内存占用
3. 向量化（embedding）性能 + 内存/存储占用
4. Milvus 存储写入性能 + 存储空间估算
5. 三种检索模式（direct/hybrid/agent）的性能和准度
"""

import asyncio
import json
import os
import sys
import time
import statistics
import tracemalloc
from pathlib import Path

# 确保可以导入 backend 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

from dotenv import load_dotenv
load_dotenv(".env")

from app.config import get_settings
from app.pipeline.loaders.pdf_loader import PdfLoader
from app.pipeline.chunker import HierarchicalChunker
from app.pipeline.embedder import PipelineEmbedder
from app.models.embedding.remote import RemoteEmbedder
from app.models.rerank.remote import RemoteReranker
from app.retrieval.vector import VectorRetriever
from app.retrieval.sparse import SparseRetriever
from app.retrieval.hybrid import HybridRetriever
from app.storage.milvus import MilvusClient
from app.storage.database import async_session, init_db, engine
from app.schema.db import Base, KnowledgeBase, Document, Chunk

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
import resource


# ============================================================
# 配置
# ============================================================

PDF_PATH = str(Path(__file__).resolve().parent.parent.parent / "民事裁定书(2023)冀0984民初125号_3.pdf")
BENCHMARK_KB_ID = "benchmark-perf-test"
BENCHMARK_DOC_ID = "benchmark-doc-001"

# 检索测试查询（针对民事裁定书类文档的典型查询）
TEST_QUERIES = [
    "原告是谁",
    "被告的诉讼请求是什么",
    "法院的裁定结果",
    "案件受理费多少",
    "本案的事实与理由",
]


# ============================================================
# 工具函数
# ============================================================

def format_time(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds:.2f} s"


def format_size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count} B"
    elif bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    return f"{bytes_count / (1024 * 1024):.1f} MB"


def get_process_memory_mb() -> float:
    """获取当前进程 RSS 内存占用（MB）"""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # macOS 返回字节，Linux 返回 KB
    if sys.platform == "darwin":
        return usage.ru_maxrss / 1024 / 1024
    return usage.ru_maxrss / 1024


def estimate_vector_storage(dense_vectors: list, sparse_vectors: list) -> dict:
    """估算向量数据的存储空间占用"""
    # 稠密向量：每个 float32 = 4 bytes，dim=1024
    dense_count = len(dense_vectors)
    dense_dim = len(dense_vectors[0]) if dense_vectors else 0
    dense_bytes = dense_count * dense_dim * 4  # float32

    # 稀疏向量：每个非零项 = 4 bytes (int32 key) + 4 bytes (float32 value)
    sparse_total_entries = sum(len(sv) for sv in sparse_vectors)
    sparse_bytes = sparse_total_entries * 8  # int32 + float32

    # 文本内容存储（UTF-8 编码估算）
    return {
        "dense_bytes": dense_bytes,
        "sparse_bytes": sparse_bytes,
        "sparse_avg_entries": sparse_total_entries / len(sparse_vectors) if sparse_vectors else 0,
        "total_vector_bytes": dense_bytes + sparse_bytes,
    }


def print_section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_metric(label: str, value: str, indent: int = 2):
    print(f"{' ' * indent}{label}: {value}")


# ============================================================
# 测试步骤
# ============================================================

async def benchmark_pdf_load() -> dict:
    """测试 PDF 加载性能"""
    print_section("1. PDF 加载性能")

    loader = PdfLoader()
    file_size = os.path.getsize(PDF_PATH)
    print_metric("文件路径", PDF_PATH)
    print_metric("文件大小", format_size(file_size))

    # 多次加载取平均
    times = []
    result = None
    for i in range(3):
        start = time.perf_counter()
        result = loader.load(PDF_PATH)
        elapsed = time.perf_counter() - start
        times.append(elapsed)

    avg_time = statistics.mean(times)
    print_metric("加载次数", "3 次取平均")
    print_metric("平均耗时", format_time(avg_time))
    print_metric("最快", format_time(min(times)))
    print_metric("最慢", format_time(max(times)))
    print_metric("提取文本长度", f"{len(result.content)} 字符")
    print_metric("页数", str(result.metadata.get("page_count", "未知")))
    print_metric("吞吐量", f"{file_size / avg_time / 1024 / 1024:.1f} MB/s")

    return {
        "content": result.content,
        "metadata": result.metadata,
        "load_time": avg_time,
        "file_size": file_size,
        "text_length": len(result.content),
        "page_count": result.metadata.get("page_count", 0),
    }


def benchmark_chunking(content: str) -> dict:
    """测试分片性能 + 内存占用"""
    print_section("2. 分片（Chunking）性能")

    settings = get_settings()
    chunker = HierarchicalChunker(
        parent_size=settings.parent_chunk_size,
        child_size=settings.child_chunk_size,
        overlap=settings.chunk_overlap,
    )

    print_metric("父块大小", f"{settings.parent_chunk_size} 字符")
    print_metric("子块大小", f"{settings.child_chunk_size} 字符")
    print_metric("重叠", f"{settings.chunk_overlap} 字符")
    print_metric("输入文本长度", f"{len(content)} 字符")

    # 多次分片取平均，同时追踪内存
    times = []
    result = None
    tracemalloc.start()
    mem_before = tracemalloc.get_traced_memory()[0]

    for i in range(5):
        start = time.perf_counter()
        result = chunker.chunk(content)
        elapsed = time.perf_counter() - start
        times.append(elapsed)

    mem_after = tracemalloc.get_traced_memory()[0]
    mem_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    avg_time = statistics.mean(times)
    parent_sizes = [len(p) for p in result.parent_chunks]
    child_sizes = [len(c) for c in result.child_chunks]

    # 计算分片结果的内存占用
    parent_text_bytes = sum(len(p.encode("utf-8")) for p in result.parent_chunks)
    child_text_bytes = sum(len(c.encode("utf-8")) for c in result.child_chunks)
    total_chunk_text_bytes = parent_text_bytes + child_text_bytes
    input_text_bytes = len(content.encode("utf-8"))

    print_metric("分片次数", "5 次取平均")
    print_metric("平均耗时", format_time(avg_time))
    print_metric("父块数量", str(len(result.parent_chunks)))
    print_metric("子块数量", str(len(result.child_chunks)))
    print_metric("父块平均大小", f"{statistics.mean(parent_sizes):.0f} 字符" if parent_sizes else "N/A")
    print_metric("子块平均大小", f"{statistics.mean(child_sizes):.0f} 字符" if child_sizes else "N/A")
    print_metric("子块大小范围", f"{min(child_sizes)} ~ {max(child_sizes)} 字符" if child_sizes else "N/A")
    print_metric("分片速率", f"{len(content) / avg_time / 1000:.0f} K字符/s")

    print(f"\n  [内存与存储]")
    print_metric("原始文本大小 (UTF-8)", format_size(input_text_bytes))
    print_metric("父块文本总大小", format_size(parent_text_bytes))
    print_metric("子块文本总大小", format_size(child_text_bytes))
    print_metric("分片后总文本大小", format_size(total_chunk_text_bytes))
    print_metric("膨胀率", f"{total_chunk_text_bytes / input_text_bytes:.2f}x（因 overlap 和父子冗余）")
    print_metric("分片过程内存增量", format_size(mem_after - mem_before))
    print_metric("分片过程内存峰值", format_size(mem_peak))

    return {
        "chunk_result": result,
        "chunk_time": avg_time,
        "parent_count": len(result.parent_chunks),
        "child_count": len(result.child_chunks),
        "child_texts": result.child_chunks,
        "parent_text_bytes": parent_text_bytes,
        "child_text_bytes": child_text_bytes,
        "total_chunk_text_bytes": total_chunk_text_bytes,
        "input_text_bytes": input_text_bytes,
        "expansion_ratio": total_chunk_text_bytes / input_text_bytes,
    }


async def benchmark_embedding(child_texts: list[str]) -> dict:
    """测试向量化性能 + 内存/存储占用"""
    print_section("3. 向量化（Embedding）性能")

    settings = get_settings()
    print_metric("模型", settings.embed_model)
    print_metric("服务地址", settings.embed_base_url or "未配置")
    print_metric("待向量化文本数", str(len(child_texts)))

    # 记录模型加载前的内存
    mem_before_model = get_process_memory_mb()

    # 初始化模型（首次加载会较慢）
    print("\n  正在连接远程 Embedding 服务...")
    model_start = time.perf_counter()
    embedder = RemoteEmbedder(
        base_url=settings.embed_base_url,
        model=settings.embed_model,
        api_key=settings.embed_api_key,
        sparse_enabled=settings.embed_sparse_enabled,
    )
    model_load_time = time.perf_counter() - model_start

    mem_after_model = get_process_memory_mb()
    print_metric("模型加载耗时", format_time(model_load_time))
    print_metric("模型内存占用", f"{mem_after_model - mem_before_model:.1f} MB")

    pipeline_embedder = PipelineEmbedder(embed_provider=embedder, batch_size=32)

    # 向量化，追踪内存
    print("\n  正在生成向量...")
    tracemalloc.start()
    mem_before_embed = get_process_memory_mb()

    start = time.perf_counter()
    embed_result = await pipeline_embedder.embed(child_texts)
    embed_time = time.perf_counter() - start

    mem_after_embed = get_process_memory_mb()
    _, mem_peak_embed = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 计算向量存储空间
    storage_info = estimate_vector_storage(embed_result.dense_vectors, embed_result.sparse_vectors)

    print_metric("向量化总耗时", format_time(embed_time))
    print_metric("稠密向量维度", str(len(embed_result.dense_vectors[0])) if embed_result.dense_vectors else "N/A")
    print_metric("稀疏向量平均非零项", f"{storage_info['sparse_avg_entries']:.0f}")
    print_metric("每块平均耗时", format_time(embed_time / len(child_texts)))
    print_metric("吞吐量", f"{len(child_texts) / embed_time:.1f} chunks/s")

    print(f"\n  [内存与存储]")
    print_metric("向量化过程内存增量 (RSS)", f"{mem_after_embed - mem_before_embed:.1f} MB")
    print_metric("向量化过程内存峰值 (tracemalloc)", format_size(mem_peak_embed))
    print_metric("稠密向量存储空间", format_size(storage_info["dense_bytes"]))
    print_metric("稀疏向量存储空间", format_size(storage_info["sparse_bytes"]))
    print_metric("向量总存储空间", format_size(storage_info["total_vector_bytes"]))
    print_metric("每块平均向量大小", format_size(storage_info["total_vector_bytes"] // len(child_texts)))
    print_metric("当前进程总内存 (RSS)", f"{mem_after_embed:.1f} MB")

    return {
        "embed_result": embed_result,
        "embed_time": embed_time,
        "model_load_time": model_load_time,
        "embedder": embedder,
        "model_memory_mb": mem_after_model - mem_before_model,
        "embed_memory_mb": mem_after_embed - mem_before_embed,
        "storage_info": storage_info,
    }


def _load_collection_sync(milvus_client: MilvusClient, kb_id: str):
    """显式 load collection 确保索引就绪"""
    from pymilvus import Collection, connections
    milvus_client._connect()
    name = milvus_client._collection_name(kb_id)
    collection = Collection(name=name, using=milvus_client._alias)
    collection.load()


async def benchmark_storage(
    child_texts: list[str],
    embed_result,
    chunk_result,
) -> dict:
    """测试 Milvus 存储写入性能"""
    print_section("4. Milvus 存储写入性能")

    settings = get_settings()
    milvus = MilvusClient(host=settings.milvus_host, port=settings.milvus_port)

    print_metric("Milvus 地址", f"{settings.milvus_host}:{settings.milvus_port}")
    print_metric("写入数据量", f"{len(child_texts)} 条向量")

    # 清理旧的测试 collection
    if await milvus.has_collection(BENCHMARK_KB_ID):
        await milvus.drop_collection(BENCHMARK_KB_ID)
        # 等待 Milvus 完成 drop
        await asyncio.sleep(1)

    # 创建 collection
    start = time.perf_counter()
    await milvus.create_collection(BENCHMARK_KB_ID)
    create_time = time.perf_counter() - start
    print_metric("创建 Collection 耗时", format_time(create_time))

    # 准备数据
    parent_ids = [str(uuid.uuid4()) for _ in range(len(chunk_result.parent_chunks))]
    milvus_data = []
    for idx, child_text in enumerate(child_texts):
        parent_idx = None
        for pidx, children in chunk_result.parent_child_map.items():
            if idx in children:
                parent_idx = pidx
                break
        parent_id = parent_ids[parent_idx] if parent_idx is not None else ""

        milvus_data.append({
            "chunk_id": str(uuid.uuid4()),
            "doc_id": BENCHMARK_DOC_ID,
            "content": child_text[:65535],
            "dense_vector": embed_result.dense_vectors[idx],
            "sparse_vector": embed_result.sparse_vectors[idx],
            "parent_id": parent_id,
            "chunk_index": idx,
        })

    # 写入
    start = time.perf_counter()
    count = await milvus.insert(BENCHMARK_KB_ID, milvus_data)
    insert_time = time.perf_counter() - start

    # 显式 load collection，确保索引就绪可供检索
    await asyncio.to_thread(_load_collection_sync, milvus, BENCHMARK_KB_ID)

    # 估算 Milvus 中该 collection 的存储占用
    # 每条记录：chunk_id(64B) + doc_id(64B) + content(avg) + dense(4096B) + sparse(avg) + parent_id(64B) + chunk_index(8B)
    avg_content_bytes = statistics.mean(len(item["content"].encode("utf-8")) for item in milvus_data)
    avg_sparse_entries = statistics.mean(len(item["sparse_vector"]) for item in milvus_data)
    per_record_bytes = 64 + 64 + avg_content_bytes + (1024 * 4) + (avg_sparse_entries * 8) + 64 + 8
    total_milvus_bytes = int(per_record_bytes * len(milvus_data))

    print_metric("写入耗时", format_time(insert_time))
    print_metric("写入条数", str(count))
    print_metric("写入速率", f"{count / insert_time:.0f} 条/s")

    print(f"\n  [存储空间估算]")
    print_metric("每条记录平均大小", format_size(int(per_record_bytes)))
    print_metric("  - 稠密向量", format_size(1024 * 4))
    print_metric("  - 稀疏向量 (平均)", format_size(int(avg_sparse_entries * 8)))
    print_metric("  - 文本内容 (平均)", format_size(int(avg_content_bytes)))
    print_metric("  - 元数据字段", format_size(64 + 64 + 64 + 8))
    print_metric("Milvus 总存储估算", format_size(total_milvus_bytes))
    print_metric("存储膨胀率 (vs 原始PDF)", f"{total_milvus_bytes / os.path.getsize(PDF_PATH):.2f}x")

    # 同时写入 SQLite 元数据（用于后续检索测试的父块扩展）
    await init_db()
    async with async_session() as session:
        # 清理旧数据
        await session.execute(delete(Chunk).where(Chunk.kb_id == BENCHMARK_KB_ID))
        await session.execute(delete(Document).where(Document.kb_id == BENCHMARK_KB_ID))
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == BENCHMARK_KB_ID))
        await session.commit()

        # 创建知识库
        kb = KnowledgeBase(id=BENCHMARK_KB_ID, name="性能测试知识库")
        session.add(kb)

        # 创建文档
        doc = Document(
            id=BENCHMARK_DOC_ID,
            kb_id=BENCHMARK_KB_ID,
            filename="民事裁定书(2023)冀0984民初125号_3.pdf",
            file_type="pdf",
            file_size=os.path.getsize(PDF_PATH),
            status="completed",
            chunk_count=len(child_texts),
        )
        session.add(doc)

        # 写入父块
        for pidx, content in enumerate(chunk_result.parent_chunks):
            parent_chunk = Chunk(
                id=parent_ids[pidx],
                doc_id=BENCHMARK_DOC_ID,
                kb_id=BENCHMARK_KB_ID,
                parent_id=None,
                content=content,
                chunk_index=pidx,
            )
            session.add(parent_chunk)

        # 写入子块
        for item in milvus_data:
            child_chunk = Chunk(
                id=item["chunk_id"],
                doc_id=BENCHMARK_DOC_ID,
                kb_id=BENCHMARK_KB_ID,
                parent_id=item["parent_id"] or None,
                content=item["content"],
                chunk_index=item["chunk_index"],
            )
            session.add(child_chunk)

        await session.commit()

    return {
        "milvus": milvus,
        "create_time": create_time,
        "insert_time": insert_time,
        "insert_count": count,
        "milvus_data": milvus_data,
        "total_milvus_bytes": total_milvus_bytes,
        "per_record_bytes": per_record_bytes,
    }


async def benchmark_retrieval(embedder, milvus: MilvusClient) -> dict:
    """测试三种检索模式的性能和准度"""
    print_section("5. 检索性能测试")

    settings = get_settings()
    reranker = RemoteReranker(
        base_url=settings.rerank_base_url,
        model=settings.rerank_model,
        api_key=settings.rerank_api_key,
    )

    # 构建检索器
    vector_retriever = VectorRetriever(embedder, milvus)
    sparse_retriever = SparseRetriever(embedder, milvus)
    hybrid_retriever = HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=reranker,
        db_session_factory=async_session,
    )

    top_k = 5
    results_all = {}

    # --- 5.1 Direct 模式（纯稠密向量检索）---
    print(f"\n  --- 5.1 Direct 模式（纯稠密向量） ---")
    direct_times = []
    direct_results = {}
    for query in TEST_QUERIES:
        start = time.perf_counter()
        results = await vector_retriever.search(query, BENCHMARK_KB_ID, top_k=top_k)
        elapsed = time.perf_counter() - start
        direct_times.append(elapsed)
        direct_results[query] = results

    avg_direct = statistics.mean(direct_times)
    print_metric("查询数", str(len(TEST_QUERIES)))
    print_metric("平均延迟", format_time(avg_direct))
    print_metric("最快", format_time(min(direct_times)))
    print_metric("最慢", format_time(max(direct_times)))
    print_metric("P50", format_time(sorted(direct_times)[len(direct_times) // 2]))

    # --- 5.2 Sparse 模式（纯稀疏向量检索）---
    print(f"\n  --- 5.2 Sparse 模式（纯稀疏向量） ---")
    sparse_times = []
    sparse_results = {}
    for query in TEST_QUERIES:
        start = time.perf_counter()
        results = await sparse_retriever.search(query, BENCHMARK_KB_ID, top_k=top_k)
        elapsed = time.perf_counter() - start
        sparse_times.append(elapsed)
        sparse_results[query] = results

    avg_sparse = statistics.mean(sparse_times)
    print_metric("查询数", str(len(TEST_QUERIES)))
    print_metric("平均延迟", format_time(avg_sparse))
    print_metric("最快", format_time(min(sparse_times)))
    print_metric("最慢", format_time(max(sparse_times)))
    print_metric("P50", format_time(sorted(sparse_times)[len(sparse_times) // 2]))

    # --- 5.3 Hybrid 模式（稠密+稀疏+RRF+Rerank+父块扩展）---
    print(f"\n  --- 5.3 Hybrid 模式（稠密+稀疏+RRF+Rerank） ---")
    hybrid_times = []
    hybrid_results = {}
    for query in TEST_QUERIES:
        start = time.perf_counter()
        results = await hybrid_retriever.search(query, BENCHMARK_KB_ID, top_k=top_k)
        elapsed = time.perf_counter() - start
        hybrid_times.append(elapsed)
        hybrid_results[query] = results

    avg_hybrid = statistics.mean(hybrid_times)
    print_metric("查询数", str(len(TEST_QUERIES)))
    print_metric("平均延迟", format_time(avg_hybrid))
    print_metric("最快", format_time(min(hybrid_times)))
    print_metric("最慢", format_time(max(hybrid_times)))
    print_metric("P50", format_time(sorted(hybrid_times)[len(hybrid_times) // 2]))

    results_all = {
        "direct": {"times": direct_times, "results": direct_results, "avg": avg_direct},
        "sparse": {"times": sparse_times, "results": sparse_results, "avg": avg_sparse},
        "hybrid": {"times": hybrid_times, "results": hybrid_results, "avg": avg_hybrid},
    }

    return results_all


def print_retrieval_quality(retrieval_data: dict):
    """打印检索质量分析"""
    print_section("6. 检索质量分析")

    for mode_name, mode_data in retrieval_data.items():
        print(f"\n  --- {mode_name.upper()} 模式 ---")
        for query, results in mode_data["results"].items():
            print(f"\n    查询: \"{query}\"")
            print(f"    返回 {len(results)} 条结果:")
            for i, r in enumerate(results[:3]):  # 只显示前3条
                content_preview = r.content[:80].replace("\n", " ")
                print(f"      [{i+1}] score={r.score:.4f} | {content_preview}...")


def print_summary(load_data: dict, chunk_data: dict, embed_data: dict, storage_data: dict, retrieval_data: dict):
    """打印最终汇总报告"""
    print_section("性能测试汇总报告")

    total_pipeline_time = (
        load_data['load_time'] + chunk_data['chunk_time'] +
        embed_data['embed_time'] + storage_data['insert_time']
    )

    print(f"""
  文件: 民事裁定书(2023)冀0984民初125号_3.pdf
  大小: {format_size(load_data['file_size'])}
  页数: {load_data['page_count']}
  文本长度: {load_data['text_length']} 字符

  ┌──────────────────────────────────────────────────────────────┐
  │ 阶段              │ 耗时          │ 备注                     │
  ├──────────────────────────────────────────────────────────────┤
  │ PDF 加载          │ {format_time(load_data['load_time']):13s} │ pymupdf 提取文本         │
  │ 分片              │ {format_time(chunk_data['chunk_time']):13s} │ {chunk_data['parent_count']}父块/{chunk_data['child_count']}子块            │
  │ 向量化            │ {format_time(embed_data['embed_time']):13s} │ bge-m3 (remote)           │
  │ Milvus 写入       │ {format_time(storage_data['insert_time']):13s} │ {storage_data['insert_count']} 条向量              │
  ├──────────────────────────────────────────────────────────────┤
  │ 端到端入库总耗时  │ {format_time(total_pipeline_time):13s} │ load→chunk→embed→store    │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │ 内存与存储        │ 数值          │ 说明                     │
  ├──────────────────────────────────────────────────────────────┤
  │ 原始 PDF 文件     │ {format_size(load_data['file_size']):13s} │ 磁盘文件大小             │
  │ 提取文本 (UTF-8)  │ {format_size(chunk_data['input_text_bytes']):13s} │ pymupdf 提取后           │
  │ 分片后文本总量    │ {format_size(chunk_data['total_chunk_text_bytes']):13s} │ 膨胀率 {chunk_data['expansion_ratio']:.2f}x           │
  │ 向量存储空间      │ {format_size(embed_data['storage_info']['total_vector_bytes']):13s} │ 稠密+稀疏向量            │
  │ Milvus 总存储     │ {format_size(storage_data['total_milvus_bytes']):13s} │ 向量+文本+元数据         │
  │ Embedding 模型    │ {embed_data['model_memory_mb']:.0f} MB{' ' * 8}│ 模型加载内存占用         │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │ 检索模式          │ 平均延迟      │ 特点                     │
  ├──────────────────────────────────────────────────────────────┤
  │ Direct (稠密)     │ {format_time(retrieval_data['direct']['avg']):13s} │ 语义相似度               │
  │ Sparse (稀疏)     │ {format_time(retrieval_data['sparse']['avg']):13s} │ 关键词匹配               │
  │ Hybrid (混合)     │ {format_time(retrieval_data['hybrid']['avg']):13s} │ RRF+Rerank+父块扩展      │
  └──────────────────────────────────────────────────────────────┘
""")


# ============================================================
# 主入口
# ============================================================

async def main():
    print("\n" + "=" * 60)
    print("  Artoo RAG 性能基准测试")
    print(f"  目标文件: 民事裁定书(2023)冀0984民初125号_3.pdf")
    print("=" * 60)

    # 1. PDF 加载
    load_data = await benchmark_pdf_load()

    # 2. 分片
    chunk_data = benchmark_chunking(load_data["content"])

    # 3. 向量化
    embed_data = await benchmark_embedding(chunk_data["child_texts"])

    # 4. 存储
    storage_data = await benchmark_storage(
        chunk_data["child_texts"],
        embed_data["embed_result"],
        chunk_data["chunk_result"],
    )

    # 5. 检索性能
    retrieval_data = await benchmark_retrieval(
        embed_data["embedder"],
        storage_data["milvus"],
    )

    # 6. 检索质量
    print_retrieval_quality(retrieval_data)

    # 7. 汇总
    print_summary(load_data, chunk_data, embed_data, storage_data, retrieval_data)

    # 清理测试数据
    print("\n  清理测试数据...")
    await storage_data["milvus"].drop_collection(BENCHMARK_KB_ID)
    async with async_session() as session:
        await session.execute(delete(Chunk).where(Chunk.kb_id == BENCHMARK_KB_ID))
        await session.execute(delete(Document).where(Document.kb_id == BENCHMARK_KB_ID))
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == BENCHMARK_KB_ID))
        await session.commit()
    print("  清理完成。")


if __name__ == "__main__":
    asyncio.run(main())
