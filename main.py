"""多服务商文生图与图生图插件。

功能：
- 聊天指令 /文生图、/t2i 与 /图生图、/i2i。
- 向 AI 智能体注册 text_to_image 与 image_to_image 两个工具。
- 可视化设置页：选择提供商、填写 Base URL 与 API Key、检测可用模型、
  文生图/图生图测试，测试通过后一键保存配置。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .client import ImageProviderError, generate_image, list_models
from .providers import PROVIDERS, PROVIDER_IDS, resolve_provider

PLUGIN_NAME = "astrbot_plugin_image_studio"

# 允许从可视化设置页写入并持久化的配置键。
SAVEABLE_KEYS = [
    "provider",
    "base_url",
    "api_key",
    "model",
    "size",
    "quality",
    "n",
    "stream",
    "response_format",
    "t2i_path",
    "i2i_path",
    "timeout",
    "allow_commands",
    "register_llm_tools",
    "allowed_users",
]

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def resolve_config(config: dict) -> dict:
    """把插件配置解析成可直接调用的客户端配置。

    当用户选择内置提供商但 Base URL 仍是其它提供商的默认值时，自动纠正为
    所选提供商的地址；apikey.fan 需要流式返回，因此强制开启 stream。
    """
    cfg = dict(config)
    provider_id = str(cfg.get("provider") or "custom")
    preset = resolve_provider(provider_id)

    configured_url = (cfg.get("base_url") or "").strip().rstrip("/")
    preset_url = (preset.get("base_url") or "").strip().rstrip("/")
    if provider_id != "custom":
        other_urls = {
            (p.get("base_url") or "").strip().rstrip("/")
            for p in PROVIDERS
            if (p.get("base_url") or "").strip()
        }
        if not configured_url or (
            configured_url in other_urls and configured_url != preset_url
        ):
            cfg["base_url"] = preset_url

    if not (cfg.get("model") or "").strip():
        cfg["model"] = preset.get("default_model") or ""

    if provider_id == "apikey":
        cfg["stream"] = True

    return cfg


class ImageStudioPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config

        self._register_web_apis()
        logger.info(f"{PLUGIN_NAME} 已加载。")

    # ------------------------------------------------------------------ #
    # 基础工具方法
    # ------------------------------------------------------------------ #
    def _data_dir(self) -> Path:
        path = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _runtime_cfg(self) -> dict:
        return resolve_config(dict(self.config))

    def _check_allowed(self, event: AstrMessageEvent) -> bool:
        allowed = self.config.get("allowed_users") or []
        if not allowed:
            return True
        return event.get_sender_id() in allowed

    async def _gather_image_paths(
        self, event: AstrMessageEvent, limit: int = 3
    ) -> list[str]:
        """递归收集消息链（含引用消息）中的图片，并转为本地文件路径。"""
        found: list[Image] = []

        def walk(items):
            for comp in items or []:
                if isinstance(comp, Image):
                    found.append(comp)
                elif isinstance(comp, Reply) and getattr(comp, "chain", None):
                    walk(comp.chain)

        walk(event.get_messages())
        paths: list[str] = []
        for img in found[:limit]:
            try:
                paths.append(await img.convert_to_file_path())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"转换图片失败：{exc}")
        return paths

    async def _materialize_images(self, images: list[dict]) -> list[Path]:
        """把 b64/url 图片统一落盘，返回本地文件路径列表。"""
        out_dir = self._data_dir() / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        paths: list[Path] = []
        for idx, img in enumerate(images):
            try:
                if img.get("b64"):
                    mime = img.get("mime") or "image/png"
                    ext = _MIME_EXT.get(mime, "png")
                    data = base64.b64decode(img["b64"])
                    target = out_dir / f"img_{stamp}_{idx}.{ext}"
                    target.write_bytes(data)
                    paths.append(target)
                elif img.get("url"):
                    async with httpx.AsyncClient(
                        timeout=120, follow_redirects=True
                    ) as client:
                        resp = await client.get(img["url"])
                        resp.raise_for_status()
                    ext = "png"
                    target = out_dir / f"img_{stamp}_{idx}.{ext}"
                    target.write_bytes(resp.content)
                    paths.append(target)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"保存第 {idx + 1} 张图片失败：{exc}")
        return paths

    async def _generate_and_save(
        self, prompt: str, image_paths: list[str] | None = None
    ) -> list[Path]:
        cfg = self._runtime_cfg()
        if not (cfg.get("base_url") or "").strip():
            raise ImageProviderError("请先在插件配置中填写 Base URL")
        if not (cfg.get("api_key") or "").strip():
            raise ImageProviderError("请先在插件配置中填写 API Key")
        images = await generate_image(cfg, prompt, image_paths)
        paths = await self._materialize_images(images)
        if not paths:
            raise ImageProviderError("图片生成成功但保存失败")
        return paths

    # ------------------------------------------------------------------ #
    # 聊天指令
    # ------------------------------------------------------------------ #
    @filter.command("文生图", alias={"t2i", "draw"})
    async def cmd_text_to_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        """文生图指令：/文生图 一只在太空中的猫"""
        if not self.config.get("allow_commands", True):
            yield event.plain_result("文生图指令未启用，请先在插件配置中开启。")
            return
        if not self._check_allowed(event):
            yield event.plain_result("你没有使用该指令的权限。")
            return
        prompt = (prompt or "").strip()
        if not prompt:
            yield event.plain_result("用法：/文生图 <描述>，例如 /文生图 一只在太空中的猫")
            return
        yield event.plain_result("正在生成图片，请稍候…")
        try:
            paths = await self._generate_and_save(prompt)
            for path in paths:
                yield event.image_result(str(path))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"文生图失败：{exc}")

    @filter.command("图生图", alias={"i2i", "edit"})
    async def cmd_image_to_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        """图生图指令：发送一张图片并附上 /图生图 描述"""
        if not self.config.get("allow_commands", True):
            yield event.plain_result("图生图指令未启用，请先在插件配置中开启。")
            return
        if not self._check_allowed(event):
            yield event.plain_result("你没有使用该指令的权限。")
            return
        prompt = (prompt or "").strip()
        if not prompt:
            yield event.plain_result("用法：发送一张图片，并附上文字 /图生图 <修改描述>")
            return
        yield event.plain_result("正在生成图片，请稍候…")
        try:
            refs = await self._gather_image_paths(event)
            if not refs:
                yield event.plain_result("没有找到参考图片，请发送一张图片并附上 /图生图 描述。")
                return
            paths = await self._generate_and_save(prompt, refs)
            for path in paths:
                yield event.image_result(str(path))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"图生图失败：{exc}")

    # ------------------------------------------------------------------ #
    # LLM 工具（供 AstrBot 智能体调用）
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="text_to_image")
    async def text_to_image(self, event: AstrMessageEvent, prompt: str) -> MessageEventResult:
        """根据文字描述生成图片并直接发送给用户。

        Args:
            prompt(string): 图片的文字描述，例如“一只穿着宇航服的猫”。
        """
        if not self.config.get("register_llm_tools", True):
            yield event.plain_result("文生图工具未启用，请先在插件配置中开启。")
            return
        try:
            paths = await self._generate_and_save(prompt.strip())
            for path in paths:
                yield event.image_result(str(path))
            if not paths:
                yield event.plain_result("没有生成任何图片。")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"文生图失败：{exc}")

    @filter.llm_tool(name="image_to_image")
    async def image_to_image(self, event: AstrMessageEvent, prompt: str) -> MessageEventResult:
        """根据用户当前消息附带的参考图片进行图生图（改图），并直接发送结果。

        Args:
            prompt(string): 对参考图片的修改描述，例如“把背景换成星空”。
        """
        if not self.config.get("register_llm_tools", True):
            yield event.plain_result("图生图工具未启用，请先在插件配置中开启。")
            return
        try:
            refs = await self._gather_image_paths(event)
            if not refs:
                yield event.plain_result("没有找到参考图片，请先发送一张图片。")
                return
            paths = await self._generate_and_save(prompt.strip(), refs)
            for path in paths:
                yield event.image_result(str(path))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"图生图失败：{exc}")

    # ------------------------------------------------------------------ #
    # 可视化设置页后端 API
    # ------------------------------------------------------------------ #
    def _register_web_apis(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/providers", self.api_providers, ["GET"], "获取提供商预设"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/config", self.api_config, ["GET"], "读取当前配置"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/config/save", self.api_save_config, ["POST"], "保存配置"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/models", self.api_detect_models, ["POST"], "检测可用模型"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/test", self.api_test, ["POST"], "测试文生图/图生图"
        )

    async def api_providers(self):
        return json_response({"providers": PROVIDERS, "ids": PROVIDER_IDS})

    async def api_config(self):
        return json_response({"config": dict(self.config)})

    async def api_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        for key in SAVEABLE_KEYS:
            if key in payload:
                self.config[key] = payload[key]

        # 规范化数值与布尔值
        try:
            self.config["n"] = max(1, int(self.config.get("n") or 1))
        except (TypeError, ValueError):
            self.config["n"] = 1
        try:
            self.config["timeout"] = max(10, int(self.config.get("timeout") or 300))
        except (TypeError, ValueError):
            self.config["timeout"] = 300
        self.config["stream"] = bool(self.config.get("stream"))
        self.config["allow_commands"] = bool(self.config.get("allow_commands", True))
        self.config["register_llm_tools"] = bool(
            self.config.get("register_llm_tools", True)
        )
        if not isinstance(self.config.get("allowed_users"), list):
            self.config["allowed_users"] = []

        await self.config.save_config_async()
        return json_response({"ok": True, "config": dict(self.config)})

    async def api_detect_models(self):
        payload = await request.json(default={})
        base_url = (payload.get("base_url") or "").strip()
        api_key = (payload.get("api_key") or "").strip()
        if not base_url:
            return error_response("请先填写 Base URL", status_code=400)
        cfg = {
            "base_url": base_url,
            "api_key": api_key,
            "timeout": payload.get("timeout") or 60,
        }
        try:
            models = await list_models(cfg)
            return json_response({"ok": True, "models": models})
        except ImageProviderError as exc:
            return error_response(str(exc), status_code=400)

    async def api_test(self):
        payload = await request.json(default={})
        mode = payload.get("mode") or "text"
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            return error_response("请先填写测试 prompt", status_code=400)

        cfg = {
            "provider": payload.get("provider"),
            "base_url": payload.get("base_url"),
            "api_key": payload.get("api_key"),
            "model": payload.get("model"),
            "size": payload.get("size") or "1024x1024",
            "quality": payload.get("quality") or "auto",
            "n": payload.get("n") or 1,
            "stream": bool(payload.get("stream")),
            "response_format": payload.get("response_format") or "b64_json",
            "t2i_path": payload.get("t2i_path") or "/images/generations",
            "i2i_path": payload.get("i2i_path") or "/images/edits",
            "timeout": payload.get("timeout") or 300,
        }
        cfg = resolve_config(cfg)

        image_paths: list[str] = []
        if mode == "image":
            image_paths = await self._resolve_test_image(payload)
            if not image_paths:
                return error_response("请先上传参考图片", status_code=400)

        try:
            images = await generate_image(cfg, prompt, image_paths)
            return json_response({"ok": True, "images": images})
        except ImageProviderError as exc:
            return error_response(str(exc), status_code=400)

    async def _resolve_test_image(self, payload: dict) -> list[str]:
        tmp_dir = self._data_dir() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        b64 = payload.get("image_b64") or ""
        if b64:
            if "," in b64 and b64.lstrip().lower().startswith("data:"):
                b64 = b64.split(",", 1)[1]
            try:
                data = base64.b64decode(b64)
            except Exception:
                return []
            target = tmp_dir / f"ref_{int(time.time() * 1000)}.png"
            target.write_bytes(data)
            return [str(target)]

        url = (payload.get("image_url") or "").strip()
        if url:
            try:
                async with httpx.AsyncClient(
                    timeout=60, follow_redirects=True
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                target = tmp_dir / f"ref_{int(time.time() * 1000)}.png"
                target.write_bytes(resp.content)
                return [str(target)]
            except Exception:
                return []
        return []

    async def terminate(self):
        """插件卸载/停用时调用。"""
        logger.info(f"{PLUGIN_NAME} 已卸载。")
