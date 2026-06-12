# astrbot_plugin_gpt_image2

面向 `gpt-image-2` 的 AstrBot 生图/改图插件。它不是通用绘图框架，而是一个窄域稳定插件：命令与 LLM Tool 都只提交后台任务，最终图片由插件后台直接发送到原会话。

## 设计目标

- 只支持 `gpt-image-2` 兼容 Images API endpoint；fallback 也必须是同一 API 形态。
- 只走 OpenAI-compatible Images API：`/v1/images/generations` 与 `/v1/images/edits`。
- 支持命令触发与 LLM Tool 触发。
- LLM Tool **不等待最终图片**，只返回 `job_id`。
- 后台 worker 完成后主动发送图片。
- 失败时直接发送脱敏、截断后的原始错误摘要，不再调用 AstrBot LLM 润色。
- 内置本地队列与单并发保护，避免长图任务重复提交导致 429。

## 命令

```text
/gptimg <prompt>       # 无图=文生图；当前/引用消息有图=改图
/gptimg --size 1536x1024 --quality medium <prompt>
/gptedit <prompt>      # 兼容保留：强制改图
/gptimg_status [job_id]
/gptimg_cancel <job_id>
/gptimg_cache
/gptimg_cache_clear
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
LLM Tool -> usage-limit check -> enqueue local job -> immediately return job_id
background worker -> call gpt-image-2 -> save image -> send image to original chat
```

因此即使图片生成耗时 300+ 秒，也不会卡住 AstrBot 的 tool executor。

使用限制在 LLM Tool 路径同样生效：如果是用户让 agent 使用生图工具，插件按该事件的 sender/group 做白名单与每日额度检查。

## 配置

插件提供 `_conf_schema.json`，安装后在 AstrBot WebUI 中配置：

```text
api.base_url: https://your-openai-compatible-endpoint/v1
api.api_key: sk-...
api.model: gpt-image-2
api.timeout_seconds: 900
api.fallback_enabled: false
api.fallback_endpoints:
  - __template_key: fallback_endpoint  # WebUI 自动生成；手写配置时保留
    base_url: https://backup.example.com/v1
    api_key: sk-...
    model: gpt-image-2

runtime.global_max_concurrent: 1
runtime.queue_max_size: 5
runtime.per_user_queue_max_size: 5
runtime.cleanup_interval_minutes: 360
runtime.max_cache_mb: 1024
runtime.quiet_mode: false

prompt.prefix: ""

access.enabled: false
access.user_blacklist: ""
access.user_whitelist: ""
access.group_whitelist: ""
access.non_whitelist_daily_limit: 0

llm_tool.enabled: true
```

强烈建议给图像生成使用独立 key/channel，避免长图任务占用普通聊天模型并发。

## Fallback endpoint

`api.fallback_endpoints` 在 WebUI 中使用“添加 Fallback API”逐条配置；每条需要填写：

```text
Fallback API Base URL: https://backup.example.com/v1
Fallback API Key: sk-...
Fallback Model: gpt-image-2
```

每个备用 API 使用自己填写的 `Fallback Model`。若留空，运行时默认使用 `gpt-image-2`。

手写配置时需保留 `__template_key: fallback_endpoint`；v0.3.0 起插件启动时会为缺失该字段的旧 list 配置自动补齐，以符合 AstrBot `template_list` 的保存校验规则。

保守策略：

- 只在明确安全的状态码后 fallback：`401` / `403` / `404` / `429`。
- 不在 timeout、`504`、`5xx` 后 fallback，因为原 endpoint 可能仍在处理非幂等 image POST，盲目 fallback 可能重复扣费或重复生成。
- 不重试同一个 endpoint。

## 静默模式

`runtime.quiet_mode=true` 时，成功路径尽量不发送固定系统文字：

- 命令提交成功后不回 `job_id/queue_position`。
- 后台开始处理时不发开始提示。
- 最终图片不带完成 caption。
- 成功时用户只看到最终成品图。

失败路径仍会发送脱敏错误摘要，避免任务失败却完全无声。

LLM Tool 仍会向模型返回一个极短的内部排队结果，防止模型重复调用工具；最终用户侧是否显示这句话取决于 AstrBot/模型回复策略。

## Prompt prefix

`prompt.prefix` 会固定追加到用户 prompt 最前面：

```text
<prompt.prefix>
<user prompt>
```

留空则不追加。

## 使用限制

`access.enabled=false` 时不做使用限制。

开启后：

- `access.user_blacklist` 非空：黑名单用户禁止提交生图任务；黑名单优先于白名单与每日额度。
- `access.group_whitelist` 为空：不限制群组。
- `access.group_whitelist` 非空：不在群组白名单里的群消息会被静默忽略。
- `access.user_whitelist` 为空：不限制用户，也不计算每日额度。
- `access.user_whitelist` 非空：白名单用户无限制；非白名单用户受 `access.non_whitelist_daily_limit` 限制。
- `access.non_whitelist_daily_limit=0`：禁止非用户白名单用户提交任务。

黑名单与白名单格式：逗号或换行分隔 ID。

## 失败处理

- `429 Concurrency limit exceeded`: 可按 fallback 策略尝试备用 endpoint；没有 fallback 或备用也失败则直接向原会话发送上游错误。
- `504` / timeout: 不自动重复 POST。图像请求是非幂等操作，重复提交可能重复扣费。
- 插件重启时，未完成的 queued/running job 会被标记失败，避免重启后重复提交。

## 持久化数据与缓存

运行时状态与生成图片保存在 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_gpt_image2/
```

主要文件：

```text
jobs.json          # job 状态，最多保存最近 200 个
access_state.json  # 非白名单用户每日额度计数
outputs/           # 插件自管输出图片缓存
```

缓存清理：

- 插件启动时会清理一次 `outputs/`。
- `runtime.cleanup_interval_minutes > 0` 时，启动后按该周期继续清理；最大 10080 分钟。
- `runtime.job_ttl_hours` 控制按时间清理过期图片与历史 job。
- `runtime.max_cache_mb > 0` 时，一旦输出缓存超过上限，会按最旧文件优先删除直到低于上限。
- 插件只清理自己 `outputs/` 下的 `.png/.jpg/.jpeg/.webp`，不会清理 AstrBot adapter 的临时参考图。

手动查看/清理：

```text
/gptimg_cache
/gptimg_cache_clear
```

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
