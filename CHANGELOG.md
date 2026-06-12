# Changelog

所有值得用户注意的变更都会记录在这里。

格式参考 Keep a Changelog，但按本插件的实际发布节奏保持轻量。

## v0.3.0 - 2026-06-12

### Fixed

- 按 AstrBot `template_list` 官方格式补齐 fallback endpoint 配置中的 `__template_key` 运行时迁移，避免手写/旧配置在 WebUI 保存校验时报“缺少模板选择”。
- 增强 sender/group id 提取兼容性：优先使用 `AstrMessageEvent.get_sender_id()` / `get_group_id()`，缺失时回退到 `message_obj.sender.user_id` / `message_obj.group_id` / `message_obj.group.group_id`，避免少数适配器或测试事件导致使用限制误判。

## v0.2.3 - 2026-06-12

### Added

- 使用限制新增 `access.user_blacklist` 用户黑名单；黑名单用户禁止提交生图任务，并优先于白名单与每日额度。

## v0.2.2 - 2026-06-12

### Changed

- WebUI 与文档中将“访问控制”命名调整为“使用限制”，更贴近白名单与每日额度语义。

## v0.2.1 - 2026-06-12

### Fixed

- Fallback API 配置项补充独立模型名字段；每个 fallback endpoint 现在使用自己填写的 `model`。
- 同步插件注册版本号与 User-Agent 到 v0.2.1。

## v0.2.0 - 2026-06-12

### Added

- `/gptimg` 自动路由：无参考图时文生图，有当前/引用图片时改图。
- `/gptedit` 兼容保留，用于强制改图。
- 图片输出缓存治理：启动清理、周期清理、TTL 清理与缓存体积上限。
- 使用限制：用户白名单、群组白名单、非白名单每日额度。
- Prompt 前缀配置。
- `runtime.quiet_mode` 静默生图模式。
- 备用 API fallback 链路。
- 启动时将旧版 JSON 字符串 fallback 配置重置为空列表，避免 WebUI template_list 收到旧标量值。
- 本地 smoke tests：核心逻辑测试与 AstrBot import modes 测试。

### Changed

- 备用 API 在 WebUI 中使用可逐条添加的 Fallback API 列表配置。
- 每个 Fallback API 只需填写 `Fallback API Base URL` 与 `Fallback API Key`。
- 备用 API 沿用主配置的 `api.model`，当前固定为 `gpt-image-2`。
- 精简 `_conf_schema.json` 中所有配置项描述；详细说明移至 README。
- 命令入口与 LLM Tool 入口共享使用限制、额度、队列和 prompt prefix 策略。
- 后台任务成功时始终发送最终图片；`send_finish_message=false` 仅关闭完成 caption。
- 全局上游并发继续强制钳制为 1，避免 gpt-image-2 并发触发 429。

### Fixed

- 修复 AstrBot package import 模式下的导入问题。
- 修复静默访问拒绝路径可能继续落入默认 LLM 流程的问题。
- 修复图片发送失败时任务状态与错误阶段记录不准确的问题。

### Safety

- fallback 仅在明确安全的状态码后尝试：`401` / `403` / `404` / `429`。
- timeout、`504`、`5xx` 后不自动 fallback，避免非幂等 image POST 重复扣费或重复生成。
- 插件重启时 queued/running job 会标记失败，避免重复提交。

## v0.1.0

### Added

- 初始 MVP：面向 `gpt-image-2` 的稳定生图插件。
- 支持 `/gptimg` 文生图命令。
- 支持 `gpt_image2_generate` LLM Tool。
- 内置后台任务队列，LLM Tool 只提交任务并立即返回 `job_id`。
- 后台 worker 完成后主动发送图片到原会话。
- 上游错误脱敏与截断后直接返回，不再调用 AstrBot LLM 润色。
