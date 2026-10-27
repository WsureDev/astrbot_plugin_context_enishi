import {
  state,
  groups,
  entries,
  current,
  edit,
  groupName,
  modeNames,
  settingNames
} from "./model.js"
import { byId, node, button, badge, segmented, toast } from "./components.js"

export function openSettings() {
  const fields = {
    enabled: "enabled",
    new_group_mode: "new-group-mode",
    idle_rounds: "idle-rounds",
    fold_skills: "fold-skills",
    archive_results: "archive-results",
    mcp_poll_seconds: "mcp-poll-seconds"
  }
  for (const [key, id] of Object.entries(fields)) {
    const input = byId(id)
    if (input.type === "checkbox") {
      input.checked = current("setting", key, "value")
    } else {
      input.value = current("setting", key, "value")
    }
  }
  byId("settings-dialog").showModal()
}

export function applySettings() {
  edit("setting", "enabled", "value", byId("enabled").checked)
  edit("setting", "new_group_mode", "value", byId("new-group-mode").value)
  edit("setting", "idle_rounds", "value", Number(byId("idle-rounds").value))
  edit("setting", "fold_skills", "value", byId("fold-skills").checked)
  edit("setting", "archive_results", "value", byId("archive-results").checked)
  edit("setting", "mcp_poll_seconds", "value", Number(byId("mcp-poll-seconds").value))
  byId("settings-dialog").close()
  toast("设置已加入草稿，点击保存修改后生效")
}

function label(text, container) {
  container.append(node("span", text, "detail-label"))
}
function copyLine(text) {
  const line = node("div", undefined, "id-line")
  line.append(
    node("code", text),
    button(
      "复制",
      async () => {
        try {
          await navigator.clipboard.writeText(text)
          toast("已复制")
        } catch {
          toast("复制失败，请手动选择文字复制")
        }
      },
      "subtle"
    )
  )
  return line
}

export function openDetail(id, actions) {
  const entry = entries().find((item) => {
    return item.id === id
  })
  if (!entry) {
    toast("该能力已不在目录中")
    return
  }
  const body = byId("detail-content")
  body.replaceChildren(node("h2", entry.name), node("p", groupName(entry.group), "page-subtitle"))
  label("配置策略", body)
  const policy = node("div")
  const effective = node("p", undefined, "page-subtitle")
  function updatePolicy() {
    const latest = entries().find((item) => {
      return item.id === id
    })
    policy.replaceChildren(
      segmented(latest.override, true, latest.name + " 的详情策略", (mode) => {
        edit("tool", id, "mode", mode)
        updatePolicy()
        actions.render()
      })
    )
    let inheritance = "单独设置"
    if (latest.override === "inherit") {
      inheritance = "跟随组"
    }
    effective.textContent = inheritance + " → " + modeNames[latest.mode] + "；配置用于新请求。"
  }
  updatePolicy()
  body.append(policy, effective)
  label("完整说明", body)
  body.append(node("p", entry.description || "暂无说明", "full-description"))
  label("完整 ID", body)
  body.append(copyLine(entry.id))
  label("来源", body)
  body.append(copyLine(entry.group.id))
  const detail = node("details", undefined, "advanced")
  const summary = node("summary", "参数定义 / Skill 路径")
  const content = node("pre", "正在读取…")
  detail.append(summary, content)
  let loaded = false
  detail.addEventListener("toggle", async () => {
    if (!detail.open || loaded) {
      return
    }
    loaded = true
    try {
      const result = await actions.post("detail", { id })
      if (result.path) {
        content.textContent = result.path
      } else {
        content.textContent = JSON.stringify(result.parameters, null, 2)
      }
    } catch (cause) {
      content.textContent = "读取失败：" + cause.message + "。收起后可重试。"
      loaded = false
    }
  })
  body.append(detail)
  byId("detail-dialog").showModal()
}

export function openGroup(id, render) {
  const group = groups().find((item) => {
    return item.id === id
  })
  if (!group) {
    return
  }
  const body = byId("detail-content")
  body.replaceChildren(node("h2", "编辑组"), node("p", groupName(group), "page-subtitle"))
  const form = node("form")
  const nameLabel = node("label", "组显示名", "detail-label")
  const name = node("input")
  name.type = "text"
  name.value = group.name || ""
  nameLabel.append(name)
  const idleLabel = node("label", "闲置轮数（留空跟随默认）", "detail-label")
  const idle = node("input")
  idle.type = "number"
  idle.min = "1"
  idle.value = group.idle_rounds ?? ""
  idleLabel.append(idle)
  let mode = group.mode
  const modes = node("div")
  function renderModes() {
    modes.replaceChildren(
      segmented(mode, false, "组的默认策略", (next) => {
        mode = next
        renderModes()
      })
    )
  }
  renderModes()
  form.append(nameLabel)
  label("默认策略", form)
  form.append(modes, idleLabel)
  const exceptions = group.entries.filter((entry) => {
    return current("tool", entry.id, "mode") !== "inherit"
  }).length
  form.append(node("p", exceptions + " 项单独覆盖将保留。新增工具自动跟随本组。", "page-subtitle"))
  const footer = node("div", undefined, "dialog-footer")
  const submit = node("button", "加入修改", "button primary")
  submit.type = "submit"
  footer.append(node("span", "统一保存后生效。"), submit)
  form.append(footer)
  form.addEventListener("submit", (event) => {
    event.preventDefault()
    let rounds = null
    if (idle.value) {
      rounds = Number(idle.value)
    }
    edit("group", id, "name", name.value.trim() || id.slice(id.indexOf(":") + 1))
    edit("group", id, "mode", mode)
    edit("group", id, "idle_rounds", rounds)
    byId("detail-dialog").close()
    render()
  })
  body.append(form)
  label("来源 ID", body)
  body.append(copyLine(group.id))
  byId("detail-dialog").showModal()
}

function readable(value) {
  if (value === undefined) {
    return "已移除"
  }
  if (value === null || value === "") {
    return "跟随默认"
  }
  if (typeof value === "boolean") {
    if (value) {
      return "开启"
    }
    return "关闭"
  }
  return modeNames[value] || String(value)
}

export function renderReview() {
  const container = byId("review-content")
  container.replaceChildren()
  for (const draft of state.drafts.values()) {
    const row = node("div", undefined, "change-row")
    let title = settingNames[draft.id] || draft.id
    if (draft.type === "group") {
      const group = groups().find((item) => {
        return item.id === draft.id
      })
      if (group) {
        title = groupName(group)
      }
      title += " · " + { mode: "整组策略", name: "显示名", idle_rounds: "闲置轮数" }[draft.field]
    } else if (draft.type === "tool") {
      const entry = entries().find((item) => {
        return item.id === draft.id
      })
      if (entry) {
        title = groupName(entry.group) + " / " + entry.name
      }
    }
    row.append(
      node("strong", title),
      node("p", readable(draft.before) + " → " + readable(draft.value))
    )
    if (draft.conflict) {
      row.append(node("small", "与远端配置冲突，请先处理"))
    }
    container.append(row)
  }
}

let chosenSession = ""
export function renderRuntime() {
  const container = byId("runtime-view")
  const snapshot = state.snapshot
  const title = node("div", undefined, "page-heading")
  const heading = node("div")
  let name = "会话状态"
  let subtitle = "查看当前实际展开的能力，以及各会话的闲置收纳记录。"
  if (state.view === "diagnostics") {
    name = "诊断"
    subtitle = "目录同步、监听与运行错误。"
  }
  heading.append(
    node("p", "RUNTIME OBSERVABILITY", "eyebrow"),
    node("h1", name),
    node("p", subtitle, "page-subtitle")
  )
  title.append(heading)
  const card = node("section", undefined, "runtime-card")
  container.replaceChildren(title, card)
  if (state.view === "diagnostics") {
    for (const [name, value] of [
      ["目录监听", snapshot.watchers_intact],
      ["MCP 变更通知", snapshot.mcp_notifications]
    ]) {
      const line = node("div", undefined, "diagnostic-line")
      let text = "未接入 / 不支持"
      if (value) {
        text = "正常"
      }
      line.append(node("span", name), node("span", text, "badge"))
      card.append(line)
    }
    if (snapshot.conflict) {
      card.append(node("p", snapshot.conflict, "notice warning"))
    }
    if (!snapshot.errors.length) {
      card.append(node("p", "目前没有记录到运行错误。", "page-subtitle"))
    }
    for (const message of snapshot.errors) {
      card.append(node("pre", message))
    }
    return
  }
  if (!snapshot.sessions.length) {
    card.append(
      node("p", "还没有受管理的会话。启用管理后，在聊天中触发一次模型请求即可。", "page-subtitle")
    )
    return
  }
  const selection = node("select")
  selection.setAttribute("aria-label", "选择会话")
  for (const session of snapshot.sessions) {
    selection.append(
      new Option(session.id.split("|")[0] + " · " + session.completed + " 轮", session.id)
    )
  }
  if (
    !snapshot.sessions.some((session) => {
      return session.id === chosenSession
    })
  ) {
    chosenSession = snapshot.sessions[0].id
  }
  selection.value = chosenSession
  selection.addEventListener("change", () => {
    chosenSession = selection.value
    renderRuntime()
  })
  card.append(selection)
  const session = snapshot.sessions.find((item) => {
    return item.id === chosenSession
  })
  const metrics = node("div", undefined, "metric-grid")
  for (const [name, value] of [
    ["已完成轮数", session.completed],
    ["自动收起", session.counters.auto_unloads],
    ["两轮内重新加载", session.counters.quick_reloads]
  ]) {
    const metric = node("div", undefined, "metric")
    metric.append(node("strong", value), node("span", name))
    metrics.append(metric)
  }
  card.append(metrics)
  const runs = (snapshot.runs || []).filter((run) => {
    return run.session === chosenSession && !run.completed
  })
  if (!runs.length) {
    card.append(
      node("h3", "当前没有正在执行的受管理请求"),
      node(
        "p",
        "历史租约不代表下一次请求一定加载；具体候选集还取决于会话配置与权限。",
        "page-subtitle"
      )
    )
  }
  for (const run of runs) {
    card.append(
      node("h3", "当前请求 · " + run.tools + " 个工具"),
      node(
        "p",
        "Schema 估算 " + run.estimated_schema_tokens + " tokens（非实际账单）",
        "page-subtitle"
      )
    )
    const list = node("ul")
    for (const id of run.visible_ids || []) {
      const entry = entries().find((item) => {
        return item.id === id
      })
      let name = id
      if (entry) {
        name = entry.name
      }
      list.append(node("li", name))
    }
    card.append(list)
  }
  label("完整会话 ID", card)
  card.append(copyLine(session.id))
}
