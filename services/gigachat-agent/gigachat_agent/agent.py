"""Function-calling loop поверх GigaChat SDK.

Логика одного запроса /chat:

    1. Собираем messages: SYSTEM_PROMPT + история + новое сообщение пользователя.
    2. Вызываем giga.chat(Chat(messages, functions=TOOL_DEFS)).
    3. Если finish_reason == "function_call":
         - выполняем функцию через tools.dispatch(),
         - добавляем результат как Messages(role=FUNCTION, content=json),
         - идём на шаг 2 (но не больше max_iterations раз).
    4. Иначе берём content и возвращаем оператору.

Защита от зацикливания: max_iterations (default 5). Если модель упорно
зовёт функции по кругу — обрываем и возвращаем последнее content (или
сообщение об ошибке).

GigaChat SDK здесь синхронный (`GigaChat`, не `GigaChatAsync`):
FastAPI исполнит наш sync-эндпоинт в threadpool, event loop не блокируется,
а кода и зависимостей в разы меньше.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from gigachat import GigaChat
from gigachat.exceptions import (
    AuthenticationError,
    GigaChatException,
    ResponseError,
)
from gigachat.models import Chat, Messages, MessagesRole

from .config import Config
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_DEFS, RescueApi, dispatch

log = logging.getLogger("gigachat_agent.agent")


@dataclass
class ChatTurn:
    """Одно сообщение в истории /chat. Что пришло от UI и что отдаём обратно."""
    role: str       # "user" / "assistant"
    content: str


@dataclass
class ChatResult:
    """Что возвращаем дашборду на POST /chat."""
    reply: str
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None        # человекочитаемая причина "AI недоступен (...)"


def _history_to_messages(history: list[ChatTurn]) -> list[Messages]:
    """UI-формат → SDK-формат. Невалидные роли отбрасываем молча."""
    out = []
    for t in history:
        role = t.role.lower()
        if role == "user":
            out.append(Messages(role=MessagesRole.USER, content=t.content))
        elif role == "assistant":
            out.append(Messages(role=MessagesRole.ASSISTANT, content=t.content))
        # system от UI игнорируем — у нас свой
    return out


class Agent:
    """Долгоживущий клиент GigaChat + RescueApi.

    Создаётся один раз в lifespan FastAPI. Если auth_mode == "none" —
    SDK не инициализируем, .ask() сразу вернёт error="ключ не задан".
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = RescueApi(cfg.rescue_api_url, timeout=cfg.timeout_sec)
        self._giga: GigaChat | None = None

        if cfg.auth_mode == "authorization_key":
            self._giga = GigaChat(
                credentials=cfg.authorization_key,
                scope=cfg.scope,
                model=cfg.model,
                verify_ssl_certs=False,    # у Сбера свой root CA, в дев-окружении проще выключить
                timeout=cfg.timeout_sec,
            )
            log.info("GigaChat client инициализирован (credentials, scope=%s)", cfg.scope)
        elif cfg.auth_mode == "access_token":
            self._giga = GigaChat(
                access_token=cfg.access_token,
                scope=cfg.scope,
                model=cfg.model,
                verify_ssl_certs=False,
                timeout=cfg.timeout_sec,
            )
            log.warning(
                "GigaChat client инициализирован с access_token — это короткоживущий "
                "JWT, упадёт через ~30 минут. Замени token-key на Authorization_key."
            )
        else:
            log.error("GigaChat ключ не задан — /chat будет возвращать ошибку")

    def close(self):
        try:
            if self._giga is not None:
                self._giga.close()
        except Exception:
            pass
        self.api.close()

    # --- основной публичный метод ---

    def ask(self, user_message: str, history: list[ChatTurn]) -> ChatResult:
        if self._giga is None:
            return ChatResult(
                reply="",
                error="AI недоступен (ключ авторизации не задан, см. token-key)",
            )

        messages: list[Messages] = [
            Messages(role=MessagesRole.SYSTEM, content=SYSTEM_PROMPT),
            *_history_to_messages(history),
            Messages(role=MessagesRole.USER, content=user_message),
        ]
        tools_used: list[str] = []

        for step in range(self.cfg.max_iterations):
            try:
                resp = self._giga.chat(Chat(messages=messages, functions=TOOL_DEFS))
            except AuthenticationError as e:
                log.warning("GigaChat auth error: %s", e)
                return ChatResult(reply="", error=f"AI недоступен (ошибка авторизации: {e})")
            except ResponseError as e:
                log.warning("GigaChat response error: %s", e)
                return ChatResult(reply="", error=f"AI недоступен (ошибка GigaChat: {e})")
            except GigaChatException as e:
                log.warning("GigaChat error: %s", e)
                return ChatResult(reply="", error=f"AI недоступен (GigaChat: {e})")
            except Exception as e:
                # httpx.ConnectError, ssl, и т.п. — обычно означает "нет интернета"
                log.warning("GigaChat call failed: %s: %s", type(e).__name__, e)
                return ChatResult(reply="", error=f"AI недоступен (нет связи с GigaChat: {e})")

            choice = resp.choices[0]
            mess = choice.message
            messages.append(mess)

            if choice.finish_reason == "function_call" and mess.function_call is not None:
                fc = mess.function_call
                tools_used.append(fc.name)
                arguments = _coerce_arguments(fc.arguments)
                log.info("tool call #%d: %s args=%s", step + 1, fc.name, arguments)
                result = dispatch(self.api, fc.name, arguments)
                # Возвращаем результат модели в строго JSON-сериализуемом виде.
                messages.append(
                    Messages(
                        role=MessagesRole.FUNCTION,
                        content=json.dumps(result, ensure_ascii=False, default=str),
                    )
                )
                continue

            # Финальный ответ модели
            return ChatResult(
                reply=(mess.content or "").strip() or "(пустой ответ модели)",
                tools_used=tools_used,
            )

        # Зациклились — возвращаем что есть
        log.warning("превышен лимит итераций (%d), tools_used=%s",
                    self.cfg.max_iterations, tools_used)
        last = next(
            (m.content for m in reversed(messages)
             if m.role == MessagesRole.ASSISTANT and m.content),
            "",
        )
        return ChatResult(
            reply=last or "(модель не дала финального ответа за отведённое число итераций)",
            tools_used=tools_used,
            error="превышен лимит итераций function calling",
        )


def _coerce_arguments(args: Any) -> dict:
    """SDK иногда отдаёт arguments dict-ом (распарсил), иногда — JSON-строкой.
    Приводим к dict, пустоту — к {}."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            v = json.loads(args)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
