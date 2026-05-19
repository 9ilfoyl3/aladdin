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

    # Embedding
    embed_provider: str = "sentence-transformers"  # sentence-transformers | flag-embedding
    embed_model: str = "BAAI/bge-m3"
    embed_device: str = "cpu"  # cuda | cpu | mps

    # Rerank
    rerank_provider: str = "sentence-transformers"  # sentence-transformers | flag-embedding
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str = "cpu"  # cuda | cpu | mps

    # Agent
    agent_max_iterations: int = 3
    agent_timeout: float = 30.0

    # Chunking
    parent_chunk_size: int = 1500
    child_chunk_size: int = 300
    chunk_overlap: int = 50

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

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
