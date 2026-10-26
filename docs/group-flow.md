# 原生群聊流水与 Flow 接管

v0.4.3 内置独立流水，可替代 Group Context Flow 0.2.2。行为参考已核对的上游提交 a6be7054ad73c38579dc7b6c0dbc5efbd606cfcf；记录、状态与保存确认在 Enishi 自己的数据目录中实现。图片、成员查询与群聊流水的开关互相独立。

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

日志与共享 state.json 由同一锁保护，文件用原子替换写入。文件操作在线程执行；任务取消会等待已经开始的文件事务结束，再释放迁移检查。首次接管支持单进程部署，不提供多进程共享文件写入保证。

AstrBot 历史与插件水位不是跨文件事务。进程在历史保存后、水位提交前崩溃时，恢复依靠已保存的原生消息键排除可确认的重复；没有 ID、历史已被压缩等情况下仍可能有限重放，不承诺严格 exactly-once。已压缩进普通回答的旧内容不做语义猜测清理。

LLM 回复近期平台回流按机器人账号和已发出的回复文本过滤；不是对所有用户进行正文去重。record_self_messages=false 时其他机器人自身消息也不记录。开启该选项后，非 LLM 平台消息可进入流水；平台改写或拆分回复而无法匹配时，仍可能有限回流。原生追问的同一发送者限制没有放开。

## 命令

| 命令 | 权限与作用 |
| --- | --- |
| /enishi_flow_status | 当前群的开关、运行状态、冲突、记录数、游标和待保存任务数 |
| /enishi_flow_clear | 管理员；只清当前群的 Enishi 原始流水并建立新边界，保留 AstrBot 会话历史 |
| /enishi_flow_import | 管理员；复制当前群的旧 Flow 日志与游标，要求 Enishi 接收关闭、现有任务结束、外部注入关闭、目标为空 |
| /enishi_flow_export | 管理员；在 Enishi 数据目录生成当前群的外部 Flow 回滚副本及游标补丁，不写入外部插件目录 |

导入中断时会标记 importing，停止消费半完成的数据。可以用同一未变来源重试；管理员明确执行 clear 可放弃导入并开启新边界，seq 仍保持单调。无法确定来源一致时不覆盖目标。

## 建议切换流程

1. 在维护窗口暂停触发新任务，等待当前 run 完成，备份外部 Flow 的整个数据目录、配置和 AstrBot 当前会话。原始群消息仍可能在切换窗口到达，需明确窗口覆盖限制。
2. 停用外部 Flow，并关闭当前范围的内置群聊消息注入。更新 Enishi，保持「接管群聊流水」关闭。Enishi 不再安装外部 Flow 图片包装。
3. 核对原 Flow 的 max_log_records、max_delta_messages、record_self_messages、record_empty_messages，在 Enishi 设置相同或明确选择的新值。新增加的字符预算也会影响覆盖，不自动修改外部配置。
4. 如果需要接续未读流水，在每个群/话题执行 /enishi_flow_import。默认来源是 data/plugin_data/astrbot_plugin_group_context_flow，只复制该范围的日志和 conversation 游标；保留 seq、旧 flow_id 和原记录供回滚，不移动或删除来源。
5. 不需要回灌旧待注入历史时可跳过导入，启用后执行正常 /new 或 /reset 建立新边界。这是主动跳过旧历史，不是无损迁移。
6. 打开对应渠道范围的「接管群聊流水」，检查 /enishi_flow_status 无冲突，再恢复触发任务。先观察一次真实请求，确认只有一份 delta、当前触发消息未重复、图片 ID 可按需读取。

旧 Flow 的 message_id 可能是原生 ID，也可能是自行生成的后备字符串。未明确记录来源的旧值保存在内部原记录中，不凭字符串长相冒充原生查询 ID；模型侧该列留空。旧文本中已保存的完整图片 URI 保留；旧引用、提及和缺失图片不会从昵称或摘要猜补。新流水与 Enishi 自身导出的记录保留可确认的原生 ID 和结构化关系。

TG 以实际框架会话键隔离话题。检测到旧 cursor_meta 指向不同的完整范围时拒绝导入，改用新边界。群名称、群成员覆盖与历史覆盖不是同一件事。

P0 已保存的 user metadata、旧工具 JSON 和旧会话回答不会因代码升级自动从 AstrBot 数据库消失。新输出采用 META；若不需要继续旧会话，切换时用 /new 开始紧凑的新历史。需要保留旧会话时继续使用框架正常压缩，不批量重写其他插件的历史。

## 回滚

1. 暂停新任务并等待当前 run 完成；关闭该范围的 Enishi 接收。已完成的任务仍可排空保存确认，pending 应归零。
2. 执行 /enishi_flow_export。输出目录位于本插件数据目录的 flow_exports 下，只包含当前范围的日志、state.json 和 cursor_patch.json。
3. 备份外部 Flow 数据后，将导出的当前群日志放入外部 Flow 副本，并按 cursor_patch.json 合并该群的 cursors 与 cursor_meta，保留其他群的键。提供的 state.json 会保留导出时能读取到的外部其他群状态；批量导出时仍应逐群合并补丁，避免旧快照覆盖其他群的更新。确认后再用副本替换外部数据。
4. 校验记录最大 seq、会话游标和群范围后启用外部 Flow；Enishi 的接收保持关闭。

导出保留 seq、原生图片 URI 和原记录；额外的 enishi_record 供重新导入时恢复完整关系，外部 Flow 可忽略。清空后的流水会导出一个已消费的边界锚点，以维持外部 Flow 根据日志分配 seq 的行为。

没有导出切换期间的新数据时，不能仅恢复旧快照后声称无损。可执行 new/reset 建立回滚边界，同时保留 Enishi 原始文件供补查。上述部署和文件替换都需要在实际部署环境另行执行，本次代码改造没有修改生产容器或配置。

## 验证记录

使用 AstrBot v4.28.0（a412146）、v4.29.3（f61a7f4）及本次核对的最新源码（832fc6d），分别运行完整测试；同时指定复制到 /tmp 的 LLM Monitor 0.4.1 和 Comfy 插件 2.4.2 源码，三组均为 **185 passed**，无跳过。Ruff 检查、格式检查和 git diff --check 通过。

验证覆盖 META 转义与消息身份关系、真实消息序列化、当前消息排除、原生 ID 去重、同句不同作者、预算覆盖、保存失败、取消与重复取消、两个群并发提交、reset/new 代次与会话游标、日志裁剪后 seq 单调、卸载排空与重载、导入中断恢复、导入导出往返和其他群游标保留。旧监控与后台图片交付的兼容测试也通过。测试使用临时文件与模拟渠道调用，真实生产接管仍需按上面的流程观察一次实际请求。
