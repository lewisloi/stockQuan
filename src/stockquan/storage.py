from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self, default: Any) -> Any:
        if not self.path.exists():
            return default
        with self.path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def write(self, value: Any) -> None:
        normalized = self._normalize(value)
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(normalized, file, indent=2, ensure_ascii=False)

    def _normalize(self, value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, list):
            return [self._normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: self._normalize(item) for key, item in value.items()}
        return value

