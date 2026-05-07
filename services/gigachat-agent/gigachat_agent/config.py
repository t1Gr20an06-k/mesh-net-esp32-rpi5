"""Загрузка конфига gigachat-agent.

Авторизация в GigaChat — через файл `services/gigachat-agent/token-key`.
Поддерживаем 3 формата:

1. Простой key=value (как из ЛК Сбера):
       client_id="..."
       scope="GIGACHAT_API_PERS"
       Authorization_key="MDE5..."
   Ключ Authorization_key — это base64(client_id:client_secret), SDK
   использует его для OAuth-обмена и сам обновляет access_token. Это
   рекомендуемый формат — токен обновляется автоматически каждые 30 мин.

2. JSON со свежим access_token (короткоживущий, старый формат):
       {"access_token":"eyJ...","expires_at":1775828035649}
   Будет работать пока токен жив (~30 минут). Не для прода.

3. Просто длинная строка-ключ без обвязки — трактуется как
   Authorization_key.

Любой из этих режимов может быть переопределён ENV-переменными
GIGACHAT_AUTHORIZATION_KEY / GIGACHAT_ACCESS_TOKEN — удобно для systemd.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("gigachat_agent.config")

# token-key лежит в services/gigachat-agent/. Этот файл — в gigachat_agent/,
# на один уровень глубже.
DEFAULT_TOKEN_KEY = Path(__file__).resolve().parents[1] / "token-key"

# rescue-api на той же машине, дефолт — 127.0.0.1:8000
DEFAULT_RESCUE_API_URL = "http://127.0.0.1:8000"


@dataclass
class Config:
    """Конфиг сервиса. Создаётся один раз при старте."""

    # Что-то одно из двух обязательно (auth_key — приоритет).
    # Если оба None — сервис стартанёт, но /chat будет возвращать
    # "AI недоступен (ключ не задан)".
    authorization_key: str | None
    access_token: str | None

    scope: str               # "GIGACHAT_API_PERS" / "_B2B" / "_CORP"
    model: str               # "GigaChat" по умолчанию
    client_id: str | None    # для логов, может быть None
    timeout_sec: float       # таймаут одного вызова GigaChat
    max_iterations: int      # сколько раз подряд можно вызвать tool в одном /chat

    rescue_api_url: str
    host: str
    port: int

    @property
    def auth_mode(self) -> str:
        if self.authorization_key:
            return "authorization_key"
        if self.access_token:
            return "access_token"
        return "none"


def _parse_kv_file(text: str) -> dict[str, str]:
    """Разбираем простой key=value ('"..."' или без кавычек) построчно.

    Игнорируем пустые строки и комментарии (`#`). Регистр ключей нормализуем
    в нижний — пользователь мог написать Authorization_key или AUTHORIZATION_KEY.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?([^"]*)"?\s*$', line)
        if not m:
            continue
        out[m.group(1).lower()] = m.group(2)
    return out


def _load_token_key(path: Path) -> tuple[str | None, str | None, str | None, str | None]:
    """Парсит файл token-key. Возвращает (auth_key, access_token, scope, client_id).

    Все элементы могут быть None — отсутствующие просто не вернутся.
    """
    if not path.exists():
        log.info("token-key не найден: %s", path)
        return None, None, None, None

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None, None, None, None

    # Формат 2 — JSON со старым access_token
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
            tok = data.get("access_token")
            if tok:
                log.warning(
                    "token-key содержит JSON access_token — он короткоживущий (~30 мин). "
                    "Замени на Authorization_key из ЛК Сбера, см. CLAUDE.md."
                )
                return None, tok, None, None
        except json.JSONDecodeError:
            pass  # упадёт ниже, попробуем как kv

    # Формат 1 — key=value
    if "=" in text:
        kv = _parse_kv_file(text)
        return (
            kv.get("authorization_key") or None,
            kv.get("access_token") or None,
            kv.get("scope") or None,
            kv.get("client_id") or None,
        )

    # Формат 3 — голая строка, считаем Authorization key
    return text, None, None, None


def load_config() -> Config:
    """Собирает конфиг из ENV + token-key. Не падает, даже если ключа нет —
    только логирует, чтобы /chat сам мог вернуть осмысленную ошибку."""

    token_path = Path(os.environ.get("GIGACHAT_TOKEN_FILE") or DEFAULT_TOKEN_KEY)
    file_auth, file_tok, file_scope, file_client = _load_token_key(token_path)

    # ENV перетирает файл — удобно для контейнеров и systemd-overrides
    auth_key = os.environ.get("GIGACHAT_AUTHORIZATION_KEY") or file_auth
    access_token = os.environ.get("GIGACHAT_ACCESS_TOKEN") or file_tok

    scope = os.environ.get("GIGACHAT_SCOPE") or file_scope or "GIGACHAT_API_PERS"
    model = os.environ.get("GIGACHAT_MODEL") or "GigaChat"

    cfg = Config(
        authorization_key=auth_key,
        access_token=access_token,
        scope=scope,
        model=model,
        client_id=file_client,
        timeout_sec=float(os.environ.get("GIGACHAT_TIMEOUT", "20")),
        max_iterations=int(os.environ.get("GIGACHAT_MAX_ITER", "5")),
        rescue_api_url=os.environ.get("RESCUE_API_URL", DEFAULT_RESCUE_API_URL).rstrip("/"),
        host=os.environ.get("GIGACHAT_AGENT_HOST", "127.0.0.1"),
        port=int(os.environ.get("GIGACHAT_AGENT_PORT", "8001")),
    )

    log.info(
        "gigachat-agent конфиг: auth=%s scope=%s model=%s client_id=%s rescue_api=%s",
        cfg.auth_mode, cfg.scope, cfg.model, cfg.client_id or "?", cfg.rescue_api_url,
    )
    return cfg
