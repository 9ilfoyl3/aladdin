"""配置管理"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 服务
    host: str = "0.0.0.0"
    port: int = 8000

    # 数据库
    database_url: str = "sqlite:///data/rag.db"

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530

    # LLM
    llm_provider: str = "ollama"  # ollama | vllm
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    llm_api_key: str = ""  # 远端 API 的密钥（vllm provider 使用）

    # Embedding
    embed_model: str = "BAAI/bge-m3"
    embed_device: str = "cuda"  # cuda | cpu

    # Rerank
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str = "cuda"  # cuda | cpu

    # Agent
    agent_max_iterations: int = 3
    agent_timeout: float = 30.0

    # Chunking
    parent_chunk_size: int = 1500
    child_chunk_size: int = 300
    chunk_overlap: int = 50

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
