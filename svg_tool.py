"""本地 SVG 绘图：校验、清理 SVG，并光栅化为 PNG。

渲染由 resvg-py（Rust resvg 绑定）完成，不调用任何远程图像模型。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

try:
    import resvg_py
except ImportError:  # 依赖尚未安装时仍允许插件加载，只禁用 SVG 渲染。
    resvg_py = None

MAX_SVG_BYTES = 512 * 1024
MIN_DIMENSION = 32
MAX_DIMENSION = 8192
DEFAULT_DIMENSION = 1024


class SVGError(Exception):
    """SVG 内容非法或渲染失败。"""


_FORBIDDEN_TAGS = ("script", "foreignobject", "iframe", "object", "embed")


def sanitize_svg(svg: str) -> str:
    """做基础安全检查，返回可交给 resvg 的 SVG 文本。"""
    if not isinstance(svg, str):
        raise SVGError("SVG 必须是字符串")
    cleaned = svg.strip().lstrip("\ufeff")
    cleaned = re.sub(r"^\s*<\?xml[^>]*\?>", "", cleaned, flags=re.I).strip()
    if not cleaned:
        raise SVGError("SVG 内容为空")
    if len(cleaned.encode("utf-8")) > MAX_SVG_BYTES:
        raise SVGError(f"SVG 过大（上限 {MAX_SVG_BYTES // 1024} KB）")

    lower = cleaned.lower()
    for tag in _FORBIDDEN_TAGS:
        if re.search(rf"<\s*{tag}\b", lower):
            raise SVGError(f"SVG 中不允许使用 <{tag}>")
    if re.search(r"(?i)url\(\s*['\"]?\s*(?:https?|file|ftp):", cleaned):
        raise SVGError("SVG 中不允许引用外部资源")

    # 去掉事件处理器，例如 onclick="..."。
    cleaned = re.sub(
        r"\s+on[a-z0-9_-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        cleaned,
        flags=re.I,
    )
    # javascript: 链接置空。
    cleaned = re.sub(
        r"(?i)((?:xlink:)?href\s*=\s*[\"'])\s*javascript:[^\"']*([\"'])",
        r"\1#\2",
        cleaned,
    )
    # 不允许外部 http/file/ftp 引用，避免 SSRF 或本地文件探测。
    cleaned = re.sub(
        r"(?i)((?:xlink:)?href\s*=\s*[\"'])\s*(?:https?:|file:|ftp:)[^\"']*([\"'])",
        r"\1\2",
        cleaned,
    )

    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise SVGError(f"SVG 不是合法 XML：{exc}") from exc
    if root.tag.split("}")[-1].lower() != "svg":
        raise SVGError("根节点必须是 <svg>")
    return cleaned


def _parse_length(value: str | None) -> float | None:
    if not value:
        return None
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:px)?\s*$", value, re.I)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    return number if number > 0 else None


def _viewbox_size(root: ET.Element) -> tuple[float | None, float | None]:
    box = root.get("viewBox")
    if not box:
        return None, None
    parts = [p for p in re.split(r"[\s,]+", box.strip()) if p]
    if len(parts) != 4:
        return None, None
    try:
        width = float(parts[2])
        height = float(parts[3])
    except ValueError:
        return None, None
    if width <= 0 or height <= 0:
        return None, None
    return width, height


def infer_dimensions(
    svg: str,
    width: int = 0,
    height: int = 0,
    max_dimension: int = MAX_DIMENSION,
) -> tuple[int, int]:
    root = ET.fromstring(svg)
    svg_w = _parse_length(root.get("width"))
    svg_h = _parse_length(root.get("height"))
    view_w, view_h = _viewbox_size(root)
    orig_w = svg_w or view_w or DEFAULT_DIMENSION
    orig_h = svg_h or view_h or DEFAULT_DIMENSION

    out_w = float(width) if width and width > 0 else 0.0
    out_h = float(height) if height and height > 0 else 0.0
    if out_w and out_h:
        target_w, target_h = out_w, out_h
    elif out_w:
        target_w = out_w
        target_h = orig_h * (out_w / orig_w)
    elif out_h:
        target_h = out_h
        target_w = orig_w * (out_h / orig_h)
    else:
        target_w, target_h = orig_w, orig_h

    limit = max(MIN_DIMENSION, min(MAX_DIMENSION, int(max_dimension or MAX_DIMENSION)))
    scale = min(1.0, limit / max(target_w, target_h))
    final_w = max(MIN_DIMENSION, min(limit, int(round(target_w * scale))))
    final_h = max(MIN_DIMENSION, min(limit, int(round(target_h * scale))))
    return final_w, final_h


def render_svg_to_png(
    svg: str,
    width: int = 0,
    height: int = 0,
    background: str = "#ffffff",
    max_dimension: int = MAX_DIMENSION,
) -> tuple[bytes, int, int]:
    """把 SVG 渲染为 PNG，返回 (png_bytes, width, height)。"""
    if resvg_py is None:
        raise SVGError(
            "缺少 resvg-py 依赖，请安装 requirements.txt 中的依赖后重载插件"
        )
    cleaned = sanitize_svg(svg)
    out_w, out_h = infer_dimensions(cleaned, width, height, max_dimension)
    bg = (background or "").strip()
    if bg.lower() in ("", "none", "transparent"):
        bg_value = None
    else:
        bg_value = bg
    try:
        data = resvg_py.svg_to_bytes(
            svg_string=cleaned,
            width=out_w,
            height=out_h,
            background=bg_value,
        )
    except Exception as exc:  # noqa: BLE001
        raise SVGError(f"SVG 渲染失败：{exc}") from exc
    return bytes(data), out_w, out_h


SAMPLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="800" height="480" viewBox="0 0 800 480">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#4f46e5"/>
      <stop offset="1" stop-color="#0ea5e9"/>
    </linearGradient>
  </defs>
  <rect width="800" height="480" rx="28" fill="url(#bg)"/>
  <circle cx="640" cy="120" r="72" fill="#ffffff" opacity="0.18"/>
  <rect x="70" y="95" width="360" height="22" rx="11" fill="#ffffff" opacity="0.9"/>
  <rect x="70" y="145" width="500" height="14" rx="7" fill="#ffffff" opacity="0.55"/>
  <text x="70" y="275" fill="#ffffff" font-family="sans-serif" font-size="56" font-weight="700">SVG 绘图</text>
  <text x="72" y="325" fill="#ffffff" font-family="sans-serif" font-size="24" opacity="0.85">本地渲染 · 无需 AI 图片模型</text>
</svg>"""
