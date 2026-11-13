"""关键提醒（任务异常 / 长时间等待 / 长时间运行）的邮件投递。

和 ``try_send_notification`` 的区别：

* 不受「通知总开关」影响，但**受「设置 → 通知 → 邮件」开关控制**：
  关掉那个开关即可一键静默所有提醒邮件；
* 内置节流（默认同一类提醒 15 分钟内只发一次），避免任务重试或循环里刷屏；
* 可以选择后台线程发送，SMTP 超时（最长 15 秒）不会卡住任务主流程。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from SRACore.util.logger import logger

DEFAULT_DEDUP_SECONDS = 900

_recent: dict[str, float] = {}
_lock = threading.Lock()


def _should_send(key: str, ttl: int) -> bool:
    """按 key 做时间窗节流；返回 True 表示这次真的需要发送。"""
    now = time.monotonic()
    with _lock:
        last = _recent.get(key)
        if last is not None and now - last < ttl:
            return False
        _recent[key] = now
        if len(_recent) > 256:  # 顺带清理过期键，避免长期运行无限增长
            for k, ts in list(_recent.items()):
                if now - ts > ttl:
                    _recent.pop(k, None)
    return True


def reset_throttle(key: str | None = None) -> None:
    """清空节流记录（key 为 None 时清空全部），主要给测试与人工重试用。"""
    with _lock:
        if key is None:
            _recent.clear()
        else:
            _recent.pop(key, None)


def _is_mail_enabled(config: Any) -> bool:
    """邮件提醒总开关（设置 → 通知 → 邮件 → 邮件通知复选框）。

    关闭时所有提醒邮件都不发；属性缺失视为关闭，避免新版本默认偷偷发信。
    """
    return bool(getattr(config, "isEmailEnabled", False))


def _is_mail_configured(config: Any) -> bool:
    return bool(
        getattr(config, "smtpServer", "")
        and getattr(config, "smtpSender", "")
        and getattr(config, "smtpReceiver", "")
        and getattr(config, "EncryptedSmtpAuthCode", "")
    )


def send_alert_email(
    settings: Any,
    title: str,
    message: str,
    *,
    result: str = "failed",
    dedup_key: str | None = None,
    dedup_seconds: int = DEFAULT_DEDUP_SECONDS,
    async_send: bool = True,
) -> bool:
    """发送关键提醒邮件。

    :param settings: ``AppSettings`` 或 ``NotificationSettings``
    :param result: 写进邮件正文的结果文案（failed / timeout / running 等）
    :return: True 表示已发送（或已提交后台线程）；开关关闭、配置不全或被节流时返回 False
    """
    config = getattr(settings, "Notification", settings)
    if not _is_mail_enabled(config):
        logger.debug(f"邮件提醒已关闭，跳过提醒：{title}（可在 设置 → 通知 → 邮件 打开）")
        return False
    if not _is_mail_configured(config):
        logger.warning(f"提醒邮件未发送（{title}）：SMTP 配置不完整，请到 设置 → 通知 → 邮件 填写")
        return False

    key = dedup_key or f"{title}|{message[:80]}"
    if not _should_send(key, dedup_seconds):
        logger.debug(f"提醒已节流，跳过重复邮件：{title}")
        return False

    def _deliver() -> None:
        try:
            from SRACore.notification.channels.email import EmailChannel
            from SRACore.notification.models import NotificationContext
            from SRACore.notification.service import build_notification_payload

            context = NotificationContext(
                title="SRA",
                message=message,
                payload=build_notification_payload(title, message, result),
                settings=config,
            )
            if EmailChannel().send(context):
                logger.info(f"提醒邮件已发送：{title}")
            else:
                logger.warning(f"提醒邮件发送失败：{title}")
        except Exception as err:  # 通知失败绝不能影响任务本身
            logger.warning(f"提醒邮件发送异常：{err}")

    if async_send:
        threading.Thread(target=_deliver, name="sra-alert-mail", daemon=True).start()
        return True
    _deliver()
    return True
