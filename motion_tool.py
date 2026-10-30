"""声明式 SVG 动画解释器。

机器人把用户描述转换成 JSON 动画脚本，本模块负责：
- 校验脚本和限制参数；
- 按时间逐帧生成 SVG；
- 用 resvg-py 光栅化矢量帧；
- 用 Pillow 编码 GIF/WebP，用 imageio-ffmpeg 编码 MP4。

不调用任何视频生成模型。支持的 pattern：
flow / timeline / cycle / comparison / text / chart / slideshow
"""

from __future__ import annotations

import base64
import io
import math
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape as xml_escape

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

try:
    import resvg_py
except ImportError:
    resvg_py = None

from PIL import Image, ImageOps

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 360
DEFAULT_FPS = 12
DEFAULT_MAX_DURATION = 15.0
DEFAULT_MAX_FRAMES = 360
DEFAULT_MAX_DIMENSION = 1280
DEFAULT_MAX_FILE_MB = 20
MIN_DIMENSION = 180
MIN_FPS = 5
MAX_FPS = 24
GIF_MAX_FRAMES = 180
GIF_MAX_DIMENSION = 960

PATTERNS = ("flow", "timeline", "cycle", "comparison", "text", "chart", "slideshow")
FORMATS = ("gif", "webp", "mp4")

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg1": "#070b17",
        "bg2": "#111b33",
        "fg": "#f8fafc",
        "muted": "#94a3b8",
        "accent": "#38bdf8",
        "secondary": "#a78bfa",
        "card": "#0f172a",
        "stroke": "#24324d",
    },
    "light": {
        "bg1": "#f8fafc",
        "bg2": "#eef2ff",
        "fg": "#0f172a",
        "muted": "#64748b",
        "accent": "#4f46e5",
        "secondary": "#0ea5e9",
        "card": "#ffffff",
        "stroke": "#dbe4f0",
    },
    "ocean": {
        "bg1": "#041c2c",
        "bg2": "#063b52",
        "fg": "#ecfeff",
        "muted": "#8fb8c9",
        "accent": "#22d3ee",
        "secondary": "#2dd4bf",
        "card": "#082f42",
        "stroke": "#155e75",
    },
    "sunset": {
        "bg1": "#1b0b1e",
        "bg2": "#3b1231",
        "fg": "#fff7ed",
        "muted": "#c4a3ac",
        "accent": "#fb7185",
        "secondary": "#f59e0b",
        "card": "#2a1026",
        "stroke": "#5b2246",
    },
}

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


class MotionError(Exception):
    """动画脚本非法或渲染失败。"""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ease(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _ease_out(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _fade(t: float, start: float, end: float) -> float:
    if end <= start:
        return 1.0 if t >= end else 0.0
    return _ease((t - start) / (end - start))


def _ui_scale(spec: dict) -> float:
    return _clamp(min(spec["width"] / 960, spec["height"] / 540), 0.58, 1.35)


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _color(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if _HEX_RE.match(text):
        return text
    if text.lower() in ("white", "black", "transparent"):
        return text.lower()
    return fallback


def _esc(value: Any) -> str:
    return xml_escape(str(value or ""))


def _text_width_estimate(text: str, size: float) -> float:
    width = 0.0
    for char in text:
        width += size * (0.58 if ord(char) < 128 else 1.0)
    return width


def _normalize_string_list(value: Any, limit: int, label: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise MotionError(f"{label} 必须是非空数组")
    result: list[dict] = []
    for item in value[:limit]:
        if isinstance(item, str):
            result.append({"text": item.strip()[:120]})
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or "").strip()
            if not text:
                continue
            entry = {"text": text[:120]}
            for key in ("color", "size", "id", "label", "description", "time"):
                if key in item:
                    entry[key] = item[key]
            result.append(entry)
    if not result:
        raise MotionError(f"{label} 中没有有效条目")
    return result


def normalize_spec(spec: dict, defaults: dict | None = None) -> dict:
    """校验并规范化动画规格。"""
    if not isinstance(spec, dict):
        raise MotionError("动画规格必须是 JSON 对象")
    defaults = defaults or {}
    pattern = str(spec.get("pattern") or "text").strip().lower()
    if pattern not in PATTERNS:
        raise MotionError(
            "pattern 必须是以下之一：" + ", ".join(PATTERNS)
        )

    theme_name = str(spec.get("theme") or "dark").strip().lower()
    theme = dict(THEMES.get(theme_name, THEMES["dark"]))
    fmt = str(spec.get("format") or defaults.get("format") or "gif").strip().lower()
    if fmt not in FORMATS:
        raise MotionError("format 必须是 gif、webp 或 mp4")
    max_dimension = max(
        MIN_DIMENSION,
        min(4096, _to_int(defaults.get("max_dimension"), DEFAULT_MAX_DIMENSION)),
    )
    if fmt in ("gif", "webp"):
        max_dimension = min(max_dimension, GIF_MAX_DIMENSION)
    width = max(1, _to_int(spec.get("width"), _to_int(defaults.get("width"), DEFAULT_WIDTH)))
    height = max(1, _to_int(spec.get("height"), _to_int(defaults.get("height"), DEFAULT_HEIGHT)))
    scale_up = max(1.0, MIN_DIMENSION / min(width, height))
    width = int(round(width * scale_up))
    height = int(round(height * scale_up))
    scale_down = min(1.0, max_dimension / max(width, height))
    width = max(MIN_DIMENSION, int(round(width * scale_down)))
    height = max(MIN_DIMENSION, int(round(height * scale_down)))
    width -= width % 2
    height -= height % 2

    fps = _to_int(spec.get("fps"), _to_int(defaults.get("fps"), DEFAULT_FPS))
    fps = max(MIN_FPS, min(MAX_FPS, fps))
    max_duration = max(
        2.0,
        min(60.0, _to_float(defaults.get("max_duration"), DEFAULT_MAX_DURATION)),
    )
    max_frames = max(
        10,
        min(600, _to_int(defaults.get("max_frames"), DEFAULT_MAX_FRAMES)),
    )
    if fmt in ("gif", "webp"):
        max_frames = min(max_frames, GIF_MAX_FRAMES)

    normalized: dict[str, Any] = {
        "pattern": pattern,
        "format": fmt,
        "width": width,
        "height": height,
        "fps": fps,
        "title": str(spec.get("title") or "").strip()[:40],
        "subtitle": str(spec.get("subtitle") or "").strip()[:70],
        "theme": theme,
        "background": _color(spec.get("background"), theme["bg1"]),
        "foreground": _color(spec.get("foreground"), theme["fg"]),
        "muted": _color(spec.get("muted_color"), theme["muted"]),
        "accent": _color(spec.get("accent"), theme["accent"]),
        "secondary": _color(spec.get("secondary"), theme["secondary"]),
        "card": _color(spec.get("card"), theme["card"]),
        "stroke": _color(spec.get("stroke"), theme["stroke"]),
        "font_family": (
            re.sub(
                r"[^A-Za-z0-9\u4e00-\u9fff _,-]",
                "",
                str(spec.get("font_family") or "sans-serif"),
            )[:80].strip()
            or "sans-serif"
        ),
        "loop": bool(spec.get("loop", True)),
        "send_as_file": bool(spec.get("send_as_file", False)),
        "caption": bool(spec.get("caption", True)),
    }

    computed_duration = 6.0
    if pattern == "flow":
        actors = spec.get("actors")
        if not isinstance(actors, list) or not actors:
            raise MotionError("flow 需要 actors 数组")
        normalized_actors = []
        for actor in actors[:6]:
            if not isinstance(actor, dict):
                continue
            actor_id = str(actor.get("id") or actor.get("label") or "").strip()
            if not actor_id:
                continue
            normalized_actors.append(
                {
                    "id": actor_id[:40],
                    "label": str(actor.get("label") or actor_id).strip()[:12],
                    "color": _color(actor.get("color"), normalized["accent"]),
                }
            )
        if len(normalized_actors) < 1:
            raise MotionError("flow 至少需要 1 个 actor")
        actor_ids = {actor["id"] for actor in normalized_actors}
        steps = spec.get("steps")
        if not isinstance(steps, list) or not steps:
            raise MotionError("flow 需要 steps 数组")
        normalized_steps = []
        for step in steps[:12]:
            if not isinstance(step, dict):
                continue
            src = str(step.get("from") or "").strip()
            dst = str(step.get("to") or "").strip()
            if src not in actor_ids or dst not in actor_ids:
                raise MotionError("flow 的 step.from / step.to 必须引用 actors 中的 id")
            normalized_steps.append(
                {
                    "from": src,
                    "to": dst,
                    "label": str(step.get("label") or "").strip()[:24],
                    "description": str(step.get("description") or "").strip()[:40],
                }
            )
        if not normalized_steps:
            raise MotionError("flow 没有有效 steps")
        normalized["actors"] = normalized_actors
        normalized["steps"] = normalized_steps
        computed_duration = 1.8 + 1.15 * len(normalized_steps)

    elif pattern == "timeline":
        events = _normalize_string_list(spec.get("events"), 10, "events")
        for index, event in enumerate(events):
            event.setdefault("time", str(index + 1))
            event["text"] = event["text"][:30]
            event["label"] = str(event.get("label") or event["text"])[:30]
            event["description"] = str(event.get("description") or "")[:60]
        normalized["events"] = events
        computed_duration = 1.8 + 0.9 * len(events)

    elif pattern == "cycle":
        nodes = _normalize_string_list(spec.get("nodes"), 8, "nodes")
        for index, node in enumerate(nodes):
            node["id"] = str(node.get("id") or index)
            node["label"] = str(node.get("label") or node["text"])[:12]
        if len(nodes) < 2:
            raise MotionError("cycle 至少需要 2 个节点")
        normalized["nodes"] = nodes
        normalized["center"] = str(spec.get("center") or normalized["title"] or "循环")[:60]
        computed_duration = 1.8 + 0.8 * len(nodes)

    elif pattern == "comparison":
        left = spec.get("left") if isinstance(spec.get("left"), dict) else {}
        right = spec.get("right") if isinstance(spec.get("right"), dict) else {}
        left_items = _normalize_string_list(left.get("items"), 5, "left.items")
        right_items = _normalize_string_list(right.get("items"), 5, "right.items")
        for item in left_items + right_items:
            item["text"] = item["text"][:36]
        normalized["left"] = {
            "title": str(left.get("title") or "方案 A")[:60],
            "items": left_items,
        }
        normalized["right"] = {
            "title": str(right.get("title") or "方案 B")[:60],
            "items": right_items,
        }
        computed_duration = 1.8 + 0.75 * max(len(left_items), len(right_items))

    elif pattern == "text":
        raw_lines = spec.get("lines")
        if raw_lines is None and spec.get("text"):
            raw_lines = str(spec["text"]).splitlines()
        lines = _normalize_string_list(raw_lines, 6, "lines")
        for line in lines:
            line["text"] = line["text"][:50]
        normalized["lines"] = lines
        normalized["style"] = str(spec.get("style") or "fade").strip().lower()
        if normalized["style"] not in ("fade", "slide", "typewriter", "neon"):
            normalized["style"] = "fade"
        total_chars = sum(len(line["text"]) for line in lines)
        computed_duration = 1.8 + 0.45 * len(lines) + 0.025 * total_chars

    elif pattern == "chart":
        chart_type = str(spec.get("chart_type") or "bar").strip().lower()
        if chart_type not in ("bar", "line"):
            raise MotionError("chart_type 必须是 bar 或 line")
        labels = spec.get("labels")
        if not isinstance(labels, list) or not labels:
            raise MotionError("chart 需要 labels 数组")
        labels = [str(label)[:16] for label in labels[:12]]
        series_raw = spec.get("series")
        if not isinstance(series_raw, list):
            values = spec.get("values")
            if isinstance(values, list) and values:
                series_raw = [
                    {
                        "name": str(spec.get("series_name") or "数值"),
                        "values": values,
                        "color": spec.get("accent"),
                    }
                ]
            else:
                raise MotionError("chart 需要 series 或 values 数组")
        series = []
        for index, item in enumerate(series_raw[:4]):
            if not isinstance(item, dict):
                continue
            values = item.get("values")
            if not isinstance(values, list) or len(values) != len(labels):
                raise MotionError("每个 series.values 必须与 labels 等长")
            try:
                numbers = [max(0.0, float(value)) for value in values]
            except (TypeError, ValueError) as exc:
                raise MotionError("chart 的 values 必须是数字") from exc
            series.append(
                {
                    "name": str(item.get("name") or f"系列 {index + 1}")[:40],
                    "values": numbers,
                    "color": _color(
                        item.get("color"),
                        normalized["accent"] if index == 0 else normalized["secondary"],
                    ),
                }
            )
        if not series:
            raise MotionError("chart 没有有效 series")
        normalized["chart_type"] = chart_type
        normalized["labels"] = labels
        normalized["series"] = series
        normalized["unit"] = str(spec.get("unit") or "")[:12]
        normalized["max_value"] = max(
            [value for item in series for value in item["values"]] + [1.0]
        )
        computed_duration = 1.8 + 0.5 * len(labels)

    elif pattern == "slideshow":
        images = spec.get("images")
        if images is not None and not isinstance(images, list):
            raise MotionError("slideshow.images 必须是数组")
        normalized["images"] = [str(item) for item in (images or [])][:12]
        transition = str(spec.get("transition") or "fade").strip().lower()
        if transition not in ("fade", "slide", "zoom"):
            transition = "fade"
        normalized["transition"] = transition
        normalized["per_image"] = _clamp(
            _to_float(spec.get("per_image"), 2.2), 0.8, 8.0
        )
        normalized["fit"] = str(spec.get("fit") or "cover").strip().lower()
        if normalized["fit"] not in ("cover", "contain"):
            normalized["fit"] = "cover"
        computed_duration = max(1.0, len(normalized["images"]) * normalized["per_image"])

    duration = _clamp(
        _to_float(spec.get("duration"), computed_duration), 1.0, max_duration
    )
    frame_count = max(2, min(max_frames, int(round(duration * fps))))
    normalized["duration"] = frame_count / fps
    normalized["frames"] = frame_count
    normalized["max_file_mb"] = max(
        1, _to_int(defaults.get("max_file_mb"), DEFAULT_MAX_FILE_MB)
    )
    return normalized

_PATTERN_LABEL = {
    "flow": "FLOW",
    "timeline": "TIMELINE",
    "cycle": "CYCLE",
    "comparison": "COMPARISON",
    "text": "TYPE",
    "chart": "CHART",
    "slideshow": "SHOW",
}


def _svg_document(spec: dict, body: str, extra_defs: str = "") -> str:
    w = spec["width"]
    h = spec["height"]
    theme = spec["theme"]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{spec['background']}"/>
    <stop offset="1" stop-color="{theme['bg2']}"/>
  </linearGradient>
  <radialGradient id="glowA" cx="50%" cy="50%" r="50%">
    <stop offset="0" stop-color="{spec['accent']}" stop-opacity="0.22"/>
    <stop offset="1" stop-color="{spec['accent']}" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="glowB" cx="50%" cy="50%" r="50%">
    <stop offset="0" stop-color="{spec['secondary']}" stop-opacity="0.16"/>
    <stop offset="1" stop-color="{spec['secondary']}" stop-opacity="0"/>
  </radialGradient>
  <marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6" markerHeight="6" orient="auto">
    <path d="M 0 0 L 10 5 L 0 10 z" fill="{spec['accent']}"/>
  </marker>
  {extra_defs}
</defs>
<rect width="{w}" height="{h}" fill="url(#bg)"/>
<circle cx="{w * 0.9:.2f}" cy="{h * 0.07:.2f}" r="{w * 0.22:.2f}" fill="url(#glowA)"/>
<circle cx="{w * 0.08:.2f}" cy="{h * 0.94:.2f}" r="{w * 0.2:.2f}" fill="url(#glowB)"/>
{body}
</svg>'''


def _header(spec: dict, t: float = 1.0, accent_width: float = 72) -> str:
    s = _ui_scale(spec)
    x = max(24, 56 * s)
    title = spec["title"] or _PATTERN_LABEL[spec["pattern"]]
    eyebrow = _PATTERN_LABEL[spec["pattern"]]
    opacity = _fade(t, 0.0, 0.14)
    width = accent_width * s * _ease_out(_clamp(t / 0.25, 0.0, 1.0))
    subtitle = ""
    if spec.get("subtitle"):
        subtitle = (
            f'<text x="{_fmt(x)}" y="{_fmt(132 * s)}" fill="{spec["muted"]}" '
            f'font-family="{spec["font_family"]}" font-size="{_fmt(max(10, 16 * s))}">'
            f'{_esc(spec["subtitle"])}</text>'
        )
    return f'''<g opacity="{_fmt(opacity)}">
  <text x="{_fmt(x)}" y="{_fmt(45 * s)}" fill="{spec['accent']}" font-family="{spec['font_family']}" font-size="{_fmt(max(10, 13 * s))}" letter-spacing="{_fmt(2.4 * s)}">{_esc(eyebrow)}</text>
  <text x="{_fmt(x)}" y="{_fmt(94 * s)}" fill="{spec['foreground']}" font-family="{spec['font_family']}" font-size="{_fmt(max(18, 38 * s))}" font-weight="700">{_esc(title)}</text>
  <rect x="{_fmt(x)}" y="{_fmt(110 * s)}" width="{_fmt(width)}" height="{_fmt(max(3, 4 * s))}" rx="{_fmt(2 * s)}" fill="{spec['accent']}"/>
  {subtitle}
</g>'''


def _caption(spec: dict, text: str, x: float, y: float, *, size: float = 15, color: str | None = None, anchor: str = "middle", opacity: float = 1.0, weight: int = 400) -> str:
    return (
        f'<text x="{_fmt(x)}" y="{_fmt(y)}" fill="{color or spec["foreground"]}" '
        f'font-family="{spec["font_family"]}" font-size="{_fmt(size)}" '
        f'font-weight="{weight}" text-anchor="{anchor}" opacity="{_fmt(opacity)}">'
        f"{_esc(text)}</text>"
    )


def _render_svg_frame(svg: str, width: int, height: int, background: str | None = None) -> Image.Image:
    if resvg_py is None:
        raise MotionError("缺少 resvg-py 依赖，请安装 requirements.txt 后重载插件")
    data = resvg_py.svg_to_bytes(
        svg_string=svg,
        width=width,
        height=height,
        background=background,
    )
    return Image.open(io.BytesIO(bytes(data))).convert("RGBA")


def _flow_frames(spec: dict) -> Iterable[Image.Image]:
    w, h = spec["width"], spec["height"]
    s = _ui_scale(spec)
    actors = spec["actors"]
    steps = spec["steps"]
    total = len(actors)
    margin = max(42, 92 * s)
    if total == 1:
        box_w = min(190 * s, w * 0.55)
        xs = [w / 2]
    else:
        lane_w = (w - 2 * margin) / (total - 1)
        box_w = min(190 * s, max(56, lane_w * 0.72))
        margin = max(margin, box_w / 2 + 8)
        xs = [margin + (w - 2 * margin) * i / (total - 1) for i in range(total)]
    x_by_id = {actor["id"]: xs[i] for i, actor in enumerate(actors)}
    actor_y = max(82, 185 * s)
    box_h = max(32, 58 * s)
    step_top = max(132, 260 * s)
    step_bottom = h - max(16, 32 * s)
    row_h = max(26, (step_bottom - step_top) / max(1, len(steps)))
    show_descriptions = h >= 360
    step_start = 0.16
    step_span = 0.78 / len(steps)

    for frame_index in range(spec["frames"]):
        t = frame_index / max(1, spec["frames"] - 1)
        active_ids: set[str] = set()
        step_parts: list[str] = []
        for index, step in enumerate(steps):
            start = step_start + index * step_span
            end = start + step_span * 0.78
            if t < start:
                continue
            progress = _ease((t - start) / max(0.0001, end - start))
            y = step_top + row_h * (index + 0.45)
            x1 = x_by_id[step["from"]]
            x2 = x_by_id[step["to"]]
            if start <= t <= start + step_span:
                active_ids.add(step["from"])
                active_ids.add(step["to"])
            label_opacity = _fade(t, start + step_span * 0.25, end)
            if x1 == x2:
                length = 80.0 * s
                dash = length * (1 - progress)
                path = (
                    f"M {_fmt(x1 - 22 * s)} {_fmt(y - 8 * s)} "
                    f"C {_fmt(x1 - 58 * s)} {_fmt(y - 48 * s)}, {_fmt(x1 + 58 * s)} {_fmt(y - 48 * s)}, "
                    f"{_fmt(x1 + 22 * s)} {_fmt(y - 8 * s)}"
                )
                step_parts.append(
                    f'<path d="{path}" fill="none" stroke="{spec["accent"]}" stroke-width="{_fmt(max(2.2, 3.5 * s))}" '
                    f'stroke-linecap="round" stroke-dasharray="{length:.1f}" '
                    f'stroke-dashoffset="{dash:.1f}" marker-end="url(#arrow)"/>'
                )
                label_x = x1
            else:
                direction = 1 if x2 > x1 else -1
                start_x = x1 + direction * (box_w / 2 + 10 * s)
                end_x = x2 - direction * (box_w / 2 + 10 * s)
                if (end_x - start_x) * direction < 12:
                    start_x, end_x = x1, x2
                length = abs(end_x - start_x)
                dash = length * (1 - progress)
                step_parts.append(
                    f'<line x1="{_fmt(start_x)}" y1="{_fmt(y)}" x2="{_fmt(end_x)}" y2="{_fmt(y)}" '
                    f'stroke="{spec["accent"]}" stroke-width="{_fmt(max(2.2, 3.5 * s))}" stroke-linecap="round" '
                    f'stroke-dasharray="{length:.1f}" stroke-dashoffset="{dash:.1f}" marker-end="url(#arrow)"/>'
                )
                dot_x = _lerp(start_x, end_x, progress)
                dot_opacity = max(0.0, min(1.0, progress * (1 - progress) * 5))
                step_parts.append(
                    f'<circle cx="{_fmt(dot_x)}" cy="{_fmt(y)}" r="{_fmt(max(3, 5 * s))}" fill="{spec["foreground"]}" opacity="{_fmt(dot_opacity)}"/>'
                )
                label_x = (start_x + end_x) / 2
            if step["label"]:
                step_parts.append(
                    _caption(spec, step["label"], label_x, y - 14 * s, size=max(12, 17 * s), color=spec["foreground"], opacity=label_opacity, weight=600)
                )
            if step["description"] and show_descriptions:
                step_parts.append(
                    _caption(spec, step["description"], label_x, y + 28 * s, size=max(10, 13 * s), color=spec["muted"], opacity=label_opacity)
                )

        actor_parts: list[str] = []
        for actor, x in zip(actors, xs):
            is_active = actor["id"] in active_ids
            stroke = actor["color"] if is_active else spec["stroke"]
            glow_opacity = 0.16 if is_active else 0.0
            actor_parts.append(
                f'<rect x="{_fmt(x - box_w / 2 - 5 * s)}" y="{_fmt(actor_y - box_h / 2 - 5 * s)}" '
                f'width="{_fmt(box_w + 10 * s)}" height="{_fmt(box_h + 10 * s)}" rx="{_fmt(max(10, 18 * s))}" fill="{actor["color"]}" opacity="{_fmt(glow_opacity)}"/>'
                f'<line x1="{_fmt(x)}" y1="{_fmt(actor_y + box_h / 2)}" x2="{_fmt(x)}" y2="{_fmt(h - max(12, 24 * s))}" '
                f'stroke="{spec["stroke"]}" stroke-width="1.5" stroke-dasharray="5 8" opacity="0.55"/>'
                f'<rect x="{_fmt(x - box_w / 2)}" y="{_fmt(actor_y - box_h / 2)}" width="{_fmt(box_w)}" height="{_fmt(box_h)}" '
                f'rx="{_fmt(max(9, 15 * s))}" fill="{spec["card"]}" stroke="{stroke}" stroke-width="2"/>'
                f'<circle cx="{_fmt(x - box_w / 2 + 22 * s)}" cy="{_fmt(actor_y)}" r="{_fmt(max(3, 5 * s))}" fill="{actor["color"]}"/>'
                f'<text x="{_fmt(x + 10)}" y="{_fmt(actor_y + 6)}" fill="{spec["foreground"]}" '
                f'font-family="{spec["font_family"]}" font-size="{_fmt(max(11, 17 * s))}" font-weight="600" text-anchor="middle">{_esc(actor["label"])}</text>'
            )
        body = _header(spec, t) + "".join(actor_parts) + "".join(step_parts)
        svg = _svg_document(spec, body)
        yield _render_svg_frame(svg, w, h).convert("RGB")


def _timeline_frames(spec: dict) -> Iterable[Image.Image]:
    w, h = spec["width"], spec["height"]
    s = _ui_scale(spec)
    events = spec["events"]
    x1, x2 = max(36, 72 * s), w - max(36, 72 * s)
    y = h * 0.6
    for frame_index in range(spec["frames"]):
        t = frame_index / max(1, spec["frames"] - 1)
        line_progress = _ease(_clamp(t / 0.32, 0.0, 1.0))
        parts = [
            _header(spec, t),
            f'<line x1="{x1}" y1="{_fmt(y)}" x2="{_fmt(_lerp(x1, x2, line_progress))}" y2="{_fmt(y)}" '
            f'stroke="{spec["accent"]}" stroke-width="{_fmt(max(2, 3 * s))}" stroke-linecap="round"/>',
            f'<line x1="{x1}" y1="{_fmt(y)}" x2="{x2}" y2="{_fmt(y)}" stroke="{spec["stroke"]}" '
            f'stroke-width="2" stroke-dasharray="2 10" opacity="0.55"/>',
        ]
        count = len(events)
        for index, event in enumerate(events):
            ex = x1 if count == 1 else x1 + (x2 - x1) * index / (count - 1)
            appear = 0.18 + index * (0.58 / max(1, count))
            pop = _fade(t, appear, appear + 0.12)
            up = index % 2 == 0
            label_y = y - 82 * s if up else y + 96 * s
            stem_end = y - 24 * s if up else y + 36 * s
            connector_opacity = pop * 0.75
            parts.append(
                f'<line x1="{_fmt(ex)}" y1="{_fmt(y)}" x2="{_fmt(ex)}" y2="{_fmt(stem_end)}" '
                f'stroke="{spec["accent"]}" stroke-width="2" opacity="{_fmt(connector_opacity)}"/>'
                f'<circle cx="{_fmt(ex)}" cy="{_fmt(y)}" r="{_fmt(max(5, 9 * s) * pop)}" fill="{spec["background"]}" '
                f'stroke="{spec["accent"]}" stroke-width="{_fmt(max(2.5, 4 * s))}"/>'
            )
            time_label = str(event.get("time") or "")
            if time_label and count <= 6:
                parts.append(
                    _caption(spec, time_label, ex, y - 18 * s if up else y + 54 * s, size=max(10, 13 * s), color=spec["accent"], opacity=pop, weight=600)
                )
            parts.append(
                _caption(spec, str(event.get("label") or event["text"]), ex, label_y, size=max(12, 20 * s), color=spec["foreground"], opacity=pop, weight=600)
            )
            if event.get("description") and count <= 6:
                parts.append(
                    _caption(spec, str(event["description"]), ex, label_y + 26 * s, size=max(10, 13 * s), color=spec["muted"], opacity=pop)
                )
        svg = _svg_document(spec, "".join(parts))
        yield _render_svg_frame(svg, w, h).convert("RGB")


def _cycle_frames(spec: dict) -> Iterable[Image.Image]:
    w, h = spec["width"], spec["height"]
    s = _ui_scale(spec)
    nodes = spec["nodes"]
    count = len(nodes)
    cx, cy = w / 2, h / 2 + 26 * s
    radius = min(w, h) * 0.29
    angles = [-90 + i * 360 / count for i in range(count)]
    positions = [
        (
            cx + radius * math.cos(math.radians(angle)),
            cy + radius * math.sin(math.radians(angle)),
        )
        for angle in angles
    ]
    for frame_index in range(spec["frames"]):
        t = frame_index / max(1, spec["frames"] - 1)
        parts = [_header(spec, t)]
        arc_radius = radius + max(28, 52 * s)
        for index in range(count):
            start_angle = angles[index] + 9
            end_angle = angles[(index + 1) % count] - 9
            if end_angle <= start_angle:
                end_angle += 360
            a1 = math.radians(start_angle)
            a2 = math.radians(end_angle)
            sx = cx + arc_radius * math.cos(a1)
            sy = cy + arc_radius * math.sin(a1)
            ex = cx + arc_radius * math.cos(a2)
            ey = cy + arc_radius * math.sin(a2)
            length = arc_radius * abs(a2 - a1)
            start = 0.28 + index * (0.52 / max(1, count))
            progress = _ease(_clamp((t - start) / 0.14, 0.0, 1.0))
            if progress <= 0:
                continue
            dash = length * (1 - progress)
            sweep = 1
            parts.append(
                f'<path d="M {_fmt(sx)} {_fmt(sy)} A {_fmt(arc_radius)} {_fmt(arc_radius)} 0 0 {sweep} {_fmt(ex)} {_fmt(ey)}" '
                f'fill="none" stroke="{spec["accent"]}" stroke-width="{_fmt(max(2, 2.5 * s))}" stroke-linecap="round" '
                f'stroke-dasharray="{_fmt(length)}" stroke-dashoffset="{_fmt(dash)}" marker-end="url(#arrow)" opacity="0.9"/>'
            )
        for index, node in enumerate(nodes):
            x, y = positions[index]
            appear = 0.14 + index * (0.5 / max(1, count))
            pop = _ease_out(_clamp((t - appear) / 0.16, 0.0, 1.0))
            r = max(20, 38 * s) * pop
            if pop <= 0:
                continue
            parts.append(
                f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(r + max(6, 9 * s))}" fill="{spec["accent"]}" opacity="0.10"/>'
                f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(r)}" fill="{spec["card"]}" '
                f'stroke="{spec["accent"]}" stroke-width="2.5"/>'
                f'<text x="{_fmt(x)}" y="{_fmt(y + 6)}" fill="{spec["foreground"]}" '
                f'font-family="{spec["font_family"]}" font-size="{_fmt(max(10, 15 * s))}" font-weight="600" text-anchor="middle" '
                f'opacity="{_fmt(pop)}">{_esc(str(node.get("label") or node["text"])[:10])}</text>'
            )
        center_opacity = _fade(t, 0.55, 0.8)
        parts.append(
            _caption(spec, str(spec.get("center") or "循环"), cx, cy + 7 * s, size=max(12, 18 * s), color=spec["muted"], opacity=center_opacity, weight=600)
        )
        svg = _svg_document(spec, "".join(parts))
        yield _render_svg_frame(svg, w, h).convert("RGB")


def _comparison_frames(spec: dict) -> Iterable[Image.Image]:
    w, h = spec["width"], spec["height"]
    s = _ui_scale(spec)
    left = spec["left"]
    right = spec["right"]
    max_items = max(len(left["items"]), len(right["items"]))
    card_y = max(82, 165 * s)
    card_w = (w - 150 * s) / 2
    card_h = h - card_y - max(30, 58 * s)
    item_top = card_y + 92 * s
    item_bottom = card_y + card_h - 20 * s
    item_spacing = min(
        max(24, 42 * s),
        max(16, (item_bottom - item_top) / max(1, max_items - 1)),
    )
    for frame_index in range(spec["frames"]):
        t = frame_index / max(1, spec["frames"] - 1)
        left_shift = -34 * s * (1 - _ease(_clamp(t / 0.22, 0.0, 1.0)))
        right_shift = 34 * s * (1 - _ease(_clamp(t / 0.22, 0.0, 1.0)))
        parts = [_header(spec, t)]
        for side, card, shift, color in (
            ("left", left, left_shift, spec["accent"]),
            ("right", right, right_shift, spec["secondary"]),
        ):
            x = 56 * s + shift if side == "left" else w / 2 + 22 * s + shift
            opacity = _fade(t, 0.05, 0.22)
            parts.append(
                f'<rect x="{_fmt(x)}" y="{card_y}" width="{_fmt(card_w)}" height="{_fmt(card_h)}" '
                f'rx="18" fill="{spec["card"]}" stroke="{spec["stroke"]}" stroke-width="1.5" opacity="{_fmt(opacity)}"/>'
                f'<rect x="{_fmt(x)}" y="{card_y}" width="{_fmt(card_w)}" height="6" rx="3" fill="{color}" opacity="{_fmt(opacity)}"/>'
            )
            parts.append(
                _caption(spec, card["title"], x + 28 * s, card_y + 48 * s, size=max(14, 23 * s), color=color, anchor="start", opacity=opacity, weight=700)
            )
            for item_index, item in enumerate(card["items"][:5]):
                start = 0.22 + item_index * (0.52 / max(1, max_items))
                item_opacity = _fade(t, start, start + 0.12)
                item_shift = (1 - _ease_out(_clamp((t - start) / 0.16, 0.0, 1.0))) * (-18 if side == "left" else 18)
                item_y = item_top + item_index * item_spacing
                parts.append(
                    f'<circle cx="{_fmt(x + 38 * s + item_shift)}" cy="{_fmt(item_y - 5 * s)}" r="{_fmt(max(8, 12 * s))}" fill="{color}" opacity="{_fmt(item_opacity * 0.15)}"/>'
                    f'<text x="{_fmt(x + 38 * s + item_shift)}" y="{_fmt(item_y)}" fill="{color}" '
                    f'font-family="{spec["font_family"]}" font-size="{_fmt(max(11, 16 * s))}" font-weight="700" text-anchor="middle" '
                    f'opacity="{_fmt(item_opacity)}">{"+" if side == "left" else "−"}</text>'
                )
                parts.append(
                    _caption(spec, item["text"], x + 60 * s + item_shift, item_y, size=max(11, 16 * s), color=spec["foreground"], anchor="start", opacity=item_opacity)
                )
        vs_opacity = _fade(t, 0.35, 0.55)
        vs_x = w / 2
        parts.append(
            f'<circle cx="{vs_x}" cy="{card_y + card_h / 2}" r="{_fmt(max(18, 30 * s))}" fill="{spec["background"]}" '
            f'stroke="{spec["accent"]}" stroke-width="2" opacity="{_fmt(vs_opacity)}"/>'
            f'<text x="{vs_x}" y="{_fmt(card_y + card_h / 2 + 7)}" fill="{spec["foreground"]}" '
            f'font-family="{spec["font_family"]}" font-size="{_fmt(max(11, 16 * s))}" font-weight="700" text-anchor="middle" '
            f'opacity="{_fmt(vs_opacity)}">VS</text>'
        )
        svg = _svg_document(spec, "".join(parts))
        yield _render_svg_frame(svg, w, h).convert("RGB")


def _text_frames(spec: dict) -> Iterable[Image.Image]:
    w, h = spec["width"], spec["height"]
    lines = spec["lines"]
    style = spec["style"]
    s = _ui_scale(spec)
    longest = max(len(line["text"]) for line in lines)
    header_bottom = (160 if spec.get("subtitle") else 130) * s
    content_top = max(76, header_bottom)
    bottom_margin = max(50, 80 * s)
    available = max(60, h - content_top - bottom_margin)
    max_line_height = available / max(1, len(lines))
    base_size = min(
        66 * s,
        max_line_height / 1.6,
        max(16, (w - 180 * s) / max(6, longest) * 1.55),
    )
    base_size = max(16, base_size)
    line_height = base_size * 1.38
    start_y = content_top + (len(lines) - 1) * line_height / 2
    total_chars = max(1, sum(len(line["text"]) for line in lines))

    for frame_index in range(spec["frames"]):
        t = frame_index / max(1, spec["frames"] - 1)
        parts = [_header(spec, t)]
        if style == "typewriter":
            reveal_count = int(total_chars * _ease(_clamp((t - 0.12) / 0.72, 0.0, 1.0)))
            consumed = 0
            for index, line in enumerate(lines):
                text = line["text"]
                visible = max(0, min(len(text), reveal_count - consumed))
                shown = text[:visible]
                consumed += len(text)
                y = start_y + index * line_height
                color = _color(line.get("color"), spec["foreground"])
                parts.append(
                    _caption(spec, shown, w / 2, y, size=base_size, color=color, opacity=1.0, weight=700)
                )
            cursor_opacity = 1.0 if int(t * spec["fps"]) % 2 == 0 else 0.0
            parts.append(
                f'<rect x="{_fmt(w / 2 + 6)}" y="{_fmt(start_y - base_size * 0.78)}" width="4" height="{_fmt(base_size)}" '
                f'fill="{spec["accent"]}" opacity="{_fmt(cursor_opacity)}"/>'
            )
        else:
            for index, line in enumerate(lines):
                start = 0.12 + index * (0.58 / max(1, len(lines)))
                opacity = _fade(t, start, start + 0.18)
                y = start_y + index * line_height
                if style == "slide":
                    offset = (1 - _ease_out(_clamp((t - start) / 0.22, 0.0, 1.0))) * (-46 if index % 2 == 0 else 46)
                    x = w / 2 + offset
                elif style == "neon":
                    x = w / 2
                    parts.append(
                        _caption(spec, line["text"], x, y, size=base_size, color=spec["accent"], opacity=opacity * 0.18, weight=700)
                    )
                else:
                    x = w / 2
                    y += (1 - _ease_out(_clamp((t - start) / 0.2, 0.0, 1.0))) * 20
                color = _color(line.get("color"), spec["foreground"])
                parts.append(
                    _caption(spec, line["text"], x, y, size=_to_float(line.get("size"), base_size), color=color, opacity=opacity, weight=700)
                )
        line_width = (w - 180) * _ease(_clamp((t - 0.18) / 0.55, 0.0, 1.0))
        parts.append(
            f'<rect x="{_fmt((w - line_width) / 2)}" y="{_fmt(h - max(18, 30 * s))}" width="{_fmt(line_width)}" height="4" rx="2" '
            f'fill="{spec["accent"]}" opacity="0.85"/>'
        )
        svg = _svg_document(spec, "".join(parts))
        yield _render_svg_frame(svg, w, h).convert("RGB")

def _chart_frames(spec: dict) -> Iterable[Image.Image]:
    w, h = spec["width"], spec["height"]
    labels = spec["labels"]
    series = spec["series"]
    chart_type = spec["chart_type"]
    unit = spec["unit"]
    max_value = spec["max_value"]
    s = _ui_scale(spec)
    plot_x = max(46, 92 * s)
    plot_y = max(106, 194 * s)
    plot_right = w - max(34, 72 * s)
    plot_w = plot_right - plot_x
    plot_h = max(80, h - plot_y - max(54, 90 * s))
    count = len(labels)
    series_count = len(series)

    for frame_index in range(spec["frames"]):
        t = frame_index / max(1, spec["frames"] - 1)
        parts = [_header(spec, t)]
        for grid_index in range(5):
            gy = plot_y + plot_h * grid_index / 4
            value = max_value * (1 - grid_index / 4)
            parts.append(
                f'<line x1="{plot_x}" y1="{_fmt(gy)}" x2="{_fmt(plot_x + plot_w)}" y2="{_fmt(gy)}" '
                f'stroke="{spec["stroke"]}" stroke-width="1" opacity="0.55"/>'
            )
            parts.append(
                _caption(spec, f"{value:.0f}", plot_x - 14 * s, gy + 5 * s, size=max(9, 12 * s), color=spec["muted"], anchor="end", opacity=0.8)
            )
        parts.append(
            f'<line x1="{plot_x}" y1="{_fmt(plot_y)}" x2="{plot_x}" y2="{_fmt(plot_y + plot_h)}" '
            f'stroke="{spec["stroke"]}" stroke-width="1.5"/>'
            f'<line x1="{plot_x}" y1="{_fmt(plot_y + plot_h)}" x2="{_fmt(plot_x + plot_w)}" y2="{_fmt(plot_y + plot_h)}" '
            f'stroke="{spec["stroke"]}" stroke-width="1.5"/>'
        )
        for index, label in enumerate(labels):
            if count == 1:
                x = plot_x + plot_w / 2
            else:
                x = plot_x + plot_w * (index + 0.5) / count
            parts.append(
                _caption(spec, label, x, plot_y + plot_h + 30 * s, size=max(9, 13 * s), color=spec["muted"], opacity=0.95)
            )

        if chart_type == "bar":
            group_w = plot_w / count
            inner = group_w * 0.72
            bar_w = max(8, inner / series_count)
            for series_index, item in enumerate(series):
                for index, value in enumerate(item["values"]):
                    start = 0.16 + index * (0.42 / max(1, count))
                    grow = _ease(_clamp((t - start) / 0.38, 0.0, 1.0))
                    bar_h = value / max_value * plot_h * grow
                    x = plot_x + group_w * index + (group_w - inner) / 2 + series_index * bar_w
                    y = plot_y + plot_h - bar_h
                    parts.append(
                        f'<rect x="{_fmt(x + 2)}" y="{_fmt(y)}" width="{_fmt(max(2, bar_w - 4))}" height="{_fmt(bar_h)}" '
                        f'rx="6" fill="{item["color"]}" opacity="0.92"/>'
                    )
                    if grow > 0.72:
                        label_opacity = _fade(grow, 0.72, 1.0)
                        parts.append(
                            _caption(
                                spec,
                                f"{value:g}{unit}",
                                x + bar_w / 2,
                                y - 10,
                                size=max(9, 12 * s),
                                color=item["color"],
                                opacity=label_opacity,
                                weight=600,
                            )
                        )
        else:
            for series_index, item in enumerate(series):
                points = []
                for index, value in enumerate(item["values"]):
                    x = plot_x + plot_w * (index + 0.5) / count
                    y = plot_y + plot_h - value / max_value * plot_h
                    points.append((x, y))
                if len(points) > 1:
                    path = " L ".join(f"{_fmt(x)} {_fmt(y)}" for x, y in points)
                    path = "M " + path
                    length = sum(
                        math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)
                    )
                    progress = _ease(_clamp((t - 0.16 - series_index * 0.04) / 0.55, 0.0, 1.0))
                    dash = length * (1 - progress)
                    parts.append(
                        f'<path d="{path}" fill="none" stroke="{item["color"]}" stroke-width="{_fmt(max(2.5, 4 * s))}" '
                        f'stroke-linecap="round" stroke-linejoin="round" '
                        f'stroke-dasharray="{_fmt(length)}" stroke-dashoffset="{_fmt(dash)}"/>'
                    )
                for index, (x, y) in enumerate(points):
                    point_t = 0.22 + index * (0.5 / max(1, count))
                    pop = _fade(t, point_t, point_t + 0.1)
                    if pop <= 0:
                        continue
                    parts.append(
                        f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(max(3, 5 * s) * pop)}" fill="{spec["background"]}" '
                        f'stroke="{item["color"]}" stroke-width="3"/>'
                    )
                    if pop > 0.7:
                        parts.append(
                            _caption(
                                spec,
                                f"{item['values'][index]:g}{unit}",
                                x,
                                y - 14,
                                size=max(8, 11 * s),
                                color=item["color"],
                                opacity=pop,
                                weight=600,
                            )
                        )
        legend_x = w - 92
        for index, item in enumerate(series):
            ly = max(28, 43 * s) + index * max(16, 24 * s)
            parts.append(
                f'<circle cx="{legend_x - 120 * s}" cy="{ly - 5 * s}" r="{_fmt(max(3, 5 * s))}" fill="{item["color"]}"/>'
                f'<text x="{legend_x - 108 * s}" y="{ly}" fill="{spec["muted"]}" font-family="{spec["font_family"]}" '
                f'font-size="{_fmt(max(9, 12 * s))}" text-anchor="start">{_esc(item["name"])}</text>'
            )
        svg = _svg_document(spec, "".join(parts))
        yield _render_svg_frame(svg, w, h).convert("RGB")


def _load_slideshow_images(spec: dict, message_images: list[str]) -> list[Image.Image]:
    sources = list(spec.get("images") or [])
    if not sources:
        sources = list(message_images)
    if not sources:
        raise MotionError(
            "slideshow 需要图片：请在消息中附带图片，或在 spec.images 中提供 data:image/...;base64 数据"
        )
    allowed_paths = {str(Path(path).resolve()) for path in message_images}
    images: list[Image.Image] = []
    for source in sources[:12]:
        try:
            if source.startswith("data:image/"):
                header, encoded = source.split(",", 1)
                raw = base64.b64decode(encoded)
                if len(raw) > 16 * 1024 * 1024:
                    raise MotionError("单张幻灯片图片不能超过 16 MB")
                image = Image.open(io.BytesIO(raw))
            else:
                resolved = str(Path(source).resolve())
                if resolved not in allowed_paths:
                    raise MotionError("slideshow 只允许使用当前消息附带的图片或 data URI")
                if Path(resolved).stat().st_size > 16 * 1024 * 1024:
                    raise MotionError("单张幻灯片图片不能超过 16 MB")
                image = Image.open(resolved)
            image = ImageOps.exif_transpose(image).convert("RGB")
            if max(image.size) > 4096:
                image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            images.append(image)
        except MotionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MotionError(f"读取幻灯片图片失败：{exc}") from exc
    if not images:
        raise MotionError("slideshow 没有可用图片")
    return images


def _cover_image(image: Image.Image, width: int, height: int, zoom: float = 1.0, pan_x: float = 0.0, pan_y: float = 0.0) -> Image.Image:
    scale = max(width / image.width, height / image.height) * zoom
    new_w = max(width, int(round(image.width * scale)))
    new_h = max(height, int(round(image.height * scale)))
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - width) / 2 + pan_x
    top = (new_h - height) / 2 + pan_y
    left = _clamp(left, 0, max(0, new_w - width))
    top = _clamp(top, 0, max(0, new_h - height))
    return resized.crop((int(left), int(top), int(left) + width, int(top) + height))


def _slideshow_frames(spec: dict, message_images: list[str]) -> Iterable[Image.Image]:
    w, h = spec["width"], spec["height"]
    images = _load_slideshow_images(spec, message_images)
    count = len(images)
    per_image = spec["duration"] / count
    transition = spec["transition"]
    overlay_svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs>
    <linearGradient id="shade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#000000" stop-opacity="0"/>
      <stop offset="1" stop-color="#000000" stop-opacity="0.82"/>
    </linearGradient>
  </defs>
  <rect x="0" y="{h * 0.58:.1f}" width="{w}" height="{h * 0.42:.1f}" fill="url(#shade)"/>
</svg>'''
    overlay = _render_svg_frame(overlay_svg, w, h, background=None)
    if spec["title"]:
        text_svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <text x="56" y="{_fmt(h - 92 * _ui_scale(spec))}" fill="{spec['foreground']}" font-family="{spec['font_family']}" font-size="{_fmt(max(18, 34 * _ui_scale(spec)))}" font-weight="700">{_esc(spec['title'])}</text>
  <text x="58" y="{_fmt(h - 58 * _ui_scale(spec))}" fill="{spec['muted']}" font-family="{spec['font_family']}" font-size="{_fmt(max(10, 16 * _ui_scale(spec)))}">{_esc(spec['subtitle'])}</text>
</svg>'''
        overlay = Image.alpha_composite(overlay, _render_svg_frame(text_svg, w, h, background=None))

    for frame_index in range(spec["frames"]):
        time_s = frame_index / spec["fps"]
        segment = min(count - 1, int(time_s / per_image))
        local = _clamp((time_s - segment * per_image) / per_image, 0.0, 1.0)
        zoom = 1.04 + 0.06 * local
        pan_x = math.sin(local * math.pi * 2 + segment) * w * 0.012
        pan_y = math.cos(local * math.pi * 1.5 + segment) * h * 0.01
        current = _cover_image(images[segment], w, h, zoom, pan_x, pan_y)
        if transition == "slide" and local > 0.82 and segment + 1 < count:
            blend = _ease((local - 0.82) / 0.18)
            nxt = _cover_image(images[segment + 1], w, h, 1.04, 0, 0)
            shifted_current = Image.new("RGB", (w, h), spec["background"])
            shifted_current.paste(current, (int(-w * 0.55 * blend), 0))
            shifted_next = Image.new("RGB", (w, h), spec["background"])
            shifted_next.paste(nxt, (int(w * 0.55 * (1 - blend)), 0))
            base = Image.blend(shifted_current, shifted_next, blend)
        elif local > 0.82 and segment + 1 < count:
            blend = _ease((local - 0.82) / 0.18)
            nxt = _cover_image(images[segment + 1], w, h, 1.0 + 0.04 * (1 - blend))
            base = Image.blend(current, nxt, blend)
        else:
            base = current
        frame = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
        yield frame


_FRAME_BUILDERS = {
    "flow": _flow_frames,
    "timeline": _timeline_frames,
    "cycle": _cycle_frames,
    "comparison": _comparison_frames,
    "text": _text_frames,
    "chart": _chart_frames,
}


def _ensure_frame(frame: Image.Image, width: int, height: int) -> Image.Image:
    frame = frame.convert("RGB")
    if frame.size != (width, height):
        frame = frame.resize((width, height), Image.Resampling.LANCZOS)
    return frame


def _make_gif_palette(frames: list[Image.Image]) -> Image.Image:
    count = min(12, len(frames))
    if count <= 1:
        return frames[0].resize((160, 90), Image.Resampling.LANCZOS).quantize(colors=256)
    indices = [round(i * (len(frames) - 1) / (count - 1)) for i in range(count)]
    strip = Image.new("RGB", (160 * count, 90))
    for index, frame_index in enumerate(indices):
        strip.paste(frames[frame_index].resize((160, 90), Image.Resampling.LANCZOS), (index * 160, 0))
    return strip.quantize(colors=256, method=Image.Quantize.MEDIANCUT)


def _encode_gif(path: Path, frames: list[Image.Image], fps: int, loop: bool) -> None:
    palette = _make_gif_palette(frames)
    paletted = [
        frame.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
        for frame in frames
    ]
    duration = max(20, int(1000 / fps))
    paletted[0].save(
        path,
        save_all=True,
        append_images=paletted[1:],
        duration=duration,
        loop=0 if loop else 1,
        optimize=True,
        disposal=2,
    )


def _encode_webp(path: Path, frames: list[Image.Image], fps: int, loop: bool) -> None:
    duration = max(20, int(1000 / fps))
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0 if loop else 1,
        quality=82,
        method=6,
    )


def _encode_mp4(path: Path, frames: Iterable[Image.Image], fps: int, width: int, height: int) -> None:
    if imageio_ffmpeg is None:
        raise MotionError("缺少 imageio-ffmpeg 依赖，请安装 requirements.txt 后重载插件")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-s",
        f"{width}x{height}",
        "-pix_fmt",
        "rgb24",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-vcodec",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-movflags",
        "+faststart",
        str(path),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        for frame in frames:
            frame = _ensure_frame(frame, width, height)
            process.stdin.write(frame.tobytes())
        process.stdin.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        code = process.wait(timeout=180)
    except Exception as exc:
        process.kill()
        stderr = process.stderr.read() if process.stderr is not None else b""
        snippet = stderr.decode("utf-8", "replace")[-600:]
        raise MotionError(f"MP4 编码失败：{snippet or exc}") from exc
    if code != 0:
        snippet = stderr.decode("utf-8", "replace")[-600:]
        raise MotionError(f"MP4 编码失败：{snippet}")


def encode_animation(spec: dict, frames: Iterable[Image.Image], path: Path) -> None:
    fmt = spec["format"]
    width, height, fps = spec["width"], spec["height"], spec["fps"]
    if fmt == "mp4":
        _encode_mp4(path, frames, fps, width, height)
        return
    collected = [_ensure_frame(frame, width, height) for frame in frames]
    if fmt == "gif":
        _encode_gif(path, collected, fps, spec["loop"])
    else:
        _encode_webp(path, collected, fps, spec["loop"])


def render_animation(
    spec: dict,
    out_dir: Path,
    message_images: list[str] | None = None,
    defaults: dict | None = None,
) -> dict:
    """渲染动画，返回路径和元数据。"""
    normalized = normalize_spec(spec, defaults)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = normalized["pattern"]
    if pattern == "slideshow":
        frames = _slideshow_frames(normalized, list(message_images or []))
    else:
        frames = _FRAME_BUILDERS[pattern](normalized)
    filename = f"motion_{pattern}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.{normalized['format']}"
    path = out_dir / filename
    try:
        encode_animation(normalized, frames, path)
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        raise
    if not path.exists() or path.stat().st_size == 0:
        raise MotionError("动画编码失败，输出文件为空")
    size = path.stat().st_size
    if size > normalized["max_file_mb"] * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise MotionError(
            f"输出文件超过 {normalized['max_file_mb']} MB，请缩短时长、降低帧率或分辨率"
        )
    return {
        "path": str(path),
        "format": normalized["format"],
        "mime": {
            "gif": "image/gif",
            "webp": "image/webp",
            "mp4": "video/mp4",
        }[normalized["format"]],
        "width": normalized["width"],
        "height": normalized["height"],
        "frames": normalized["frames"],
        "fps": normalized["fps"],
        "duration": normalized["duration"],
        "size_bytes": size,
        "title": normalized["title"] or _PATTERN_LABEL[pattern],
        "pattern": pattern,
        "send_as_file": normalized["send_as_file"],
        "caption": normalized["caption"],
    }
