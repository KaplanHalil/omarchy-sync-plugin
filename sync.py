#!/usr/bin/env python3
"""Omarchy Sync backend.

One small CLI used by Panel.qml. Every command prints a single JSON object to
stdout, so the QML side only has to render state and call actions.

Layout synced into the repo:

    <repo>/
      .omarchy-sync.json        marker/format
      config/                   -> ~/.config/ (whitelist below)
      plugins.json              {"version":1,"plugins":{"<id>":"<git-url>"}}

Commands: status | link | publish | apply | undo
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

PLUGIN_ID = "io.github.kaplanhalil.sync"
DEFAULT_BRANCH = "main"
MARKER = ".omarchy-sync.json"
PLUGIN_LIST = "plugins.json"

HOME = Path(os.environ.get("OMARCHY_SYNC_HOME", str(Path.home()))).expanduser()
CONFIG = HOME / ".config"
DATA = HOME / ".local/share/omarchy-sync"
STATE = HOME / ".local/state/omarchy-sync"
SETTINGS_FILE = DATA / "settings.json"
BACKUP_ROOT = STATE / "backups"

# Files under ~/.config that this plugin mirrors. Everything else stays local.
OMARCHY_DIRS = {"hooks", "extensions", "branding", "agents"}
TERMINAL_DIRS = {"alacritty", "foot", "ghostty", "kitty"}
THEME_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".avif", ".heic", ".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache"}

# What the settings view can switch on and off. Machine-local: each machine
# decides which slice of the repo it publishes and applies.
CATEGORY_DEFAULTS = {
    "hypr": True,
    "monitors": False,
    "shell": True,
    "themes": True,
    "terminals": True,
    "hooks": True,
    "extensions": True,
    "branding": True,
    "agents": True,
    "plugins": True,
}

# How often the bar checks the remote for changes. 0 disables the timer.
DEFAULT_REFRESH_MINUTES = 30
REFRESH_CHOICES = (0, 30, 60, 180, 360)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fail(message: str, status=None, **extra) -> int:
    payload = {"ok": False, "error": message}
    if status is not None:
        payload["status"] = status
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def ok(status=None, **extra) -> int:
    payload = {"ok": True}
    if status is not None:
        payload["status"] = status
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def short(text: str, limit: int = 400) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    value = lines[-1] if lines else ""
    return value[:limit]


def run(cmd, cwd=None, timeout=60, env_extra=None):
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"})
    if env_extra:
        env.update(env_extra)
    try:
        return subprocess.run(
            [str(part) for part in cmd],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"command timed out: {cmd[0]}")
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(cmd, 127, "", str(error))


def git(*args, cwd=None, timeout=60):
    return run(["git", *args], cwd=cwd, timeout=timeout)


# --------------------------------------------------------------------------
# settings + lock
# --------------------------------------------------------------------------


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(settings: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def repo_dir(settings: dict) -> Path:
    value = settings.get("repo_dir")
    return Path(value).expanduser() if value else DATA / "repo"


@contextmanager
def locked():
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "lock", "w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another sync operation is still running")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# whitelist
# --------------------------------------------------------------------------


def category_of(rel: str):
    """Map a config-relative path to a sync category, or None."""
    parts = PurePosixPath(rel).parts
    if not parts:
        return None
    top = parts[0]
    suffix = PurePosixPath(rel).suffix.lower()
    if top == "hypr":
        if rel == "hypr/monitors.lua":
            return "monitors"
        return "hypr" if suffix in {".lua", ".conf"} else None
    if top == "omarchy":
        if len(parts) == 2 and parts[1] in {"shell.json", "shell.toml"}:
            return "shell"
        if len(parts) >= 3:
            if parts[1] == "themes":
                return "themes" if suffix not in THEME_SKIP_SUFFIXES else None
            if parts[1] in OMARCHY_DIRS:
                return parts[1]
        return None
    if top in TERMINAL_DIRS:
        return "terminals"
    return None


def categories(settings: dict) -> dict:
    stored = settings.get("categories")
    merged = dict(CATEGORY_DEFAULTS)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in merged and isinstance(value, bool):
                merged[key] = value
    return merged


def refresh_minutes(settings: dict) -> int:
    value = settings.get("refresh_minutes")
    if isinstance(value, int) and not isinstance(value, bool) and value in REFRESH_CHOICES:
        return value
    return DEFAULT_REFRESH_MINUTES


def settings_payload(settings: dict) -> dict:
    return {
        "categories": categories(settings),
        "refresh_minutes": refresh_minutes(settings),
    }


def offered(rel: str, cats: dict) -> bool:
    if any(part in SKIP_DIRS or ".bak" in part for part in PurePosixPath(rel).parts):
        return False
    category = category_of(rel)
    return bool(category) and bool(cats.get(category, False))


def scan_config(root: Path, cats: dict) -> dict:
    """Return {relative path under config/: absolute machine path}."""
    found = {}
    for top in ("hypr", "omarchy", *sorted(TERMINAL_DIRS)):
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                if offered(rel, cats) and path.is_file():
                    found[rel] = path
    return found


def same_file(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".sync-tmp")
    shutil.copyfile(source, tmp)
    os.chmod(tmp, source.stat().st_mode & 0o777)
    os.replace(tmp, target)


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    # prune now-empty directories, but never the whitelisted root itself
    parent = path.parent
    for _ in range(6):
        if not parent.is_dir():
            break
        try:
            next(parent.iterdir())
            break
        except StopIteration:
            if parent == CONFIG:
                break
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break


def pretty(rel: str) -> str:
    return rel[len("config/"):] if rel.startswith("config/") else rel


# --------------------------------------------------------------------------
# plugin list
# --------------------------------------------------------------------------


def plugin_map() -> dict:
    base = CONFIG / "omarchy" / "plugins"
    plugins = {}
    if not base.is_dir():
        return plugins
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name == PLUGIN_ID:
            continue
        if not (entry / ".git").exists():
            continue
        result = git("remote", "get-url", "origin", cwd=entry, timeout=10)
        url = result.stdout.strip()
        if result.returncode == 0 and url:
            plugins[entry.name] = url
    return plugins


def plugin_list_payload() -> dict:
    return {"version": 1, "plugins": plugin_map()}


def read_plugin_list(repo: Path) -> dict:
    try:
        data = json.loads((repo / PLUGIN_LIST).read_text(encoding="utf-8"))
        plugins = data.get("plugins")
        return plugins if isinstance(plugins, dict) else {}
    except Exception:
        return {}


def current_theme() -> str:
    for candidate in (
        HOME / ".local/state/omarchy/current/theme.name",
        HOME / ".local/state/omarchy/theme.name",
    ):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    link = HOME / ".local/state/omarchy/current/theme"
    try:
        if link.exists():
            return link.resolve().name
    except OSError:
        pass
    return ""


def repo_theme(repo: Path) -> str:
    try:
        return (repo / "config" / "omarchy" / "theme.name").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def has_commit(repo: Path) -> bool:
    return git("rev-parse", "--verify", "HEAD", cwd=repo, timeout=10).returncode == 0


def head_commit(repo: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo, timeout=10).stdout.strip()


def upstream_counts(repo: Path, branch: str) -> tuple:
    upstream = f"origin/{branch}"
    ref = f"refs/remotes/{upstream}"
    if git("rev-parse", "--verify", "--quiet", ref, cwd=repo, timeout=10).returncode != 0:
        return 0, 0
    if not has_commit(repo):
        result = git("rev-list", "--count", upstream, cwd=repo)
        try:
            return int(result.stdout.strip() or 0), 0
        except ValueError:
            return 0, 0
    result = git("rev-list", "--left-right", "--count", f"{upstream}...HEAD", cwd=repo)
    if result.returncode != 0:
        return 0, 0
    try:
        behind, ahead = (int(part) for part in result.stdout.split()[:2])
        return behind, ahead
    except ValueError:
        return 0, 0


def count_backups() -> int:
    if not BACKUP_ROOT.is_dir():
        return 0
    return sum(1 for entry in BACKUP_ROOT.iterdir() if entry.is_dir() and not entry.name.endswith(".undone"))


def repo_label(settings: dict) -> str:
    url = str(settings.get("repo_url") or settings.get("repo_dir") or "")
    match = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)
    return url


def path_offered(rel: str, cats: dict) -> bool:
    if rel == PLUGIN_LIST:
        return bool(cats.get("plugins"))
    if rel == "omarchy/theme.name":
        return bool(cats.get("themes"))
    category = category_of(rel)
    return bool(category) and bool(cats.get(category, False))


def build_status(settings: dict, fetch: bool = False) -> dict:
    repo = repo_dir(settings)
    cats = categories(settings)
    linked = repo.is_dir() and (repo / ".git").exists() and bool(settings.get("repo_url") or settings.get("repo_dir"))
    status = {
        "linked": bool(linked),
        "repo": repo_label(settings),
        "branch": settings.get("branch", DEFAULT_BRANCH),
        "host": socket.gethostname(),
        "local_changes": [],
        "remote_changes": [],
        "ahead": 0,
        "behind": 0,
        "diverged": False,
        "repo_dirty": False,
        "fetch_error": "",
        "last_sync": settings.get("last_sync", ""),
        "backups": count_backups(),
        "settings": settings_payload(settings),
    }
    if not linked:
        return status

    branch = status["branch"]
    if fetch:
        result = git("fetch", "--prune", "origin", cwd=repo, timeout=90)
        if result.returncode != 0:
            status["fetch_error"] = short(result.stderr)

    behind, ahead = upstream_counts(repo, branch)
    status["behind"], status["ahead"] = behind, ahead
    status["diverged"] = behind > 0 and ahead > 0

    upstream = f"origin/{branch}"
    if git("rev-parse", "--verify", "--quiet", f"refs/remotes/{upstream}", cwd=repo, timeout=10).returncode == 0:
        result = git("diff", "--name-only", f"HEAD..{upstream}", cwd=repo)
        if result.returncode == 0:
            status["remote_changes"] = [
                pretty(path) for path in result.stdout.splitlines()
                if path and not path.endswith(MARKER) and path_offered(pretty(path), cats)
            ]

    machine = scan_config(CONFIG, cats)
    in_repo = scan_config(repo / "config", cats)
    changes = set()
    for rel, machine_path in machine.items():
        repo_path = in_repo.get(rel)
        if repo_path is None or not same_file(machine_path, repo_path):
            changes.add(rel)
    for rel in in_repo:
        if rel not in machine:
            changes.add(rel)

    if cats.get("plugins") and plugin_list_payload() != {"version": 1, "plugins": read_plugin_list(repo)}:
        changes.add(PLUGIN_LIST)

    if cats.get("themes") and current_theme() != repo_theme(repo):
        changes.add("omarchy/theme.name")

    dirty = git("status", "--porcelain", cwd=repo, timeout=15)
    status["repo_dirty"] = dirty.returncode == 0 and bool(dirty.stdout.strip())
    if status["repo_dirty"]:
        for line in dirty.stdout.splitlines():
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path and path != MARKER and path_offered(pretty(path), cats):
                changes.add(pretty(path))

    status["local_changes"] = sorted(changes)
    return status


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def normalize_target(target: str) -> str:
    value = target.strip()
    expanded = Path(value).expanduser()
    if expanded.exists():
        return str(expanded.resolve())
    if re.match(r"^[\w.-]+/[\w.-]+$", value):
        return f"https://github.com/{value}.git"
    if value.startswith(("https://", "http://", "ssh://", "git@", "file://")):
        return value
    return value


def cmd_link(settings: dict, target: str) -> int:
    if not target.strip():
        return fail("Repo address is empty")

    value = normalize_target(target)
    repo = repo_dir(settings)
    local = Path(value)

    if local.exists() and (local / ".git").exists():
        repo = local.resolve()
    elif local.exists() and (local / "HEAD").exists() and (local / "objects").is_dir():
        # bare repo: clone into the state dir
        if repo.exists() and any(repo.iterdir()) and not (repo / ".git").exists():
            return fail(f"Target directory is not empty: {repo}")
        if not (repo / ".git").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            result = git("clone", str(local), str(repo), timeout=300)
            if result.returncode != 0:
                return fail(f"Could not clone: {short(result.stderr)}")
    else:
        if repo.exists() and any(repo.iterdir()) and not (repo / ".git").exists():
            return fail(f"Target directory is not empty: {repo}")
        if not (repo / ".git").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            result = git("clone", value, str(repo), timeout=300)
            if result.returncode != 0:
                return fail(f"Could not clone: {short(result.stderr)}")

    branch = DEFAULT_BRANCH
    if has_commit(repo):
        result = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, timeout=10)
        if result.returncode == 0 and "/" in result.stdout.strip():
            branch = result.stdout.strip().split("/", 1)[1]
        else:
            current = git("symbolic-ref", "--short", "HEAD", cwd=repo, timeout=10).stdout.strip()
            branch = current or DEFAULT_BRANCH
    else:
        # A bare repo whose HEAD points at a branch that was never pushed
        # clones to an unborn checkout. Pick a real remote branch first.
        branch = ""
        for candidate in (DEFAULT_BRANCH, "master"):
            ref = f"refs/remotes/origin/{candidate}"
            if git("rev-parse", "--verify", "--quiet", ref, cwd=repo, timeout=10).returncode == 0:
                branch = candidate
                break
        if branch == "":
            listing = git(
                "for-each-ref", "--format=%(refname:strip=3)",
                "refs/remotes/origin", cwd=repo, timeout=10,
            )
            names = [
                name for name in listing.stdout.split()
                if name and name != "HEAD"
            ]
            branch = names[0] if names else DEFAULT_BRANCH
        if git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=repo, timeout=10).returncode == 0:
            git("checkout", "-B", branch, f"origin/{branch}", cwd=repo, timeout=10)
        else:
            git("checkout", "-B", branch, cwd=repo, timeout=10)

    settings.update(
        repo_url=value,
        repo_dir=str(repo),
        branch=branch,
        linked_at=now_iso(),
    )
    save_settings(settings)
    return ok(build_status(settings, fetch=True), message="Repo linked")


def cmd_publish(settings: dict) -> int:
    repo = repo_dir(settings)
    branch = settings.get("branch", DEFAULT_BRANCH)
    if not (repo / ".git").exists():
        return fail("Link a repo first", build_status(settings))

    git("fetch", "--prune", "origin", cwd=repo, timeout=90)
    behind, _ = upstream_counts(repo, branch)
    if behind > 0:
        return fail("Remote has new commits; Apply first", build_status(settings))

    cats = categories(settings)
    machine = scan_config(CONFIG, cats)
    in_repo = scan_config(repo / "config", cats)
    written = deleted = 0

    for rel, machine_path in machine.items():
        repo_path = repo / "config" / rel
        if not repo_path.exists() or not same_file(machine_path, repo_path):
            copy_file(machine_path, repo_path)
            written += 1

    for rel in sorted(set(in_repo) - set(machine)):
        remove_file(repo / "config" / rel)
        deleted += 1

    marker = repo / MARKER
    marker.write_text(
        json.dumps({"format": "omarchy-sync", "version": 1, "updated": now_iso()}, indent=2) + "\n",
        encoding="utf-8",
    )

    cats = categories(settings)
    if cats.get("plugins"):
        plugins = plugin_list_payload()
        plugin_file = repo / PLUGIN_LIST
        new_plugin_text = json.dumps(plugins, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if not plugin_file.exists() or plugin_file.read_text(encoding="utf-8") != new_plugin_text:
            plugin_file.write_text(new_plugin_text, encoding="utf-8")

    theme = current_theme() if cats.get("themes") else ""
    if theme:
        theme_file = repo / "config" / "omarchy" / "theme.name"
        theme_text = theme + "\n"
        if not theme_file.exists() or theme_file.read_text(encoding="utf-8") != theme_text:
            theme_file.parent.mkdir(parents=True, exist_ok=True)
            theme_file.write_text(theme_text, encoding="utf-8")
            written += 1

    git("add", "-A", cwd=repo)
    staged = git("diff", "--cached", "--quiet", cwd=repo)
    if staged.returncode == 0:
        settings["last_sync"] = now_iso()
        save_settings(settings)
        return ok(build_status(settings), changed=0, pushed=True, message="No changes")

    name = git("config", "user.name", cwd=repo).stdout.strip() or "Omarchy Sync"
    email = git("config", "user.email", cwd=repo).stdout.strip() or "omarchy-sync@localhost"
    message = f"Sync from {socket.gethostname()} {now_iso()}"
    result = git(
        "-c", f"user.name={name}", "-c", f"user.email={email}",
        "commit", "-m", message, cwd=repo, timeout=60,
    )
    if result.returncode != 0:
        return fail(f"Commit failed: {short(result.stderr)}", build_status(settings))

    commit = head_commit(repo)
    push = git("push", "-u", "origin", branch, cwd=repo, timeout=180)
    settings.update(last_sync=now_iso(), last_commit=commit)
    save_settings(settings)

    if push.returncode != 0:
        return fail(
            f"Push failed: {short(push.stderr)} (the commit is local; try again)",
            build_status(settings),
            changed=written + deleted,
            commit=commit,
            pushed=False,
        )

    return ok(
        build_status(settings),
        changed=written + deleted,
        commit=commit,
        pushed=True,
        message=f"{written + deleted} files published",
    )


def backup_path_for(timestamp: str) -> Path:
    path = BACKUP_ROOT / timestamp
    suffix = 1
    while path.exists():
        suffix += 1
        path = BACKUP_ROOT / f"{timestamp}-{suffix}"
    return path


def install_plugins(repo: Path) -> list:
    if os.environ.get("OMARCHY_SYNC_NO_INSTALL") == "1":
        return []
    wanted = read_plugin_list(repo)
    base = CONFIG / "omarchy" / "plugins"
    installed = []
    for plugin_id, url in sorted(wanted.items()):
        if plugin_id == PLUGIN_ID or (base / plugin_id).exists():
            continue
        result = run(["omarchy", "plugin", "add", url, "--yes"], timeout=300)
        installed.append({
            "id": plugin_id,
            "ok": result.returncode == 0,
            "error": "" if result.returncode == 0 else short(result.stderr),
        })
    return installed


def reload_after(changed: set, plugins_installed: list, theme_override: str = "") -> list:
    warnings = []
    if os.environ.get("OMARCHY_SYNC_NO_RELOAD") == "1":
        return warnings
    if any(rel.startswith("hypr/") for rel in changed):
        run(["hyprctl", "reload"], timeout=15)

    theme = theme_override
    if not theme and "omarchy/theme.name" in changed:
        try:
            theme = (CONFIG / "omarchy/theme.name").read_text(encoding="utf-8").strip()
        except OSError:
            theme = ""
    if theme:
        result = run(["omarchy", "theme", "set", theme], timeout=180)
        if result.returncode != 0:
            warnings.append(f"Theme could not be applied: {theme}")

    # Rescanning reloads bar widgets and closes any open panel, so only do it
    # when plugin directories actually changed. shell.json/shell.toml are
    # watched by the shell and hot-reload on their own.
    if plugins_installed:
        run(["omarchy-shell", "shell", "rescanPlugins"], timeout=15)
    return warnings


def cmd_apply(settings: dict) -> int:
    repo = repo_dir(settings)
    branch = settings.get("branch", DEFAULT_BRANCH)
    if not (repo / ".git").exists():
        return fail("Link a repo first", build_status(settings))

    fetch = git("fetch", "--prune", "origin", cwd=repo, timeout=120)
    if fetch.returncode != 0:
        return fail(f"Fetch failed: {short(fetch.stderr)}", build_status(settings))

    behind, ahead = upstream_counts(repo, branch)
    if behind > 0 and ahead > 0:
        return fail(
            "Repo diverged (local and remote have different commits). "
            "Resolve the divergence in a terminal: " + str(repo),
            build_status(settings),
        )

    if behind > 0:
        dirty = git("status", "--porcelain", cwd=repo, timeout=15)
        if dirty.returncode == 0 and dirty.stdout.strip():
            return fail("Repo working tree is dirty; Publish first", build_status(settings))

        merge = git("merge", "--ff-only", f"origin/{branch}", cwd=repo, timeout=60)
        if merge.returncode != 0:
            return fail(f"Could not update: {short(merge.stderr)}", build_status(settings))

    cats = categories(settings)
    machine = scan_config(CONFIG, cats)
    in_repo = scan_config(repo / "config", cats)

    to_write = {}
    for rel, repo_path in in_repo.items():
        machine_path = machine.get(rel)
        if machine_path is None or not same_file(machine_path, repo_path):
            to_write[rel] = repo_path
    to_delete = {rel: path for rel, path in machine.items() if rel not in in_repo}

    backup_files = {}
    for rel in to_write:
        backup_files[rel] = machine.get(rel)
    for rel, machine_path in to_delete.items():
        backup_files[rel] = machine_path

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_path_for(timestamp)
    manifest = {
        "timestamp": now_iso(),
        "commit": head_commit(repo),
        "branch": branch,
        "theme": current_theme(),
        "files": {},
    }
    for rel, machine_path in backup_files.items():
        existed = bool(machine_path and machine_path.is_file())
        if existed:
            copy_file(machine_path, backup / "config" / rel)
        manifest["files"]["config/" + rel] = {"existed": existed}

    if manifest["files"]:
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    for rel, repo_path in to_write.items():
        copy_file(repo_path, CONFIG / rel)
    for rel, machine_path in to_delete.items():
        remove_file(machine_path)

    plugins_installed = install_plugins(repo) if cats.get("plugins") else []
    target_theme = repo_theme(repo) if cats.get("themes") else ""
    changed = set(to_write) | set(to_delete)
    theme_changed = bool(target_theme) and target_theme != current_theme()
    if theme_changed:
        changed.add("omarchy/theme.name")
    warnings = reload_after(
        changed, plugins_installed,
        target_theme if theme_changed else "",
    )

    settings.update(last_sync=now_iso(), last_commit=head_commit(repo))
    save_settings(settings)
    changed_count = len(to_write) + len(to_delete)
    return ok(
        build_status(settings),
        applied=changed_count,
        backup=str(backup) if manifest["files"] else "",
        plugins=plugins_installed,
        warnings=warnings,
        message=f"{changed_count} files applied" if changed_count else "Already up to date",
    )


def cmd_undo(settings: dict) -> int:
    if not BACKUP_ROOT.is_dir():
        return fail("No backup to restore", build_status(settings))
    backups = sorted(
        (entry for entry in BACKUP_ROOT.iterdir() if entry.is_dir() and not entry.name.endswith(".undone")),
        reverse=True,
    )
    if not backups:
        return fail("No backup to restore", build_status(settings))

    backup = backups[0]
    try:
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return fail(f"Could not read backup: {backup.name}", build_status(settings))

    restored = 0
    changed = set()
    for repo_rel, meta in (manifest.get("files") or {}).items():
        rel = pretty(repo_rel)
        target = CONFIG / rel
        changed.add(rel)
        if meta.get("existed"):
            source = backup / repo_rel
            if source.is_file():
                copy_file(source, target)
                restored += 1
        else:
            remove_file(target)

    restore_theme = str(manifest.get("theme") or "")
    theme_changed = bool(restore_theme) and restore_theme != current_theme()
    if theme_changed:
        changed.add("omarchy/theme.name")
    warnings = reload_after(changed, [], restore_theme if theme_changed else "")
    backup.rename(backup.with_name(backup.name + ".undone"))
    settings["last_sync"] = now_iso()
    save_settings(settings)
    return ok(
        build_status(settings),
        restored=restored,
        backup=str(backup),
        warnings=warnings,
        message="Backup restored",
    )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def cmd_settings(settings: dict, key: str, value: str) -> int:
    cats = categories(settings)
    if key == "reset":
        settings["categories"] = dict(CATEGORY_DEFAULTS)
        settings["refresh_minutes"] = DEFAULT_REFRESH_MINUTES
    elif key == "all":
        if value not in {"true", "false"}:
            return fail("Value must be true or false", build_status(settings))
        settings["categories"] = {name: value == "true" for name in CATEGORY_DEFAULTS}
    elif key == "refresh":
        try:
            minutes = int(value)
        except ValueError:
            return fail("Refresh must be a whole number of minutes", build_status(settings))
        if minutes not in REFRESH_CHOICES:
            return fail(
                "Refresh must be 30, 60, 180, 360 minutes, or 0 for off",
                build_status(settings),
            )
        settings["refresh_minutes"] = minutes
    else:
        if key not in CATEGORY_DEFAULTS:
            return fail(f"Unknown category: {key}", build_status(settings))
        if value not in {"true", "false"}:
            return fail("Value must be true or false", build_status(settings))
        cats[key] = value == "true"
        settings["categories"] = cats
    save_settings(settings)
    return ok(build_status(settings), message="Settings saved")


def main() -> int:
    parser = argparse.ArgumentParser(description="Omarchy Sync backend")
    parser.add_argument("command", choices=["status", "link", "publish", "apply", "undo", "settings"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    try:
        if args.command == "status":
            return ok(build_status(settings, fetch=args.fetch))
        with locked():
            if args.command == "link":
                return cmd_link(settings, args.args[0] if args.args else "")
            if args.command == "publish":
                return cmd_publish(settings)
            if args.command == "apply":
                return cmd_apply(settings)
            if args.command == "undo":
                return cmd_undo(settings)
            if args.command == "settings":
                if not args.args:
                    return ok(build_status(settings), message="")
                value = args.args[1] if len(args.args) > 1 else ""
                return cmd_settings(settings, args.args[0], value)
    except RuntimeError as error:
        return fail(str(error), build_status(settings))
    except Exception as error:  # keep the panel alive on unexpected failures
        return fail(f"Unexpected error: {error}", build_status(settings))
    return fail("Unknown command", build_status(settings))


if __name__ == "__main__":
    sys.exit(main())
