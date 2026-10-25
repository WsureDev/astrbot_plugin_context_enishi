# 消息字段对齐：P0

本次实现规格的首批交付（P0：完善 Enishi 自身）。参考仓库只读克隆至 `/tmp/context-enishi-alignment-Abya59/astrbot_plugin_group_context_flow`，核对提交为 `a6be7054ad73c38579dc7b6c0dbc5efbd606cfcf`（0.2.2），与规格一致。Enishi 改动前为 `a63f0cc`（0.4.0）。验证用 AstrBot v4.29.3 标签实际指向 `f61a7f4`，不同于规格中列出的 `de1d4ef`，以实际检出的字段和序列化实现为准。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `channels/message_schema.py` | QQ、TG 与当前事件的标准消息投影、会话范围和时间来源；不访问网络 |
| `channels/relations.py` | 原生 Reply、At、AtAll、QQ 消息段、TG entities 的关系提取 |
| `context/additions.py` | 持久当前身份块与临时环境摘要的独立写入，重复请求替换身份块 |
| `context/service.py`、`main.py` | 按当前渠道开关写入 metadata，与图片模式独立 |
| `awareness/sources.py` | 消息、历史与成员查询补充标准字段；单条 QQ 引用作者的有限补查 |
| `awareness/service.py`、`participants.py` | 环境结果标准别名、人员活跃时间来源；保留覆盖信息 |
| `settings.py`、`_conf_schema.json` | 每个渠道的 `context.message_metadata_enabled`，默认 false |

公共投影位于 `channels/`，保持 context 与 awareness 两个功能域互不导入的既有架构约束。

## 实际映射

| 现有字段或来源 | 标准字段 |
| --- | --- |
| `event.get_platform_id()` | `platform_id`，实例 ID |
| `event.get_platform_name()` | `platform_name`，保留 aiocqhttp 等框架值 |
| `event.unified_msg_origin` | `scope_key`，不重新拼接，保留 TG 话题隔离 |
| 原生群号、TG chat.id | `group_id`；TG 框架话题后缀保留在 scope_key 与旧 chat_id |
| `message_obj.self_id`、旧 `bot_id` | `self_id`；保留 bot_id |
| QQ message_id、TG message_id | 字符串 `message_id`；通知生成的框架后备 ID 不作为原生 ID |
| QQ sender.user_id / card / nickname | `sender_id` / `sender_name`，优先群名片 |
| TG from.id / 姓名 | `sender_id` / `sender_name`；匿名 sender_chat 的用户身份为空，actor_type=chat |
| current_speaker.user_id / display_name | 新增 `sender_id` / `sender_name`，保留旧键 |
| Reply.id / sender_id / sender_nickname | `reply_to.message_id` / `sender_id` / `sender_name` |
| 环境 reply_to.user_id / display_name | 新增 `sender_id` / `sender_name`，保留旧键 |
| QQ at.qq、At、AtAll | `mentions` 中的字符串 user_id、display_name、is_all |
| TG text_mention、mention | 已知实体用户 ID，或仅展示 @用户名且 user_id=null；按 UTF-16 偏移取文本 |
| QQ time、TG date | `timestamp`，`time_basis=platform_message` |
| 框架 timestamp | `received_at`；无平台发送时间时后备为 timestamp，time_basis=framework_received |
| TG 本地 created_at、id | `timestamp` / `local_record_id`，time_basis=local_record_created，message_id=null |
| 原生图片定位 | 完整 `image_ids` URI，不存本地临时路径、不裁剪查询参数 |
| QQ member_info.card / nickname、TG name | 新增 display_name，user_id 字符串化，保留原字段 |
| 旧 chat.platform / chat_id / name | 保留；增加实例、框架平台、scope_key、group_id、group_name、self_id |

投影新增 `schema_version=1`、`kind=current/query`、`actor_type`、`reply_to`、`mentions` 和 `relations_complete`。当前身份块省略正文；查询正文保留原换行和 8000 字符上限。名称只作展示，不用于关系推断。

未知原生 ID、作者或时间为 null，未知时间来源为 unknown。未保存原始关系的 TG 本地历史标记 relations_complete=false，空数组不表示原消息无图片或提及；每条附 coverage 说明。原生关系已读到但引用作者未知时可保持 relations_complete=true，引用对象保留 message_id 和 null 作者。批量历史不额外逐条查引用；当前 metadata 仅读取本地原生结构，不补网络请求。单条 QQ 查询和按需环境查询最多补查一次引用，失败或范围不匹配时保留部分结果。

人员活动保留旧 activity.time_basis，并为每人增加 last_spoke_at_basis；current_speaker 也区分消息时间与观察时间。所有原有分页、扫描限额、source、coverage 和 membership_complete 含义保留。

## 配置与回滚

新增开关位于每个渠道的「上下文整理」内，默认关闭，独立于图片模式和聊天感知工具。启用后将一个带固定标签的身份块追加到 extra_user_content_parts，不标为临时；成员环境摘要继续标为临时。昵称中的标签分隔符经过 JSON Unicode 转义。系统身份解释规则每个请求只追加一次。

关闭 message_metadata_enabled 即停止新请求注入，已经保存的 user 身份块保留。查询新增字段始终提供；需要恢复旧返回对象时回退本次代码。无数据库或 Flow 日志迁移，无新群流水或第二个历史注入者。现有 Flow 图片兼容包装、生图工具与临时文件生命周期保持原有实现。

P1 的历史渲染、正文去重替换、旧日志适配，以及 P2 的独立流水和迁移均未实施。测试使用合成 QQ/TG 载荷和真实 AstrBot 序列化方法，不代表真实平台端到端部署验证。

## 验证结果

在专用 `/tmp` 虚拟环境中，分别设置 ASTRBOT_SOURCE 指向 AstrBot v4.28.0（a412146）与 v4.29.3（f61a7f4），运行完整 `pytest -q`，两者均为 **144 passed, 6 skipped**。Ruff 检查、格式检查和 `git diff --check` 通过。

新增验证覆盖作者与引用及提及身份分离、同名与改名、原生组件枚举、AtAll、未知作者与消息 ID、QQ 接收时间后备、TG UTF-16 提及、匿名引用、完整话题范围、引用查询失败与范围不匹配、历史扫描预算、成员旧键、metadata 重复替换及分隔符转义。真实 AstrBot 历史序列化确认身份块保留且临时环境摘要排除。

6 项跳过为旧监控 0.4.1（2 项）及旧 Comfy 2.4.2（4 项）的源码兼容检查，需要分别设置 MONITOR_COMPAT_SOURCE 与 COMFY_COMPAT_SOURCE；本次未提供这些独立旧版本源码。图片与后台文件的其他既有测试通过，生产容器未修改。
