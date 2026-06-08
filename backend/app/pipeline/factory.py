"""Pipeline 工厂函数

提供创建 DocumentPipeline 实例的统一入口，
避免 API 和 Worker 各自手动组装依赖。
"""

from app.models.manager import get_model_manager
from app.pipeline.pipeline import DocumentPipeline
from app.startup import load_ocr_manager
from app.storage.database import async_session
from app.storage.milvus import get_milvus_client


async def create_pipeline() -> DocumentPipeline:
    """创建完整的 DocumentPipeline 实例（含所有依赖）

    Returns:
        配置完整的 DocumentPipeline 实例
    """
    model_manager = get_model_manager()
    milvus_client = get_milvus_client()
    ocr_manager = await load_ocr_manager()

    return DocumentPipeline(
        model_manager=model_manager,
        milvus_client=milvus_client,
        db_session_factory=async_session,
        ocr_manager=ocr_manager,
    )
