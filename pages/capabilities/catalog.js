import { state, groups, entries, filteredEntries, groupName, edit, patchFor } from "./model.js"
import { byId, node, button, badge, segmented } from "./components.js"

export function renderSources(onSource) {
  const container = byId("sources")
  const scrollTop = container.scrollTop
  const focusedId = document.activeElement?.closest(".source-item")?.dataset.id
  container.replaceChildren()
  byId("mobile-source").replaceChildren(new Option("全部来源", ""))
  const categories = new Map()
  for (const group of groups()) {
    if (
      !group.entries.some((entry) => {
        return entry.kind === state.view
      }) &&
      group.available
    ) {
      continue
    }
    const kind = group.id.split(":")[0]
    if (!categories.has(kind)) {
      categories.set(kind, [])
    }
    categories.get(kind).push(group)
  }
  const names = { plugin: "插件", mcp: "MCP 服务", builtin: "内置工具", skill: "Skills 来源" }
  for (const [kind, rows] of categories) {
    container.append(node("div", names[kind] || kind, "source-category"))
    for (const group of rows) {
      const item = button(
        "",
        () => {
          onSource(group.id)
        },
        "source-item"
      )
      item.dataset.id = group.id
      item.title = group.id
      item.classList.toggle("active", state.source === group.id)
      item.classList.toggle("unavailable", !group.available)
      item.append(
        node("span", undefined, "source-dot " + group.mode),
        node("span", groupName(group), "name"),
        node("span", group.entries.length, "number")
      )
      container.append(item)
      const option = new Option(groupName(group), group.id)
      byId("mobile-source").append(option)
    }
  }
  byId("mobile-source").value = state.source
  container.scrollTop = scrollTop
  for (const item of container.querySelectorAll(".source-item")) {
    if (item.dataset.id === focusedId) {
      item.focus({ preventScroll: true })
    }
  }
}

export function renderCatalog(actions) {
  const all = entries()
  const rows = filteredEntries()
  const totalPages = Math.max(1, Math.ceil(rows.length / state.pageSize))
  state.page = Math.min(state.page, totalPages - 1)
  const page = rows.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize)
  let title = "工具目录"
  if (state.view === "skill") {
    title = "Skills"
  }
  const group = groups().find((item) => {
    return item.id === state.source
  })
  if (group) {
    title = groupName(group)
  }
  byId("page-title").textContent = title
  byId("visible-count").textContent = rows.length
  byId("tool-total").textContent = all.filter((entry) => {
    return entry.kind === "tool"
  }).length
  byId("skill-total").textContent = all.filter((entry) => {
    return entry.kind === "skill"
  }).length
  byId("page-subtitle").textContent = "常用能力常驻，低频能力按需加载。所有修改统一保存。"
  const scope = all.filter((entry) => {
    return entry.kind === state.view && (!state.source || entry.group.id === state.source)
  })
  const summary = byId("catalog-summary")
  summary.replaceChildren()
  for (const [mode, label] of [
    ["primary", "常驻"],
    ["folded", "按需"],
    ["override", "单独覆盖"]
  ]) {
    const count = scope.filter((entry) => {
      if (mode === "override") {
        return entry.override !== "inherit"
      }
      return entry.mode === mode
    }).length
    const item = node("span")
    item.append(
      node("i", undefined, "summary-dot " + mode),
      node("strong", count),
      document.createTextNode(label)
    )
    summary.append(item)
  }
  const groupBox = byId("group-policy")
  groupBox.hidden = !group
  if (group) {
    const overrides = scope.filter((entry) => {
      return entry.override !== "inherit"
    }).length
    const text = node("div")
    text.append(
      node("strong", "整组策略"),
      node("small", overrides + " 项单独覆盖；修改组策略会保留这些例外。")
    )
    if (!group.available) {
      text.append(node("small", "来源暂不可用，已保存的策略仍保留。"))
    }
    const controls = node("div", undefined, "actions")
    controls.append(
      segmented(group.mode, false, "整组策略", (mode) => {
        edit("group", group.id, "mode", mode)
        actions.render()
      }),
      button(
        "编辑组",
        () => {
          actions.group(group.id)
        },
        "subtle"
      ),
      button(
        "全部跟随组",
        () => {
          for (const entry of group.entries) {
            edit("tool", entry.id, "mode", "inherit")
          }
          actions.render()
        },
        "subtle"
      )
    )
    groupBox.replaceChildren(text, controls)
  }
  const body = byId("entries")
  body.replaceChildren()
  for (const entry of page) {
    const row = node("tr")
    row.dataset.id = entry.id
    row.classList.toggle("selected", state.selected.has(entry.id))
    row.classList.toggle(
      "modified",
      Object.keys(patchFor("tool", entry.id)).length > 0 ||
        Object.keys(patchFor("group", entry.group.id)).length > 0
    )
    const checkCell = node("td", undefined, "check-cell")
    const check = node("input")
    check.type = "checkbox"
    check.checked = state.selected.has(entry.id)
    check.setAttribute("aria-label", "选择 " + entry.name)
    check.addEventListener("change", () => {
      if (check.checked) {
        state.selected.add(entry.id)
      } else {
        state.selected.delete(entry.id)
      }
      actions.render()
    })
    checkCell.append(check)
    const info = node("td")
    const name = button(
      entry.name,
      () => {
        actions.detail(entry.id)
      },
      "entry-title"
    )
    name.title = entry.name
    const description = node(
      "span",
      entry.description.trim().split(/\r?\n/)[0] || "暂无说明",
      "entry-description"
    )
    description.title = entry.description
    info.append(name, description)
    info.append(node("span", groupName(entry.group), "mobile-entry-source"))
    const source = node("td", undefined, "source-column")
    source.append(
      button(
        groupName(entry.group),
        () => {
          actions.source(entry.group.id)
        },
        "source-link"
      )
    )
    const policy = node("td")
    policy.append(
      segmented(entry.override, true, entry.name + " 的策略", (mode) => {
        edit("tool", entry.id, "mode", mode)
        actions.render()
      })
    )
    const effective = node("td", undefined, "effective-column")
    const indicator = badge(entry.mode)
    indicator.title = "配置生效策略；当前请求是否加载请查看会话状态"
    effective.append(indicator)
    row.append(checkCell, info, source, policy, effective)
    body.append(row)
  }
  const empty = byId("empty")
  empty.hidden = rows.length > 0
  let emptyTitle = "没有匹配的能力"
  let emptyHint = "试试其他关键词，或清除来源和策略筛选。"
  if (group && !group.available) {
    emptyTitle = "来源暂不可用"
    emptyHint = "策略已保留，来源重新上线后将继续使用。"
  }
  if (all.length === 0) {
    emptyTitle = "目录中还没有能力"
    emptyHint = "点击刷新目录，检查 AstrBot 中的插件与 MCP 是否启用。"
  }
  empty.replaceChildren(node("strong", emptyTitle), node("span", emptyHint))
  byId("result-count").textContent = "共 " + rows.length + " 项 · 每页 " + state.pageSize + " 项"
  byId("page-number").textContent = state.page + 1 + " / " + totalPages
  byId("previous-page").disabled = state.page === 0
  byId("next-page").disabled = state.page >= totalPages - 1
  byId("select-page").checked =
    page.length > 0 &&
    page.every((entry) => {
      return state.selected.has(entry.id)
    })
  byId("select-page").indeterminate =
    page.some((entry) => {
      return state.selected.has(entry.id)
    }) && !byId("select-page").checked
  byId("bulk-bar").hidden = state.selected.size === 0
  byId("selection-count").textContent = "已选择 " + state.selected.size + " 项（含跨页选择）"
}
