import {
  state,
  adopt,
  edit,
  draftCounts,
  filteredEntries,
  payload,
  resolveConflicts
} from "./model.js"
import { byId, node, button, icons, toast, error } from "./components.js"
import { renderSources, renderCatalog } from "./catalog.js"
import {
  openSettings,
  applySettings,
  openDetail,
  openGroup,
  renderReview,
  renderRuntime
} from "./panels.js"

const bridge = window.AstrBotPluginPage
let generation = 0
let refreshPending = false
let toastOnSave = false

async function post(endpoint, body) {
  const response = await bridge.apiPost("capabilities/" + endpoint, body)
  if (response.error) {
    throw new Error(response.error)
  }
  return response
}

function source(id) {
  state.source = id
  state.page = 0
  render()
}

function view(name) {
  state.view = name
  state.source = ""
  state.page = 0
  state.selected.clear()
  render()
}

function renderSaveBar() {
  byId("catalog-view").inert = state.saving
  byId("open-settings").disabled = state.saving
  const counts = draftCounts()
  let text = "所有修改已保存"
  if (counts.total) {
    text = "已修改 " + counts.total + " 项"
  }
  if (state.saving) {
    text = "正在保存…"
  }
  byId("dirty-count").textContent = text
  byId("draft-summary").textContent =
    counts.groups + " 个组 · " + counts.tools + " 个工具 / Skill · " + counts.settings + " 项设置"
  byId("draft-summary").hidden = counts.total === 0
  byId("save-indicator").classList.toggle("dirty", counts.total > 0)
  byId("save").disabled = !counts.total || state.saving || counts.conflicts > 0
  byId("review-save").disabled = byId("save").disabled
  byId("discard").disabled = !counts.total || state.saving
  byId("review").disabled = !counts.total || state.saving
  byId("conflicts").hidden = counts.conflicts === 0
}

function render() {
  if (!state.snapshot) {
    return
  }
  const catalogView = ["tool", "skill"].includes(state.view)
  byId("catalog-view").hidden = !catalogView
  byId("runtime-view").hidden = catalogView
  byId("sources").hidden = !catalogView
  document.querySelector(".sidebar-heading").hidden = !catalogView
  for (const item of document.querySelectorAll("[data-view]")) {
    item.classList.toggle("active", item.dataset.view === state.view)
    item.setAttribute("aria-current", String(item.dataset.view === state.view))
  }
  let status = "管理已关闭"
  const snapshot = state.snapshot
  if (snapshot.enabled) {
    status = "管理运行中"
  }
  if (snapshot.conflict) {
    status = "管理冲突"
  }
  byId("status").textContent = status
  byId("status").classList.toggle("running", snapshot.enabled && !snapshot.conflict)
  let notice = ""
  if (snapshot.conflict) {
    notice = snapshot.conflict
  } else if (!snapshot.enabled) {
    notice = "管理尚未开启。可先配置策略，再到设置中启用管理并保存。"
  } else if (!snapshot.watchers_intact) {
    notice = "部分目录监听已被替换，请到诊断页查看插件重载状态。"
  }
  byId("notice").textContent = notice
  byId("notice").hidden = !notice
  renderSources(source)
  if (catalogView) {
    const focused = document.activeElement
    const rowId = focused?.closest("tr")?.dataset.id
    const focusedMode = focused?.dataset.mode
    renderCatalog({
      render,
      source,
      detail: (id) => {
        openDetail(id, { render, post })
      },
      group: (id) => {
        openGroup(id, render)
      }
    })
    if (rowId && focusedMode) {
      for (const row of byId("entries").children) {
        if (row.dataset.id === rowId) {
          row.querySelector('[data-mode="' + focusedMode + '"]')?.focus({ preventScroll: true })
        }
      }
    }
  } else {
    renderRuntime()
  }
  renderSaveBar()
  if (byId("review-dialog").open) {
    renderReview()
  }
}

async function refresh(remoteRefresh = false) {
  if (refreshPending || state.saving) {
    return
  }
  refreshPending = true
  const ticket = ++generation
  byId("refresh").disabled = true
  try {
    let result
    if (remoteRefresh) {
      result = await post("refresh", {})
    } else {
      result = await bridge.apiGet("capabilities/state")
    }
    if (ticket !== generation) {
      return
    }
    if (!result.ready) {
      throw new Error("能力管理尚未初始化，请稍后重试")
    }
    adopt(result)
    render()
    if (remoteRefresh) {
      toast("目录已刷新，未保存的修改已保留")
    }
  } catch (cause) {
    error(cause.message)
  } finally {
    refreshPending = false
    byId("refresh").disabled = false
  }
}

async function save() {
  if (state.saving || !state.drafts.size || draftCounts().conflicts) {
    return
  }
  state.saving = true
  generation++
  const body = payload()
  renderSaveBar()
  error()
  try {
    const result = await post("commit", body)
    // Clear only edits included in this request; later edits stay as a draft.
    for (const [key, draft] of state.drafts) {
      let sent
      if (draft.type === "setting") {
        sent = body.settings[draft.id]
      } else if (draft.type === "group") {
        sent = body.groups.find((row) => {
          return row.id === draft.id
        })?.[draft.field]
      } else {
        sent = body.tools.find((row) => {
          return row.id === draft.id
        })?.mode
      }
      if (JSON.stringify(sent) === JSON.stringify(draft.value)) {
        state.drafts.delete(key)
      } else if (sent !== undefined) {
        draft.before = sent
      }
    }
    adopt(result.state)
    toastOnSave = true
    byId("review-dialog").close()
  } catch (cause) {
    error("保存失败：" + cause.message + "。草稿已保留。")
  } finally {
    state.saving = false
    render()
  }
  if (toastOnSave) {
    toast("修改已保存，新请求将使用新策略")
    toastOnSave = false
  } else {
    await refresh()
  }
}

byId("search").addEventListener("input", () => {
  state.query = byId("search").value
  state.page = 0
  render()
})
byId("policy-filter").addEventListener("change", () => {
  state.filter = byId("policy-filter").value
  state.page = 0
  render()
})
byId("mobile-source").addEventListener("change", () => {
  source(byId("mobile-source").value)
})
byId("clear-source").addEventListener("click", () => {
  source("")
})
byId("previous-page").addEventListener("click", () => {
  state.page--
  render()
})
byId("next-page").addEventListener("click", () => {
  state.page++
  render()
})
byId("select-page").addEventListener("change", () => {
  const rows = filteredEntries().slice(
    state.page * state.pageSize,
    (state.page + 1) * state.pageSize
  )
  for (const entry of rows) {
    if (byId("select-page").checked) {
      state.selected.add(entry.id)
    } else {
      state.selected.delete(entry.id)
    }
  }
  render()
})
byId("clear-selection").addEventListener("click", () => {
  state.selected.clear()
  render()
})
for (const control of document.querySelectorAll("[data-bulk]")) {
  control.addEventListener("click", () => {
    for (const id of state.selected) {
      edit("tool", id, "mode", control.dataset.bulk)
    }
    toast(state.selected.size + " 项策略已加入草稿")
    render()
  })
}
byId("save").addEventListener("click", save)
byId("review-save").addEventListener("click", save)
byId("review").addEventListener("click", () => {
  renderReview()
  byId("review-dialog").showModal()
})
byId("discard").addEventListener("click", () => {
  state.drafts.clear()
  error()
  render()
  toast("未保存的修改已放弃")
})
byId("keep-mine").addEventListener("click", () => {
  resolveConflicts(true)
  render()
})
byId("use-remote").addEventListener("click", () => {
  resolveConflicts(false)
  render()
})
byId("open-settings").addEventListener("click", () => {
  if (state.snapshot) {
    openSettings()
  }
})
byId("settings-form").addEventListener("submit", (event) => {
  event.preventDefault()
  applySettings()
  render()
})
byId("refresh").addEventListener("click", () => {
  error()
  refresh(true)
})
for (const close of document.querySelectorAll("[data-close]")) {
  close.addEventListener("click", () => {
    close.closest("dialog").close()
  })
}
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("click", (event) => {
    const rect = dialog.getBoundingClientRect()
    if (
      event.target === dialog &&
      (event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom)
    ) {
      dialog.close()
    }
  })
}
const mobileTabs = node("nav", undefined, "mobile-tabs")
mobileTabs.setAttribute("aria-label", "移动端工作区")
for (const [name, text] of [
  ["tool", "工具"],
  ["skill", "Skills"],
  ["sessions", "会话"],
  ["diagnostics", "诊断"]
]) {
  const tab = button(text, () => {
    view(name)
  })
  tab.dataset.view = name
  mobileTabs.append(tab)
}
document.querySelector("main").prepend(mobileTabs)
for (const item of document.querySelectorAll(".sidebar [data-view]")) {
  item.title = item.textContent.trim()
  item.addEventListener("click", () => {
    view(item.dataset.view)
  })
}
document.querySelector(".brand").addEventListener("click", (event) => {
  event.preventDefault()
  view("tool")
})
window.addEventListener("beforeunload", (event) => {
  if (state.drafts.size) {
    event.preventDefault()
    event.returnValue = ""
  }
})
document.addEventListener("keydown", (event) => {
  if (
    event.key === "/" &&
    !document.querySelector("dialog[open]") &&
    !document.activeElement.matches("input, textarea, select")
  ) {
    event.preventDefault()
    byId("search").focus()
  }
})
async function initialize() {
  icons()
  if (!bridge) {
    error("请从 AstrBot 插件页面打开")
    return
  }
  try {
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
  if (
    document.hidden ||
    state.saving ||
    document.querySelector("dialog[open]") ||
    document.activeElement?.matches("input, select, button")
  ) {
    return
  }
  refresh()
}, 5000)
