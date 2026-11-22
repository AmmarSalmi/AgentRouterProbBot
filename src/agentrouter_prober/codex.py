from __future__ import annotations

import json
import os
import re
import shutil
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path


class CodexConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexConfig:
    model: str | None
    provider: str | None


class CodexConfigManager:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> CodexConfig:
        try:
            text = self.path.read_text(encoding="utf-8-sig")
            parsed = tomllib.loads(text)
        except FileNotFoundError as exc:
            raise CodexConfigError(f"Codex config not found: {self.path}") from exc
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise CodexConfigError(f"Codex config cannot be read: {self.path}") from exc
        model = parsed.get("model")
        provider = parsed.get("model_provider")
        return CodexConfig(
            model if isinstance(model, str) else None,
            provider if isinstance(provider, str) else None,
        )

    @staticmethod
    def _set_top_level(text: str, key: str, value: str) -> str:
        table = re.search(r"(?m)^\s*\[", text)
        boundary = table.start() if table else len(text)
        prefix, suffix = text[:boundary], text[boundary:]
        encoded = json.dumps(value, ensure_ascii=False)
        pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
        if pattern.search(prefix):
            prefix = pattern.sub(f"{key} = {encoded}", prefix, count=1)
        else:
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            prefix += f"{key} = {encoded}\n"
            if suffix and not prefix.endswith("\n\n"):
                prefix += "\n"
        return prefix + suffix

    def set_model(self, model: str) -> CodexConfig:
        if not model or any(character in model for character in "\r\n\0"):
            raise CodexConfigError("Invalid Codex model ID")
        with self._lock:
            try:
                original = self.path.read_text(encoding="utf-8-sig")
                parsed = tomllib.loads(original)
            except FileNotFoundError as exc:
                raise CodexConfigError(f"Codex config not found: {self.path}") from exc
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise CodexConfigError(f"Codex config cannot be read: {self.path}") from exc

            providers = parsed.get("model_providers")
            if not isinstance(providers, dict) or "AgentRouter" not in providers:
                raise CodexConfigError(
                    "Codex config has no [model_providers.AgentRouter] provider"
                )

            updated = self._set_top_level(original, "model", model)
            updated = self._set_top_level(updated, "model_provider", "AgentRouter")
            try:
                check = tomllib.loads(updated)
            except tomllib.TOMLDecodeError as exc:
                raise CodexConfigError("Refusing to write invalid Codex TOML") from exc
            if check.get("model") != model or check.get("model_provider") != "AgentRouter":
                raise CodexConfigError("Codex config update did not validate")

            backup = self.path.with_name(f"{self.path.name}.agentrouter-prober.bak")
            temporary = self.path.with_name(f".{self.path.name}.agentrouter-prober.tmp")
            try:
                shutil.copy2(self.path, backup)
                temporary.write_text(updated, encoding="utf-8", newline="")
                shutil.copymode(self.path, temporary)
                os.replace(temporary, self.path)
            except OSError as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise CodexConfigError(f"Codex config cannot be updated: {self.path}") from exc
            return CodexConfig(model, "AgentRouter")
