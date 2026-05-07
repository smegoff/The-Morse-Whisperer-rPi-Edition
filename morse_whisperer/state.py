from __future__ import annotations
import copy
import threading
import time
from typing import Any, Dict

class SharedState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "project": "The Morse Whisperer",
            "mode": "starting",
            "started_at": time.time(),
            "updated_at": None,
            "audio": {},
            "decode": {
                "raw": "",
                "copy": "",
                "stable_copy": "",
                "events": [],
            },
            "quality": {},
            "status": [],
            "config": {},
        }

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                self._data[k] = v
            self._data["updated_at"] = time.time()

    def merge(self, section: str, value: Dict[str, Any]) -> None:
        with self._lock:
            current = self._data.get(section, {})
            if not isinstance(current, dict):
                current = {}
            current.update(value)
            self._data[section] = current
            self._data["updated_at"] = time.time()

    def append_status(self, message: str, limit: int = 80) -> None:
        with self._lock:
            status = list(self._data.get("status", []))
            status.append({"ts": time.time(), "message": message})
            self._data["status"] = status[-limit:]
            self._data["updated_at"] = time.time()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def reset_copy(self) -> None:
        with self._lock:
            self._data["decode"] = {"raw": "", "copy": "", "stable_copy": "", "events": []}
            self._data["updated_at"] = time.time()

