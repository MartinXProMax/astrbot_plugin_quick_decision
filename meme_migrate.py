"""将旧自制图库迁移为官方表情包规范图包，不修改 meme_manager 源码。

写入 packs/<pack_id> + registry.json 记录，不覆盖已有图包，不改 selection_rules。
仅在可以原样保留已有描述时实施；无法验证时返回阻碍，不丢弃旧库文件。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .legacy_meme_index import LegacyMemeIndex
from .meme_adapter import (
    MemeManagerAdapter,
    content_sha256,
    semantic_entry_id,
)
from .plugin_paths import MEME_MANAGER_DATA_NAME

DEFAULT_PACK_ID = "quick-decision-legacy"
DEFAULT_PACK_NAME = "快速决策迁移图包"
DEFAULT_CATEGORY = "imported"
CATEGORY_DESCRIPTION = "从快速决策旧自制图库迁移的图片，保留每张原描述。"
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
JOURNAL_NAME = "meme_manager_migration.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """替换单个 JSON 文件，避免中断时留下半个文件。"""
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def journal_path(plugin_data_dir: Path) -> Path:
    return plugin_data_dir / JOURNAL_NAME


def load_journal(plugin_data_dir: Path) -> dict[str, Any] | None:
    path = journal_path(plugin_data_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_journal(plugin_data_dir: Path, data: dict[str, Any]) -> None:
    path = journal_path(plugin_data_dir)
    _atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    )


def _safe_filename(original: str, fallback_stem: str, suffix: str) -> str:
    name = Path(original).name
    if SAFE_FILENAME.match(name) and Path(name).suffix.lower() == suffix.lower():
        return name
    stem = fallback_stem if SAFE_FILENAME.match(fallback_stem) else "meme"
    return f"{stem}{suffix}"


def build_pack_payload(
    records,
    *,
    pack_id: str,
    pack_name: str = DEFAULT_PACK_NAME,
    existing_hashes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing_hashes = existing_hashes or {}
    images: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for record in records:
        meme = record.item
        if record.missing or record.path is None:
            items.append(
                {
                    "legacy_id": meme.id,
                    "filename": meme.filename,
                    "status": "skipped_missing_file",
                    "caption_preserved": False,
                }
            )
            continue
        data = record.path.read_bytes()
        digest = content_sha256(data)
        suffix = record.path.suffix.lower() or ".jpg"
        if digest in existing_hashes:
            existing = existing_hashes[digest]
            existing_caption = getattr(existing, "description", "") or ""
            if existing_caption.strip() == "（暂无描述）":
                existing_caption = ""
            source_caption = (meme.description or "").strip()
            items.append(
                {
                    "legacy_id": meme.id,
                    "filename": meme.filename,
                    "content_sha256": digest,
                    "status": "skipped_duplicate",
                    "existing_pack_id": getattr(existing, "pack_id", ""),
                    "existing_path": str(getattr(existing, "relative_path", "")),
                    "existing_entry_id": getattr(existing, "id", ""),
                    "caption_preserved": bool(
                        source_caption and existing_caption == source_caption
                    ),
                    "existing_caption_empty": not bool(existing_caption),
                    "source_description": source_caption,
                }
            )
            continue
        filename = _safe_filename(meme.filename or record.path.name, meme.id, suffix)
        if filename in used_names:
            filename = _safe_filename(f"{meme.id}{suffix}", meme.id, suffix)
        used_names.add(filename)
        relative_path = f"memes/{DEFAULT_CATEGORY}/{filename}"
        entry_id = semantic_entry_id(digest, DEFAULT_CATEGORY, relative_path)
        caption = (meme.description or "").strip()
        caption_status = "done" if caption else "pending"
        provenance = meme.description_source if meme.description_source in {"manual", "auto"} else "quick_decision"
        images[entry_id] = {
            "entry_id": entry_id,
            "content_sha256": digest,
            "relative_path": relative_path,
            "category": DEFAULT_CATEGORY,
            "caption": caption,
            "caption_status": caption_status,
            "provenance": provenance,
        }
        files.append(
            {
                "relative_path": relative_path,
                "data": data,
                "legacy_id": meme.id,
                "entry_id": entry_id,
                "content_sha256": digest,
                "caption": caption,
            }
        )
        items.append(
            {
                "legacy_id": meme.id,
                "filename": meme.filename,
                "content_sha256": digest,
                "entry_id": entry_id,
                "relative_path": relative_path,
                "status": "migrated",
                "caption_preserved": bool(caption) or not bool(meme.description),
            }
        )

    captions_needed = [
        item
        for item in items
        if item["status"] == "migrated" and (item.get("caption") or item.get("caption_preserved"))
    ]
    manifest = {
        "schema_version": 1,
        "id": pack_id,
        "name": pack_name,
        "version": "1.0.0",
        "description": "从快速决策 1.1.x 自制图库迁移，保留每张原描述。",
        "author": "狼与葡萄酒",
        "categories": {DEFAULT_CATEGORY: {"description": CATEGORY_DESCRIPTION}},
        "extensions": {"semantic": {"version": 1, "file": "semantic_metadata.json"}},
    }
    semantic = {
        "schema_version": "2.0",
        "pack_id": pack_id,
        "images": images,
    }
    memes_data = {DEFAULT_CATEGORY: CATEGORY_DESCRIPTION}
    return {
        "manifest": manifest,
        "semantic": semantic,
        "memes_data": memes_data,
        "files": files,
        "items": items,
        "captions_needed": captions_needed,
    }


def _verify_written_pack(pack_dir: Path, payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    semantic = json.loads((pack_dir / "semantic_metadata.json").read_text(encoding="utf-8"))
    images = semantic.get("images") or {}
    if semantic.get("pack_id") != payload["manifest"]["id"]:
        errors.append("semantic pack_id 与清单不一致")
    for file_item in payload["files"]:
        path = pack_dir / file_item["relative_path"]
        if not path.is_file():
            errors.append(f"缺少文件 {file_item['relative_path']}")
            continue
        digest = content_sha256(path.read_bytes())
        if digest != file_item["content_sha256"]:
            errors.append(f"哈希不一致 {file_item['relative_path']}")
        rec = images.get(file_item["entry_id"]) or {}
        if rec.get("caption") != file_item["caption"]:
            errors.append(f"描述未保留 {file_item['legacy_id']}")
        if rec.get("content_sha256") != file_item["content_sha256"]:
            errors.append(f"语义哈希不一致 {file_item['legacy_id']}")
    return errors


def _allocate_pack_id(packs_dir: Path, base: str) -> str:
    if not (packs_dir / base).exists():
        return base
    index = 2
    while (packs_dir / f"{base}-{index}").exists():
        index += 1
    return f"{base}-{index}"


def _append_registry(data_dir: Path, pack_id: str, pack_name: str, version: str) -> None:
    path = data_dir / "registry.json"
    registry = {"schema_version": 1, "installed_packs": []}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            registry = loaded
    packs = registry.get("installed_packs")
    if not isinstance(packs, list):
        packs = []
        registry["installed_packs"] = packs
    for item in packs:
        if isinstance(item, dict) and item.get("id") == pack_id:
            return
    packs.append(
        {
            "id": pack_id,
            "name": pack_name,
            "version": version,
            "enabled": True,
            "installed_at": datetime.now(UTC).isoformat(),
        }
    )
    registry["schema_version"] = 1
    _atomic_write_text(
        path, json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    )


def _remove_registry_entry(data_dir: Path, pack_id: str) -> None:
    path = data_dir / "registry.json"
    if not path.is_file():
        return
    registry = json.loads(path.read_text(encoding="utf-8-sig"))
    packs = registry.get("installed_packs")
    if not isinstance(packs, list):
        return
    registry["installed_packs"] = [
        item
        for item in packs
        if not (isinstance(item, dict) and item.get("id") == pack_id)
    ]
    _atomic_write_text(
        path, json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    )


def migrate_legacy_library(
    plugin_data_dir: Path,
    meme_manager_data_dir: Path,
    *,
    pack_id: str = DEFAULT_PACK_ID,
    force: bool = False,
) -> dict[str, Any]:
    """把旧自制库导入为新图包。不删除旧文件。"""
    existing = load_journal(plugin_data_dir)
    if existing and existing.get("status") == "completed" and not force:
        return {**existing, "skipped": True, "reason": "already_migrated"}

    if not meme_manager_data_dir.is_dir():
        return {
            "status": "blocked",
            "reason": "meme_manager_data_missing",
            "message": "未找到表情包管理器数据目录，已中止迁移，旧库未改动。",
        }

    index = LegacyMemeIndex(plugin_data_dir)
    records = index.list_records()
    if not records:
        result = {
            "status": "completed",
            "reason": "nothing_to_migrate",
            "pack_id": None,
            "migrated": 0,
            "items": [],
        }
        save_journal(plugin_data_dir, result)
        return result

    adapter = MemeManagerAdapter(
        data_dir=meme_manager_data_dir, require_plugin=False
    )
    existing_hashes = adapter.existing_content_hashes()
    packs_dir = meme_manager_data_dir / "packs"
    packs_dir.mkdir(parents=True, exist_ok=True)
    allocated = _allocate_pack_id(packs_dir, pack_id)
    payload = build_pack_payload(
        records, pack_id=allocated, existing_hashes=existing_hashes
    )

    # 有源描述却无法写入语义文件时阻断
    source_with_desc = [
        rec for rec in records if (rec.item.description or "").strip() and not rec.missing
    ]
    if source_with_desc:
        preserved = {
            item["legacy_id"]
            for item in payload["items"]
            if item.get("status") == "migrated" and item.get("caption_preserved")
        }
        unresolved = []
        for rec in source_with_desc:
            item = next((row for row in payload["items"] if row["legacy_id"] == rec.item.id), None)
            if item is None:
                unresolved.append(rec.item.id)
                continue
            if item["status"] == "migrated" and rec.item.id not in preserved:
                unresolved.append(rec.item.id)
            if item["status"] == "skipped_duplicate" and not (
                item.get("caption_preserved") or item.get("existing_caption_empty")
            ):
                # 已有图且描述不同：不覆盖，记录阻碍但不丢旧数据
                unresolved.append(rec.item.id)
        # duplicates with empty existing caption can be filled later; migrated must preserve
        caption_block = [
            rec.item.id
            for rec in source_with_desc
            if rec.item.id not in preserved
            and not any(
                row["legacy_id"] == rec.item.id and row["status"] == "skipped_duplicate"
                for row in payload["items"]
            )
        ]
        if caption_block:
            return {
                "status": "blocked",
                "reason": "cannot_preserve_descriptions",
                "message": "无法在目标图包中验证保留原描述，已中止迁移，旧库未改动。",
                "legacy_ids": caption_block,
            }

    to_write = [item for item in payload["items"] if item["status"] == "migrated"]
    fillable_dups = [
        item
        for item in payload["items"]
        if item["status"] == "skipped_duplicate"
        and item.get("existing_caption_empty")
        and item.get("source_description")
    ]
    if not to_write and not fillable_dups:
        result = {
            "status": "completed",
            "reason": "all_skipped",
            "pack_id": None,
            "migrated": 0,
            "items": payload["items"],
        }
        save_journal(plugin_data_dir, result)
        return result

    pack_dir = packs_dir / allocated
    created = False
    if to_write:
        try:
            pack_dir.mkdir(parents=True, exist_ok=False)
            created = True
            for file_item in payload["files"]:
                dest = pack_dir / file_item["relative_path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(file_item["data"])
            (pack_dir / "manifest.json").write_text(
                json.dumps(payload["manifest"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (pack_dir / "memes_data.json").write_text(
                json.dumps(payload["memes_data"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (pack_dir / "semantic_metadata.json").write_text(
                json.dumps(payload["semantic"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            errors = _verify_written_pack(pack_dir, payload)
            if errors:
                shutil.rmtree(pack_dir, ignore_errors=True)
                return {
                    "status": "blocked",
                    "reason": "verification_failed",
                    "message": "写入后校验未通过，已回滚新图包，旧库未改动。",
                    "errors": errors,
                }
            _append_registry(
                meme_manager_data_dir,
                allocated,
                payload["manifest"]["name"],
                payload["manifest"]["version"],
            )
        except Exception as exc:  # noqa: BLE001
            if created:
                shutil.rmtree(pack_dir, ignore_errors=True)
            logger.exception("迁移图包写入失败")
            return {
                "status": "blocked",
                "reason": "write_failed",
                "message": f"写入新图包失败：{exc}。旧库未改动。",
            }

    filled = []
    for item in fillable_dups:
        # 只给已有空描述补上原描述，不改图片，也不覆盖非空描述。
        existing = existing_hashes.get(item["content_sha256"])
        if existing is None:
            continue
        pack_dir_existing = meme_manager_data_dir / "packs" / existing.pack_id
        semantic_path = pack_dir_existing / "semantic_metadata.json"
        data = {"schema_version": "2.0", "pack_id": existing.pack_id, "images": {}}
        if semantic_path.is_file():
            loaded = json.loads(semantic_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        images = data.setdefault("images", {})
        rec = images.get(existing.id)
        if not isinstance(rec, dict):
            rec = {
                "entry_id": existing.id,
                "content_sha256": existing.content_sha256,
                "relative_path": existing.relative_path,
                "category": existing.relative_path.split("/")[1]
                if existing.relative_path.startswith("memes/")
                else DEFAULT_CATEGORY,
            }
            images[existing.id] = rec
        if str(rec.get("caption") or "").strip():
            continue
        rec["caption"] = item["source_description"]
        rec["caption_status"] = "done"
        rec.setdefault("provenance", "quick_decision")
        _atomic_write_text(
            semantic_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )
        item["caption_preserved"] = True
        item["status"] = "filled_existing_caption"
        filled.append(item["legacy_id"])

    result = {
        "status": "completed",
        "version": 1,
        "pack_id": allocated if to_write else None,
        "migrated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "source_index": str(index.index_path),
        "migrated": len(to_write),
        "filled_existing": len(filled),
        "items": payload["items"],
        "rollback": {
            "remove_pack_id": allocated if to_write else None,
            "restore_selection_rules": False,
        },
    }
    save_journal(plugin_data_dir, result)
    return result


def rollback_migration(
    plugin_data_dir: Path, meme_manager_data_dir: Path
) -> dict[str, Any]:
    """移除本次迁移写入的图包和注册表项，不删除旧自制库。"""
    journal = load_journal(plugin_data_dir)
    if not journal:
        return {"status": "blocked", "reason": "no_journal"}
    pack_id = ((journal.get("rollback") or {}) or {}).get("remove_pack_id") or journal.get(
        "pack_id"
    )
    if not pack_id:
        return {"status": "completed", "reason": "nothing_to_rollback"}
    pack_dir = meme_manager_data_dir / "packs" / pack_id
    if pack_dir.is_dir():
        shutil.rmtree(pack_dir)
    _remove_registry_entry(meme_manager_data_dir, pack_id)
    journal["status"] = "rolled_back"
    journal["rolled_back_at"] = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    save_journal(plugin_data_dir, journal)
    return {"status": "rolled_back", "pack_id": pack_id}


def maybe_migrate_legacy_library(
    plugin_data_dir: Path,
    meme_manager_data_dir: Path | None = None,
) -> dict[str, Any] | None:
    journal = load_journal(plugin_data_dir)
    if journal and journal.get("status") in {"completed", "rolled_back"}:
        return journal
    if meme_manager_data_dir is None or not meme_manager_data_dir.is_dir():
        logger.warning("表情包管理器数据目录不可用，跳过旧库迁移，旧库保持原样。")
        return None
    if not LegacyMemeIndex(plugin_data_dir).exists():
        return None
    result = migrate_legacy_library(plugin_data_dir, meme_manager_data_dir)
    if result.get("status") == "blocked":
        logger.warning("旧自制图库迁移已中止：%s", result.get("message") or result.get("reason"))
    else:
        logger.info(
            "旧自制图库迁移结果：status=%s migrated=%s pack_id=%s",
            result.get("status"),
            result.get("migrated"),
            result.get("pack_id"),
        )
    return result
