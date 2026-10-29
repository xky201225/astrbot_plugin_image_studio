# 图像工坊 Image Studio

为 AstrBot 提供多服务商 **文生图 / 图生图** 能力。用户只需配置服务地址（Base URL）与 API Key，即可测试、检测可用模型并保存配置；同时为机器人提供聊天指令和 AI 智能体工具。

## 特性

- 内置多家国内外 OpenAI 兼容图像服务提供商：OpenAI、apikey.fan 聚合、硅基流动 SiliconFlow、智谱 BigModel、火山方舟（豆包），并支持完全自定义。
- 配置后可在可视化设置页一键 **测试文生图 / 图生图**，测试通过后再保存。
- 一键 **检测可用模型**（调用 `GET /models`），并下拉选择。
- 聊天指令：`/文生图 <描述>`、`/t2i <描述>`、`/图生图 <描述>`、`/i2i <描述>`（图生图需附上一张参考图片）。
- 向 AI 智能体注册 `text_to_image` 与 `image_to_image` 两个工具，机器人可在对话中按需调用并直接回图。
- 完整支持 apikey.fan 的 SSE 流式返回（`image_generation.completed` 事件）与多图 multipart 图生图。

## 安装

将本目录放入 AstrBot 的 `data/plugins/` 下（目录名保持 `astrbot_plugin_image_studio`），然后在 WebUI「插件」页启用并重载。

依赖：`httpx`（已写入 `requirements.txt`，AstrBot 会自动安装）。

## 使用

1. 打开 WebUI → 插件 → 图像工坊 Image Studio → 打开「设置」视图。
2. 选择服务提供商，会自动填入 Base URL 与推荐模型；也可手动修改，或选择「自定义」填写任意 OpenAI 兼容接口。
3. 填入 API Key，点击「检测可用模型」拉取真实模型列表并选择。
4. 在「测试」区先跑一次文生图或图生图，确认能正常出图。
5. 点击「保存配置」。
6. 之后可在聊天中使用指令，或让 AI 智能体自然语言调用生成图片。

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
├─ _conf_schema.json  # 基础配置 Schema
├─ metadata.yaml      # 插件元数据
├─ requirements.txt
├─ logo.png
└─ views/settings/    # 可视化设置页
   └─ index.html
```

生成图片保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_image_studio/outputs/` 目录，重装/更新插件不会覆盖。
