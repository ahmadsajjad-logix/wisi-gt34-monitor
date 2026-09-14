from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = 1

@dataclass(slots=True)
class ComponentState:
    state: str = "UNKNOWN"       # ACTIVE / SUSPECTED_LOSS / CONFIRMED_LOSS / RECOVERING / UNKNOWN / NOT_DECLARED
    inactive_streak: int = 0
    recovery_streak: int = 0
    last_good_utc: str | None = None
    last_bad_utc: str | None = None
    last_seen_utc: str | None = None

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _component_key(host: str, module: int, channel: int, sid: int, component: str) -> str:
    return f"{host}|{module}|{channel}|{sid}|{component.upper()}"

class PersistentAVState:
    """Small atomic JSON state store. No PRTG/database/baseline/config writes."""
    def __init__(self, path: str | Path, confirm_loss: int = 3, confirm_recovery: int = 2):
        if confirm_loss < 2:
            raise ValueError("confirm_loss must be >= 2")
        if confirm_recovery < 1:
            raise ValueError("confirm_recovery must be >= 1")
        self.path = Path(path)
        self.confirm_loss = confirm_loss
        self.confirm_recovery = confirm_recovery
        self.components: dict[str, ComponentState] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("version") != STATE_VERSION:
            raise ValueError(f"Unsupported A/V state version: {raw.get('version')}")
        for key, value in raw.get("components", {}).items():
            self.components[key] = ComponentState(**value)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_utc": utc_now(),
            "confirm_loss": self.confirm_loss,
            "confirm_recovery": self.confirm_recovery,
            "components": {k: asdict(v) for k, v in sorted(self.components.items())},
        }
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                        dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def reset_scope(self, host: str, module: int, channel: int) -> None:
        prefix = f"{host}|{module}|{channel}|"
        for key in list(self.components):
            if key.startswith(prefix):
                del self.components[key]

    def observe(self, host: str, module: int, channel: int, sid: int,
                component: str, observation: str, now: str | None = None) -> ComponentState:
        """
        observation: ACTIVE / INACTIVE / UNKNOWN / NOT_DECLARED.
        UNKNOWN never creates or advances a fault. Carrier/TS suppression should
        call reset_scope() instead of observe().
        """
        now = now or utc_now()
        key = _component_key(host, module, channel, sid, component)
        st = self.components.setdefault(key, ComponentState())
        st.last_seen_utc = now

        if observation == "NOT_DECLARED":
            st.state = "NOT_DECLARED"
            st.inactive_streak = 0
            st.recovery_streak = 0
            return st

        if observation == "UNKNOWN":
            # Fail closed: preserve evidence but never advance a loss/recovery.
            if st.state not in {"CONFIRMED_LOSS", "SUSPECTED_LOSS", "RECOVERING"}:
                st.state = "UNKNOWN"
            return st

        if observation == "INACTIVE":
            st.last_bad_utc = now
            st.inactive_streak += 1
            st.recovery_streak = 0
            st.state = "CONFIRMED_LOSS" if st.inactive_streak >= self.confirm_loss else "SUSPECTED_LOSS"
            return st

        if observation != "ACTIVE":
            raise ValueError(f"Unsupported observation: {observation}")

        st.last_good_utc = now
        st.inactive_streak = 0
        if st.state == "CONFIRMED_LOSS":
            st.recovery_streak = 1
            st.state = "ACTIVE" if self.confirm_recovery == 1 else "RECOVERING"
        elif st.state == "RECOVERING":
            st.recovery_streak += 1
            if st.recovery_streak >= self.confirm_recovery:
                st.state = "ACTIVE"
                st.recovery_streak = 0
        else:
            st.state = "ACTIVE"
            st.recovery_streak = 0
        return st

def service_verdict(video_declared: bool, audio_declared: bool,
                    video: ComponentState, audio: ComponentState) -> str:
    vloss = video_declared and video.state == "CONFIRMED_LOSS"
    aloss = audio_declared and audio.state == "CONFIRMED_LOSS"
    if vloss and aloss:
        return "CONFIRMED_VIDEO_AUDIO_LOSS"
    if vloss:
        return "CONFIRMED_VIDEO_LOSS"
    if aloss:
        return "CONFIRMED_AUDIO_LOSS"
    if (video_declared and video.state == "SUSPECTED_LOSS") or (audio_declared and audio.state == "SUSPECTED_LOSS"):
        return "SUSPECTED_LOSS"
    if (video_declared and video.state == "RECOVERING") or (audio_declared and audio.state == "RECOVERING"):
        return "RECOVERING"
    if (video_declared and video.state == "UNKNOWN") or (audio_declared and audio.state == "UNKNOWN"):
        return "UNKNOWN"
    return "ACTIVE"
