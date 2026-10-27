import { afterAll, beforeAll, expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { chromium } from "playwright"

let browser
const root = resolve(import.meta.dir, "..")

beforeAll(async () => {
  const options = { headless: true }
  if (process.env.ENISHI_CHROMIUM_PATH) {
    options.executablePath = process.env.ENISHI_CHROMIUM_PATH
  }
  browser = await chromium.launch(options)
})
afterAll(async () => {
  await browser?.close()
})

function fixture() {
  return {
    ready: true,
    enabled: false,
    new_group_mode: "folded",
    idle_rounds: 5,
    fold_skills: true,
    archive_results: true,
    conflict: "",
    watchers_intact: true,
    errors: [],
    groups: [
      {
        id: "plugin:example",
        mode: "folded",
        idle_rounds: null,
        available: true,
        entries: [
          {
            id: "plugin:example/tool",
            name: "sample_tool",
            kind: "tool",
            description: "Original full description\nSECOND LINE MUST REMAIN",
            override: "inherit"
          }
        ]
      },
      {
        id: "mcp:example",
        mode: "primary",
        idle_rounds: 10,
        available: true,
        entries: [
          {
            id: "mcp:example/search",
            name: "search",
            kind: "tool",
            description: "Search tools",
            override: "inherit"
          }
        ]
      }
    ],
    sessions: [
      {
        id: "account:GroupMessage:123|conversation",
        completed: 7,
        counters: {
          auto_unloads: 2,
          reloads: 1,
          quick_reloads: 1,
          prefetches: 3,
          management_steps: 2,
          load_ms: 4,
          max_schema_tokens: 1000
        }
      }
    ]
  }
}

async function pageWith(state = fixture(), width = 1200) {
  const page = await browser.newPage({ viewport: { width, height: 900 } })
  const html = await readFile(resolve(root, "pages/capabilities/index.html"), "utf8")
  await page.setContent(html.replace(/<script[\s\S]*?<\/script>/g, "").replace(/<link[^>]+>/g, ""))
  await page.addStyleTag({ path: resolve(root, "pages/capabilities/style.css") })
  await page.evaluate((data) => {
    window.auditState = data
    window.apiCalls = []
    window.AstrBotPluginPage = {
      ready: async () => {
        return { isDark: false }
      },
      apiGet: async () => {
        return structuredClone(window.auditState)
      },
      apiPost: async (endpoint, body) => {
        window.apiCalls.push({ endpoint, body })
        if (endpoint.endsWith("settings")) {
          Object.assign(window.auditState, {
            enabled: body.enabled,
            new_group_mode: body.new_group_mode,
            idle_rounds: body.idle_rounds,
            fold_skills: body.fold_skills,
            archive_results: body.archive_results
          })
        }
        if (endpoint.endsWith("group")) {
          const group = window.auditState.groups.find((item) => {
            return item.id === body.group
          })
          Object.assign(group, { mode: body.mode, idle_rounds: body.idle_rounds, name: body.name })
        }
        if (endpoint.endsWith("tool")) {
          for (const group of window.auditState.groups) {
            const entry = group.entries.find((item) => {
              return item.id === body.id
            })
            if (entry) {
              entry.override = body.mode
            }
          }
        }
        return { ok: true }
      }
    }
  }, state)
  await page.addScriptTag({ path: resolve(root, "pages/capabilities/app.js") })
  await page.getByText("2 / 2 组").waitFor()
  return page
}

test("group policies, inherited tools and complete descriptions are editable", async () => {
  const page = await pageWith()
  try {
    await page.locator('details[data-id="plugin:example"] > summary').click()
    await page
      .locator('details[data-id="plugin:example"]')
      .getByLabel("组显示名")
      .fill("Example renamed")
    await page.locator('details[data-id="plugin:example"] .controls select').selectOption("primary")
    await page.locator('details[data-id="plugin:example"] .controls input[type="number"]').fill("8")
    await page.locator('details[data-id="plugin:example"] button').click()
    await page.waitForFunction(() => {
      return window.apiCalls.length === 1
    })
    const calls = await page.evaluate(() => {
      return window.apiCalls
    })
    expect(calls[0].body).toEqual({
      group: "plugin:example",
      mode: "primary",
      idle_rounds: 8,
      name: "Example renamed"
    })
    await page.getByLabel("sample_tool 的策略").selectOption("folded")
    await page.waitForFunction(() => {
      return window.apiCalls.length === 2
    })
    await page.locator('details[data-id="plugin:example"] .entry summary').click()
    expect(await page.getByText("SECOND LINE MUST REMAIN", { exact: false }).isVisible()).toBe(true)
  } finally {
    await page.close()
  }
})

test("global defaults save without silently changing existing groups", async () => {
  const page = await pageWith()
  try {
    await page.getByLabel("启用管理").check()
    await page.locator("#new-group-mode").selectOption("primary")
    await page.locator("#idle-rounds").fill("6")
    await page.getByRole("button", { name: "保存设置" }).click()
    await page.waitForFunction(() => {
      return window.apiCalls.length === 1
    })
    expect(
      await page.evaluate(() => {
        return window.auditState.groups[0].mode
      })
    ).toBe("folded")
    expect(await page.locator("#enabled").isChecked()).toBe(true)
  } finally {
    await page.close()
  }
})

test("refresh shows newly registered groups and safely renders source text", async () => {
  const page = await pageWith()
  try {
    await page.evaluate(() => {
      window.auditState.groups.push({
        id: "mcp:<img src=x onerror=alert(1)>",
        mode: "folded",
        idle_rounds: null,
        available: true,
        entries: []
      })
    })
    await page.getByRole("button", { name: "刷新目录" }).click()
    await page.getByText("3 / 3 组").waitFor()
    expect(await page.locator("img").count()).toBe(0)
    await page.locator("#search").fill("sample_tool")
    expect(await page.locator("#count").textContent()).toBe("1 / 3 组")
  } finally {
    await page.close()
  }
})

test("mobile layout exposes conflict and has no horizontal overflow", async () => {
  const state = fixture()
  state.enabled = true
  state.conflict = "Lazy Tools 正在管理工具或 Skills，请先关闭其采集变换"
  const page = await pageWith(state, 390)
  try {
    await page.locator('details[data-id="plugin:example"] > summary').click()
    expect(await page.locator("#status").textContent()).toContain("Lazy Tools")
    const size = await page.evaluate(() => {
      return {
        width: document.documentElement.clientWidth,
        content: document.documentElement.scrollWidth
      }
    })
    expect(size.content).toBeLessThanOrEqual(size.width)
    if (process.env.ENISHI_SCREENSHOT_DIR) {
      await page.screenshot({
        path: resolve(process.env.ENISHI_SCREENSHOT_DIR, "capabilities-mobile.png"),
        fullPage: true
      })
    }
  } finally {
    await page.close()
  }
})
