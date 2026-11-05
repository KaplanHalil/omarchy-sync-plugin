.pragma library

function length(value) {
  return Array.isArray(value) ? value.length : 0
}

function plural(count, singular, pluralForm) {
  return count === 1 ? singular : pluralForm
}

function stateOf(status, busy) {
  if (busy) return "busy"
  if (!status || status.linked !== true) return "unlinked"
  if (status.diverged === true) return "diverged"
  if (status.fetch_error) return "error"
  if (length(status.remote_changes) > 0) return "remote"
  if (length(status.local_changes) > 0) return "local"
  return "synced"
}

// Font Awesome codepoints; Nerd Fonts ships these in the bar font.
// The bar keeps a stable cloud identity; state is carried by color and dot.
function icon(state) {
  switch (state) {
    case "busy": return "\uf021"
    case "unlinked": return "\uf0c1"
    default: return "\uf0c2"
  }
}

function dotVisible(state) {
  return state === "local" || state === "remote" || state === "diverged" || state === "error"
}

function summary(status, busy) {
  if (busy) return "Working…"
  if (!status || status.linked !== true) return "No repo linked"
  if (status.diverged === true) return "Diverged — resolve in a terminal"
  if (status.fetch_error) return "Offline · " + status.fetch_error
  var local = length(status.local_changes)
  var remote = length(status.remote_changes)
  if (local > 0 && remote > 0) return local + " local · " + remote + " remote changes"
  if (local > 0) return local + " local " + plural(local, "change", "changes")
  if (remote > 0) return remote + " remote " + plural(remote, "change", "changes")
  return "In sync"
}

function tooltip(state) {
  switch (state) {
    case "busy": return "Omarchy Sync · working"
    case "unlinked": return "Omarchy Sync · link a repo"
    case "diverged": return "Omarchy Sync · diverged"
    case "error": return "Omarchy Sync · connection error"
    case "local": return "Omarchy Sync · changes to publish"
    case "remote": return "Omarchy Sync · changes to apply"
    default: return "Omarchy Sync · in sync"
  }
}

function statusTitle(state, local, remote) {
  if (state === "busy") return "Working…"
  if (state === "unlinked") return "No repo linked"
  if (state === "diverged") return "Diverged"
  if (state === "error") return "Connection error"
  if (local > 0 && remote > 0) return "Changes"
  if (local > 0) return local + " to publish"
  if (remote > 0) return remote + " to apply"
  return "In sync"
}

function statusChip(local, remote) {
  var parts = []
  if (local > 0) parts.push("↑" + local)
  if (remote > 0) parts.push("↓" + remote)
  return parts.join(" ")
}

function relativeTime(iso) {
  if (!iso) return ""
  var then = new Date(iso).getTime()
  if (!isFinite(then)) return ""
  var minutes = Math.floor(Math.max(0, Date.now() - then) / 60000)
  if (minutes < 1) return "just now"
  if (minutes < 60) return minutes + "m ago"
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return hours + "h ago"
  var days = Math.floor(hours / 24)
  if (days < 7) return days + "d ago"
  try {
    return Qt.formatDateTime(new Date(then), "MMM d")
  } catch (error) {
    return ""
  }
}

function footer(status, tick) {
  var text = "Not synced yet"
  if (status && status.last_sync) text = "Last: " + relativeTime(status.last_sync)
  var backups = (status && status.backups) || 0
  if (backups > 0) text += " · " + backups + " " + plural(backups, "backup", "backups")
  return text
}

function repoLine(status) {
  if (!status || !status.repo) return ""
  return status.repo + (status.branch ? " · " + status.branch : "")
}

function fileIcon(path) {
  var value = String(path || "")
  if (value === "plugins.json") return "\uf1e6"
  if (value === "omarchy/theme.name" || value.indexOf("omarchy/themes/") === 0) return "\uf1fc"
  if (value.indexOf("omarchy/shell") === 0) return "\uf120"
  if (value.indexOf("omarchy/hooks/") === 0) return "\uf0ad"
  if (value.indexOf("omarchy/extensions/") === 0) return "\uf0c1"
  if (value.indexOf("omarchy/branding/") === 0) return "\uf02b"
  if (value.indexOf("omarchy/agents/") === 0) return "\uf0c0"
  if (value.indexOf("hypr/") === 0) return "\uf0ad"
  if (value.indexOf("alacritty/") === 0 || value.indexOf("foot/") === 0 ||
      value.indexOf("ghostty/") === 0 || value.indexOf("kitty/") === 0) return "\uf120"
  return "\uf15b"
}

function rowsFor(paths, limit) {
  var rows = []
  if (!Array.isArray(paths)) return rows
  for (var i = 0; i < paths.length && i < limit; i++) {
    rows.push({ path: String(paths[i]), icon: fileIcon(paths[i]) })
  }
  return rows
}

function localRows(status, limit) {
  return rowsFor(status.local_changes, limit)
}

function remoteRows(status, limit) {
  return rowsFor(status.remote_changes, limit)
}

function moreCount(paths, limit) {
  return Math.max(0, length(paths) - limit)
}

// ---------------------------------------------------------------------------
// Settings view
// ---------------------------------------------------------------------------

var CATEGORIES = [
  { key: "hypr", section: "CONFIG", label: "Hyprland", detail: "hypr/*.lua, *.conf" },
  { key: "monitors", section: "CONFIG", label: "Display layout", detail: "monitors.lua (machine-specific)" },
  { key: "shell", section: "CONFIG", label: "Shell", detail: "shell.json, shell.toml" },
  { key: "themes", section: "CONFIG", label: "Themes", detail: "selected theme + overlays" },
  { key: "terminals", section: "CONFIG", label: "Terminals", detail: "alacritty, foot, ghostty, kitty" },
  { key: "hooks", section: "OMARCHY", label: "Hooks", detail: "omarchy/hooks/" },
  { key: "extensions", section: "OMARCHY", label: "Extensions", detail: "omarchy/extensions/" },
  { key: "branding", section: "OMARCHY", label: "Branding", detail: "omarchy/branding/" },
  { key: "agents", section: "OMARCHY", label: "Agents", detail: "omarchy/agents/" },
  { key: "plugins", section: "OMARCHY", label: "Plugins", detail: "plugins.json (git URLs)" }
]

function settingsRows() {
  var rows = []
  var section = ""
  for (var i = 0; i < CATEGORIES.length; i++) {
    var category = CATEGORIES[i]
    rows.push({
      key: category.key,
      label: category.label,
      detail: category.detail,
      header: category.section !== section ? category.section : ""
    })
    section = category.section
  }
  return rows
}

function categoryEnabled(status, key) {
  var settings = status ? status.settings : null
  var categories = settings ? settings.categories : null
  return !!(categories && categories[key] === true)
}

function enabledCount(status) {
  var count = 0
  for (var i = 0; i < CATEGORIES.length; i++) {
    if (categoryEnabled(status, CATEGORIES[i].key)) count++
  }
  return count
}

function refreshMinutes(status) {
  var settings = status ? status.settings : null
  var value = settings ? settings.refresh_minutes : null
  return (typeof value === "number" && value >= 0) ? value : 30
}

function refreshOptions() {
  return [
    { value: "30", label: "30m" },
    { value: "60", label: "1h" },
    { value: "180", label: "3h" },
    { value: "360", label: "6h" },
    { value: "0", label: "Off" }
  ]
}

function coffeeUrl() {
  return "https://buymeacoffee.com/kaplanhalil"
}
