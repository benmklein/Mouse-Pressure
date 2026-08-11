"""Profile persistence for runtime configuration snapshots."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mouse_pressure.bridge.config import RuntimeConfig
from mouse_pressure.web.config_store import (
    SCHEMA_VERSION,
    resolve_config_dir,
    runtime_config_from_dict,
    runtime_config_to_dict,
)
from mouse_pressure.web.models import ProfileNotFoundError, ValidationError, validate_profile_name


class ProfileStore:
    """CRUD store for named runtime config profiles."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self.config_dir = resolve_config_dir(config_dir)
        self.profile_dir = self.config_dir / "profiles"

    def _profile_path(self, name: str) -> Path:
        return self.profile_dir / f"{name}.json"

    def list(self) -> list[dict[str, Any]]:
        if not self.profile_dir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self.profile_dir.glob("*.json")):
            rows.append(
                {
                    "name": path.stem,
                    "modified_at": int(path.stat().st_mtime),
                }
            )
        return rows

    def save(self, name: str, config: RuntimeConfig) -> None:
        errors = validate_profile_name(name)
        if errors:
            raise ValidationError(errors[0])

        clean_name = name.strip()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        payload = runtime_config_to_dict(config)
        path = self._profile_path(clean_name)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)

    def load(self, name: str) -> RuntimeConfig:
        path = self._profile_path(name)
        if not path.exists():
            raise ProfileNotFoundError(name)
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return runtime_config_from_dict(raw)

    def delete(self, name: str) -> None:
        path = self._profile_path(name)
        if not path.exists():
            raise ProfileNotFoundError(name)
        path.unlink()

    def export_json(self, name: str) -> str:
        config = self.load(name)
        return json.dumps(runtime_config_to_dict(config), indent=2, sort_keys=True)

    def import_json(self, json_str: str) -> str:
        raw = json.loads(json_str)
        if not isinstance(raw, dict):
            raise ValidationError("profile import must be a JSON object")
        schema_version = raw.get("schema_version", SCHEMA_VERSION)
        if schema_version != SCHEMA_VERSION:
            from mouse_pressure.web.models import SchemaMismatchError

            raise SchemaMismatchError(
                f"Unsupported schema_version: {schema_version!r}; expected {SCHEMA_VERSION}"
            )

        config = runtime_config_from_dict(raw)
        imported_name = self._pick_import_name(raw.get("name"))
        self.save(imported_name, config)
        return imported_name

    def _pick_import_name(self, raw_name: object) -> str:
        if isinstance(raw_name, str):
            errors = validate_profile_name(raw_name)
            if not errors:
                return raw_name.strip()

        base = f"imported_{int(time.time())}"
        candidate = base
        idx = 1
        while self._profile_path(candidate).exists():
            idx += 1
            candidate = f"{base}_{idx}"
        return candidate
