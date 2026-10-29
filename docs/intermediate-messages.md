# Agent 中间正文为什么会发到群里

针对任务 `fbf027fb32fd4345b549ffa4b710c69a` 的只读核对：2026-10-29 11:55:36–11:57:52，模型调用 16 次，工具执行 28 次；没有执行发送消息工具，但 10 个工具步骤同时返回了非空过程正文，最后另有一次普通回答。`send_message_to_user` 出现在 16 次模型请求的工具定义中；它的说明也明确普通文字可以直接输出。

容器内 `astr_agent_run_util.py` 和 `tool_loop_agent_runner.py` 的 SHA256 与 AstrBot v4.28.0 源码相同。原生 runner 对附带 tool_calls 的模型响应也会生成 llm_result；非流式 run_agent 把它设为 LLM_RESULT，再交给平台发送。这条路径无需插件或发送消息工具参与。Monitor 工具时间线记录的是工具执行，不包含普通模型回复的每次平台发送。

全局配置只读检查时为：

```json
{
  "provider_settings": {
    "streaming_response": false,
    "show_tool_use_status": false,
    "show_tool_call_result": false,
    "buffer_intermediate_messages": false
  },
  "platform_settings": {
    "segmented_reply": {
      "enable": true,
      "only_llm_result": true,
      "split_mode": "regex",
      "regex": ".*?[。？！~…]+|.+$"
    }
  }
}
```

这是检查时的配置，不能据此恢复任务开始时的所有动态设置。但模型记录和原生发送代码已经能解释过程正文的来源。工具状态关闭仅隐藏“调用工具”等状态通知，不会隐藏模型正文；分段回复又可能把一段正文拆成多条群消息。

## 原生能配置什么

AstrBot 的「合并 Agent 中间消息」对应 `provider_settings.buffer_intermediate_messages=true`，适用于非流式模式。它会缓冲过程正文，待任务结束后**合并所有正文**发送，能够减少逐步刷屏，但不会删除过程叙述。

关闭分段回复 `platform_settings.segmented_reply.enable` 只能避免同一正文按标点拆分，仍会发送各步骤正文。关闭工具状态、隐藏发送工具、隐藏推理内容都无法过滤这里的普通正文。

AstrBot v4.28.0 没有“丢弃中间正文、只发送最终回答”的独立原生开关。若需要该行为，应另做明确的输出过滤：保留工具执行与上下文，在有 tool_calls 的步骤阻止其中的普通正文进入发送链，保留最后无 tool_calls 的回答、错误通知与显式发送工具。流式时必须先缓冲到能判断本步是否调用工具，不能等文字发出后再撤回。

此次没有改生产配置，也没有在复杂消息功能中暗加正文过滤。
