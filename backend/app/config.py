"""配置管理"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 服务
    host: str = "0.0.0.0"
    port: int = 8000

    # 数据库
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/artoo"

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530

    # LLM
    llm_provider: str = "ollama"  # ollama | vllm
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    llm_api_key: str = ""  # 远端 API 的密钥（vllm provider 使用）

    # Embedding（远程服务）
    embed_model: str = "BAAI/bge-m3"
    embed_base_url: str = ""  # 远程 Embedding 服务地址（如 http://server:8080/v1）
    embed_api_key: str = ""  # API 密钥（可选）
    embed_sparse_enabled: bool = True  # 是否启用 sparse 向量（需服务支持 /embed_sparse 端点）

    # Rerank（远程服务）
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_base_url: str = ""  # 远程 Rerank 服务地址（如 http://server:8001/v1）
    rerank_api_key: str = ""  # API 密钥（可选）

    # Agent
    agent_max_iterations: int = 10
    agent_timeout: float = 30.0
    searxng_url: str = "http://localhost:8080"

    # Chunking
    parent_chunk_size: int = 2500
    child_chunk_size: int = 450
    chunk_overlap: int = 70

    # Redis（检索缓存 + 任务队列）
    redis_url: str = "redis://localhost:6379/0"
    retrieval_cache_ttl: int = 1800  # 缓存 TTL（秒），默认 30 分钟

    # OCR 配置
    ocr_enabled: bool = True
    ocr_provider: str = "external_api"
    ocr_fallback_provider: str = ""

    # 外部 OCR API 配置
    ocr_external_api_url: str = ""
    ocr_external_api_key: str = ""
    ocr_external_api_timeout: float = 30.0

    # Pipeline Worker
    # 文档准入并发数：Worker 同时推进处理的文档数（含 load/chunk/index 等阶段）。
    # 注意：准入信号量在单个文档「全程」（含 Embedding/OCR I/O 等待）持有，不会中途释放，
    # 所以小文件并不会自动趁大文件 I/O 间隙插队。保护小文件不被大文件饿死，依赖的是
    # 「慢道预留」机制（见 pipeline_slow_lane_min_mb / pipeline_slow_max_concurrent）：
    # 大文件走慢道并受 slow_max_concurrent 限流，快道始终保留
    # (max_concurrent - slow_max_concurrent) 个名额给小文件。
    pipeline_max_concurrent: int = 4
    pipeline_max_retries: int = 3
    pipeline_slow_threshold_ms: int = 30000
    pipeline_task_timeout_minutes: int = 60  # 单个文档处理总超时（分钟）
    pipeline_circuit_breaker_threshold: int = 5  # 连续失败 N 次触发熔断
    pipeline_health_check_interval: int = 30  # 健康检查/熔断恢复轮询间隔（秒）
    # PEL 孤儿任务周期性回收（崩溃恢复）
    pipeline_claim_interval_seconds: int = 60  # 周期性认领 PEL 中超时消息的间隔（秒）
    # 消息 idle 超过此值才回收。必须大于 task_timeout，否则会抢走正在被合法处理的消息
    # 导致同一文档重复处理。运行时会强制提升到 task_timeout+5 以下不生效。
    pipeline_claim_min_idle_minutes: int = 65
    pipeline_embed_batch_size: int = 32  # Embedding 每批文本数
    # Embedding 全局并发：所有文档共享的并发上限，保护远程 Embedding 服务不被打爆
    pipeline_embed_concurrency: int = 6
    # 单文档 Embedding 并发上限：限制单个文档最多占用多少个全局 slot，
    # 必须小于 pipeline_embed_concurrency，保证多文档之间交错执行、小文件不被大文件饿死
    pipeline_embed_per_doc_concurrency: int = 2
    # OCR 全局并发：所有文档共享的 OCR 调用并发上限
    pipeline_ocr_concurrency: int = 4
    pipeline_embed_max_connections: int = 20  # httpx 连接池上限

    # 大文件分道（fast/slow 双队列）：文件大小 ≥ 此阈值走 slow 道，避免大文件占满快道。
    # 该值须低于业务中常见「大文件」的体积，否则大文件全部落入快道、占满 max_concurrent，
    # 慢道机制失效、小文件被队头阻塞。
    pipeline_slow_lane_min_mb: int = 10
    # slow 道在单个 Worker 内的最大在途文档数（小于 max_concurrent，给快道留出固定额度）
    pipeline_slow_max_concurrent: int = 1

    # 前端配置（通过 /api/system/frontend-config 下发）
    upload_max_concurrent: int = 3  # 前端并发上传数
    upload_max_file_size_mb: int = 500  # 单文件最大 MB

    # ============================================================
    # 认证与授权（tenant-auth）
    # ============================================================
    # 鉴权总开关（灰度阀）：False 时旁路 Authorization_Guard，仅用于联调/分步验证。
    # 见 design.md「显式兼容清单」C1——正式启用后应置 True 或移除旁路。
    auth_enabled: bool = True

    # JWT（HS256）。jwt_secret 为启动期硬依赖：auth_enabled=True 时缺失则 fail-fast。
    jwt_secret: str = ""
    jwt_expire_minutes: int = 720  # JWT 有效期（分钟），默认 12 小时

    # Super_Admin 引导（SuperAdminBootstrap）。首次启动且无 Super_Admin 时据此创建，
    # 并强制改密。缺失时 fail-fast，禁止用默认口令静默兜底。
    super_admin_username: str = ""
    super_admin_password: str = ""

    # 注册模式（env 可配置）：
    #   invite_only（默认）—— 关闭自助注册：登录页无注册入口，/api/auth/register 返回 403；
    #     建号仅由租户管理员在本租户内创建，或经邀请链接。
    #   self_serve —— 开放“租户自助注册”：任何人可注册并**自动开通一个独立租户**，
    #     注册人成为该租户管理员（不暴露/穿透他人租户，符合硬隔离）。
    registration_mode: str = "invite_only"

    # 超管业务内容可见边界：False（默认）= Super_Admin 不可查看业务内容正文。
    content_view_boundary_open: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
