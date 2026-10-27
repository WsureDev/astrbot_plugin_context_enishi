"use strict"

const bridge = window.AstrBotPluginPage
let snapshot = null
let generation = 0
let dirtySettings = false
const groupDrafts = new Map()
const byId = (id) => {
  return document.getElementById(id)
}

function node(tag, text, className) {
  const element = document.createElement(tag)
  if (text !== undefined) {
    element.textContent = text
  }
  if (className) {
    element.className = className
  }
  return element
}

function select(value, inherited) {
  const element = node("select")
  const choices = [
    ["folded", "收纳"],
    ["primary", "一级常驻"]
  ]
  if (inherited) {
    choices.unshift(["inherit", "跟随组"])
  }
  for (const [key, label] of choices) {
    const option = node("option", label)
    option.value = key
    element.append(option)
  }
  element.value = value
  return element
}

function error(message) {
  byId("error").textContent = message
  byId("error").hidden = !message
}

async function post(endpoint, body) {
  const result = await bridge.apiPost("capabilities/" + endpoint, body)
  if (result.error) {
    throw new Error(result.error)
  }
  if (endpoint === "settings") {
    dirtySettings = false
  }
  if (endpoint === "group") {
    groupDrafts.delete(body.group)
  }
  await refresh()
}

function renderGroups() {
  const container = byId("groups")
  const expanded = new Set(
    [...container.querySelectorAll("details[open]")].map((item) => {
      return item.dataset.id
    })
  )
  container.replaceChildren()
  const query = byId("search").value.toLocaleLowerCase()
  let shown = 0
  for (const group of snapshot.groups) {
    const entries = group.entries.filter((entry) => {
      return (entry.name + " " + entry.description + " " + group.id + " " + (group.name ?? ""))
        .toLocaleLowerCase()
        .includes(query)
    })
    if (
      query &&
      entries.length === 0 &&
      !(group.id + " " + (group.name ?? "")).toLocaleLowerCase().includes(query)
    ) {
      continue
    }
    shown += 1
    const card = node("details", undefined, "group")
    card.dataset.id = group.id
    card.open = expanded.has(group.id)
    const summary = node("summary")
    summary.append(
      node("strong", group.name ?? group.id),
      node("span", group.entries.length + " 项", "badge")
    )
    if (!group.available) {
      summary.append(node("span", "来源暂不可用，策略保留", "badge"))
    }
    card.append(summary)
    card.append(node("p", group.id, "source"))
    const controls = node("div", undefined, "controls")
    const draft = groupDrafts.get(group.id)
    const nameLabel = node("label", "组显示名")
    const name = node("input")
    name.value = draft?.name ?? group.name ?? group.id
    nameLabel.append(name)
    const modeLabel = node("label", "整组策略")
    const mode = select(draft?.mode ?? group.mode, false)
    modeLabel.append(mode)
    const idleLabel = node("label", "闲置轮数，留空跟随默认")
    const idle = node("input")
    idle.type = "number"
    idle.min = "1"
    idle.value = draft?.idle ?? group.idle_rounds ?? ""
    idleLabel.append(idle)
    const save = node("button", "保存组策略")
    save.type = "button"
    save.addEventListener("click", async () => {
      save.disabled = true
      try {
        let rounds = null
        if (idle.value) {
          rounds = Number(idle.value)
        }
        await post("group", {
          group: group.id,
          mode: mode.value,
          idle_rounds: rounds,
          name: name.value
        })
      } catch (cause) {
        error(cause.message)
        save.disabled = false
      }
    })
    controls.append(nameLabel, modeLabel, idleLabel, save)
    function rememberDraft() {
      groupDrafts.set(group.id, { name: name.value, mode: mode.value, idle: idle.value })
    }
    controls.addEventListener("input", rememberDraft)
    controls.addEventListener("change", rememberDraft)
    card.append(controls)
    for (const entry of entries) {
      const row = node("div", undefined, "entry")
      const info = node("div", undefined, "entry-info")
      info.append(
        node("strong", entry.name),
        node("span", entry.kind, "badge"),
        node("p", entry.id, "source")
      )
      const description = node("details")
      description.append(node("summary", "完整说明"), node("p", entry.description, "description"))
      info.append(description)
      const override = select(entry.override, true)
      override.setAttribute("aria-label", entry.name + " 的策略")
      override.addEventListener("change", async () => {
        override.disabled = true
        try {
          await post("tool", { id: entry.id, mode: override.value })
        } catch (cause) {
          error(cause.message)
          override.disabled = false
        }
      })
      row.append(info, override)
      card.append(row)
    }
    container.append(card)
  }
  byId("count").textContent = shown + " / " + snapshot.groups.length + " 组"
}

function renderSessions() {
  const container = byId("sessions")
  container.replaceChildren()
  for (const session of snapshot.sessions) {
    const row = node("div", undefined, "session")
    row.append(node("strong", session.id))
    const counters = session.counters
    row.append(
      node(
        "p",
        "已完成 " +
          session.completed +
          " 轮 · 自动收起 " +
          counters.auto_unloads +
          " 次 · 重新加载 " +
          counters.reloads +
          " 次 · 两轮内重载 " +
          counters.quick_reloads +
          " 次 · 明确点名预加载 " +
          counters.prefetches +
          " 次",
        "metrics"
      )
    )
    row.append(
      node(
        "p",
        "能力管理步骤 " +
          counters.management_steps +
          " · 加载累计 " +
          Math.round(counters.load_ms) +
          " ms · 最高 schema 估算 " +
          counters.max_schema_tokens +
          " tokens",
        "metrics"
      )
    )
    container.append(row)
  }
  if (!snapshot.sessions.length) {
    container.append(node("p", "尚无受管理的会话。"))
  }
}

async function refresh() {
  const ticket = ++generation
  try {
    const result = await bridge.apiGet("capabilities/state")
    if (ticket !== generation) {
      return
    }
    if (!result.ready) {
      throw new Error("能力管理尚未初始化")
    }
    snapshot = result
    if (!dirtySettings) {
      byId("enabled").checked = result.enabled
      byId("new-group-mode").value = result.new_group_mode
      byId("idle-rounds").value = result.idle_rounds
      byId("fold-skills").checked = result.fold_skills
      byId("archive-results").checked = result.archive_results
    }
    let status = "已连接，目录与 AstrBot 同步"
    if (!result.enabled) {
      status = "管理已关闭，可先设置组策略"
    }
    if (result.conflict) {
      status = result.conflict
    }
    if (!result.watchers_intact) {
      status = "部分变化监听已被替换，请检查插件重载状态"
    }
    byId("status").textContent = status
    error(result.errors.join("\n"))
    renderGroups()
    renderSessions()
  } catch (cause) {
    error(cause.message)
  }
}

byId("settings").addEventListener("submit", async (event) => {
  event.preventDefault()
  try {
    await post("settings", {
      enabled: byId("enabled").checked,
      new_group_mode: byId("new-group-mode").value,
      idle_rounds: Number(byId("idle-rounds").value),
      fold_skills: byId("fold-skills").checked,
      archive_results: byId("archive-results").checked
    })
  } catch (cause) {
    error(cause.message)
  }
})
byId("settings").addEventListener("change", () => {
  dirtySettings = true
})
byId("search").addEventListener("input", () => {
  if (snapshot) {
    renderGroups()
  }
})
byId("refresh").addEventListener("click", async () => {
  try {
    await post("refresh", {})
  } catch (cause) {
    error(cause.message)
  }
})

async function initialize() {
  try {
    if (!bridge) {
      throw new Error("请从 AstrBot 插件页面打开")
    }
    const context = await bridge.ready()
    if (context.isDark) {
      document.documentElement.dataset.theme = "dark"
    }
    await refresh()
  } catch (cause) {
    error(cause.message)
  }
}
initialize()
setInterval(() => {
  const focused = document.activeElement
  if (
    dirtySettings ||
    groupDrafts.size ||
    document.hidden ||
    focused?.matches("input, select, button")
  ) {
    return
  }
  refresh()
}, 5000)
