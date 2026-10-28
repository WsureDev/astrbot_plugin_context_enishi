# 请求诊断

诊断统一使用 `astrbot.api.logger`，沿用 AstrBot 的日志级别、输出和保留设置。普通插件配置中可调整：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `diagnostics.enabled` | `false` | 开启阶段诊断；关闭时不生成慢阶段监测任务 |
| `diagnostics.log_group_messages` | `true` | 记录群消息进入时的唤醒标记、提及目标、自身 ID、角色与插件范围 |
| `diagnostics.slow_phase_seconds` | `10` | 阶段仍未完成时提示 waiting；0 关闭慢阶段提示 |
| `diagnostics.include_task_stacks` | `true` | 慢阶段附加当前任务的 await 调用栈 |
| `capabilities.mcp_refresh_timeout_seconds` | `15` | 单次远程 MCP 目录拉取的超时，必须大于 0 |

诊断默认常量集中在 `observability.py` 开头；MCP 超时默认常量在 `capabilities/policy.py` 开头。慢阶段提示只记录一次，不取消请求；MCP 超时仅取消 Enishi 发起的该次目录拉取，不关闭原生连接。

普通插件配置的「请求诊断日志 → 启用阶段诊断」是总开关。默认关闭，排查时再打开；已显式保存为开启的配置不会被新默认值覆盖，需要手动关闭。关闭诊断仍会通过 AstrBot logger 记录真实业务错误，不影响 MCP 刷新超时、已失效连接处理等修复。

## 关联一条消息

按 `umo` 找群，再按 `message_id` 或 `trace` 串起本次处理。`task` 是 asyncio 任务名。日志只记录阶段、数量和定位字段，不复制聊天正文、完整工具 schema、图片 Base64 或上游凭据。完整模型载荷继续由 Monitor 保存。

```text
[astrbot_plugin_context_enishi] stage=message.received {"trace":"...","umo":"Haqi8mi:GroupMessage:332993267","message_id":"...","wake":true,...}
[astrbot_plugin_context_enishi] stage=native.session_lock.wait.begin {...}
[astrbot_plugin_context_enishi] stage=native.session_lock.wait.waiting {...,"elapsed_seconds":10.0,"await_stack":["...internal.py:239 process",...]}
```

常见顺序如下，具体阶段随 AstrBot 版本和请求内容变化：

1. `message.received`、`flow.record`、`message.handlers_complete`：消息分派和流水记录。此处位于插件处理阶段，不能证明消息已通过所有后续唤醒条件；被前置处理器拦截的消息也可能到不了此处。
2. `llm.waiting_for_session`、`native.file_attachments`、`native.session_lock.wait`、`native.session_lock.acquired`：原生请求入口与会话串行锁。锁的等待日志可区分旧请求占用同一会话的情形。
3. `native.build_agent` 及其子阶段：模型选择、历史读取、人设与 Skill、引用消息、工作目录提示和知识库检索。`.result` 的 `returned_none=true` 表示原生函数主动返回空；模型选择结果同时记录 provider ID。
4. `llm.request_hook`：群聊增量、图片策略和聊天感知；`llm.request_ready` 记录历史消息与工具数量。
5. `runner.reset.enter`、`capabilities.prepare`、`runner.native_reset`：能力目录、租约与原生 runner 装配。`catalog.lock`、`catalog.refresh`、`capabilities.save_lock`、`capabilities.persist` 细分目录和文件等待。
6. `capabilities.project`、`media.before_step`、`history.budget`、`context.native_process`：每步上下文处理；`llm.response` 记录返回角色、文字长度和工具名，`runner.done`、`agent.done` 记录结束。

阶段使用 `.begin`、`.end`、`.waiting`、`.failed`、`.cancelled` 后缀。先查看最后一个有 begin 但没有 end 的阶段，再查看同 trace 的 waiting 等待栈或异常 traceback。外层阶段也可能报告 waiting，应优先查看最深层的栈。普通闲聊不会出现完整模型阶段；其他插件停止事件时也会提前结束。

启动时 `diagnostics.native_hooks` 列出实际安装的原生观察点；AstrBot 中不存在的入口会跳过。观察包装不改变原函数参数、返回值或业务超时，插件卸载时恢复。未替换 Monitor 的 `step`、`_iter_llm_responses` 或工具执行入口。

## MCP 刷新

`mcp.catalog_pull` 附带 `server`，异常记录服务名及完整 traceback。收到 `ClosedResourceError`、`BrokenResourceError` 或 `EndOfStream` 后记住该失效会话，保留旧工具列表并停止向它重复发送目录请求。原生框架重连并更换 session 后重新允许刷新。原生连接任务已结束时记录 `mcp.connection_closed`。其他刷新异常按正常轮询重试。

目前未确认目标群不回复的根因。已有故障记录中，目标消息未进入 Monitor 的模型任务记录，其他群仍有成功调用；MCP 警告不足以证明它导致目标群停止回复。需用新增阶段日志确定实际停在哪一步。
