# 渠道图片压缩与查询工具

AstrBot 插件：使用 QQ/NapCat 和 Telegram 原生图片标识，减少主模型上下文中的图片，提供按需转述、图片文件、历史发言、头像及成员信息工具。基于 AstrBot 4.28.0 开发。

## 三种模式

| 模式 | 主模型输入 | 持久化对话历史 |
| --- | --- | --- |
| off／关 | 保持框架原有行为 | 保持框架原有行为 |
| A | 图片原生 ID；需要查看时调用转述工具，返回文字 | ID、转述文字和工具记录，不保存图片像素 |
| B | 仅本次消息和其引用范围内的图片以框架原有 Base64 进入当轮上下文 | 保存 ID 和文字，移除图片像素 |

B 的引用合并转发范围沿用框架的引用解析深度、远程获取次数限制。其他工具取得的历史图片不会因此自动进入 B 的视觉上下文；需要了解内容时调用转述工具。

A 关闭框架在主模型不支持视觉时提前执行的自动转述，仅修改本次请求配置的副本；图片阅读统一交给按需转述工具及缓存。B 和关闭模式保留原有框架转述策略。

不建立接收时图片索引。标识直接包含渠道原生 `file_id`／`image.file`，或原生消息 ID＋图片段位置、转发 ID＋节点位置。新插件进程、清空文件缓存、此前未收到原消息，都不影响解析这些标识；平台已删除、过期或不可访问的资源返回明确错误。QQ 消息 ID 映射和文件回取仍受 NapCat 自身缓存与 QQ 历史可达范围限制，不能承诺永久可用。

TG 被引用消息的原始载荷包含文件 ID 时可以直接回取，无需曾接收原消息；载荷未提供媒体、仅给出消息 ID 时，Bot API 无法按任意消息 ID 查询。不会伪造来源标识或在空载荷上解引用。

## 配置

默认全部关闭。先配置一个支持看图的独立转述模型，再设置模式。例如 `channels` 填写：

```json
{
  "qq": {"mode": "B", "caption_provider_id": "你的视觉转述provider-ID"},
  "telegram": {"mode": "A", "caption_provider_id": "你的视觉转述provider-ID"},
  "platform:Haqi8mi": {"mode": "A"},
  "session:telegram:GroupMessage:-100123456#2": {"mode": "off"}
}
```

匹配顺序：全局默认 → 平台类型 → 平台实例 → 完整会话 UMO。群与 TG 话题可分别覆盖。`/渠道图片模式` 显示当前匹配结果。provider ID 是 AstrBot 的提供商条目 ID，不是模型名称。

## LLM 工具

| 工具 | 用途 |
| --- | --- |
| `describe_channel_image(image_id, question)` | 使用独立模型转述图片，主模型只接收文字；按渠道、图片内容、模型、问题和提示词缓存 |
| `fetch_channel_image(image_id)` | 获取 AstrBot 本地图片文件路径、尺寸、MIME 和字节数；不返回 ImageContent／Base64 |
| `get_channel_user_history(user_id, limit, days, before_id)` | QQ 从 NapCat 分页回溯；TG 查询已有本地群历史，返回数据来源和覆盖边界 |
| `get_channel_user_avatar(user_id)` | 获取头像的渠道原生图片 ID，再交给转述或文件工具 |
| `get_channel_member_info(user_id)` | 查询当前群成员昵称、名片及角色 |
| `get_channel_message(message_id)` | QQ 从 NapCat 按原生消息 ID 查询；TG 读取当前载荷中的当前／引用消息，任意旧消息 ID 明确返回能力限制 |

例子：

```text
用户引用一张 AstrBot 从未收到过的 TG 图片：帮我看这个错误。
主模型：当前问题＋[图片 id=telegram://.../原生file_id?...]
describe_channel_image(image_id, question="解释截图中的错误")
→ 主模型收到转述文字；第二次相同问题直接命中缓存。

用户：用它作为参考图画一张。
fetch_channel_image(image_id)
→ {path: /AstrBot/data/temp/astrbot_plugin_channel_media_tools/....png, ...}
generate_image(reference_images=[path], use_context_images=false, prompt=...)
```

文件缓存在 AstrBot temp 目录，兼容已安装通用生图插件的受限本地参考图入口。独立 MCP／其他容器不能直接读取 AstrBot 容器内路径，需要消费方自己的上传或文件桥接机制。

开启 A/B 时，通用生图工具的 `use_context_images=true` 会被拒绝并提示显式获取参考图，防止其旧缓存覆盖新图。显式传入本插件的原生图片 URI 也会转换为本地路径。

## 上下文与框架集成

使用可卸载的运行时包装覆盖每一步模型调用前的图片策略及最终保存；不修改 AstrBot 源码文件。包括外部工具返回的图片，A 会转述为文字，B 会移出视觉上下文。仅对开启模式且启用本插件的 QQ/TG 会话生效。

框架创建的定时／后台完成事件继承其平台实例的档位；A 同样禁止将后台图片像素交给主模型，B 不把没有当前／引用来源的后台工具图片视作当前图片。

TG 适配器转换消息时仅在现有图片组件上保留原生文件 ID，避免相册合并丢失早期 Update。它不是接收索引，也不影响未接收原消息的引用回取。

安装 `astrbot_plugin_group_context_flow` 时，保留来源 ID 到它原有的文本记录中；QQ 已有 `[图片]` 记录可通过原生消息 ID 构造回取定位。没有另建图片登记表。已有 TG 历史若只剩 `[图片]` 或过期下载 URL，无法还原原生 `file_id`；已有 Base64 对话若丢失来源 ID，会去掉像素并明确标记来源无法恢复。

TG 历史工具只读取 AstrBot 现有本地记录，明确返回 `source=astrbot_local`、`complete=false`；不创建额外接收记录。完整 TG 群历史需要另行接入获得授权的 MTProto 客户端，本插件不会创建用户登录会话。

## 安装与验证

上传 `dist/astrbot_plugin_channel_media_tools-v0.1.0.zip` 到 AstrBot 插件管理。安装后默认关闭，通过配置分别启用 QQ/TG 档位。本项目未自动安装到生产容器。

宿主机测试：

```bash
pytest
ruff check .
```

测试覆盖未见过的引用图片、嵌套转发、模式隔离、多轮工具图片、图片转述缓存、并发读取、文件缓存失效、渠道隔离及历史覆盖边界。设置 `ASTRBOT_SOURCE=/path/to/AstrBot` 后，还执行该源码中的实际 runner 方法及消息保存方法，不导入 AstrBot 应用初始化代码。

v0.1.0 验证结果：57 项测试通过，Ruff 检查通过。生产容器只读内存探针使用真实 Telegram SDK 与消息模型：A 模型输入／保存历史为 0／0 张图片，B 为 2／0，关闭为 3／3；六个工具的文档参数经过真实 SDK 注册解析。TG 的 `getMe`、头像查询与 `getFile` 元信息请求实测通过，没有下载图片或调用收费转述模型。QQ 回取和分页使用协议响应测试及对应 NapCat 源码核对，尚未在生产 QQ 群执行端到端读图。
