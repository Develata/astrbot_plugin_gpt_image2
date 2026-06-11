from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AccessConfig
from .models import JobOrigin


@dataclass(slots=True)
class AccessDecision:
    allowed: bool
    silent: bool = False
    reason: str = ""
    reserved: bool = False


class AccessController:
    def __init__(self, *, config: AccessConfig, state_path: Path) -> None:
        self.config = config
        self.state_path = state_path
        self._state: dict[str, dict[str, int]] = {"daily": {}}
        self.load()

    def load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(raw, dict) and isinstance(raw.get("daily"), dict):
            self._state = {"daily": {str(k): int(v) for k, v in raw["daily"].items()}}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def check_and_reserve(self, origin: JobOrigin) -> AccessDecision:
        """Check ACL and reserve one daily quota slot synchronously.

        The method intentionally has no await points, so in AstrBot's event loop a
        non-whitelisted user's quota cannot be bypassed by two concurrent submits
        checking before either increments.
        """
        if not self.config.enabled:
            return AccessDecision(True)
        decision = self._check_group(origin)
        if not decision.allowed:
            return decision
        user_ids = _split_ids(self.config.user_whitelist)
        if not user_ids:
            return AccessDecision(True)
        if origin.sender_id in user_ids:
            return AccessDecision(True)
        limit = self.config.non_whitelist_daily_limit
        if limit <= 0:
            return AccessDecision(False, reason="user_not_whitelisted")
        key = self._daily_key(origin.sender_id)
        self._prune_old_days()
        daily = self._state.setdefault("daily", {})
        used = int(daily.get(key, 0))
        if used >= limit:
            return AccessDecision(False, reason=f"daily_limit_exceeded:{used}/{limit}")
        daily[key] = used + 1
        self.save()
        return AccessDecision(True, reserved=True)

    def release_reservation(self, origin: JobOrigin, decision: AccessDecision) -> None:
        if not decision.reserved:
            return
        key = self._daily_key(origin.sender_id)
        daily = self._state.setdefault("daily", {})
        current = int(daily.get(key, 0))
        if current <= 1:
            daily.pop(key, None)
        else:
            daily[key] = current - 1
        self.save()

    def check(self, origin: JobOrigin) -> AccessDecision:
        """Read-only check for tests/status; submissions should use check_and_reserve."""
        if not self.config.enabled:
            return AccessDecision(True)
        decision = self._check_group(origin)
        if not decision.allowed:
            return decision
        user_ids = _split_ids(self.config.user_whitelist)
        if not user_ids:
            return AccessDecision(True)
        if origin.sender_id in user_ids:
            return AccessDecision(True)
        limit = self.config.non_whitelist_daily_limit
        if limit <= 0:
            return AccessDecision(False, reason="user_not_whitelisted")
        used = self._daily_used(origin.sender_id)
        if used >= limit:
            return AccessDecision(False, reason=f"daily_limit_exceeded:{used}/{limit}")
        return AccessDecision(True)

    def _check_group(self, origin: JobOrigin) -> AccessDecision:
        group_ids = _split_ids(self.config.group_whitelist)
        if not group_ids:
            return AccessDecision(True)
        if not origin.is_group_chat:
            return AccessDecision(True)
        if not origin.group_id or origin.group_id not in group_ids:
            return AccessDecision(False, silent=True, reason="group_not_whitelisted")
        return AccessDecision(True)

    def _daily_used(self, sender_id: str) -> int:
        self._prune_old_days()
        return int(self._state.setdefault("daily", {}).get(self._daily_key(sender_id), 0))

    def _daily_key(self, sender_id: str) -> str:
        day = time.strftime("%Y-%m-%d", time.localtime())
        return f"{day}:{sender_id or '<unknown>'}"

    def _prune_old_days(self) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime())
        daily = self._state.setdefault("daily", {})
        for key in list(daily):
            if not key.startswith(today + ":"):
                daily.pop(key, None)


def _split_ids(value: str | list[str] | tuple[str, ...] | None) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    else:
        raw = list(value)
    return {str(item).strip() for item in raw if str(item).strip()}
