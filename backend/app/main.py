"""FastAPI 应用入口"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.api_key import router as api_key_router
from app.api.chat import router as chat_router
from app.api.document import router as document_router
from app.api.knowledge_base import router as kb_router
from app.api.llm_config import router as llm_config_router
from app.api.ocr_config import router as ocr_config_router
from app.api.middleware import ApiKeyAuthMiddleware
from app.api.retrieval import router as retrieval_router
from app.api.system import router as system_router
from app.storage.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库"""
    await init_db()
    yield


app = FastAPI(
    title="Agentic RAG System",
    description="基于 Agent 编排的 RAG 知识库系统",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key 认证中间件（仅拦截 /v1/ 路径）
app.add_middleware(ApiKeyAuthMiddleware)

# 注册路由
app.include_router(chat_router, tags=["Chat"])
app.include_router(kb_router)
app.include_router(document_router)
app.include_router(retrieval_router)
app.include_router(system_router)
app.include_router(api_key_router)
app.include_router(llm_config_router)
app.include_router(ocr_config_router)


@app.get("/")
async def root():
    """根路径健康检查"""
    return {"message": "Agentic RAG System is running"}
