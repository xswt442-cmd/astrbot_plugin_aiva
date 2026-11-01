"""`_conf_schema.json` 与代码是否对得上。

配置项有两种坏法：代码读一个 schema 里没有的键（用户在 WebUI 里根本看不到），
或者 schema 里摆一个没人读的键（改了没反应，还让人以为功能存在）。
两边都钉住。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
MAIN_SOURCE = (REPO_ROOT / "main.py").read_text(encoding="utf-8")


def _keys_read_by_code() -> set[str]:
    keys = set(re.findall(r"config\.get\(\s*[\"']([^\"']+)[\"']", MAIN_SOURCE))
    keys |= set(
        re.findall(r"_config_(?:int|flag)\(\s*config\s*,\s*[\"']([^\"']+)[\"']", MAIN_SOURCE)
    )
    return keys


def test_keys_read_are_in_schema():
    missing = _keys_read_by_code() - set(SCHEMA)
    assert not missing, f"这些键代码在读，但 schema 里没有，用户看不到：{sorted(missing)}"


def test_schema_has_no_unused_keys():
    extra = set(SCHEMA) - _keys_read_by_code()
    assert not extra, f"这些配置项没人读，改了不会有任何反应：{sorted(extra)}"


def test_each_entry_has_type_desc_default():
    for key, entry in SCHEMA.items():
        assert entry.get("type"), f"{key} 缺 type"
        assert entry.get("description"), f"{key} 缺 description"
        assert "default" in entry, f"{key} 缺 default"
        assert entry.get("hint"), f"{key} 缺 hint，用户在 WebUI 里不知道该怎么填"


def test_types_supported():
    allowed = {"string", "int", "float", "bool", "text", "list", "dict", "file"}
    bad = {
        key: entry.get("type") for key, entry in SCHEMA.items() if entry.get("type") not in allowed
    }
    assert not bad, f"这些类型 AstrBot 不认：{bad}"
