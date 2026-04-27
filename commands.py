"""Slash command handlers for /codex (WebSocket variant).

Subcommands: list [--threads], models, model, reply, approve, deny, archive, help.
"""

from __future__ import annotations

import argparse
import logging
import shlex
from typing import Optional

from .bridge import CodexBridge

logger = logging.getLogger(__name__)

MAX_TASKS_DISPLAY = 20
MAX_PREVIEW_LENGTH = 60


class _CodexHelpRequested(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _CodexArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        raise _CodexHelpRequested(message or self.format_help())


def _build_parser() -> argparse.ArgumentParser:
    parser = _CodexArgumentParser(prog="/codex", add_help=True)
    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", add_help=True, help="list tasks or threads")
    list_p.add_argument("--threads", "-t", action="store_true")

    sub.add_parser("models", add_help=True, help="list available models")

    model_p = sub.add_parser("model", add_help=True, help="show or set default model")
    model_p.add_argument("model_id", nargs="?", help="set or show default model")

    reply_p = sub.add_parser("reply", add_help=True, help="send follow-up to a task")
    reply_p.add_argument("task_id")
    reply_p.add_argument("message", nargs=argparse.REMAINDER)

    for name in ("approve", "deny"):
        p = sub.add_parser(name, add_help=True, help=f"{name} a pending approval")
        p.add_argument("approval_key")

    archive_p = sub.add_parser("archive", add_help=True, help="archive tasks or threads")
    archive_p.add_argument("target", help="task_id, 'all', or 'allthreads'")

    plan_p = sub.add_parser("plan", add_help=True, help="show or toggle plan mode")
    plan_p.add_argument("toggle", nargs="?", help="'on' or 'off'; omit to query")

    verbose_p = sub.add_parser("verbose", add_help=True, help="show or toggle verbose mode")
    verbose_p.add_argument("toggle", nargs="?", help="'on' or 'off'; omit to query")

    help_p = sub.add_parser("help", add_help=True, help="show help")
    help_p.add_argument("topic", nargs="?", help="optional subcommand name")

    return parser


PARSER = _build_parser()


def _parse_args(raw: str) -> Optional[argparse.Namespace]:
    try:
        tokens = shlex.split(raw) if raw else []
    except ValueError:
        tokens = raw.split()
    if not tokens:
        return argparse.Namespace(command=None)
    try:
        return PARSER.parse_args(tokens)
    except _CodexHelpRequested as exc:
        return argparse.Namespace(command="__help__", help_text=exc.text)
    except (SystemExit, Exception):
        return None


def _cmd_help() -> str:
    return (
        "Usage:\n"
        "  `/codex` or `/codex list` — list this session's tasks\n"
        "  `/codex list --threads` — list all threads on the server\n"
        "  `/codex models` — list available models from app-server\n"
        "  `/codex model` — show current default model\n"
        "  `/codex model <model_id>` — set default model for future tasks\n"
        "  `/codex reply <task_id> <message>` — send follow-up to Codex\n"
        "  `/codex approve <key>` — approve a pending Codex command\n"
        "  `/codex deny <key>` — deny a pending Codex command\n"
        "  `/codex archive <task_id>` — archive a task thread\n"
        "  `/codex archive all` — archive this session's tasks\n"
        "  `/codex archive allthreads` — archive every thread on the server\n"
        "  `/codex plan on|off` — toggle plan collaboration mode for future turns\n"
        "  `/codex plan` — show current plan-mode state\n"
        "  `codex_revive` tool — restore a thread from a previous session (use via agent)"
    )


def _cmd_help_topic(topic: Optional[str]) -> str:
    if not topic:
        return _cmd_help()
    try:
        return PARSER.parse_args([topic, "--help"]).help_text
    except _CodexHelpRequested as exc:
        return exc.text.strip()
    except Exception:
        return f"Unknown help topic `{topic}`. Try `/codex --help`."


def _cmd_list(bridge: CodexBridge, show_threads: bool = False) -> str:
    if show_threads:
        return _list_threads(bridge)
    return _list_tasks(bridge)


def _cmd_models(bridge: CodexBridge) -> str:
    result = bridge.list_models()
    if not result.get("ok"):
        return f"Failed to list models: {result.get('error')}"

    models = result.get("data") or []
    if not models:
        return "No models returned by app-server."

    current = bridge.get_default_model()
    lines = ["Available models:"]
    for item in models:
        model_id = item.get("id") or item.get("model") or "?"
        display = item.get("displayName") or ""
        flags = []
        if item.get("isDefault"):
            flags.append("server default")
        if model_id == current or item.get("model") == current:
            flags.append("current")
        suffix = f" ({', '.join(flags)})" if flags else ""
        label = f" — {display}" if display and display != model_id else ""
        lines.append(f"  `{model_id}`{label}{suffix}")
    return "\n".join(lines)


def _cmd_model(bridge: CodexBridge, model_id: Optional[str]) -> str:
    started = bridge.ensure_started()
    if not started.get("ok"):
        return f"Failed: {started.get('error')}"
    if not model_id:
        return f"Default model is `{bridge.get_default_model()}`."

    result = bridge.set_default_model(model_id)
    if not result.get("ok"):
        return f"Failed: {result.get('error')}"
    return f"Default model set to `{result['model']}`."


def _list_tasks(bridge: CodexBridge) -> str:
    task_map = bridge._task_map
    if not task_map:
        return "No Codex tasks in this session."
    lines = ["Codex tasks:"]
    for task_id in list(task_map)[:MAX_TASKS_DISPLAY]:
        lines.append(f"  `{task_id}`")
    lines.append("\nReply: `/codex reply <task_id> <message>`")
    return "\n".join(lines)


def _list_threads(bridge: CodexBridge) -> str:
    try:
        result = bridge.list_tasks()
        threads = (result or {}).get("data", [])
    except Exception as exc:
        return f"Failed to list threads: {exc}"
    if not threads:
        return "No threads on server."
    lines = ["Codex threads:"]
    for t in threads[:MAX_TASKS_DISPLAY]:
        tid = t.get("id", "?")
        cwd = t.get("cwd", "?")
        preview = (t.get("preview") or "").replace("\n", " ")[:MAX_PREVIEW_LENGTH]
        lines.append(f"  `{tid}` — `{cwd}` {preview}")
    return "\n".join(lines)


def _cmd_approve(bridge: CodexBridge, key: str) -> str:
    result = bridge.resolve_approval(key, "accept")
    if result.get("ok"):
        return f"Approved `{key}`."
    return f"Failed: {result.get('error')}"


def _cmd_deny(bridge: CodexBridge, key: str) -> str:
    result = bridge.resolve_approval(key, "decline")
    if result.get("ok"):
        return f"Denied `{key}`."
    return f"Failed: {result.get('error')}"


def _cmd_archive(bridge: CodexBridge, target: str) -> str:
    if target == "allthreads":
        result = bridge.archive_all_threads()
        if result.get("ok"):
            return f"Archived {result['removed']} threads."
        return f"Archived {result['removed']}, failed: {', '.join(result['errors'])}"
    if target == "all":
        result = bridge.remove_all_tasks()
        if result.get("ok"):
            return f"Archived {result['removed']} tasks."
        return f"Archived {result['removed']}, failed: {', '.join(result['errors'])}"
    result = bridge.remove_task(target)
    if result.get("ok"):
        return f"Task `{target}` archived."
    return f"Failed: {result.get('error')}"


def _cmd_plan(bridge: CodexBridge, toggle: Optional[str]) -> str:
    if toggle is None:
        state = "on" if bridge.plan_mode() else "off"
        return f"Plan mode is `{state}`."
    normalized = toggle.strip().lower()
    if normalized in ("on", "true", "1", "enable", "enabled"):
        bridge.set_plan_mode(True)
        return "Plan mode `on` — future turns will use collaborationMode=plan."
    if normalized in ("off", "false", "0", "disable", "disabled"):
        bridge.set_plan_mode(False)
        return "Plan mode `off` — future turns will use collaborationMode=default."
    return f"Unknown toggle `{toggle}`. Use `/codex plan on` or `/codex plan off`."


def _cmd_verbose(bridge: CodexBridge, toggle: Optional[str]) -> str:
    if toggle is None:
        state = "on" if bridge.verbose_mode() else "off"
        return f"Verbose mode is `{state}`."
    normalized = toggle.strip().lower()
    if normalized in ("on", "true", "1", "enable", "enabled"):
        bridge.set_verbose_mode(True)
        return "Verbose mode `on` — item/completed notifications will be shown."
    if normalized in ("off", "false", "0", "disable", "disabled"):
        bridge.set_verbose_mode(False)
        return "Verbose mode `off` — only turn/completed notifications will be shown."
    return f"Unknown toggle `{toggle}`. Use `/codex verbose on` or `/codex verbose off`."


def _cmd_reply(bridge: CodexBridge, ns: argparse.Namespace) -> str:
    task_id = ns.task_id
    message = " ".join(ns.message).strip() if ns.message else ""
    if not message:
        return "Missing message. Usage: `/codex reply <task_id> <message>`"
    try:
        result = bridge.send_reply(task_id, message)
    except Exception as exc:
        logger.exception("codex /reply failed")
        return f"Failed to send reply: {exc}"
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    return f"Message sent to Codex task `{task_id}`, waiting for reply..."


def handle_slash(raw_args: str) -> str:
    bridge = CodexBridge.instance()
    ns = _parse_args(raw_args or "")

    if ns is None or ns.command is None:
        return _cmd_list(bridge)

    if ns.command == "__help__":
        return (ns.help_text or _cmd_help()).strip()

    if ns.command == "help":
        return _cmd_help_topic(getattr(ns, "topic", None))

    if ns.command == "list":
        return _cmd_list(bridge, show_threads=ns.threads)

    if ns.command == "models":
        return _cmd_models(bridge)

    if ns.command == "model":
        return _cmd_model(bridge, ns.model_id)

    if ns.command == "approve":
        return _cmd_approve(bridge, ns.approval_key)

    if ns.command == "deny":
        return _cmd_deny(bridge, ns.approval_key)

    if ns.command == "archive":
        return _cmd_archive(bridge, ns.target)

    if ns.command == "plan":
        return _cmd_plan(bridge, ns.toggle)

    if ns.command == "verbose":
        return _cmd_verbose(bridge, ns.toggle)

    if ns.command == "reply":
        return _cmd_reply(bridge, ns)

    return f"Unknown subcommand `{ns.command}`. Try `/codex help`."
