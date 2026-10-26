# META 上下文

v0.4.3 用 META 文本替代 P0 的逐消息 JSON。模型消息外层仍由 AstrBot 使用 `role`、`content`、`tool_call_id` 包装；下面的表格直接放在 content 中，不会再展开成 20 个字段的对象。

```text
META platform_meta.id="Haqi8mi" session_id="332993267" group.group_id="332993267" self_id="9000"
FIELDS message_id|timestamp|sender.user_id|sender.nickname|message_str|message
101|1792940266|1001|A|C喜欢蓝发，看这张图。|Image(file="qq://Haqi8mi/332993267/image/example.jpg?m=101&i=0")
102|1792940276|1002|B|C还喜欢猫耳。|Reply(id="101",sender_id="1001");At(qq="1003",name="C")
```

公共范围使用框架的适配器实例、session_id、群信息和 self_id，每批一次。内部始终用原始 unified_msg_origin 隔离群、私聊、机器人实例和 TG 话题，不从表格中的昵称推导路由。

消息列沿用 AstrBotMessage 的字段：

| 列 | 含义 |
| --- | --- |
| message_id | 原生消息 ID；未知留空，绝不填写本地记录 ID或去重哈希 |
| timestamp | Unix 秒；实时事件优先平台时间，其次框架观察时间；旧 Flow 为框架时间；TG 本地历史为记录创建时间 |
| sender.user_id | 本消息作者 ID |
| sender.nickname | 展示名；QQ 优先群名片，不作为人物身份主键 |
| message_str | 正文，只保存一次，不重复输出 Plain 组件 |
| message | Reply、At、Image 等非文本组件，使用其官方字段 |

Reply.sender_id 是引用作者；At.qq 是被提及者，不回填到本消息作者。TG 匿名 sender_chat 不改写为虚拟机器人用户；普通 @用户名 未提供数字 ID 时只输出 At.name。图片 Image.file 保留完整 Enishi 原生 URI，包括 m、i、f、n 等查询参数；不会写入已转述状态或替换图片 ID。

字段内容的反斜杠转义为 `\\`，分隔符为 `\|`，换行、回车、制表符为 `\n`、`\r`、`\t`；尖括号 Unicode 转义，避免正文破坏历史块边界。组件参数使用 JSON 字符串转义后再作为单元格转义。每条记录始终只有一条物理数据行。

单条消息查询和历史查询使用同一表格。分页游标、扫描页数、source、complete、coverage 等查询说明放在 META 头部一次。TG 本地记录 ID 只用于 next_cursor，不作为可调用平台 get_msg 的消息 ID；已丢失的图片定位显示为 Image()。

人员表使用 user_id、nickname 和所请求的角色、活跃时间、观察次数等列；环境表的头部只出现一次，其后分别给当前消息与 MEMBERS 表。原生成员覆盖与历史覆盖分别说明，不将观察人数或次数声明为全量。

开启原生群聊流水后，历史部分放在 `<group_messages_delta>` 块中；当前消息用一个 `<channel_message>` 小表补充 ID 和关系，不重复正文。当前身份随 user 保存，环境摘要仍为临时内容。当前触发消息不进入 delta；同一范围内已保存的原生消息 ID 可用于去重，绝不按同句正文或子串删除不同人的消息。

按之前导出的真实 30 条 QQ 消息离线对比，P0 工具回包为 17,325 个 Unicode 字符，META 为 3,756 个字符，减少 78.3%。这是字符数，不是 tokenizer 实测；未把图片 URI 换成短代号。

完整转述仅在当前任务中提供，转述缓存留在转述层。历史结果保留原 image_id 和调用配对，删除 description，不写入“已转述／已省略”占位。图片来源每次仍重新读取，缓存不能让已经失效的来源重新可用。
