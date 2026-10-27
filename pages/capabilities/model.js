export const settingNames = {
  enabled: "启用管理",
  new_group_mode: "新增组策略",
  idle_rounds: "默认闲置轮数",
  fold_skills: "折叠 Skill",
  archive_results: "收纳闲置结果",
  mcp_poll_seconds: "MCP 刷新间隔"
}
export const modeNames = { inherit: "跟随组", primary: "常驻", folded: "按需" }
export const state = {
  snapshot: null,
  drafts: new Map(),
  selected: new Set(),
  view: "tool",
  source: "",
  query: "",
  filter: "all",
  page: 0,
  pageSize: 30,
  saving: false
}

function equal(first, second) {
  return JSON.stringify(first) === JSON.stringify(second)
}

export function groupName(group) {
  const base = group.id.slice(group.id.indexOf(":") + 1)
  if (group.name && group.name !== base) {
    return group.name
  }
  return group.source_name || group.name || base
}

export function groups() {
  if (!state.snapshot) {
    return []
  }
  return state.snapshot.groups.map((group) => {
    return { ...group, ...patchFor("group", group.id) }
  })
}

export function entries() {
  return groups().flatMap((group) => {
    return group.entries.map((entry) => {
      const override = current("tool", entry.id, "mode")
      let mode = override
      if (override === "inherit") {
        mode = group.mode
      }
      return { ...entry, group, override, mode }
    })
  })
}

export function filteredEntries() {
  const query = state.query.trim().toLocaleLowerCase()
  return entries().filter((entry) => {
    if (entry.kind !== state.view || (state.source && entry.group.id !== state.source)) {
      return false
    }
    if (state.filter === "override" && entry.override === "inherit") {
      return false
    }
    if (["primary", "folded"].includes(state.filter) && entry.mode !== state.filter) {
      return false
    }
    return (entry.name + " " + entry.description + " " + groupName(entry.group) + " " + entry.id)
      .toLocaleLowerCase()
      .includes(query)
  })
}

export function remote(type, id, field, snapshot = state.snapshot) {
  if (type === "setting") {
    return snapshot[id]
  }
  if (type === "group") {
    return snapshot.groups.find((group) => {
      return group.id === id
    })?.[field]
  }
  for (const group of snapshot.groups) {
    const entry = group.entries.find((item) => {
      return item.id === id
    })
    if (entry) {
      return entry.override
    }
  }
  return undefined
}

export function current(type, id, field) {
  const draft = state.drafts.get(JSON.stringify([type, id, field]))
  if (draft) {
    return draft.value
  }
  return remote(type, id, field)
}

export function edit(type, id, field, value) {
  const key = JSON.stringify([type, id, field])
  const before = remote(type, id, field)
  if (equal(value, before)) {
    state.drafts.delete(key)
    return
  }
  state.drafts.set(key, { type, id, field, before, value, conflict: false })
}

export function patchFor(type, id) {
  const patch = {}
  for (const draft of state.drafts.values()) {
    if (draft.type === type && draft.id === id) {
      patch[draft.field] = draft.value
    }
  }
  return patch
}

export function adopt(snapshot) {
  for (const [key, draft] of state.drafts) {
    const latest = remote(draft.type, draft.id, draft.field, snapshot)
    if (equal(latest, draft.value)) {
      state.drafts.delete(key)
    } else {
      draft.conflict = !equal(latest, draft.before)
    }
  }
  state.snapshot = snapshot
  const known = new Set(
    entries().map((entry) => {
      return entry.id
    })
  )
  for (const id of state.selected) {
    if (!known.has(id)) {
      state.selected.delete(id)
    }
  }
}

export function resolveConflicts(keepMine) {
  for (const [key, draft] of state.drafts) {
    if (!draft.conflict) {
      continue
    }
    const latest = remote(draft.type, draft.id, draft.field)
    if (!keepMine || latest === undefined) {
      state.drafts.delete(key)
    } else {
      draft.before = latest
      draft.conflict = false
    }
  }
}

export function payload() {
  const settings = {}
  const groupRows = new Map()
  const tools = []
  for (const draft of state.drafts.values()) {
    if (draft.type === "setting") {
      settings[draft.id] = draft.value
    } else if (draft.type === "group") {
      if (!groupRows.has(draft.id)) {
        groupRows.set(draft.id, { id: draft.id })
      }
      groupRows.get(draft.id)[draft.field] = draft.value
    } else {
      tools.push({ id: draft.id, mode: draft.value })
    }
  }
  return {
    revision: state.snapshot.policy_revision,
    settings,
    groups: [...groupRows.values()],
    tools
  }
}

export function draftCounts() {
  const groups = new Set()
  let tools = 0
  let settings = 0
  let conflicts = 0
  for (const draft of state.drafts.values()) {
    if (draft.type === "group") {
      groups.add(draft.id)
    } else if (draft.type === "tool") {
      tools++
    } else {
      settings++
    }
    if (draft.conflict) {
      conflicts++
    }
  }
  return { groups: groups.size, tools, settings, conflicts, total: groups.size + tools + settings }
}
