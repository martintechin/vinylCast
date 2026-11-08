"""Persistent state across restarts and reboots: last speaker and UI-changed settings.

Stored as JSON at `state_path`, written atomically (temporary file in the same directory,
fsync, rename), so a power cut never leaves a half-written file. A missing file gives the
default state; an unreadable or invalid one is moved aside (`.bad`) and replaced by defaults,
so a corrupt file can never stop the service from starting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vinylcast.config import ConfigError, validate_ui_settings

log = logging.getLogger("vinylcast")
VERSION = 1


@dataclass
class PersistentState:
    last_target: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)  # UI_SETTABLE keys only


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> PersistentState:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return PersistentState()
        except OSError as e:
            log.warning("state: cannot read %s (%s); using defaults", self.path, e)
            return PersistentState()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            target = data.get("last_target")
            if target is not None and not isinstance(target, str):
                raise ValueError("last_target is not a string")
            settings = validate_ui_settings(dict(data.get("settings") or {}))
        except (ValueError, ConfigError) as e:
            bad = self.path.with_suffix(self.path.suffix + ".bad")
            os.replace(self.path, bad)
            log.warning("state: %s is invalid (%s); moved to %s, using defaults", self.path, e, bad)
            return PersistentState()
        return PersistentState(last_target=target, settings=settings)

    def save(self, state: PersistentState) -> None:
        settings = validate_ui_settings(dict(state.settings))
        payload = {"version": VERSION, "last_target": state.last_target, "settings": settings}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
