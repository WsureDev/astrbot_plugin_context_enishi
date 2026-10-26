# 原生群聊流水

v0.4.3 内置独立流水，可替代 Group Context Flow 0.2.2。消息记录、状态与保存确认均在 Enishi 自己的数据目录中实现。v0.4.4 移除旧 Flow 数据的导入导出，仅参考其功能，不依赖其日志、目录或游标。图片、成员查询与群聊流水的开关互相独立。

## 配置

每个渠道条目的「上下文整理 → 接管群聊流水」默认关闭，对应 context.group_flow_enabled。适用范围沿用该条目的机器人实例、群和 TG 话题设置。

全局 group_flow：

| 配置 | 默认值 | 含义 |
| --- | ---: | --- |
| max_log_records | 5000 | 每个完整框架会话保留的原始消息数，0 不限 |
| max_delta_messages | 0 | 每次注入条数，0 不限；超出时跳过较早消息 |
| max_delta_chars | 16000 | 整个 delta 的字符预算，0 不限，非零最少 512 |
| record_self_messages | false | 是否记录非 LLM 的机器人平台消息 |
| record_empty_messages | true | 是否记录无正文的消息；非文本组件仍可保留 |

外部 Flow 与 AstrBot provider_ltm_settings.group_icl_enable 不能和 Enishi 在同一范围同时注入。检测到冲突时 Enishi 停止自己的接收与注入，输出诊断；不会停用别的插件或修改其配置。内置 group_message_history_enable 是查询存储能力，不等同于 group_icl_enable 注入开关。

## 运行语义

1. 记录群事件，使用原生消息 ID 去重，为每个完整 unified_msg_origin 分配递增 seq。没有原生 ID 时采用内部尽力去重键，模型侧 ID 留空；同人同秒同文且无 ID 的两次事件无法保证精确区分。
2. 当前触发消息为 trigger_seq；按 conversation 分别读取 cursor，只选 cursor < seq < trigger_seq 的记录。
3. 原生消息 ID 已在当前会话 META 中出现时跳过重复；相同昵称、相同正文不作为去重条件。跨实例与话题的 ID 不相互抵消。
4. 根据条数和字符预算裁剪，以 META.skipped_count 和 coverage 明示未包含的历史。推进水位后，被预算跳过的部分不会自动补发。
5. 给当前 user 附加紧凑身份表，正文仍由 AstrBot 提供。每个 request 只增加一个本轮 delta。
6. runner 成功完成后仍不立即提交游标。会话管理器 update_conversation 成功返回，且保存内容包含当前身份表及其后的 assistant 记录，才将 cursor 单调推进到 trigger_seq。
7. 失败、取消、错误、未知保存状态不盲目推进。reset/new 成功后建立新 generation；旧 run 的保存确认不能回写旧边界。清理流水也会建立新代次，不重用 seq。

日志与共享 state.json 由同一锁保护，文件用原子替换写入。文件操作在线程执行；任务取消会等待已经开始的文件事务结束，再释放同一群的写入锁。支持单进程部署，不提供多进程共享文件写入保证。

AstrBot 历史与插件水位不是跨文件事务。进程在历史保存后、水位提交前崩溃时，恢复依靠已保存的原生消息键排除可确认的重复；没有 ID、历史已被压缩等情况下仍可能有限重放，不承诺严格 exactly-once。已压缩进普通回答的旧内容不做语义猜测清理。

LLM 回复近期平台回流按机器人账号和已发出的回复文本过滤；不是对所有用户进行正文去重。record_self_messages=false 时其他机器人自身消息也不记录。开启该选项后，非 LLM 平台消息可进入流水；平台改写或拆分回复而无法匹配时，仍可能有限回流。原生追问的同一发送者限制没有放开。

## 命令

| 命令 | 权限与作用 |
| --- | --- |
| /enishi_flow_status | 当前群的开关、运行状态、冲突、记录数、游标和待保存任务数 |
| /enishi_flow_clear | 管理员；只清当前群的 Enishi 原始流水并建立新边界，保留 AstrBot 会话历史 |

## 开启与使用

1. 停用外部 Flow，并关闭当前范围的 AstrBot 内置群聊注入，避免重复注入。
2. 打开对应渠道的「上下文整理 → 接管群聊流水」，按需限定机器人实例和群／话题。
3. 无需迁移或导入。Enishi 从开启后接收的群事件生成自己的流水；没有 LLM 调用的普通群消息同样记录，不调用模型。
4. 在当前群执行 /enishi_flow_status，可检查开关、运行状态、记录数、当前序号和会话游标。
5. 当前触发消息只进入当前身份表，不进入增量列表。例如先收到 A、B 的两条普通闲聊，再收到 C 的 @ 请求，本轮 delta 包含 A、B；C 的正文仍由框架提供。

刚开启后如果只收到当前触发消息，增量为空是预期行为。当前身份表的 source=enishi_live、injected_count=0 和 coverage 会明确说明仅覆盖开启期间实际收到的消息；不会从旧 Flow 或未收到的历史中补造内容。已有 AstrBot 会话仍按框架保存，不需要为了启用流水执行 /new。

原始流水和 state.json 均位于本插件数据目录的 flow 下。停用旧 Flow 并开启本功能即可；无需替换旧 Flow 的数据目录、复制旧游标或执行导入命令。

需要关闭本功能时，关闭对应渠道开关即可。/enishi_flow_clear 只清当前群的 Enishi 原始流水，并建立新代次；不删除 AstrBot 会话，不触碰其他插件的数据。

已有 P0 user metadata、旧工具 JSON 和压缩摘要不会因代码升级自动消失；新流水仅输出 META，不批量重写旧会话。

## 验证

v0.4.4 在 AstrBot v4.28.0、v4.29.3 和本次核对的最新源码上分别运行完整测试，三组均为 179 passed，无跳过；同时使用复制到 /tmp 的 Monitor 0.4.1 和 Comfy 2.4.2 源码验证包装兼容。

新增用例执行真实 WakingCheckStage、EventMessageTypeFilter 和 ProcessStage：两个用户不 @ 机器人的群消息分别写入 Enishi 自有流水，不调用 LLM；第三个用户触发请求时，前两条消息进入 delta，当前触发不重复。首次仅收到触发消息时，当前身份表明确 source、injected_count=0 和覆盖范围。保留失败重放、原生 ID 去重、按会话游标、reset/new 边界、并发和卸载排空等回归验证。
