"""Audit log and alerts.

Every decision the executor makes - and every one it declines to make - is one
JSON line in state/audit/<date>.jsonl. When something goes wrong at 11:42 on a
Tuesday, this is how you find out what the system believed at 11:41.

Alerts go to state/alerts.log and, if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
are set, to a Telegram chat. An unattended trading process that cannot tell you
it is in trouble is worse than no process at all.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

_SECRET_HINTS = ("token", "secret", "totp", "password", "api_key", "authorization")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if any(h in str(k).lower() for h in _SECRET_HINTS) else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


class Audit:
    def __init__(self, state_dir: str, clock, echo: bool = True):
        self.dir = os.path.join(state_dir, "audit")
        self.clock = clock
        self.echo = echo
        os.makedirs(self.dir, exist_ok=True)

    def log(self, event: str, **data: Any) -> None:
        now = self.clock.now()
        rec = {"ts": now.isoformat(timespec="seconds"), "event": event, **_redact(data)}
        path = os.path.join(self.dir, f"{now.date().isoformat()}.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str, sort_keys=True) + "\n")
        if self.echo:
            brief = ", ".join(f"{k}={v}" for k, v in data.items()
                              if not isinstance(v, (dict, list)))[:160]
            print(f"  {now.strftime('%H:%M:%S')} {event:<22} {brief}")

    def read(self, day: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.dir, f"{day}.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class Notifier:
    LEVELS = ("info", "warn", "critical")

    def __init__(self, state_dir: str, clock, min_level: str = "warn"):
        self.path = os.path.join(state_dir, "alerts.log")
        self.clock = clock
        self.min_level = min_level
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.sent: List[str] = []

    def __call__(self, level: str, msg: str) -> None:
        line = f"{self.clock.now().isoformat(timespec='seconds')} [{level.upper()}] {msg}"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.sent.append(line)
        if self.LEVELS.index(level) < self.LEVELS.index(self.min_level):
            return
        if self.token and self.chat:
            self._telegram(f"swingtrader {level.upper()}: {msg}")

    def _telegram(self, text: str) -> Optional[str]:
        try:
            data = urllib.parse.urlencode({"chat_id": self.chat, "text": text[:3900]}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=8) as r:       # noqa: S310
                return r.read().decode("utf-8", "replace")
        except Exception as exc:                                    # noqa: BLE001
            # Never let a notification failure take down the trading loop.
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(f"  (telegram delivery failed: {type(exc).__name__})\n")
            return None
