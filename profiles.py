"""多服务商配置档案存储。

每个档案独立保存提供商、Base URL、API Key、模型与生成参数；active_id 指向
当前机器人实际使用的档案。档案保存在 data/plugin_data/<plugin>/profiles.json，
重装/更新插件不会覆盖。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

PROFILE_FIELDS = (
    "provider",
    "base_url",
    "api_key",
    "model",
    "size",
    "quality",
    "n",
    "stream",
    "response_format",
    "output_format",
    "output_compression",
    "t2i_path",
    "i2i_path",
    "timeout",
)

DEFAULT_PROFILE: dict = {
    "id": "default",
    "name": "默认配置",
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-image-1",
    "size": "1024x1024",
    "quality": "auto",
    "n": 1,
    "stream": False,
    "response_format": "b64_json",
    "output_format": "png",
    "output_compression": 90,
    "t2i_path": "/images/generations",
    "i2i_path": "/images/edits",
    "timeout": 300,
}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_profile(raw: dict, *, new_id: str | None = None) -> dict:
    """把任意来源（表单、旧配置、文件）规范成一个完整档案。"""
    profile = dict(DEFAULT_PROFILE)
    profile["id"] = str(raw.get("id") or new_id or uuid.uuid4().hex[:12])
    profile["name"] = str(raw.get("name") or "未命名配置").strip() or "未命名配置"
    for key in PROFILE_FIELDS:
        if key in raw and raw[key] is not None:
            profile[key] = raw[key]

    for key in (
        "provider",
        "base_url",
        "api_key",
        "model",
        "size",
        "quality",
        "response_format",
        "output_format",
        "t2i_path",
        "i2i_path",
    ):
        profile[key] = str(profile.get(key) or "").strip()

    if not profile["provider"]:
        profile["provider"] = "openai"
    if not profile["size"]:
        profile["size"] = "1024x1024"
    if not profile["quality"]:
        profile["quality"] = "auto"
    if not profile["response_format"]:
        profile["response_format"] = "b64_json"
    if profile["output_format"] not in ("png", "jpeg", "webp"):
        profile["output_format"] = "png"
    if not profile["t2i_path"]:
        profile["t2i_path"] = "/images/generations"
    if not profile["i2i_path"]:
        profile["i2i_path"] = "/images/edits"

    profile["n"] = max(1, min(4, _as_int(profile.get("n"), 1)))
    profile["timeout"] = max(10, min(900, _as_int(profile.get("timeout"), 300)))
    profile["output_compression"] = max(
        0, min(100, _as_int(profile.get("output_compression"), 90))
    )
    profile["stream"] = bool(profile.get("stream"))
    return profile


class ProfileStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "profiles.json"

    def load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("profiles"), list):
                profiles = [
                    normalize_profile(p)
                    for p in data["profiles"]
                    if isinstance(p, dict)
                ]
                if profiles:
                    data["profiles"] = profiles
                    ids = {p["id"] for p in profiles}
                    if data.get("active_id") not in ids:
                        data["active_id"] = profiles[0]["id"]
                    return data
            # 损坏时保留备份，避免直接覆盖用户数据。
            try:
                shutil.copy2(self.path, self.path.with_suffix(".json.corrupt"))
            except OSError:
                pass
        data = {"active_id": "default", "profiles": [dict(DEFAULT_PROFILE)]}
        self.save(data)
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def ensure_from_legacy(self, config: dict) -> dict:
        """首次启用时把旧的单配置迁移为一个默认档案。"""
        if self.path.exists():
            return self.load()

        profile = dict(DEFAULT_PROFILE)
        for key in PROFILE_FIELDS:
            if key in config and config[key] not in (None, ""):
                profile[key] = config[key]
        profile = normalize_profile(profile, new_id="default")
        provider = profile.get("provider") or "openai"
        profile["name"] = f"{provider} 默认配置"
        data = {"active_id": profile["id"], "profiles": [profile]}
        self.save(data)
        return data

    def get_active(self) -> dict:
        data = self.load()
        active_id = data.get("active_id")
        for profile in data["profiles"]:
            if profile["id"] == active_id:
                return profile
        return data["profiles"][0]

    def upsert(self, raw: dict, *, set_active: bool = False) -> tuple[dict, dict]:
        data = self.load()
        profiles = data["profiles"]
        pid = str(raw.get("id") or "").strip()
        profile = normalize_profile(raw, new_id=pid or None)
        if pid:
            profile["id"] = pid
            for index, existing in enumerate(profiles):
                if existing["id"] == pid:
                    profiles[index] = profile
                    break
            else:
                profiles.append(profile)
        else:
            if not str(raw.get("name") or "").strip():
                profile["name"] = f"配置 {len(profiles) + 1}"
            profiles.append(profile)
        if set_active or not data.get("active_id"):
            data["active_id"] = profile["id"]
        self.save(data)
        return profile, data

    def delete(self, profile_id: str) -> dict:
        data = self.load()
        data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
        if not data["profiles"]:
            data["profiles"] = [normalize_profile(dict(DEFAULT_PROFILE))]
        ids = {p["id"] for p in data["profiles"]}
        if data.get("active_id") not in ids:
            data["active_id"] = data["profiles"][0]["id"]
        self.save(data)
        return data

    def activate(self, profile_id: str) -> dict:
        data = self.load()
        if any(p["id"] == profile_id for p in data["profiles"]):
            data["active_id"] = profile_id
            self.save(data)
        return data
