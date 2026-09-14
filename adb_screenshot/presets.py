"""書籍ごとのキャプチャ設定（領域・タップ位置など）を JSON に保存するプリセット管理。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PRESETS_PATH = Path(__file__).resolve().parent.parent / "presets.json"


@dataclass
class Preset:
    """1 書籍ぶんのキャプチャ設定。空文字／None は「未設定（現在値を維持）」を表す。"""

    name: str
    region: str = ""  # "x,y,w,h"（映像座標）
    tap: str = ""  # "x,y"（映像座標）
    interval: float | None = None
    settle: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Preset":
        return cls(
            name=str(data.get("name", "")),
            region=str(data.get("region", "") or ""),
            tap=str(data.get("tap", "") or ""),
            interval=_opt_float(data.get("interval")),
            settle=_opt_float(data.get("settle")),
        )


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


class PresetStore:
    """presets.json の読み書き。名前の登録順を保つ。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PRESETS_PATH
        self._presets: dict[str, Preset] = {}
        self.load()

    def load(self) -> None:
        self._presets = {}
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("プリセットを読めませんでした (%s): %s", self.path, e)
            return
        for item in data.get("presets", []):
            preset = Preset.from_dict(item)
            if preset.name:
                self._presets[preset.name] = preset

    def save(self) -> None:
        payload = {"presets": [asdict(p) for p in self._presets.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def names(self) -> list[str]:
        return list(self._presets.keys())

    def get(self, name: str) -> Preset | None:
        return self._presets.get(name)

    def put(self, preset: Preset) -> None:
        """登録または上書きして保存する。"""
        if not preset.name.strip():
            raise ValueError("プリセット名（書籍名）が空です")
        self._presets[preset.name] = preset
        self.save()

    def delete(self, name: str) -> bool:
        if name not in self._presets:
            return False
        del self._presets[name]
        self.save()
        return True
