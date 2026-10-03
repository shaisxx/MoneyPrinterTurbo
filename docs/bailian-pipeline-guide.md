# 阿里云百炼 TokenPlan 全链路使用指南

本项目（MoneyPrinterTurbo 扩展分支）已把**脚本生成、封面/配图、AI 视频素材**三条链路全部接入阿里云百炼 TokenPlan，**共用同一把 API Key**。本文档覆盖配置、三种调用方式、模型参考、计费与排障。

> 适用分支：`dev-shaisxx`　｜　相关提交：`7ee8aba`(LLM)、`70b868e`(文生图)、`0037013`(文生视频)

---

## 1. 能力总览

| 链路 | 用途 | 选择方式 | 底层接口 | 计费 |
|---|---|---|---|---|
| **LLM** | 生成视频脚本、提炼素材关键词 | 大模型 Provider = `bailian_tokenplan` | `/compatible-mode/v1/chat/completions`（OpenAI 兼容，同步） | 按 token |
| **文生图** | 封面 / 配图素材（图片→放大片段） | 素材来源 = `bailian_image` | `/api/v1/services/aigc/multimodal-generation/generation`（DashScope 原生，**同步**） | 按张 |
| **文生视频** | AI 生成视频素材 | 素材来源 = `bailian_video` | `/api/v1/services/aigc/video-generation/video-synthesis`（DashScope 原生，**异步**任务） | 按片段 |

三条链路都从 `bailian_tokenplan_api_key` 取 Key；文生图/视频的原生 host 由 `bailian_tokenplan_base_url` 自动推导（剥离 `/compatible-mode/v1`），也可用 `bailian_native_base_url` 覆盖。

> 配音（TTS）当前仍走 **edge-tts（免费无 Key）** 或其它已接入的 TTS；百炼 TokenPlan 端点不提供 `/audio/speech`，故未接入百炼语音。字幕走 edge 时间戳或本地 whisper，无需第三方。

---

## 2. 前提与部署

- Python 3.11、uv、ffmpeg（本机已具备）
- 安装依赖：`uv sync --frozen`
- 配置文件：首次由 `config.example.toml` 复制为 `config.toml`（**已被 .gitignore 忽略，Key 不会入库**）

启动服务（项目根目录）：

```bash
# API 服务（默认 8080）
uv run python main.py
# WebUI（默认 8501）
.\webui.bat        # Windows
```

---

## 3. 配置项（config.toml）

### 3.1 LLM（必填）

```toml
llm_provider = "bailian_tokenplan"
bailian_tokenplan_api_key = "sk-sp-你的Key"
bailian_tokenplan_base_url = ""   # 留空=用默认 https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
bailian_tokenplan_model_name = "" # 留空=用默认 qwen3.8-max
```

> WebUI 保存时会把"等于默认值"的项归一化为空，运行时自动回落到默认地址/模型，属正常现象。

### 3.2 文生图（`bailian_image`）

```toml
bailian_image_model = "qwen-image-2.0"   # 或 qwen-image-3.0-pro / wan2.7-image
bailian_image_size = ""                   # 留空按画幅自动：竖屏 928*1664 / 横屏 1664*928 / 方形 1328*1328
bailian_image_prompt_template = ""        # 可选，用 {term} 注入关键词
```

### 3.3 文生视频（`bailian_video`）

```toml
bailian_video_model = "happyhorse-1.1-t2v"
bailian_video_resolution = "1080P"        # 480P / 720P / 1080P
bailian_video_min_duration = 3            # HappyHorse 支持 3-15 秒
bailian_video_max_duration = 15
bailian_video_watermark = false           # 默认请求无水印
bailian_video_poll_interval = 5
bailian_video_run_timeout = 1800
# bailian_video_api_key = ""              # 可选：为视频单独指定 Key（否则复用 tokenplan Key）
```

### 3.4 可选：原生 host 覆盖（图/视频共用）

```toml
bailian_native_base_url = ""              # 留空自动从 tokenplan_base_url 推导
```

---

## 4. 可用模型参考（TokenPlan）

- **文本/推理/视觉理解**：`qwen3.8-max`、`qwen3.8-flash`、`qwen3.7-plus/max`、`qwen3.6-plus/flash`、`deepseek-v4-pro/flash`、`deepseek-v3.2`、`glm-5.3/5.2/5.1/5`、`kimi-k2.5/k2.6/k2.7-code`、`MiniMax-M2.5`、`auto`
- **图片生成**：`qwen-image-3.0-pro`、`qwen-image-2.0-pro`、`qwen-image-2.0`、`wan2.7-image`、`wan2.7-image-pro`
- **视频生成**：`happyhorse-1.1-t2v`（文生视频，已接入）、`happyhorse-1.1-i2v`/`r2v`（图生/参考生视频，**尚未接入**）
- **语音**：`qwen-audio-3.0-tts-plus`（合成）、`qwen-audio-3.0-asr-flash`（识别）、`qwen-audio-3.0-realtime-plus`（实时）——**TokenPlan 兼容端点未开放，本项目未接入**

> 提示：`auto` 会由平台自动路由模型，省心但不确定具体走哪个；追求稳定质量建议显式指定（如 `qwen3.8-max`）。

---

## 5. 三种使用方式

### 5.1 WebUI（http://127.0.0.1:8501）

1. **设置 → 基础设置 → 大模型**：Provider 选「阿里云百炼 TokenPlan」，填 API Key（Model 留空即 `qwen3.8-max`）。
2. **素材来源**：
   - 配图选「AI Image → 阿里云百炼文生图」，在「AI Image Generation APIs」里配置模型/尺寸；
   - AI 视频选「AI Video → 阿里云百炼文生视频 (HappyHorse)」，在「AI Video Generation APIs」里配置模型/分辨率/水印。
3. **计费确认**：选择 `bailian_video` 后，视频设置区会显示预计片段数与「我理解这将创建付费的百炼视频任务」勾选框，**必须勾选**才能开始。
4. 填主题/文案 → 开始生成。

### 5.2 CLI

```bash
# 用百炼 LLM 写脚本 + 百炼文生图做素材（免费 edge-tts 配音）
uv run python cli.py --video-subject "人工智能如何改变日常生活" \
  --video-source bailian_image

# 用百炼文生视频做素材（付费，必须加 --confirm-bailian-charge）
uv run python cli.py --video-subject "海边日落的金毛犬" \
  --video-source bailian_video --confirm-bailian-charge

# 只跑到素材阶段（不出成片），便于先验证素材
uv run python cli.py --video-subject "..." --video-source bailian_video \
  --confirm-bailian-charge --stop-at materials
```

> `--video-source` 可选值含 `bailian_image`、`bailian_video`。LLM 由 config.toml 的 `llm_provider` 决定，CLI 无需单独指定。

### 5.3 API（http://127.0.0.1:8080，Swagger 见 /docs）

`POST /api/v1/videos`，请求体里设 `"video_source": "bailian_image"` 或 `"bailian_video"`。LLM 用服务端 config.toml 的 `llm_provider`。注意：API 默认无鉴权（`api_key` 为空时），暴露到局域网前请先在 config.toml 设 `api_key` 或把 `listen_host` 改 `127.0.0.1`。

---

## 6. 计费与安全注意

- **按量付费**：文生图按张、文生视频按片段计费。项目采用"按需逐段生成、凑够配音时长立即停止"，不会为用不到的关键词付费。
- **计费安全**：提交后若遇读超时/连接中断，视为"未确认"，会**终止任务而非自动重试**，避免重复扣费；失败状态里会带 `bailian_video_task_id`，可去百炼控制台按 task_id 找回结果。
- **Key 保护**：`config.toml` 已被 gitignore；不要把 Key 写进代码或提交。若 Key 曾出现在聊天/日志中，建议在百炼控制台**轮换**。
- **水印**：HappyHorse 默认带水印，本项目默认 `bailian_video_watermark = false` 请求无水印。

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `url error, please check url` | 用了 `/compatible-mode/v1` 调图/视频，或路径拼错 | 图/视频必须走原生 `/api/v1/...`；本项目已自动推导，检查 `bailian_native_base_url` 是否被误填 |
| `AccessDenied: does not support asynchronous calls` | 对图片用了异步 image-synthesis | 百炼图片走**同步** multimodal-generation（本项目已如此），勿改异步 |
| `Model not exist` (404) | 模型名不在 TokenPlan 可用列表 | 对照第 4 节模型参考修正 `*_model` |
| 预检失败 `requires ... API key` | 未填 `bailian_tokenplan_api_key` | 在 config.toml 或 WebUI 大模型设置里填 Key |
| 视频任务 `FAILED` | 参数非法（如 duration 越界）或内容策略 | 看失败详情；duration 收敛到 3-15，分辨率限 480P/720P/1080P |
| CLI 报 `--confirm-bailian-charge is required` | 付费视频源未确认 | 加 `--confirm-bailian-charge`（WebUI 则勾选确认框） |

---

## 8. 已验证的接口契约（实测）

**LLM（同步）**
```
POST https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer <key>
{"model":"qwen3.8-max","messages":[{"role":"user","content":"..."}]}
→ choices[0].message.content
```

**文生图（同步）**
```
POST https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
Authorization: Bearer <key>   Content-Type: application/json
{"model":"qwen-image-2.0","input":{"messages":[{"role":"user","content":[{"text":"..."}]}]},"parameters":{"size":"1328*1328","n":1}}
→ output.choices[0].message.content[0].image  (OSS 图片直链)
```

**文生视频（异步）**
```
POST https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis
Authorization: Bearer <key>   X-DashScope-Async: enable   Content-Type: application/json
{"model":"happyhorse-1.1-t2v","input":{"prompt":"..."},"parameters":{"resolution":"1080P","ratio":"16:9","duration":5,"watermark":false}}
→ output.task_id

GET https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/tasks/{task_id}
Authorization: Bearer <key>
→ output.task_status: PENDING/RUNNING/SUCCEEDED/FAILED/CANCELED/UNKNOWN
→ 成功时 output.video_url (OSS 直链，24h 有效)
```

> `ratio` 直接取画幅：竖屏 `9:16`、横屏 `16:9`、方形 `1:1`。

---

## 9. 局限与后续可扩展

- **未接入**：百炼语音（TTS/ASR，端点未开放）、`happyhorse i2v/r2v`（图生/参考生视频，需图片输入）、embedding/视频理解。
- **可扩展**：若日后需要"百炼配音"，需确认 TokenPlan 是否开放语音端点，再按 dashscope 原生 TTS 协议新增适配器（不能复用 OpenAI `/audio/speech`）。
- 建议把"全百炼"组合（LLM=bailian_tokenplan + 素材=bailian_image/bailian_video + 配音=edge-tts + 字幕=edge + BGM=内置）固化为一个预设，便于一键切换。
