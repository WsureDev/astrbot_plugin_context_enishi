# 非文本消息与原文读取

默认上下文保存类型与定位，原文按需读取。图片仍使用原来的 `Image.file` 与读图／取图工具；普通文字、文字里的 URL、Reply 和 At 的身份关系保持原样。

## NapCat 的真实消息形态

核对源码提交 [ba0a53d](https://github.com/NapNeko/NapCatQQ/tree/ba0a53dd372dbc4c4bfc83cf7132997459605210)：

- [`api/msg.ts`](https://github.com/NapNeko/NapCatQQ/blob/ba0a53dd372dbc4c4bfc83cf7132997459605210/packages/napcat-onebot/api/msg.ts) 把接收的 `arkElement.bytesData` 上报为 `type=json`、`data.data`。小程序和普通分享卡片都可能通过这个外壳出现，不能仅按消息段名识别。
- [`types/message.ts`](https://github.com/NapNeko/NapCatQQ/blob/ba0a53dd372dbc4c4bfc83cf7132997459605210/packages/napcat-onebot/types/message.ts) 允许 JSON 原文是字符串或对象；XML 原文在 `data.data`，Markdown 在 `data.content`。类型表还包含 miniapp、music、contact、location、onlinefile、flashtransfer 等。
- 多消息转发上报为 `forward`，`data.id` 是原生转发定位；未展开时不含全文，需要 `get_forward_msg`。`get_msg` 返回原始消息段和所在群／用户，供定位与范围核对。

因此不能把卡片直接删成 `Json()`，也不能把所有 `com.tencent.structmsg` 都当成网页链接：这类外壳还可用于其他结构化卡片。原文读取后，`features.kind` 将 `com.tencent.miniapp*` 标为 miniapp、`com.tencent.multimsg` 标为 forward_card、structmsg 标为 structured_card，未知 app 保留为 json_card。原始 data 始终完整保留，分类不是事实摘要。

## 上下文格式

```text
META platform_meta.id="Haqi8mi" session_id="332993267" read_with="get_channel_message_content"
FIELDS message_id|timestamp|sender.user_id|sender.nickname|message_str|message
1862917194|1793246110|844157922|用户||Json(id="qq://Haqi8mi/332993267/json/1862917194?i=0")
```

当前消息身份表、群聊流水、`get_channel_message`、`get_channel_user_history` 共用表示。当前消息即使关闭图片管理与群聊流水，仍可附带紧凑引用；受当前渠道聊天感知开关与插件启用范围约束。与框架转换出的同一个组件合并，不重复记录。

定位由机器人实例、会话、原生组件类型、原生消息 ID 和**同类型的零起始序号**组成。同一消息的两张 JSON 卡片是 `i=0` 和 `i=1`，中间插入文字不会改变卡片序号。它不是本地随机 ID、内容哈希或网页地址；没有原生消息定位的通知和旧记录不会伪造可读取 ID。

## 读取完整原文

```json
{"content_id":"qq://Haqi8mi/332993267/json/1862917194?i=0"}
```

调用 `get_channel_message_content`，例如返回：

```json
{
  "ok": true,
  "source": "napcat",
  "id": "qq://Haqi8mi/332993267/json/1862917194?i=0",
  "message_id": "1862917194",
  "type": "json",
  "index": 0,
  "data": {
    "data": "{\"app\":\"com.tencent.miniapp_01\",\"meta\":{\"detail_1\":{\"qqdocurl\":\"https://b23.tv/example\"}}}"
  },
  "features": {
    "app": "com.tencent.miniapp_01",
    "view": "",
    "kind": "miniapp",
    "links": [{"field":"meta.detail_1.qqdocurl","url":"https://b23.tv/example"}]
  }
}
```

只返回该组件的原生 data，不重复展开为另一份完整 decoded JSON。链接保留字段位置与原始值，包括嵌套的 qqdocurl、jumpUrl、url、preview 等字段；不访问链接、解短链或补全猜测地址。XML／Markdown 的原文同样保留，解析失败只增加 decode_error，不丢弃原文。

每次重新调用 `get_msg`，核对消息 ID、机器人／会话与组件类型及序号。来源已撤回、过期或无权限时返回失败，不用旧正文冒充；不建立接收时全文索引。合并转发先验证父消息，再按该消息中的原生转发 ID 调用 `get_forward_msg`。文件／音视频仅返回协议元数据，不下载二进制文件。

读取结果受已有工具结果生命周期与历史字数预算管理，本工具不另加字符截断。旧流水里的无 ID 类型标记不会自动联网补齐；可按那一行的原生 message_id 调用 `get_channel_message` 获得新定位。TG 当前不提供这一 QQ 原文读取入口。
