"""FastAPI-приложение gigachat-agent.

Принимает чат-запросы от дашборда (через прокси rescue-api), делает
function-calling в GigaChat, возвращает ответ оператору.

Слушает по умолчанию только на 127.0.0.1:8001 — наружу не светим.
Дашборд ходит через rescue-api (`POST /api/chat`), это даёт single-origin
для фронта (нет CORS) и один открытый порт у RPi5.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .agent import Agent, ChatTurn
from .config import load_config

log = logging.getLogger("gigachat_agent")


# ============================================================
# Pydantic-модели — строго совпадают с тем, что шлёт дашборд через rescue-api
# ============================================================

class ChatTurnIn(BaseModel):
    role: str               # "user" / "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # История прошлых ходов; system-сообщения не принимаем (свой промпт жёстко)
    history: list[ChatTurnIn] = Field(default_factory=list, max_length=40)


class ChatResponse(BaseModel):
    reply: str
    tools_used: list[str] = Field(default_factory=list)
    # human-readable причина если AI отказал; reply пустой
    error: str | None = None


# ============================================================
# Жизненный цикл — Agent создаём один раз, держим
# ============================================================

cfg = load_config()
_agent: Agent | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _agent
    log.info("gigachat-agent старт")
    _agent = Agent(cfg)
    yield
    log.info("gigachat-agent shutdown")
    if _agent is not None:
        _agent.close()


app = FastAPI(
    title="Mesh-net Тропы — gigachat-agent",
    description="ИИ-диспетчер с function calling в rescue-api",
    version="0.1.0",
    lifespan=lifespan,
)


# ============================================================
# Эндпоинты
# ============================================================

@app.get("/health")
def health():
    """Жив ли сервис + в каком режиме авторизации."""
    return {
        "status": "ok",
        "auth_mode": cfg.auth_mode,
        "model": cfg.model,
        "scope": cfg.scope,
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """Один ход чата.

    sync def намеренно — GigaChat SDK у нас тоже sync, FastAPI запустит
    нас в threadpool, event loop не блокируется. Параллельно держать
    несколько разговоров можно — Agent stateless поверх GigaChat.
    """
    assert _agent is not None  # lifespan гарантирует
    history = [ChatTurn(role=t.role, content=t.content) for t in req.history]
    res = _agent.ask(req.message, history)
    return ChatResponse(reply=res.reply, tools_used=res.tools_used, error=res.error)
