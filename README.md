# 图像工坊 Image Studio

为 AstrBot 提供多服务商 **文生图 / 图生图**、本地 **SVG 绘图** 和 **动画说明** 能力。用户可配置服务地址与 API Key、检测模型并保存多个服务商档案，也可以让机器人用动画解释概念。

## 特性

- 内置多家国内外 OpenAI 兼容图像服务提供商：OpenAI、apikey.fan 聚合、Packy / API.FAN（GPT-Image，含全球与优化线路）、硅基流动 SiliconFlow、智谱 BigModel、火山方舟（豆包），并支持完全自定义。
- 配置后可在可视化设置页一键 **测试文生图 / 图生图**，测试通过后再保存。
- 一键 **检测可用模型**（调用 `GET /models`），并下拉选择。
- 聊天指令：`/文生图`、`/图生图`、`/svg绘图`、`/动画`，并提供英文别名。图生图需附参考图片；动画指令接收 JSON 脚本。
- 向 AI 智能体注册 4 个工具：`text_to_image`、`image_to_image`、`svg_render`、`create_animation`，可分别生成 AI 图片、渲染 SVG 和生成说明动画。
- 完整支持 apikey.fan 的 SSE 流式返回（`image_generation.completed` 事件）与多图 multipart 图生图。
- 内置本地 SVG 绘图：用 SVG 代码绘制图表、示意图、图标和海报，渲染成 PNG 返回，不调用图像模型。
- 内置本地 SVG 动画说明：把概念转成动画脚本，逐帧渲染并编码为 GIF、WebP 或 MP4，不调用视频模型。

### Packy / API.FAN（GPT-Image）

- 全球线路 Base URL：`https://cf.api.fan/v1`
- 优化线路 Base URL：`https://slb-v1.api.fan/v1`
- 模型：`gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`，实际以 `image` 分组实时列表为准。
- 只支持 Images API（`/v1/images/generations`、`/v1/images/edits`），不支持 Responses API 或 Chat Completions 出图。
- `n` 仅支持 1；不支持 `stream` / `partial_images`；不使用 `response_format`。
- 输出格式通过 `output_format`（`png` / `jpeg` / `webp`）控制；`output_compression` 仅在 `jpeg` 时生效。
- 控制台地址为 `https://www.packyapi.ai`，它只是网页控制台，不要填成 API 地址。

## 动画说明（本地 SVG 动画）

这是“机器人理解概念，工具本地渲染动画”的功能，不调用视频生成模型。

- AI 工具：`create_animation`，智能体根据用户描述生成动画 JSON，工具逐帧绘制并回传 GIF/WebP/MP4。
- 聊天指令：`/动画 <JSON 动画脚本>`；发送 `/动画` 或 `/动画 帮助` 查看示例。
- 支持模式：
  - `flow`：流程、因果、序列图（如 TCP 三次握手）
  - `timeline`：时间线、里程碑
  - `cycle`：循环过程（如水循环、碳循环）
  - `comparison`：左右对比
  - `text`：文字淡入、滑入、打字机、霓虹
  - `chart`：柱状图、折线图
  - `slideshow`：消息中附带的多张图片轮播
- 输出格式：GIF（短动画，聊天直接显示）、WebP（体积较小）、MP4（较长或较复杂）。
- 默认 640×360、12 帧，适合聊天 GIF；需要更高清晰度时可在设置或动画脚本中调整。
- 设置视图中有动画 JSON 编辑器、模式示例、尺寸/帧率/时长设置和预览。

`flow` 示例：

```json
{
  "pattern": "flow",
  "title": "TCP 三次握手",
  "actors": [
    { "id": "client", "label": "客户端" },
    { "id": "server", "label": "服务器" }
  ],
  "steps": [
    { "from": "client", "to": "server", "label": "SYN" },
    { "from": "server", "to": "client", "label": "SYN + ACK" },
    { "from": "client", "to": "server", "label": "ACK" }
  ]
}
```

也可以让机器人直接处理自然语言，例如：“用动画解释一下 TCP 三次握手”“把这三张图做成轮播动画”。智能体会调用 `create_animation`。

限制：GIF/WebP 默认最多 180 帧，MP4 默认最多 360 帧；最大时长、边长和文件大小可在设置中调整。渲染是本地 CPU 任务，会限制并发和输出大小。

## 安装

将本目录放入 AstrBot 的 `data/plugins/` 下（目录名保持 `astrbot_plugin_image_studio`），然后在 WebUI「插件」页启用并重载。

依赖：`httpx`、`resvg-py`、`Pillow`、`imageio-ffmpeg`（已写入 `requirements.txt`，AstrBot 会自动安装）。如果更新后在日志中看到缺少依赖，重启一次 AstrBot 或执行 `pip install -r requirements.txt`。

## 多配置切换

- 在设置视图顶部点击「新建配置」，可以为不同服务商分别保存档案，例如 `OpenAI 主号`、`Packy 全球线路`、`apikey.fan 聚合`。
- 每个档案独立保存 Base URL、API Key、模型和生成参数，可以逐个测试。
- 点击「设为当前」决定机器人实际使用哪一个档案；聊天指令和 AI 智能体工具都会使用当前档案。
- 档案保存在 `data/plugin_data/astrbot_plugin_image_studio/profiles.json`，重装或更新插件不会覆盖。
- 该文件包含 API Key，请妥善保护，不要公开分享。

## SVG 绘图（本地渲染）

这是本地矢量绘图，不调用文生图/图生图模型：

- 聊天指令：`/svg绘图 <完整 SVG 代码>`，也可以直接附带一个 `.svg` 文件。
- AI 工具：`svg_render`，智能体传入完整 SVG 文档后由本机渲染并回图。
- 设置视图中有 SVG 编辑器、尺寸/背景设置和「渲染预览」。
- 渲染由 `resvg-py` 完成，不需要 Cairo 或浏览器。
- 为安全起见，会拒绝 `<script>`、`foreignObject`、外部 `http/file` 引用，并清理 `on*` 事件属性。
- 默认最大输出边长为 4096，可在设置中调整；超出时会等比缩小。
- 中文文字依赖系统字体，若部署环境缺少 CJK 字体，可能出现方框或乱码。

最小示例：

```html
<svg xmlns="http://www.w3.org/2000/svg" width="600" height="360" viewBox="0 0 600 360">
  <rect width="600" height="360" rx="24" fill="#eef2ff"/>
  <text x="40" y="110" fill="#1e1b4b" font-family="sans-serif" font-size="42">Hello SVG</text>
</svg>
```

在聊天中可以发送：

```text
/svg绘图 <svg xmlns="http://www.w3.org/2000/svg" width="600" height="360" viewBox="0 0 600 360"><rect width="600" height="360" rx="24" fill="#eef2ff"/><text x="40" y="110" fill="#1e1b4b" font-family="sans-serif" font-size="42">Hello SVG</text></svg>
```

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
├─ svg_tool.py        # SVG 校验、清理与本地光栅化
├─ motion_tool.py     # 声明式动画渲染与 GIF/WebP/MP4 编码
├─ _conf_schema.json  # 全局配置 Schema
├─ metadata.yaml      # 插件元数据
├─ requirements.txt
├─ logo.png
└─ views/settings/    # 可视化设置页
   └─ index.html
```

生成图片保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_image_studio/outputs/` 目录，重装/更新插件不会覆盖。
