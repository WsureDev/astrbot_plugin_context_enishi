import { afterAll, beforeAll, expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve, extname } from "node:path"
import { chromium } from "playwright"

let browser
let server
const root = resolve(import.meta.dir, "..", "pages", "capabilities")
beforeAll(async () => {
  const options = { headless: true }
  if (process.env.ENISHI_CHROMIUM_PATH) {
    options.executablePath = process.env.ENISHI_CHROMIUM_PATH
  }
  browser = await chromium.launch(options)
  server = Bun.serve({
    port: 0,
    async fetch(request) {
      const path = new URL(request.url).pathname
      if (path === "/favicon.ico") {
        return new Response(null, { status: 204 })
      }
      if (path === "/preview-font.ttf" && process.env.ENISHI_PREVIEW_FONT) {
        return new Response(Bun.file(process.env.ENISHI_PREVIEW_FONT))
      }
      if (path === "/api/plugin/page/bridge-sdk.js") {
        return new Response("")
      }
      let name = path.slice(1)
      if (!name) {
        name = "index.html"
      }
      if (name.includes("..")) {
        return new Response("", { status: 404 })
      }
      const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" }
      return new Response(await readFile(resolve(root, name)), {
        headers: { "Content-Type": mime[extname(name)] || "text/plain" }
      })
    }
  })
})
afterAll(async () => {
  await browser?.close()
  server?.stop()
})

function fixture() {
  return {
    ready: true,
    enabled: true,
    policy_revision: "1",
    revision: "catalog-1",
    new_group_mode: "folded",
    idle_rounds: 5,
    mcp_poll_seconds: 60,
    fold_skills: true,
    archive_results: true,
    conflict: "",
    watchers_intact: true,
    mcp_notifications: true,
    errors: [],
    runs: [],
    groups: [
      {
        id: "plugin:context_enishi",
        name: "context_enishi",
        source_name: "Context Enishi",
        mode: "folded",
        idle_rounds: null,
        available: true,
        entries: Array.from({ length: 36 }, (_, index) => {
          const names = [
            "describe_channel_image",
            "fetch_channel_image",
            "get_channel_message",
            "get_channel_user_history",
            "get_channel_member_info",
            "get_group_participants"
          ]
          const descriptions = [
            "按原生图片 ID 查看图片内容，使用独立的视觉模型转述。",
            "获取图片文件，供生成、编辑等下游工具使用。",
            "查询群聊中的消息内容和图片引用。",
            "按时间范围查询指定成员的历史发言。",
            "查看当前群成员的昵称、名片及角色。",
            "按近期发言或活跃频率查看参与者。"
          ]
          let name = "channel_tool_" + String(index).padStart(2, "0")
          let description = "查询渠道资料，提供完整原文与明确的来源信息。"
          if (index < names.length) {
            name = names[index]
            description = descriptions[index]
          }
          return {
            id: "plugin:context_enishi/" + name,
            name,
            kind: "tool",
            description: description + "\nSECOND LINE MUST REMAIN",
            override: "inherit",
            mode: "folded"
          }
        })
      },
      {
        id: "mcp:web-search",
        name: "web-search",
        source_name: "网页搜索",
        mode: "primary",
        idle_rounds: 10,
        available: true,
        entries: [
          {
            id: "mcp:web-search/search",
            name: "web_search",
            kind: "tool",
            description: "搜索网页，获取当前信息和可核查的引用。",
            override: "inherit",
            mode: "primary"
          }
        ]
      },
      {
        id: "mcp:reference-images",
        name: "reference-images",
        source_name: "参考图服务",
        mode: "folded",
        idle_rounds: null,
        available: true,
        entries: [
          {
            id: "mcp:reference-images/find",
            name: "find_reference_images",
            kind: "tool",
            description: "从多个来源查找参考图片，支持标签与人物过滤。",
            override: "primary",
            mode: "primary"
          }
        ]
      },
      {
        id: "builtin:files",
        name: "files",
        source_name: "文件操作",
        mode: "primary",
        idle_rounds: null,
        available: true,
        entries: [
          {
            id: "builtin:files/read",
            name: "read_file",
            kind: "tool",
            description: "读取文件内容。",
            override: "inherit",
            mode: "primary"
          }
        ]
      },
      {
        id: "skill:local:local",
        name: "local:local",
        source_name: "本地 Skills",
        mode: "folded",
        idle_rounds: null,
        available: true,
        entries: [
          {
            id: "skill:local:local/design",
            name: "design-review",
            kind: "skill",
            description: "检查界面的布局、视觉层级和交互可用性。",
            override: "inherit",
            mode: "folded"
          }
        ]
      }
    ],
    sessions: [
      {
        id: "Haqi8mi:GroupMessage:332993267|conversation",
        completed: 7,
        counters: {
          auto_unloads: 2,
          quick_reloads: 1,
          reloads: 1,
          prefetches: 3,
          load_ms: 4,
          max_schema_tokens: 1000
        }
      }
    ]
  }
}

async function pageWith(data = fixture(), width = 1360, dark = false) {
  const page = await browser.newPage({ viewport: { width, height: 960 } })
  const errors = []
  page.on("pageerror", (error) => {
    errors.push(error.message)
  })
  await page.addInitScript(
    ({ data, dark }) => {
      window.auditState = data
      window.apiCalls = []
      window.failSave = false
      window.AstrBotPluginPage = {
        ready: async () => {
          return { isDark: dark }
        },
        apiGet: async () => {
          return structuredClone(window.auditState)
        },
        apiPost: async (endpoint, body) => {
          window.apiCalls.push({ endpoint, body })
          if (endpoint.endsWith("detail")) {
            return {
              parameters: { type: "object", properties: { q: { type: "string" } }, required: ["q"] }
            }
          }
          if (endpoint.endsWith("refresh")) {
            return structuredClone(window.auditState)
          }
          if (window.failSave) {
            throw new Error("磁盘写入失败")
          }
          if (body.revision !== window.auditState.policy_revision) {
            throw new Error("配置已被更新")
          }
          Object.assign(window.auditState, body.settings)
          for (const row of body.groups) {
            const group = window.auditState.groups.find((item) => {
              return item.id === row.id
            })
            Object.assign(group, row)
          }
          for (const row of body.tools) {
            for (const group of window.auditState.groups) {
              const entry = group.entries.find((item) => {
                return item.id === row.id
              })
              if (entry) {
                entry.override = row.mode
              }
            }
          }
          window.auditState.policy_revision = String(Number(window.auditState.policy_revision) + 1)
          return { ok: true, state: structuredClone(window.auditState) }
        }
      }
    },
    { data, dark }
  )
  await page.goto(server.url.href)
  if (process.env.ENISHI_PREVIEW_FONT) {
    await page.addStyleTag({
      content: '@font-face { font-family: "Noto Sans CJK SC"; src: url("/preview-font.ttf") }'
    })
    await page.evaluate(async () => {
      await document.fonts.load('14px "Noto Sans CJK SC"')
    })
  }
  await page.locator("#result-count").getByText("共 39 项", { exact: false }).waitFor()
  expect(errors).toEqual([])
  return page
}

async function choose(page, name, mode) {
  await page
    .getByRole("group", { name: name + " 的策略", exact: true })
    .getByRole("button", { name: mode, exact: true })
    .click()
}

test("catalog is visible, policies are drafts and details preserve full descriptions", async () => {
  const page = await pageWith()
  try {
    expect(
      await page.getByRole("button", { name: "describe_channel_image", exact: true }).isVisible()
    ).toBe(true)
    await choose(page, "describe_channel_image", "常驻")
    expect(await page.locator("#dirty-count").textContent()).toContain("已修改 1 项")
    expect(
      await page.evaluate(() => {
        return window.apiCalls.length
      })
    ).toBe(0)
    await page.getByRole("button", { name: "describe_channel_image", exact: true }).click()
    await expectVisible(page.getByText("SECOND LINE MUST REMAIN", { exact: false }).last())
    await page.getByText("参数定义 / Skill 路径").click()
    await page.locator("#detail-content pre").getByText('"required"', { exact: false }).waitFor()
    await page.getByRole("button", { name: "关闭详情" }).click()
    await page.getByRole("button", { name: "保存修改", exact: true }).click()
    await page.getByText("所有修改已保存", { exact: true }).waitFor()
    const commit = await page.evaluate(() => {
      return window.apiCalls.find((call) => {
        return call.endpoint.endsWith("commit")
      })
    })
    expect(commit.body.tools).toEqual([
      { id: "plugin:context_enishi/describe_channel_image", mode: "primary" }
    ])
  } finally {
    await page.close()
  }
})

async function expectVisible(locator) {
  expect(await locator.isVisible()).toBe(true)
}

test("bulk selection crosses pages, group changes preserve overrides and save together", async () => {
  const page = await pageWith()
  try {
    await page.getByLabel("选择当前页").check()
    await page.getByRole("button", { name: "下一页" }).click()
    await page.getByLabel("选择 read_file", { exact: true }).check()
    await page.getByRole("button", { name: "设为常驻", exact: true }).click()
    expect(await page.locator("#dirty-count").textContent()).toContain("31")
    await page
      .locator("#sources")
      .getByRole("button", { name: "Context Enishi", exact: false })
      .click()
    await page
      .getByRole("group", { name: "整组策略", exact: true })
      .getByRole("button", { name: "常驻", exact: true })
      .click()
    await page.getByRole("button", { name: "编辑组" }).click()
    await page.getByLabel("组显示名", { exact: true }).fill("聊天工具")
    await page.getByRole("button", { name: "加入修改", exact: true }).click()
    await page.getByRole("button", { name: "查看修改" }).click()
    expect(await page.locator("#review-content").textContent()).toContain("聊天工具")
    await page.getByRole("button", { name: "保存全部修改" }).click()
    await page.getByText("所有修改已保存", { exact: true }).waitFor()
    const commits = await page.evaluate(() => {
      return window.apiCalls.filter((call) => {
        return call.endpoint.endsWith("commit")
      })
    })
    expect(commits.length).toBe(1)
    expect(commits[0].body.tools.length).toBe(31)
    expect(commits[0].body.groups[0]).toMatchObject({
      id: "plugin:context_enishi",
      mode: "primary",
      name: "聊天工具"
    })
  } finally {
    await page.close()
  }
})

test("settings join draft, failed save retains edits and retry succeeds", async () => {
  const page = await pageWith()
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click()
    await page.getByLabel("启用工具与 Skill 管理", { exact: false }).uncheck()
    await page.locator("#idle-rounds").fill("8")
    await page.getByRole("button", { name: "加入修改", exact: true }).click()
    expect(
      await page.evaluate(() => {
        return window.apiCalls.length
      })
    ).toBe(0)
    await page.evaluate(() => {
      window.failSave = true
    })
    await page.getByRole("button", { name: "保存修改", exact: true }).click()
    await page.getByRole("alert").getByText("草稿已保留", { exact: false }).waitFor()
    expect(await page.locator("#dirty-count").textContent()).toContain("2")
    await page.evaluate(() => {
      window.failSave = false
    })
    await page.getByRole("button", { name: "保存修改", exact: true }).click()
    await page.getByText("所有修改已保存", { exact: true }).waitFor()
    expect(await page.locator("#status").textContent()).toBe("管理已关闭")
  } finally {
    await page.close()
  }
})

test("refresh preserves drafts, detects conflicts and safely renders source names", async () => {
  const page = await pageWith()
  try {
    await choose(page, "describe_channel_image", "常驻")
    await page.evaluate(() => {
      window.auditState.groups[0].entries[0].override = "folded"
      window.auditState.policy_revision = "2"
      window.auditState.groups.push({
        id: "mcp:new",
        source_name: "<img src=x onerror=alert(1)>",
        name: "new",
        mode: "folded",
        entries: [],
        available: false
      })
    })
    await page.getByRole("button", { name: "刷新目录" }).click()
    await expectVisible(page.locator("#conflicts"))
    expect(await page.locator("img").count()).toBe(0)
    expect(await page.locator("#save").isDisabled()).toBe(true)
    await page.getByRole("button", { name: "保留我的修改" }).click()
    await page.getByRole("button", { name: "保存修改", exact: true }).click()
    await page.getByText("所有修改已保存", { exact: true }).waitFor()
    const revision = await page.evaluate(() => {
      return window.apiCalls.find((call) => {
        return call.endpoint.endsWith("commit")
      }).body.revision
    })
    expect(revision).toBe("2")
  } finally {
    await page.close()
  }
})

test("search filters actual policies and settings do not reset existing groups", async () => {
  const page = await pageWith()
  try {
    await page.getByLabel("搜索能力", { exact: true }).fill("find_reference")
    expect(await page.locator("#entries tr").count()).toBe(1)
    await page.getByLabel("策略筛选").selectOption("override")
    expect(await page.locator("#entries tr").count()).toBe(1)
    await page.getByRole("button", { name: "设置", exact: true }).click()
    await page.locator("#new-group-mode").selectOption("primary")
    await page.getByRole("button", { name: "加入修改", exact: true }).click()
    await page.getByRole("button", { name: "保存修改", exact: true }).click()
    await page.getByText("所有修改已保存", { exact: true }).waitFor()
    expect(
      await page.evaluate(() => {
        return window.auditState.groups[0].mode
      })
    ).toBe("folded")
  } finally {
    await page.close()
  }
})

test("light, dark and mobile layouts remain readable without overflow", async () => {
  for (const [width, dark, name] of [
    [1360, false, "desktop"],
    [1360, true, "dark"],
    [390, false, "mobile"]
  ]) {
    const page = await pageWith(fixture(), width, dark)
    try {
      const sizes = await page.evaluate(() => {
        return {
          width: document.documentElement.clientWidth,
          content: document.documentElement.scrollWidth,
          mainWidth: document.querySelector("main").clientWidth,
          mainContent: document.querySelector("main").scrollWidth
        }
      })
      expect(sizes.content).toBeLessThanOrEqual(sizes.width)
      expect(sizes.mainContent).toBeLessThanOrEqual(sizes.mainWidth)
      if (width === 390) {
        await page
          .locator(".mobile-tabs")
          .getByRole("button", { name: "Skills", exact: true })
          .click()
        await page.getByRole("button", { name: "design-review", exact: true }).waitFor()
        await page
          .locator(".mobile-tabs")
          .getByRole("button", { name: "工具", exact: true })
          .click()
      }
      if (process.env.ENISHI_SCREENSHOT_DIR) {
        await page.screenshot({
          path: resolve(process.env.ENISHI_SCREENSHOT_DIR, "capabilities-" + name + ".png"),
          fullPage: true
        })
      }
    } finally {
      await page.close()
    }
  }
})
