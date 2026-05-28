"""配置管理"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 服务
    host: str = "0.0.0.0"
    port: int = 8000

    # 数据库
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/aladdin"

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
    parent_chunk_size: int = 1500
    child_chunk_size: int = 300
    chunk_overlap: int = 50

    # Redis（检索缓存 + 任务队列）
    redis_url: str = "redis://localhost:6379/0"
    retrieval_cache_ttl: int = 1800  # 缓存 TTL（秒），默认 30 分钟

    # OCR 配置
    ocr_enabled: bool = True
    ocr_provider: str = "paddleocr"
    ocr_fallback_provider: str = ""

    # PaddleOCR 配置
    ocr_paddleocr_lang: str = "ch"
    ocr_paddleocr_use_gpu: bool = False

    # 外部 OCR API 配置
    ocr_external_api_url: str = ""
    ocr_external_api_key: str = ""
    ocr_external_api_timeout: float = 30.0

    # Pipeline Worker
    pipeline_max_concurrent: int = 3
    pipeline_max_retries: int = 3
    pipeline_slow_threshold_ms: int = 30000
    pipeline_task_timeout_minutes: int = 60  # 单个文档处理总超时（分钟）
    pipeline_circuit_breaker_threshold: int = 5  # 连续失败 N 次触发熔断
    pipeline_health_check_interval: int = 30  # 健康检查/熔断恢复轮询间隔（秒）
    pipeline_embed_batch_size: int = 128  # Embedding 每批文本数
    pipeline_embed_concurrency: int = 8  # Embedding 并发请求数

    # 前端配置（通过 /api/system/frontend-config 下发）
    upload_max_concurrent: int = 3  # 前端并发上传数
    upload_max_file_size_mb: int = 500  # 单文件最大 MB

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
