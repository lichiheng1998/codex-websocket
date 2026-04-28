"""Push user-facing messages to whichever chat platform the task is bound to.

The bridge routes every progress/approval/completion message through
``notify_user``. This module owns the platform-name → ``Platform`` enum
mapping and the dynamic ``gateway`` / ``tools.send_message_tool`` imports
(those modules are part of the hermes runtime and may be absent in unit
tests / standalone use, so failures are caught and downgraded to a log
warning).

``report_failure`` is the bridge's standard "task failed at stage X with
detail Y" helper — kept here because its only side effect is one
notify call.
"""

from __future__ import annotations

import logging
from typing import Optional

from .state import TaskTarget

logger = logging.getLogger(__name__)


async def notify_user(target: Optional[TaskTarget], message: str) -> None:
    """Best-effort push to the chat platform identified by ``target``.

    Returns silently on every failure path; callers are background driver
    coroutines that have nowhere to surface the error to anyway.
    """
    if target is None or not target.platform or not target.chat_id:
        logger.info("codex notify (no target): %s", message[:200])
        return
    try:
        from gateway.config import load_gateway_config, Platform
        from tools.send_message_tool import _send_to_platform

        platform_map = {
            "telegram": Platform.TELEGRAM, "discord": Platform.DISCORD,
            "slack": Platform.SLACK, "whatsapp": Platform.WHATSAPP,
            "signal": Platform.SIGNAL, "bluebubbles": Platform.BLUEBUBBLES,
            "qqbot": Platform.QQBOT, "matrix": Platform.MATRIX,
            "mattermost": Platform.MATTERMOST,
            "homeassistant": Platform.HOMEASSISTANT,
            "dingtalk": Platform.DINGTALK, "feishu": Platform.FEISHU,
            "wecom": Platform.WECOM, "weixin": Platform.WEIXIN,
            "email": Platform.EMAIL, "sms": Platform.SMS,
        }
        platform = platform_map.get(target.platform.lower())
        if platform is None:
            logger.warning("codex notify: unknown platform %r", target.platform)
            return

        cfg = load_gateway_config()
        pconfig = cfg.platforms.get(platform)
        if pconfig is None:
            logger.warning("codex notify: platform %s not configured", platform)
            return

        await _send_to_platform(
            platform, pconfig, target.chat_id, message,
            thread_id=target.thread_id or None,
        )
    except Exception as exc:
        logger.warning("codex notify failed: %s", exc)


async def report_failure(
    target: Optional[TaskTarget], task_id: str, stage: str, detail: str,
) -> None:
    """Fire-and-forget error reporter for background driver coroutines."""
    logger.warning("codex task %s failed at %s: %s", task_id, stage, detail)
    await notify_user(target, f"❌ Codex task `{task_id}` {stage}: {detail}")
