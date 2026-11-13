from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

from SRACore.notification.channels.base import NotificationChannel
from SRACore.notification.models import NotificationContext, format_notification_message
from SRACore.util.logger import logger
from SRACore.util import encryption


class EmailChannel(NotificationChannel):
    name = "邮件"
    enabled_attr = "isEmailEnabled"

    def send(self, context: NotificationContext) -> bool:
        config = context.settings
        # 最后一道保险：无论从 dispatcher 还是直接调用，开关关闭时都不发信
        if not getattr(config, "isEmailEnabled", False):
            logger.debug("邮件通知开关已关闭，跳过发送")
            return False
        password = encryption.decryptor(config.EncryptedSmtpAuthCode) if config.EncryptedSmtpAuthCode else ""
        return send_mail(
            title=str(context.payload.get("title", "SRA")).strip() or "SRA",
            subject="SRA通知",
            message=format_notification_message(context.payload),
            smtp_server=config.smtpServer,
            port=config.smtpPort,
            sender=config.smtpSender,
            password=password,
            receiver=config.smtpReceiver,
        )


def _open_smtp_connection(smtp_server: str, port: int, timeout: int = 15):
    """按端口选择加密方式：465 走 SMTPS，其余端口优先 STARTTLS。

    QQ/163 等邮箱同时提供 465(SSL) 与 587(STARTTLS) 两种端口，
    早期实现固定用 SMTP_SSL，填 587 时会在握手阶段直接失败。
    """
    if port == 465:
        return smtplib.SMTP_SSL(smtp_server, port, timeout=timeout)
    server = smtplib.SMTP(smtp_server, port, timeout=timeout)
    server.ehlo()
    try:
        server.starttls()
        server.ehlo()
    except smtplib.SMTPException as err:
        # 服务器不支持 STARTTLS 时退回明文（内网 SMTP 常见），认证失败会另行提示
        logger.debug(f"SMTP STARTTLS 不可用，改用明文连接：{err}")
    return server


def send_mail(
    title: str,
    subject: str,
    message: str,
    smtp_server: str,
    port: int,
    sender: str,
    password: str,
    receiver: str,
) -> bool:
    if not smtp_server or not sender or not password or not receiver:
        logger.warning("邮件通知发送失败：SMTP 配置不完整，请检查服务器、端口、发件人、授权码和收件人。")
        return False
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 465

    msg = MIMEText(message, "plain", "utf-8")
    msg["From"] = formataddr((title, sender))
    msg["To"] = formataddr(("User", receiver))
    msg["Subject"] = subject

    try:
        server = _open_smtp_connection(smtp_server, port)
        try:
            server.login(sender, password)
            server.sendmail(sender, [receiver], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True
    except smtplib.SMTPAuthenticationError as err:
        logger.warning(f"邮件通知发送失败：SMTP 认证失败，请检查是否使用了邮箱授权码，而不是登录密码。({err})")
    except smtplib.SMTPServerDisconnected as err:
        logger.warning(f"邮件通知发送失败：SMTP 连接意外断开，请检查 SMTP 地址、端口以及授权码。({err})")
    except smtplib.SMTPException as err:
        logger.warning(f"邮件通知发送失败：SMTP 服务返回错误，请检查邮箱服务配置。({err})")
    except OSError as err:
        logger.warning(f"邮件通知发送失败：网络连接异常，请检查服务器地址、端口和网络环境。({err})")
    return False

