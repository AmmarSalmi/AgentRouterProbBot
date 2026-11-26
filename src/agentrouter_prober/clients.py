from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import httpx

from .browser_session import AgentRouterSession, BrowserSessionError, ZenSessionLoader
from .config import Settings, TelegramSettings
from .state import utc_now


TELEGRAM_USER_AGENT = "AgentRouterAvailabilityProber/2.0"
DEFAULT_QUOTA_PER_UNIT = 500_000


class ClientError(RuntimeError):
    pass


class AuthenticationError(ClientError):
    pass


class TransientClientError(ClientError):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProtocolError(ClientError):
    pass


class ProbeOutcome(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ModelObservation:
    name: str
    status: str
    current_tier: str
    apis: tuple[str, ...] = ()
    success_rate_24h: float | None = None
    ttft_p90_ms_24h: int | None = None
    avg_latency_ms: int | None = None

    @property
    def classification(self) -> str:
        status = self.status.lower()
        tier = self.current_tier.lower()
        if status == "down" or tier == "bad":
            return "unavailable"
        if status in {"operational", "degraded"} and tier != "bad":
            return "candidate"
        return "unknown"

    @property
    def codex_compatible(self) -> bool:
        return "responses" in self.apis


@dataclass(frozen=True)
class ModelProbe:
    outcome: ProbeOutcome
    api: str
    latency_ms: int
    reply: str | None = None


@dataclass(frozen=True)
class AccountUsage:
    quota: int
    used_quota: int
    request_count: int
    group: str
    quota_per_unit: float
    display_in_currency: bool


def _message(payload: Any, fallback: str = "") -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("message"):
            return str(payload["message"])
    return fallback


def _looks_like_auth_error(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "access token",
        "unauthorized",
        "unauthenticated",
        "permission",
        "new-api-user",
        "not logged",
        "无权",
        "令牌",
        "未登录",
        "不匹配",
    )
    return any(marker in lowered for marker in markers)


def _response_text(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
        return None
    parts: list[str] = []
    for item in payload["output"]:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") if part.get("type") == "output_text" else None
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return " ".join(parts) or None


def _validate_responses(payload: Any) -> str | None:
    if (
        not isinstance(payload, dict)
        or payload.get("object") != "response"
        or not isinstance(payload.get("id"), str)
        or not payload["id"].strip()
        or not isinstance(payload.get("output"), list)
        or payload.get("error") is not None
        or payload.get("success") is False
    ):
        raise ProtocolError("Probe did not return a valid Responses API response")
    text = _response_text(payload)
    if payload.get("status") == "completed" and text:
        return text
    details = payload.get("incomplete_details")
    usage = payload.get("usage")
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    if (
        payload.get("status") == "incomplete"
        and isinstance(details, dict)
        and details.get("reason") == "max_output_tokens"
        and (text or (type(output_tokens) is int and output_tokens > 0))
    ):
        return text
    raise ProtocolError("Responses API probe did not finish with generated output")


def _validate_chat(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
        raise ProtocolError("Probe did not return a valid Chat Completions response")
    for choice in payload["choices"]:
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()
    raise ProtocolError("Chat Completions probe returned no generated output")


def _validate_anthropic(payload: Any) -> str:
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "message"
        or not isinstance(payload.get("id"), str)
        or not isinstance(payload.get("content"), list)
    ):
        raise ProtocolError("Probe did not return a valid Anthropic Messages response")
    parts = [
        item.get("text", "").strip()
        for item in payload["content"]
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    if not parts:
        raise ProtocolError("Anthropic Messages probe returned no generated output")
    return " ".join(parts)


class AgentRouterClient:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        session_loader: ZenSessionLoader | None = None,
    ):
        self.settings = settings
        self._owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            headers={
                "User-Agent": settings.client_user_agent,
                "originator": "codex_cli_rs",
            },
        )
        self.session_loader = session_loader
        self._quota_display_cache: tuple[float, bool] | None = None
        if (
            self.session_loader is None
            and settings.agentrouter_dashboard_token is None
            and settings.agentrouter_session_cookie is None
            and settings.zen_profile_path is not None
        ):
            self.session_loader = ZenSessionLoader(
                settings.zen_profile_path, settings.agentrouter_base_url
            )

    async def close(self) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def _dashboard_session(self) -> AgentRouterSession:
        if (
            self.settings.agentrouter_session_cookie is not None
            and self.settings.agentrouter_user_id is not None
        ):
            return AgentRouterSession(
                self.settings.agentrouter_session_cookie,
                self.settings.agentrouter_user_id,
            )
        if self.session_loader is None:
            raise AuthenticationError("No AgentRouter browser session source is configured")
        try:
            return await asyncio.to_thread(self.session_loader.load)
        except BrowserSessionError as exc:
            raise AuthenticationError(str(exc)) from exc

    async def _dashboard_payload(
        self, path: str, *, referer: str, label: str
    ) -> dict[str, Any]:
        token = self.settings.agentrouter_dashboard_token
        if token is not None:
            user_id = self.settings.agentrouter_user_id
            if user_id is None:
                raise AuthenticationError(
                    "The AgentRouter dashboard token requires a user ID"
                )
            auth_headers = {"Authorization": f"Bearer {token}"}
            auth_error = (
                "AgentRouter rejected dashboard authentication. Check "
                "AGENTROUTER_DASHBOARD_TOKEN and AGENTROUTER_USER_ID."
            )
            verification_help = "check AgentRouter access from this host"
        else:
            session = await self._dashboard_session()
            user_id = session.user_id
            auth_headers = {"Cookie": f"session={session.cookie}"}
            auth_error = "AgentRouter rejected the Zen login session; log in again in Zen"
            verification_help = "refresh the AgentRouter console in Zen"
        url = f"{self.settings.agentrouter_base_url}{path}"
        try:
            response = await self.http.get(
                url,
                headers={
                    **auth_headers,
                    "New-Api-User": str(user_id),
                    "Accept": "application/json, text/plain, */*",
                    "Cache-Control": "no-store",
                    "Referer": referer,
                },
            )
        except httpx.RequestError as exc:
            detail = str(exc)
            if token is not None:
                detail = detail.replace(token, "[redacted]")
            raise TransientClientError(f"{label} request failed: {detail}") from exc

        if response.status_code in {401, 403}:
            raise AuthenticationError(auth_error)
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientClientError(f"{label} returned HTTP {response.status_code}")
        if not response.is_success:
            raise ProtocolError(
                f"{label} returned unexpected HTTP {response.status_code}"
            )
        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type and response.text.lstrip().startswith("<"):
            raise AuthenticationError(
                f"AgentRouter returned browser verification instead of {label.lower()}; "
                f"{verification_help}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProtocolError(f"{label} returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            message = _message(payload, f"{label} request was unsuccessful")
            if _looks_like_auth_error(message):
                raise AuthenticationError(auth_error if token is not None else message)
            if token is not None:
                message = message.replace(token, "[redacted]")
            raise ProtocolError(message)
        return payload

    async def _quota_display_settings(self) -> tuple[float, bool]:
        if self._quota_display_cache is not None:
            return self._quota_display_cache
        try:
            response = await self.http.get(
                f"{self.settings.agentrouter_base_url}/api/status",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Cache-Control": "no-store",
                },
            )
            payload = response.json() if response.is_success else None
            data = payload.get("data") if isinstance(payload, dict) else None
            quota_per_unit = data.get("quota_per_unit") if isinstance(data, dict) else None
            display_in_currency = (
                data.get("display_in_currency") if isinstance(data, dict) else None
            )
            if (
                type(quota_per_unit) in (int, float)
                and math.isfinite(quota_per_unit)
                and quota_per_unit > 0
            ):
                self._quota_display_cache = (
                    float(quota_per_unit),
                    display_in_currency is not False,
                )
        except (httpx.RequestError, ValueError):
            pass
        if self._quota_display_cache is None:
            self._quota_display_cache = (float(DEFAULT_QUOTA_PER_UNIT), True)
        return self._quota_display_cache

    async def fetch_account_usage(self) -> AccountUsage:
        payload = await self._dashboard_payload(
            "/api/user/self",
            referer=f"{self.settings.agentrouter_base_url}/console",
            label="Account details",
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProtocolError("Account response did not contain account data")
        quota = data.get("quota")
        used_quota = data.get("used_quota")
        request_count = data.get("request_count", 0)
        if (
            type(quota) is not int
            or type(used_quota) is not int
            or type(request_count) is not int
            or used_quota < 0
            or request_count < 0
        ):
            raise ProtocolError("Account response did not contain valid usage data")
        raw_group = data.get("group")
        group = raw_group.strip() if isinstance(raw_group, str) else ""
        quota_per_unit, display_in_currency = await self._quota_display_settings()
        return AccountUsage(
            quota=quota,
            used_quota=used_quota,
            request_count=request_count,
            group=group,
            quota_per_unit=quota_per_unit,
            display_in_currency=display_in_currency,
        )

    async def fetch_model_status(self) -> dict[str, ModelObservation]:
        payload = await self._dashboard_payload(
            "/api/user/model-status",
            referer=f"{self.settings.agentrouter_base_url}/console/model-status",
            label="Model-status",
        )
        data = payload.get("data")
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise ProtocolError("Model-status response did not contain a model list")

        observations: dict[str, ModelObservation] = {}
        for item in models:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in observations:
                continue
            apis = item.get("apis")
            observations[name] = ModelObservation(
                name=name,
                status=str(item.get("status") or "unknown"),
                current_tier=str(item.get("current_tier") or "none"),
                apis=tuple(
                    api for api in apis if isinstance(api, str) and api.strip()
                )
                if isinstance(apis, list)
                else (),
                success_rate_24h=(
                    float(item["success_rate_24h"])
                    if type(item.get("success_rate_24h")) in (int, float)
                    else None
                ),
                ttft_p90_ms_24h=(
                    int(item["ttft_p90_ms_24h"])
                    if type(item.get("ttft_p90_ms_24h")) is int
                    else None
                ),
                avg_latency_ms=(
                    int(item["avg_latency_ms"])
                    if type(item.get("avg_latency_ms")) is int
                    else None
                ),
            )
        if not observations:
            raise ProtocolError("Model-status returned an empty model list")
        return observations

    @staticmethod
    def _api_for(observation: ModelObservation) -> str:
        for api in ("responses", "chat", "claude_native"):
            if api in observation.apis:
                return api
        raise ProtocolError(
            f"{observation.name} has no supported probe API in model-status"
        )

    async def probe_model(self, observation: ModelObservation) -> ModelProbe:
        api = self._api_for(observation)
        prompt = "Reply only with OK."
        common_headers = {"Content-Type": "application/json"}
        if api == "responses":
            url = f"{self.settings.agentrouter_base_url}/v1/responses"
            headers = {
                **common_headers,
                "Authorization": f"Bearer {self.settings.agentrouter_api_key}",
            }
            body = {
                "model": observation.name,
                "input": prompt,
                "max_output_tokens": self.settings.probe_max_output_tokens,
                "reasoning": {"effort": self.settings.reasoning_effort},
                "stream": False,
            }
        elif api == "chat":
            url = f"{self.settings.agentrouter_base_url}/v1/chat/completions"
            headers = {
                **common_headers,
                "Authorization": f"Bearer {self.settings.agentrouter_api_key}",
            }
            body = {
                "model": observation.name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.settings.probe_max_output_tokens,
                "stream": False,
            }
        else:
            url = f"{self.settings.agentrouter_base_url}/v1/messages"
            headers = {
                **common_headers,
                "x-api-key": self.settings.agentrouter_api_key,
                "anthropic-version": "2023-06-01",
            }
            body = {
                "model": observation.name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.settings.probe_max_output_tokens,
                "stream": False,
            }

        started = time.perf_counter()
        try:
            response = await self.http.post(url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise TransientClientError(
                f"Probe for {observation.name} failed: {exc}"
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        body_text = response.text
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        message = _message(parsed, body_text)
        lowered = message.lower()

        if response.status_code in {401, 403}:
            raise AuthenticationError(
                "AgentRouter rejected the model API key or client policy"
            )
        if response.is_success:
            if api == "responses":
                reply = _validate_responses(parsed)
            elif api == "chat":
                reply = _validate_chat(parsed)
            else:
                reply = _validate_anthropic(parsed)
            return ModelProbe(ProbeOutcome.AVAILABLE, api, latency_ms, reply)
        if response.status_code in {402, 503}:
            return ModelProbe(ProbeOutcome.UNAVAILABLE, api, latency_ms)
        if response.status_code == 404 and any(
            marker in lowered for marker in ("model", "not found", "不存在")
        ):
            return ModelProbe(ProbeOutcome.UNAVAILABLE, api, latency_ms)
        if response.status_code == 400 and any(
            marker in lowered
            for marker in (
                "no available channel",
                "无可用渠道",
                "budget pool quota",
                "model is not available",
            )
        ):
            return ModelProbe(ProbeOutcome.UNAVAILABLE, api, latency_ms)
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            raise TransientClientError(
                f"Probe for {observation.name} returned HTTP {response.status_code}"
            )
        raise ProtocolError(
            f"Probe for {observation.name} returned HTTP {response.status_code}: "
            f"{message[:300]}"
        )

    async def confirm_model(
        self, model: str | ModelObservation
    ) -> ProbeOutcome:
        observation = (
            model
            if isinstance(model, ModelObservation)
            else ModelObservation(model, "unknown", "none", ("responses",))
        )
        return (await self.probe_model(observation)).outcome


@dataclass(frozen=True)
class TelegramBot:
    id: int
    username: str


class TelegramClient:
    def __init__(
        self,
        settings: TelegramSettings,
        timeout_seconds: int = 30,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.chat_id = settings.chat_id
        self.bot: TelegramBot | None = None
        self.send_retry_at: datetime | None = None
        self._send_lock = asyncio.Lock()
        self.timeout_seconds = timeout_seconds
        self._owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": TELEGRAM_USER_AGENT},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def _request(
        self, method: str, data: dict[str, Any], *, poll_seconds: int = 0
    ) -> Any:
        url = (
            f"{self.settings.api_base_url}/bot"
            f"{self.settings.bot_token}/{method}"
        )
        try:
            response = await self.http.post(
                url,
                json=data,
                timeout=max(self.timeout_seconds, poll_seconds + 5),
            )
        except httpx.RequestError:
            raise TransientClientError("Telegram request failed") from None

        try:
            payload = response.json()
        except ValueError:
            payload = None
        error_code = response.status_code
        if response.is_success and isinstance(payload, dict) and payload.get("ok") is False:
            error_code = payload.get("error_code", error_code)
        if error_code in (401, 403, 404):
            raise AuthenticationError("Telegram bot token or chat access was rejected")
        if error_code == 409:
            raise ProtocolError(
                "Telegram polling conflicts with a webhook or another bot instance. "
                "Remove the webhook or stop the other update listener before retrying."
            )
        if error_code == 429:
            parameters = payload.get("parameters") if isinstance(payload, dict) else None
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if type(retry_after) is not int or retry_after <= 0:
                retry_after = 30
            raise TransientClientError("Telegram rate limit reached", retry_after)
        if isinstance(error_code, int) and error_code >= 500:
            raise TransientClientError(f"Telegram returned error {error_code}")
        if not response.is_success:
            raise ProtocolError(f"Telegram returned HTTP {response.status_code}")
        if payload is None:
            raise ProtocolError("Telegram returned invalid JSON")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ProtocolError("Telegram rejected the request")
        if "result" not in payload:
            raise ProtocolError("Telegram response did not contain a result")
        return payload["result"]

    async def get_me(self) -> TelegramBot:
        result = await self._request("getMe", {})
        if (
            not isinstance(result, dict)
            or type(result.get("id")) is not int
            or result["id"] <= 0
            or result.get("is_bot") is not True
            or not isinstance(result.get("username"), str)
            or not result["username"]
        ):
            raise ProtocolError("Telegram returned an invalid bot identity")
        self.bot = TelegramBot(result["id"], result["username"])
        return self.bot

    async def configure_menu(self, commands: list[dict[str, str]]) -> None:
        try:
            chat_id = int(self.chat_id or "")
        except ValueError:
            raise ProtocolError("Telegram menus require a registered private chat") from None
        if chat_id <= 0:
            raise ProtocolError("Telegram menus require a registered private chat")
        result = await self._request(
            "setMyCommands",
            {"commands": commands, "scope": {"type": "chat", "chat_id": chat_id}},
        )
        if result is not True:
            raise ProtocolError("Telegram did not accept the command menu")
        result = await self._request(
            "setChatMenuButton",
            {"chat_id": chat_id, "menu_button": {"type": "commands"}},
        )
        if result is not True:
            raise ProtocolError("Telegram did not accept the menu button")

    async def get_updates(
        self, offset: int | None = None, *, timeout: int = 10
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            data["offset"] = offset
        result = await self._request("getUpdates", data, poll_seconds=timeout)
        if not isinstance(result, list) or any(
            not isinstance(update, dict)
            or type(update.get("update_id")) is not int
            or update["update_id"] < 0
            for update in result
        ):
            raise ProtocolError("Telegram returned invalid updates")
        return sorted(result, key=lambda update: update["update_id"])

    async def answer_callback_query(self, query_id: str, text: str | None = None) -> None:
        data: dict[str, Any] = {"callback_query_id": query_id}
        if text:
            data["text"] = text[:200]
        result = await self._request("answerCallbackQuery", data)
        if result is not True:
            raise ProtocolError("Telegram did not accept the callback answer")

    async def send(
        self, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> Any:
        if not self.chat_id:
            raise ProtocolError("Telegram chat is not registered yet")
        async with self._send_lock:
            if self.send_retry_at is not None:
                remaining = (self.send_retry_at - utc_now()).total_seconds()
                if remaining > 0:
                    raise TransientClientError(
                        "Telegram rate limit is still active", math.ceil(remaining)
                    )
            self.send_retry_at = None
            data: dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                data["reply_markup"] = reply_markup
            try:
                return await self._request("sendMessage", data)
            except TransientClientError as exc:
                if exc.retry_after is not None:
                    self.send_retry_at = utc_now() + timedelta(seconds=exc.retry_after)
                raise
