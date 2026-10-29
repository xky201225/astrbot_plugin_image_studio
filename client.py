"""OpenAI 兼容的图像生成客户端（含 apikey.fan 的 SSE 流式解析）。

仅使用标准库 + httpx，不依赖 requests。所有网络调用均为异步。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import uuid
from typing import Any

import httpx

DEFAULT_TIMEOUT = 300.0


class ImageProviderError(Exception):
    """图像接口调用失败。"""


def _join_url(base_url: str, path: str) -> str:
    base = (base_url or "").rstrip("/")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base + "/" + path.lstrip("/")


def _auth_headers(api_key: str, extra_headers: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _extract_images(payload: Any) -> list[dict]:
    """从各类 OpenAI 兼容响应中提取图片，返回 [{"b64": ...} 或 {"url": ...}]。"""
    results: list[dict] = []

    def _add_item(item: Any) -> None:
        if not isinstance(item, dict):
            return
        b64 = item.get("b64_json")
        url = item.get("url")
        if b64:
            results.append({"b64": b64, "mime": _b64_mime(b64)})
        elif url:
            results.append({"url": url})

    if isinstance(payload, dict):
        if payload.get("b64_json"):
            results.append({"b64": payload["b64_json"], "mime": _b64_mime(payload["b64_json"])})
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                _add_item(item)
        # 部分接口把 base64 放在 output 下
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                _add_item(item)
    return results


def _b64_mime(b64: str) -> str:
    """根据 base64 头部判断图片 MIME，默认 png。"""
    try:
        head = base64.b64decode(b64[:32] + "=" * (-len(b64[:32]) % 4))[:8]
    except Exception:
        return "image/png"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class ImageClient:
    """封装一个提供商配置，提供 list_models 与 generate 两个核心能力。"""

    def __init__(self, config: dict) -> None:
        self.base_url = str(config.get("base_url") or "").strip()
        self.api_key = str(config.get("api_key") or "").strip()
        self.model = str(config.get("model") or "").strip()
        self.size = str(config.get("size") or "1024x1024").strip()
        self.quality = str(config.get("quality") or "auto").strip()
        self.n = int(config.get("n") or 1)
        self.stream = bool(config.get("stream"))
        self.response_format = str(config.get("response_format") or "b64_json").strip()
        self.t2i_path = str(config.get("t2i_path") or "/images/generations").strip()
        self.i2i_path = str(config.get("i2i_path") or "/images/edits").strip()
        self.timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
        self.extra_body = config.get("extra_body") or {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=30.0),
            follow_redirects=True,
        )

    async def list_models(self) -> list[str]:
        """调用 GET /models 拉取可用模型 id 列表。"""
        if not self.base_url:
            raise ImageProviderError("未填写 Base URL")
        url = _join_url(self.base_url, "/models")
        headers = _auth_headers(self.api_key)
        async with self._client() as client:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise ImageProviderError(f"请求 /models 失败：{exc}") from exc

        if resp.status_code >= 400:
            raise ImageProviderError(_format_http_error(resp))

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ImageProviderError("解析 /models 响应失败：不是合法 JSON") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ImageProviderError("接口未返回可识别的模型列表（缺少 data 数组）")

        models: list[str] = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
        if not models:
            raise ImageProviderError("接口返回的模型列表为空")
        return models

    async def generate(self, prompt: str, image_paths: list[str] | None = None) -> list[dict]:
        """文生图（无图）或图生图（传一张或多张本地图片路径）。"""
        if not self.base_url:
            raise ImageProviderError("未填写 Base URL")
        if not self.api_key:
            raise ImageProviderError("未填写 API Key")
        if not prompt.strip():
            raise ImageProviderError("prompt 不能为空")

        if image_paths:
            return await self._generate_edit(prompt, image_paths)
        return await self._generate_json(prompt)

    async def _generate_json(self, prompt: str) -> list[dict]:
        body: dict = {
            "model": self.model,
            "prompt": prompt,
            "n": self.n,
            "size": self.size,
        }
        if self.quality:
            body["quality"] = self.quality
        body["response_format"] = self.response_format
        if self.stream:
            body["stream"] = True
        body.update(self.extra_body)

        url = _join_url(self.base_url, self.t2i_path)
        headers = _auth_headers(self.api_key)
        if self.stream:
            headers["Accept"] = "text/event-stream"

        async with self._client() as client:
            try:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        raise ImageProviderError(_format_status_error(resp, text))
                    if self.stream:
                        payload = await self._read_sse_payload(resp)
                    else:
                        raw = await resp.aread()
                        payload = json.loads(raw.decode("utf-8", "replace"))
            except ImageProviderError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                raise ImageProviderError(f"文生图请求失败：{exc}") from exc

        images = _extract_images(payload)
        if not images:
            raise ImageProviderError("接口没有返回图片（未解析到 b64_json 或 url）")
        return images

    async def _generate_edit(self, prompt: str, image_paths: list[str]) -> list[dict]:
        from pathlib import Path

        fields: dict[str, str] = {
            "model": self.model,
            "prompt": prompt,
            "n": str(self.n),
            "size": self.size,
        }
        if self.quality:
            fields["quality"] = self.quality
        fields["response_format"] = self.response_format
        if self.stream:
            fields["stream"] = "true"
        for key, value in self.extra_body.items():
            fields[str(key)] = str(value)

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for path in image_paths:
            p = Path(path)
            data = p.read_bytes()
            content_type = mimetypes.guess_type(p.name)[0] or "image/png"
            files.append(("image", (p.name, data, content_type)))

        url = _join_url(self.base_url, self.i2i_path)
        headers = _auth_headers(self.api_key)
        # multipart 请求必须由 httpx 自己生成 boundary，不能预置 Content-Type。
        headers.pop("Content-Type", None)
        if self.stream:
            headers["Accept"] = "text/event-stream"

        async with self._client() as client:
            try:
                async with client.stream(
                    "POST", url, data=fields, files=files, headers=headers
                ) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        raise ImageProviderError(_format_status_error(resp, text))
                    content_type = resp.headers.get("content-type", "")
                    if self.stream and "text/event-stream" in content_type:
                        payload = await self._read_sse_payload(resp)
                    else:
                        raw = await resp.aread()
                        payload = json.loads(raw.decode("utf-8", "replace"))
            except ImageProviderError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                raise ImageProviderError(f"图生图请求失败：{exc}") from exc

        images = _extract_images(payload)
        if not images:
            raise ImageProviderError("接口没有返回图片（未解析到 b64_json 或 url）")
        return images

    @staticmethod
    async def _read_sse_payload(resp: httpx.Response) -> Any:
        """读取 SSE 流并拼接出最终（completed）事件；若只有 JSON 则直接返回。"""
        chunks: list[str] = []
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk.decode("utf-8", "replace"))

        text = "".join(chunks)
        if text.lstrip().startswith("{"):
            return json.loads(text)

        last_payload: Any = None
        buffer = ""
        for line in text.replace("\r\n", "\n").split("\n"):
            buffer += line + "\n"
            if line == "":
                frame = buffer
                buffer = ""
                data_lines = [
                    ln[5:].strip() for ln in frame.splitlines() if ln.startswith("data:")
                ]
                data = "\n".join(data_lines).strip()
                if data and data != "[DONE]":
                    try:
                        last_payload = json.loads(data)
                    except ValueError:
                        continue
        return last_payload


def _format_http_error(resp: httpx.Response) -> str:
    try:
        text = resp.text[:500]
    except Exception:
        text = ""
    return _format_status_error(resp, text)


def _format_status_error(resp: httpx.Response, text: str) -> str:
    snippet = text.strip()[:500]
    return f"接口返回 HTTP {resp.status_code}：{snippet or resp.reason_phrase}"


async def generate_image(config: dict, prompt: str, image_paths: list[str] | None = None) -> list[dict]:
    """便捷函数：构造 ImageClient 并生成图片。"""
    return await ImageClient(config).generate(prompt, image_paths)


async def list_models(config: dict) -> list[str]:
    """便捷函数：拉取模型列表。"""
    return await ImageClient(config).list_models()
