# astrbot_plugin_gpt_image2

面向 `gpt-image-2` 的 AstrBot 生图/改图插件。它不是通用绘图框架，而是一个窄域稳定插件：命令与 LLM Tool 都只提交后台任务，最终图片由插件后台直接发送到原会话。

## 设计目标

- 只支持 `gpt-image-2`。
- 只走 OpenAI-compatible Images API：`/v1/images/generations` 与 `/v1/images/edits`。
- 支持命令触发与 LLM Tool 触发。
- LLM Tool **不等待最终图片**，只返回 `job_id`。
- 后台 worker 完成后主动发送图片。
- 失败时直接发送脱敏、截断后的原始错误摘要，不再调用 AstrBot LLM 润色。
- 内置本地队列与单并发保护，避免长图任务重复提交导致 429。

## 命令

```text
/gptimg <prompt>
/gptimg --size 1536x1024 --quality medium <prompt>
/gptedit <prompt>       # 当前消息附图，或引用包含图片的消息
/gptimg_status [job_id]
/gptimg_cancel <job_id>
/gptimg_help
```

支持参数：

- `--size`: `1024x1024` / `1536x1024` / `1024x1536`
- `--quality`: `low` / `medium` / `high` / `auto`
- `--format`: `png` / `jpeg` / `webp`
- `--background`: `auto` / `transparent` / `opaque`

## LLM Tool

插件注册两个工具：

- `gpt_image2_generate`
- `gpt_image2_edit`

工具语义：

```text
LLM Tool -> enqueue local job -> immediately return job_id
background worker -> call gpt-image-2 -> save image -> send image to original chat
```

因此即使图片生成耗时 300+ 秒，也不会卡住 AstrBot 的 tool executor。

## 配置

插件提供 `_conf_schema.json`，安装后在 AstrBot WebUI 中配置：

```text
api.base_url: https://your-openai-compatible-endpoint/v1
api.api_key: sk-...
api.model: gpt-image-2
api.timeout_seconds: 900
runtime.global_max_concurrent: 1    # v0.1 会强制钳制为 1
runtime.queue_max_size: 5
runtime.per_user_queue_max_size: 5
llm_tool.enabled: true
```

强烈建议给图像生成使用独立 key/channel，避免长图任务占用普通聊天模型并发。

## 失败处理

- `429 Concurrency limit exceeded`: 不盲目重试，直接向原会话发送上游错误。
- `504` / timeout: 不自动重复 POST。图像请求是非幂等操作，重复提交可能重复扣费。
- 插件重启时，未完成的 queued/running job 会被标记失败，避免重启后重复提交。

## 持久化数据

运行时状态与生成图片保存在 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_gpt_image2/
```

输出文件在插件启动时会按 `runtime.job_ttl_hours` 做 best-effort 过期清理，避免长期堆积。不会写入插件源码目录。

## 本地 smoke test

不需要真实 AstrBot 实例即可运行两个基础测试：

```bash
python -m pip install -r requirements.txt
python -m compileall -q .
python tests/smoke_core.py
python tests/smoke_import_modes.py
```

其中 `smoke_import_modes.py` 会同时验证：

- AstrBot package import：`astrbot_plugin_gpt_image2.main`
- 本地 top-level import：`main`

这能提前捕获插件安装时常见的相对导入/包路径错误。

## 开发来源

- AstrBot 插件开发指南：https://docs.astrbot.app/dev/star/plugin-new.html
- AstrBot Plugin Configuration Wiki：https://github.com/AstrBotDevs/AstrBot/wiki/en-dev-star-guides-plugin-config
- AstrBot AI / LLM Tool 指南：https://github.com/AstrBotDevs/AstrBot/wiki/zh-dev-star-guides-ai
- OmniDraw 参考实现（参数解析、缓存清理、配置组织等设计）：https://github.com/diaomin66/astrbot_plugin_omnidraw
