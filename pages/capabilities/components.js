export function node(tag, text, className) {
  const element = document.createElement(tag)
  if (text !== undefined) {
    element.textContent = text
  }
  if (className) {
    element.className = className
  }
  return element
}

export function byId(id) {
  return document.getElementById(id)
}

export function button(text, action, className = "") {
  const element = node("button", text, className)
  element.type = "button"
  element.addEventListener("click", action)
  return element
}

export function badge(mode) {
  let text = "按需"
  if (mode === "primary") {
    text = "常驻"
  }
  return node("span", text, "badge " + mode)
}

export function segmented(value, inherit, label, change) {
  const wrapper = node("div", undefined, "segmented")
  wrapper.setAttribute("role", "group")
  wrapper.setAttribute("aria-label", label)
  const options = [
    ["primary", "常驻"],
    ["folded", "按需"]
  ]
  if (inherit) {
    options.unshift(["inherit", "跟随组"])
  }
  for (const [key, text] of options) {
    const option = button(text, () => {
      change(key)
    })
    option.dataset.mode = key
    option.classList.toggle("active", value === key)
    option.setAttribute("aria-pressed", String(value === key))
    wrapper.append(option)
  }
  return wrapper
}

export function icons() {
  const paths = {
    grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    book: "M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-2H4z M13 7a3 3 0 0 1 3-3h5v15h-4a4 4 0 0 0-4 2",
    activity: "M3 12h4l3-8 4 16 3-8h4",
    pulse: "M4 20V10 M10 20V4 M16 20v-7 M22 20V7",
    refresh: "M20 7v5h-5 M4 17v-5h5 M6 6a8 8 0 0 1 13 4 M5 14a8 8 0 0 0 13 4",
    settings: "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
    search: "M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    check: "M5 12l4 4L19 6"
  }
  for (const placeholder of document.querySelectorAll("[data-icon]")) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg")
    svg.setAttribute("viewBox", "0 0 24 24")
    svg.setAttribute("fill", "none")
    svg.setAttribute("stroke", "currentColor")
    svg.setAttribute("stroke-width", "1.7")
    svg.setAttribute("stroke-linecap", "round")
    svg.setAttribute("stroke-linejoin", "round")
    svg.setAttribute("aria-hidden", "true")
    const path = document.createElementNS(svg.namespaceURI, "path")
    path.setAttribute("d", paths[placeholder.dataset.icon])
    svg.append(path)
    placeholder.replaceChildren(svg)
  }
}

let toastTimer
export function toast(message) {
  clearTimeout(toastTimer)
  byId("toast").textContent = message
  byId("toast").hidden = false
  toastTimer = setTimeout(() => {
    byId("toast").hidden = true
  }, 3500)
}

export function error(message = "") {
  byId("error").textContent = message
  byId("error").hidden = !message
}
