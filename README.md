# 图像工坊 Image Studio

为 AstrBot 提供多服务商 **文生图 / 图生图** 能力。用户只需配置服务地址（Base URL）与 API Key，即可测试、检测可用模型并保存配置；同时为机器人提供聊天指令和 AI 智能体工具。

## 特性

- 内置多家国内外 OpenAI 兼容图像服务提供商：OpenAI、apikey.fan 聚合、Packy / API.FAN（GPT-Image，含全球与优化线路）、硅基流动 SiliconFlow、智谱 BigModel、火山方舟（豆包），并支持完全自定义。
- 配置后可在可视化设置页一键 **测试文生图 / 图生图**，测试通过后再保存。
- 一键 **检测可用模型**（调用 `GET /models`），并下拉选择。
- 聊天指令：`/文生图 <描述>`、`/t2i <描述>`、`/图生图 <描述>`、`/i2i <描述>`（图生图需附上一张参考图片）。
- 向 AI 智能体注册 `text_to_image` 与 `image_to_image` 两个工具，机器人可在对话中按需调用并直接回图。
- 完整支持 apikey.fan 的 SSE 流式返回（`image_generation.completed` 事件）与多图 multipart 图生图。

### Packy / API.FAN（GPT-Image）

- 全球线路 Base URL：`https://cf.api.fan/v1`
- 优化线路 Base URL：`https://slb-v1.api.fan/v1`
- 模型：`gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`，实际以 `image` 分组实时列表为准。
- 只支持 Images API（`/v1/images/generations`、`/v1/images/edits`），不支持 Responses API 或 Chat Completions 出图。
- `n` 仅支持 1；不支持 `stream` / `partial_images`；不使用 `response_format`。
- 输出格式通过 `output_format`（`png` / `jpeg` / `webp`）控制；`output_compression` 仅在 `jpeg` 时生效。
- 控制台地址为 `https://www.packyapi.ai`，它只是网页控制台，不要填成 API 地址。

## 安装

将本目录放入 AstrBot 的 `data/plugins/` 下（目录名保持 `astrbot_plugin_image_studio`），然后在 WebUI「插件」页启用并重载。

依赖：`httpx`（已写入 `requirements.txt`，AstrBot 会自动安装）。

## 多配置切换

- 在设置视图顶部点击「新建配置」，可以为不同服务商分别保存档案，例如 `OpenAI 主号`、`Packy 全球线路`、`apikey.fan 聚合`。
- 每个档案独立保存 Base URL、API Key、模型和生成参数，可以逐个测试。
- 点击「设为当前」决定机器人实际使用哪一个档案；聊天指令和 AI 智能体工具都会使用当前档案。
- 档案保存在 `data/plugin_data/astrbot_plugin_image_studio/profiles.json`，重装或更新插件不会覆盖。
- 该文件包含 API Key，请妥善保护，不要公开分享。

## 使用

1. 打开 WebUI → 插件 → 图像工坊 Image Studio → 打开「设置」视图。
2. 在顶部选择已有配置，或点击「新建配置」并填写一个容易辨认的名称。
3. 选择服务提供商，会自动填入 Base URL 与推荐模型；也可手动修改，或选择「自定义」填写任意 OpenAI 兼容接口。
4. 填入 API Key，点击「检测可用模型」拉取真实模型列表并选择。
5. 在「测试」区先跑一次文生图或图生图，确认能正常出图。
6. 勾选「保存后设为当前配置」并点击「保存配置」。
7. 需要多家服务商时重复「新建配置」；之后在顶部选择配置并点击「设为当前」即可切换。
8. 机器人聊天指令与 AI 智能体工具始终使用「当前」配置。

## 接口约定

- 文生图：`POST {base_url}/images/generations`（JSON，支持 `b64_json` / `url`，可选 `stream=true`）。
- 图生图：`POST {base_url}/images/edits`（multipart，`image` 字段可多张，其余参数以表单字段传递）。
- 模型列表：`GET {base_url}/models`。
- 认证：`Authorization: Bearer <API Key>`。

内置提供商中的模型名仅为推荐值，实际可用模型请以「检测可用模型」结果为准。

## 目录结构

```
astrbot_plugin_image_studio/
├─ main.py            # 插件入口：指令、LLM 工具、Web API
├─ client.py          # 图像接口客户端（含 SSE 解析）
├─ providers.py       # 提供商预设
├─ profiles.py        # 多配置档案存储与迁移
├─ _conf_schema.json  # 全局配置 Schema
├─ metadata.yaml      # 插件元数据
├─ requirements.txt
├─ logo.png
└─ views/settings/    # 可视化设置页
   └─ index.html
```

生成图片保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_image_studio/outputs/` 目录，重装/更新插件不会覆盖。
