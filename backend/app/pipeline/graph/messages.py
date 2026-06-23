"""知识图谱抽取慢道（``pipeline:graph``）的任务消息结构。

与文档入库主链路的 :class:`app.pipeline.queue.TaskMessage` **字段不同**，故单独定义
:class:`GraphTaskMessage`（design.md 4.4）。一个文档按抽取粒度（默认 parent chunk）
拆成 N 个子任务，每个子任务对应一条 ``GraphTaskMessage``。

序列化范式对齐 ``TaskMessage``：``TaskQueue.enqueue`` 内部用 ``dataclasses.asdict`` 把
消息转 dict 再 ``json.dumps`` 写入 Redis Stream 的 ``data`` 字段。GraphTaskMessage 是
dataclass，故可直接被 ``TaskQueue.enqueue`` 入队；消费侧（抽取 worker，task 4.2）用
:meth:`GraphTaskMessage.from_dict` 从 ``json.loads(data)`` 还原，保证编/解码一致。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class GraphTaskMessage:
    """图谱抽取子任务消息（一条对应一个待抽取 chunk）。

    Attributes:
        job_id: 所属 ``GraphExtractJob`` 的 id（终态回写 pending_subtasks 用）。
        kb_id: 知识库 id（强制租户/库隔离）。
        doc_id: 文档 id（陈旧守卫比对 Document.graph_attempt 用）。
        chunk_id: 待抽取的 chunk id。
        chunk_index: chunk 在文档内的序号（可观测/排序用）。
        attempt: 入队时的 ``Document.graph_attempt`` 快照；重解析自增后旧任务据此失效。
        tenant_id: 租户 id（写图时盖章；可为 None 表无租户）。
        retry_count: 应用层重试计数（worker 失败重投时累加）。
        created_at: 入队时间戳（``TaskQueue.enqueue`` 自动填充）。
        trace_id: 链路追踪 id（``TaskQueue.enqueue`` 自动填充）。
    """

    job_id: str
    kb_id: str
    doc_id: str
    chunk_id: str
    chunk_index: int
    attempt: int
    tenant_id: str | None = None
    retry_count: int = 0
    created_at: float = 0.0
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 编码的 dict（与 ``TaskQueue.enqueue`` 的 ``asdict`` 等价）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphTaskMessage":
        """从 ``json.loads(data)`` 还原消息，逐字段兜底缺失/类型，保证不因脏消息崩溃。

        Args:
            data: ``TaskQueue`` 写入的 ``data`` 字段经 ``json.loads`` 后的 dict。

        Returns:
            还原后的 :class:`GraphTaskMessage`。

        Raises:
            KeyError: 缺少必需字段（job_id/kb_id/doc_id/chunk_id）时由调用方决定丢弃。
        """
        return cls(
            job_id=data["job_id"],
            kb_id=data["kb_id"],
            doc_id=data["doc_id"],
            chunk_id=data["chunk_id"],
            chunk_index=int(data.get("chunk_index", 0)),
            attempt=int(data.get("attempt", 0)),
            tenant_id=data.get("tenant_id"),
            retry_count=int(data.get("retry_count", 0)),
            created_at=float(data.get("created_at", 0.0)),
            trace_id=data.get("trace_id", ""),
        )
