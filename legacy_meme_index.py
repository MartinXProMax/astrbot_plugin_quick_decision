"""只读旧自制图库索引，仅供一次性迁移使用。运行时发图不得回退到这里。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import DescriptionSource, MemeItem


@dataclass
class LegacyMemeRecord:
    item: MemeItem
    path: Path | None
    missing: bool


class LegacyMemeIndex:
    """读取 1.1.x 自制图库的 memes.json 与 memes/ 文件，不做写入。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.image_dir = data_dir / "memes"
        self.index_path = data_dir / "memes.json"

    def exists(self) -> bool:
        return self.index_path.is_file()

    def list_records(self) -> list[LegacyMemeRecord]:
        if not self.exists():
            return []
        raw = json.loads(self.index_path.read_text(encoding="utf-8-sig"))
        items = raw.get("memes", raw if isinstance(raw, list) else [])
        records: list[LegacyMemeRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meme = MemeItem.from_dict(item)
            if not meme.id:
                continue
            path = (self.image_dir / meme.filename).resolve() if meme.filename else None
            if path is not None:
                try:
                    path.relative_to(self.image_dir.resolve())
                except ValueError:
                    path = None
            missing = path is None or not path.is_file()
            records.append(
                LegacyMemeRecord(item=meme, path=None if missing else path, missing=missing)
            )
        return records
