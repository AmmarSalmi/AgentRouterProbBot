from __future__ import annotations

import asyncio
from dataclasses import replace

from .clients import AgentRouterClient, ClientError, CredentialRejected, TransientClientError
from .config import (
    ACCOUNT_ID,
    AGENTROUTER_CREDENTIALS,
    DASHBOARD_TOKEN,
    MODEL_KEY,
    Settings,
    parse_agentrouter_credential,
    save_agentrouter_credentials,
)


class AgentRouterSetup:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._credentials = {
            MODEL_KEY: settings.agentrouter_api_key,
            ACCOUNT_ID: str(settings.agentrouter_user_id) if settings.agentrouter_user_id else "",
            DASHBOARD_TOKEN: settings.agentrouter_dashboard_token,
        }
        self.problem: str | None = None
        self.retry_after: int | None = None

    @property
    def next_field(self) -> str | None:
        return next((name for name in AGENTROUTER_CREDENTIALS if not self._credentials[name]), None)

    def submit(self, value: str) -> None:
        field = self.next_field
        if field is None:
            raise RuntimeError("AgentRouter setup has no pending field")
        self._credentials[field] = parse_agentrouter_credential(field, value)
        self.problem = None
        self.retry_after = None

    async def connect(self) -> AgentRouterClient | None:
        if self.next_field is not None:
            self.retry_after = None
            return None
        settings = replace(
            self._settings,
            agentrouter_api_key=self._credentials[MODEL_KEY],
            agentrouter_user_id=int(self._credentials[ACCOUNT_ID]),
            agentrouter_dashboard_token=self._credentials[DASHBOARD_TOKEN],
        )
        client = AgentRouterClient(settings)
        connected = False
        try:
            model_check, account_check = await asyncio.gather(
                client.validate_model_key(), client.validate_dashboard_credentials(), return_exceptions=True
            )
            issues = []
            for fields, label, result in (
                ((MODEL_KEY,), "model API key", model_check),
                ((ACCOUNT_ID, DASHBOARD_TOKEN), "dashboard token and account ID pair", account_check),
            ):
                if isinstance(result, CredentialRejected):
                    for field in fields:
                        self._credentials[field] = ""
                    issues.append(f"AgentRouter rejected the {label}.")
                elif isinstance(result, ClientError):
                    issues.append(f"Could not verify the {label} with AgentRouter.")
                elif isinstance(result, BaseException):
                    raise result
            self.problem = "\n".join(issues) or None
            if self.problem:
                transient = [
                    result for result in (model_check, account_check)
                    if isinstance(result, TransientClientError)
                ]
                self.retry_after = (
                    max(result.retry_after or 0 for result in transient)
                    if len(transient) == len(issues) else None
                )
                return None
            self.retry_after = None
            try:
                save_agentrouter_credentials(settings.state_path, self._credentials)
            except OSError:
                self.problem = (
                    "Could not save the verified credentials. "
                    "Check that the bot’s data directory is writable."
                )
                return None
            connected = True
            return client
        finally:
            if not connected:
                await client.close()
