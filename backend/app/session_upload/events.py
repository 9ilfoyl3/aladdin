"""会话上传状态事件 schema 与事件总线

本模块承载会话文件异步建索引过程中的「上传状态事件」相关设施：

- ``SessionUploadEvent``：单条上传状态事件的结构（TypedDict）。
- ``CHANNEL``：跨进程事件广播使用的 Redis pub/sub channel。
- ``make_*`` 系列构造辅助函数：按事件类型产出结构良好的事件字典，
  统一填充 ``ts``（当前时间戳），避免各调用点手工拼装出现字段漂移。

事件经 Redis pub/sub 跨进程广播后，由各 API 进程 fan-out 到本进程内订阅该
会话的 WebSocket 连接（``EventHub``、``SessionUploadEventBus`` 分别在后续任务
2.2 / 2.3 中追加实现）。

事件类型（``type``）取值：
    queued     入队成功（上传接口秒回时发布）
    processing worker 开始建索引
    progress   建索引阶段进度更新（load/chunk/embed/index 等）
    completed  建索引成功（含真实 child chunk 数）
    failed     建索引失败（含错误原因）
    removed    文件被移除
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Literal, Optional, TypedDict

logger = logging.getLogger(__name__)

# 跨进程事件广播 channel（独立于失效广播 channel，避免语义混淆）
CHANNEL = "artoo:session_upload:events"

# 事件类型字面量集合
EventType = Literal[
    "queued",
    "processing",
    "progress",
    "completed",
    "failed",
    "removed",
]


class SessionUploadEvent(TypedDict):
    """单条会话上传状态事件。

    仅 ``type`` / ``session_id`` / ``file_id`` / ``ts`` 恒有值，其余字段按事件
    类型选择性填充（无关字段为 None），便于前端按 ``type`` 分支消费。

    Attributes:
        type: 事件类型，见模块文档。
        session_id: 归属会话 ID（fan-out 按此隔离）。
        file_id: 会话文件 ID（等价于 pipeline 的 doc_id）。
        filename: 文件名（可空）。
        status: ``SessionFile.status`` 快照（queued|processing|completed|failed）。
        progress: 建索引进度 0-100（可空）。
        stage: 当前阶段人类可读标识（如 load/chunk/embed/index，可空）。
        message: 进度/状态的人类可读描述（可空）。
        chunk_count: 完成时的真实 child chunk 数（可空）。
        error: 失败原因（可空）。
        ts: 事件产生时间戳（``time.time()``，秒）。
    """

    type: str
    session_id: str
    file_id: str
    filename: Optional[str]
    status: Optional[str]
    progress: Optional[int]
    stage: Optional[str]
    message: Optional[str]
    chunk_count: Optional[int]
    error: Optional[str]
    ts: float


def _base_event(
    type_: EventType,
    session_id: str,
    file_id: str,
    *,
    filename: Optional[str] = None,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error: Optional[str] = None,
) -> SessionUploadEvent:
    """构造一条结构完整的 ``SessionUploadEvent``，统一填充 ``ts``。

    所有 ``make_*`` 辅助函数最终都委托本函数，保证事件字段齐全（未指定的
    可选字段显式置为 None），避免下游读取缺失键。
    """
    return SessionUploadEvent(
        type=type_,
        session_id=session_id,
        file_id=file_id,
        filename=filename,
        status=status,
        progress=progress,
        stage=stage,
        message=message,
        chunk_count=chunk_count,
        error=error,
        ts=time.time(),
    )


def make_queued(
    session_id: str,
    file_id: str,
    *,
    filename: Optional[str] = None,
    message: Optional[str] = None,
) -> SessionUploadEvent:
    """入队成功事件（上传接口秒回时发布）。``progress`` 归零。"""
    return _base_event(
        "queued",
        session_id,
        file_id,
        filename=filename,
        status="queued",
        progress=0,
        message=message,
    )


def make_processing(
    session_id: str,
    file_id: str,
    *,
    filename: Optional[str] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    progress: Optional[int] = None,
) -> SessionUploadEvent:
    """worker 开始建索引事件。"""
    return _base_event(
        "processing",
        session_id,
        file_id,
        filename=filename,
        status="processing",
        progress=progress,
        stage=stage,
        message=message,
    )


def make_progress(
    session_id: str,
    file_id: str,
    progress: int,
    *,
    filename: Optional[str] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
) -> SessionUploadEvent:
    """建索引阶段进度更新事件（load/chunk/embed/index 等）。

    进度期间 ``status`` 仍为 processing。
    """
    return _base_event(
        "progress",
        session_id,
        file_id,
        filename=filename,
        status="processing",
        progress=progress,
        stage=stage,
        message=message,
    )


def make_completed(
    session_id: str,
    file_id: str,
    *,
    filename: Optional[str] = None,
    chunk_count: Optional[int] = None,
    message: Optional[str] = None,
) -> SessionUploadEvent:
    """建索引成功事件。``progress`` 置 100，携带真实 child chunk 数。"""
    return _base_event(
        "completed",
        session_id,
        file_id,
        filename=filename,
        status="completed",
        progress=100,
        chunk_count=chunk_count,
        message=message,
    )


def make_failed(
    session_id: str,
    file_id: str,
    error: str,
    *,
    filename: Optional[str] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
) -> SessionUploadEvent:
    """建索引失败事件。携带失败原因 ``error``。"""
    return _base_event(
        "failed",
        session_id,
        file_id,
        filename=filename,
        status="failed",
        stage=stage,
        message=message,
        error=error,
    )


def make_removed(
    session_id: str,
    file_id: str,
    *,
    filename: Optional[str] = None,
    message: Optional[str] = None,
) -> SessionUploadEvent:
    """文件被移除事件。"""
    return _base_event(
        "removed",
        session_id,
        file_id,
        filename=filename,
        status="removed",
        message=message,
    )


# =============================================================================
# EventHub：进程内连接管理与 fan-out（任务 2.2）
# =============================================================================

# 单会话连接数上限的兜底默认值（配置项 session_upload_ws_max_conn_per_session
# 由任务 9.1 添加；此处 getattr 防御式读取，避免硬依赖尚未落地的配置）。
_DEFAULT_MAX_CONN_PER_SESSION = 20


def _resolve_max_conn_per_session() -> int:
    """解析单会话连接数上限。

    优先读取配置 ``session_upload_ws_max_conn_per_session``；配置缺失或不可用
    （如任务 9.1 尚未落地）时回退到 ``_DEFAULT_MAX_CONN_PER_SESSION``。
    非正数一律回退默认值，避免误配导致「永远拒绝」。
    """
    try:
        from app.config import get_settings

        settings = get_settings()
        value = getattr(
            settings,
            "session_upload_ws_max_conn_per_session",
            _DEFAULT_MAX_CONN_PER_SESSION,
        )
    except Exception:
        value = _DEFAULT_MAX_CONN_PER_SESSION
    try:
        value = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CONN_PER_SESSION
    return value if value > 0 else _DEFAULT_MAX_CONN_PER_SESSION


class EventHub:
    """进程内 ``session_id -> {WebSocket 连接}`` 映射与事件 fan-out。

    每个 API 进程持有一个 ``EventHub`` 单例。``SessionUploadEventBus`` 收到跨进程
    广播后调用 :meth:`fanout` 把事件推给本进程内订阅该会话的所有连接。

    健壮性要点（REQ-7）：

    - **单连接失败隔离**：:meth:`fanout` 对每个连接独立 ``try/except``，某个慢/坏
      连接写失败不影响向同会话其它连接的推送；失败连接被收集并注销（不再向已断开
      连接堆积写协程）。
    - **连接上限保护**：:meth:`register` 在单会话连接数达到上限时返回 ``False``，
      路由据此 ``close(4429)``，避免资源耗尽。
    - **并发安全**：映射的增删/遍历统一由 ``asyncio.Lock`` 保护。

    连接对象只要求实现 ``async send_json(data)``（FastAPI/Starlette ``WebSocket``
    满足），便于测试注入假连接。
    """

    def __init__(self, max_conn_per_session: Optional[int] = None):
        # session_id -> set[ws]；仅在锁保护下访问
        self._connections: dict[str, set] = {}
        self._lock = asyncio.Lock()
        # 允许显式注入上限（测试友好）；否则运行期从配置解析
        self._max_conn_override = max_conn_per_session

    @property
    def max_conn_per_session(self) -> int:
        """单会话连接数上限（显式注入优先，否则从配置解析）。"""
        if self._max_conn_override is not None and self._max_conn_override > 0:
            return self._max_conn_override
        return _resolve_max_conn_per_session()

    async def register(self, session_id: str, ws) -> bool:
        """注册一个订阅指定会话的连接。

        Returns:
            ``True`` 注册成功；``False`` 表示该会话连接数已达上限（拒绝，
            路由应 ``close(4429)``）。
        """
        async with self._lock:
            conns = self._connections.get(session_id)
            current = len(conns) if conns else 0
            if current >= self.max_conn_per_session:
                logger.warning(
                    "会话 %s 连接数达上限 %d，拒绝新连接",
                    session_id,
                    self.max_conn_per_session,
                )
                return False
            if conns is None:
                conns = set()
                self._connections[session_id] = conns
            conns.add(ws)
            return True

    async def unregister(self, session_id: str, ws) -> None:
        """注销一个连接。空会话集合会被清理，避免映射无限增长。

        幂等：连接/会话不存在时静默返回（WS 路由 finally 中调用，不得抛错）。
        """
        async with self._lock:
            conns = self._connections.get(session_id)
            if not conns:
                return
            conns.discard(ws)
            if not conns:
                self._connections.pop(session_id, None)

    async def fanout(self, event: SessionUploadEvent) -> None:
        """向订阅 ``event['session_id']`` 的所有连接推送事件。

        - 仅推送该会话的连接（会话隔离）。
        - 每个连接独立 ``try/except``：单连接写失败被隔离并收集，不影响其它连接；
          失败连接推送后统一注销（REQ-7）。
        - 推送失败仅记 WARNING，不抛出（REQ-9：事件推送失败降级，不破坏主链路）。
        """
        session_id = event.get("session_id")
        if not session_id:
            return

        # 锁内快照当前连接集合，锁外执行 await send（避免持锁跨 await 造成串行/死锁）
        async with self._lock:
            conns = self._connections.get(session_id)
            targets = list(conns) if conns else []

        if not targets:
            return

        failed: list = []
        for ws in targets:
            try:
                await ws.send_json(event)
            except Exception as e:
                # 单连接失败隔离：记 WARNING、标记待清理，继续推送其余连接
                logger.warning("会话 %s 连接推送失败，标记清理: %s", session_id, e)
                failed.append(ws)

        # 清理写失败的连接，避免向已断开连接反复堆积写协程
        if failed:
            async with self._lock:
                remaining = self._connections.get(session_id)
                if remaining:
                    for ws in failed:
                        remaining.discard(ws)
                    if not remaining:
                        self._connections.pop(session_id, None)

    def connection_count(self, session_id: str) -> int:
        """返回指定会话当前连接数（无需加锁的只读快照）。"""
        conns = self._connections.get(session_id)
        return len(conns) if conns else 0


# =============================================================================
# SessionUploadEventBus：跨进程事件广播（任务 2.3）
# =============================================================================


class SessionUploadEventBus:
    """跨进程会话上传事件广播。

    结构与 ``app.storage.invalidation.InvalidationBus`` 同构（自愈重连、轮询、
    重复启动守卫、Redis 不可用降级），但语义不同：这里广播的是**完整事件对象**
    （``SessionUploadEvent``，JSON 序列化），而非失效信号。

    典型部署：

    - **API 进程**：既 :meth:`publish`（上传接口发 queued），又
      :meth:`subscribe_loop`（订阅广播 → ``EventHub.fanout`` 推给本进程 WS 连接）。
      构造时注入本进程 ``EventHub`` 作为 ``local_hub``，用于 Redis 不可用时的
      进程内直推降级。
    - **worker 进程**：仅 :meth:`publish`（各阶段发 processing/progress/completed/
      failed），不订阅、不持有 ``EventHub``（``local_hub=None``）。此时 Redis 不可用
      的 publish 退化为「记 WARNING 的 no-op」（跨进程推送不可用，属可接受降级）。

    降级语义（REQ-4 / REQ-9）：

    - Redis 可用：``publish`` 写 pub/sub channel，所有 API 进程的 ``subscribe_loop``
      收到后各自 fan-out。
    - Redis 不可用 + 有 ``local_hub``（单进程 API 部署）：``publish`` 直接调用
      ``local_hub.fanout(event)``，进程内仍实时，记 WARNING。
    - Redis 不可用 + 无 ``local_hub``（worker 进程）：``publish`` no-op + WARNING。

    事件推送失败一律降级为 WARNING，绝不抛出打断建索引主链路（REQ-9）。
    """

    def __init__(
        self,
        redis_client=None,
        local_hub: Optional["EventHub"] = None,
        instance_id: Optional[str] = None,
    ):
        self._redis = redis_client
        # 进程内 EventHub 引用：仅用于 Redis 不可用时的进程内直推降级。
        # worker 进程无 hub（None）→ Redis 不可用时 publish no-op。
        self._local_hub = local_hub
        self._instance_id = instance_id or uuid.uuid4().hex
        self._running = False
        # 订阅循环只允许启动一次：防止重复调用 init 起多个 listen 循环，
        # 导致同一进程内多份订阅交替「超时重连」刷屏（照抄 InvalidationBus）。
        self._loop_started = False

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def set_local_hub(self, hub: "EventHub") -> None:
        """设置进程内 ``EventHub``（用于 Redis 不可用时的直推降级）。

        允许 init 之后再绑定（例如 hub 与 bus 初始化顺序解耦）。
        """
        self._local_hub = hub

    async def publish(self, event: SessionUploadEvent) -> None:
        """发布一条上传状态事件。

        - Redis 可用：JSON 序列化后写 pub/sub channel（所有 API 进程订阅）。
        - Redis 不可用 + 有 ``local_hub``：进程内直推（``local_hub.fanout``）+ WARNING。
        - Redis 不可用 + 无 ``local_hub``：no-op + WARNING（worker 进程降级）。
        - 发布失败一律降级为 WARNING，不抛出（不打断主链路）。
        """
        if self._redis is None:
            # Redis 不可用降级路径
            if self._local_hub is not None:
                logger.warning(
                    "SessionUploadEventBus: Redis 不可用，进程内直推降级 (session=%s, type=%s)",
                    event.get("session_id"),
                    event.get("type"),
                )
                try:
                    await self._local_hub.fanout(event)
                except Exception as e:  # fanout 内部已隔离单连接失败，此处兜底
                    logger.warning("SessionUploadEventBus: 进程内直推失败（降级 no-op）: %s", e)
            else:
                logger.warning(
                    "SessionUploadEventBus: Redis 不可用且无本地 hub，事件丢弃 no-op "
                    "(session=%s, type=%s)",
                    event.get("session_id"),
                    event.get("type"),
                )
            return

        try:
            message = json.dumps(event)
            await self._redis.publish(CHANNEL, message)
        except Exception as e:
            logger.warning("SessionUploadEventBus: 事件广播发布失败（降级 no-op）: %s", e)

    async def subscribe_loop(self, hub: "EventHub") -> None:
        """订阅事件广播并 fan-out 给本进程 ``EventHub`` 的连接。

        - 用 ``get_message(timeout=...)`` 轮询而非 ``listen()``：空闲（无消息）是常态，
          不视为断开，避免 pubsub 长连接被误判为断开而反复重连刷屏。
        - 仅真正的连接级错误才重连，指数退避（上限 30s）。
        - 重复启动守卫：同一 bus 实例只跑一个订阅循环。
        - 每条消息 JSON 解码为 ``SessionUploadEvent`` 后 ``await hub.fanout(event)``。

        （自愈重连 / 轮询 / 重复启动守卫 / pubsub 连接释放照抄 InvalidationBus。）
        """
        # 订阅循环需要一个 hub 做 fan-out；同时把它作为降级直推目标。
        if hub is not None:
            self._local_hub = hub

        if self._redis is None:
            logger.info("SessionUploadEventBus: Redis 未配置，跳过订阅（降级进程内直推）")
            return

        if self._loop_started:
            logger.debug("SessionUploadEventBus: 订阅循环已在运行，跳过重复启动")
            return
        self._loop_started = True

        self._running = True
        retry_delay = 1.0
        max_delay = 30.0
        # 轮询间隔：无消息时阻塞读最多 1s 后返回 None（正常空闲），不触发重连。
        poll_timeout = 1.0

        while self._running:
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(CHANNEL)
                retry_delay = 1.0  # 连接成功重置
                logger.info(
                    "SessionUploadEventBus: 订阅成功 (instance=%s)", self._instance_id[:8]
                )

                while self._running:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=poll_timeout
                    )
                    if message is None:
                        continue
                    if message.get("type") != "message":
                        continue
                    try:
                        event: SessionUploadEvent = json.loads(message["data"])
                        await hub.fanout(event)
                    except Exception as e:
                        logger.warning("SessionUploadEventBus: 消息处理异常: %s", e)

            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as e:
                logger.warning(
                    "SessionUploadEventBus: 连接断开，%0.1fs 后重连: %s", retry_delay, e
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)
            finally:
                # 关键：无论正常退出还是异常断开，都释放本轮 pubsub 占用的专用连接，
                # 否则每次重连都会泄漏一条连接，最终触发 Redis "Too many connections"。
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        # 旧版本 redis-py 没有 aclose，退回 reset
                        try:
                            await pubsub.reset()
                        except Exception:
                            pass

        self._loop_started = False
        logger.info("SessionUploadEventBus: 订阅循环结束")

    def stop(self) -> None:
        """停止订阅循环。"""
        self._running = False


# ---------- 全局单例 ----------

_event_bus_instance: Optional[SessionUploadEventBus] = None

# 进程内 EventHub 单例：由 main.py lifespan（任务 8.1）在 API 进程启动时 set_event_hub
# 注入；WS 路由通过 get_event_hub() 读取（未注入时返回 None，路由据此优雅降级）。
_event_hub_instance: Optional["EventHub"] = None


def get_event_hub() -> Optional["EventHub"]:
    """获取进程内 ``EventHub`` 单例（未注入时返回 None）。

    WS 路由用它注册/注销连接。返回 None 表示事件基础设施尚未在本进程装配
    （如任务 8 尚未接线）——路由可据此降级为「仅推快照、不注册实时连接」，不 crash。
    """
    return _event_hub_instance


def set_event_hub(hub: Optional["EventHub"]) -> None:
    """设置（或清空）进程内 ``EventHub`` 单例。

    由 API 进程启动时注入本进程唯一的 hub；传 None 用于关闭时清理，避免测试/重启
    残留旧引用。
    """
    global _event_hub_instance
    _event_hub_instance = hub


def get_session_upload_event_bus() -> Optional[SessionUploadEventBus]:
    """获取全局 ``SessionUploadEventBus`` 单例（未初始化时返回 None）。"""
    return _event_bus_instance


async def init_session_upload_event_bus(
    local_hub: Optional["EventHub"] = None,
) -> SessionUploadEventBus:
    """初始化全局 ``SessionUploadEventBus``。

    Redis 不可用（``redis_url`` 未配置 / 连接失败）时返回持有 ``redis_client=None``
    的实例（降级进程内直推 / no-op），不抛错。镜像 ``init_invalidation_bus``。

    Args:
        local_hub: 进程内 ``EventHub``（API 进程传入，worker 进程可为 None）。
            用于 Redis 不可用时的进程内直推降级。

    Returns:
        全局单例 ``SessionUploadEventBus``。
    """
    global _event_bus_instance
    if _event_bus_instance is not None:
        # 已初始化：允许补绑 local_hub（顺序解耦）
        if local_hub is not None and _event_bus_instance._local_hub is None:
            _event_bus_instance.set_local_hub(local_hub)
        return _event_bus_instance

    try:
        import redis.asyncio as aioredis
        from app.config import get_settings

        settings = get_settings()
        redis_url = getattr(settings, "redis_url", "")

        if not redis_url:
            logger.info("SessionUploadEventBus: redis_url 未配置，降级进程内直推 / no-op")
            _event_bus_instance = SessionUploadEventBus(redis_client=None, local_hub=local_hub)
            return _event_bus_instance

        client = aioredis.from_url(
            redis_url,
            decode_responses=True,
            # pubsub 长连接稳定性：开启 keepalive + 周期健康检查，避免空闲连接被中间设备
            # 静默掐断；不设激进的 socket_timeout（由 get_message(timeout=...) 控制轮询节奏）。
            socket_keepalive=True,
            health_check_interval=30,
        )
        await client.ping()
        _event_bus_instance = SessionUploadEventBus(redis_client=client, local_hub=local_hub)
        logger.info(
            "SessionUploadEventBus: 初始化成功 (instance=%s)",
            _event_bus_instance.instance_id[:8],
        )
        return _event_bus_instance
    except Exception as e:
        logger.warning("SessionUploadEventBus: 初始化失败，降级进程内直推 / no-op: %s", e)
        _event_bus_instance = SessionUploadEventBus(redis_client=None, local_hub=local_hub)
        return _event_bus_instance
