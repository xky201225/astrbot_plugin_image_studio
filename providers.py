"""图像服务提供商预设。

内置提供商均为 OpenAI 兼容的 Images API（文生图 /images/generations，图生图
/images/edits）。模型列表只是推荐值，用户可在可视化视图中点击“检测可用模型”
拉取真实模型列表。

Packy（api.fan）说明：
- OpenAI 兼容 Base URL 为 https://cf.api.fan/v1（优化线路 https://slb-v1.api.fan/v1）。
- 只支持 Images API，不支持 Responses API / Chat Completions 出图。
- n 仅支持 1；不支持 stream / partial_images；不支持 response_format。
- 使用 output_format（png / jpeg / webp）控制输出格式。
"""

from __future__ import annotations

_COMMON_SIZES = [
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
    "768x1024",
    "1024x768",
    "512x512",
]

_PACKY_SIZES = [
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
]

PROVIDERS: list[dict] = [
    {
        "id": "openai",
        "name": "OpenAI（国际）",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-image-1", "dall-e-3", "dall-e-2"],
        "default_model": "gpt-image-1",
        "stream": False,
        "supports_stream": True,
        "max_n": 4,
        "supports_response_format": True,
        "supports_output_format": False,
        "default_size": "1024x1024",
        "sizes": _COMMON_SIZES,
    },
    {
        "id": "apikey",
        "name": "apikey.fan 聚合",
        "base_url": "https://api.apikey.fan/v1",
        "models": ["gpt-image-2", "gpt-image-1", "dall-e-3"],
        "default_model": "gpt-image-2",
        "stream": True,
        "supports_stream": True,
        "max_n": 4,
        "supports_response_format": True,
        "supports_output_format": False,
        "default_size": "1024x1024",
        "sizes": _COMMON_SIZES,
    },
    {
        "id": "packy",
        "name": "Packy / API.FAN（GPT-Image 全球线路）",
        "base_url": "https://cf.api.fan/v1",
        "models": ["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
        "default_model": "gpt-image-2",
        "stream": False,
        "supports_stream": False,
        "max_n": 1,
        "supports_response_format": False,
        "supports_output_format": True,
        "default_size": "auto",
        "sizes": _PACKY_SIZES,
    },
    {
        "id": "packy_optimized",
        "name": "Packy / API.FAN（GPT-Image 优化线路）",
        "base_url": "https://slb-v1.api.fan/v1",
        "models": ["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
        "default_model": "gpt-image-2",
        "stream": False,
        "supports_stream": False,
        "max_n": 1,
        "supports_response_format": False,
        "supports_output_format": True,
        "default_size": "auto",
        "sizes": _PACKY_SIZES,
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            "black-forest-labs/FLUX.1-schnell",
            "black-forest-labs/FLUX.1-dev",
            "Kwai-Kolors/Kolors",
            "stabilityai/stable-diffusion-3-5-large",
        ],
        "default_model": "black-forest-labs/FLUX.1-schnell",
        "stream": False,
        "supports_stream": False,
        "max_n": 4,
        "supports_response_format": True,
        "supports_output_format": False,
        "default_size": "1024x1024",
        "sizes": ["1024x1024", "768x1024", "1024x768", "512x512"],
    },
    {
        "id": "zhipu",
        "name": "智谱 AI BigModel",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["cogview-3-flash", "cogview-3-plus", "cogview-4"],
        "default_model": "cogview-3-flash",
        "stream": False,
        "supports_stream": False,
        "max_n": 1,
        "supports_response_format": True,
        "supports_output_format": False,
        "default_size": "1024x1024",
        "sizes": ["1024x1024", "768x1024", "1024x768"],
    },
    {
        "id": "volcengine",
        "name": "火山方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seedream-3-0-t2i-250415"],
        "default_model": "doubao-seedream-3-0-t2i-250415",
        "stream": False,
        "supports_stream": False,
        "max_n": 4,
        "supports_response_format": True,
        "supports_output_format": False,
        "default_size": "1024x1024",
        "sizes": ["1024x1024", "2048x2048", "1024x1536", "1536x1024"],
    },
    {
        "id": "custom",
        "name": "自定义 / OpenAI 兼容",
        "base_url": "",
        "models": [],
        "default_model": "",
        "stream": False,
        "supports_stream": True,
        "max_n": 4,
        "supports_response_format": True,
        "supports_output_format": False,
        "default_size": "1024x1024",
        "sizes": _COMMON_SIZES,
    },
]

PROVIDER_IDS = [p["id"] for p in PROVIDERS]


def get_provider(provider_id: str | None) -> dict:
    """按 id 返回提供商预设，找不到时返回 custom。"""
    for provider in PROVIDERS:
        if provider["id"] == provider_id:
            return provider
    return PROVIDERS[-1]


def resolve_provider(provider_id: str | None) -> dict:
    """返回一个安全可序列化的预设副本（不修改全局常量）。"""
    source = get_provider(provider_id)
    return dict(source)
