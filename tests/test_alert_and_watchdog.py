"""邮件告警与货币战争无人值守监控的单元测试。

覆盖：
* 587 端口走 STARTTLS、465 端口走 SMTPS；
* SMTP 未配置 / 重复提醒的节流行为；
* 「长时间无进展」「同一关卡过久」「单局过久」三个告警条件；
* 人工暂停、任务异常时的行为。
"""

import smtplib
import time
from types import SimpleNamespace

from SRACore.notification import alerts
from SRACore.notification.channels import email as email_channel
from SRACore.notification.channels.email import send_mail
from tasks.currency_wars.CurrencyWars import (
    GAME_LONG_ALERT_SECONDS,
    SAME_ROUND_ALERT_SECONDS,
    STALL_ALERT_SECONDS,
    CurrencyWars,
)


# ----------------------------------------------------------------------
# 邮件通道
# ----------------------------------------------------------------------

class _FakeSmtp:
    instances: list["_FakeSmtp"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.sent = None
        _FakeSmtp.instances.append(self)

    def ehlo(self):
        return None

    def starttls(self):
        self.started_tls = True

    def login(self, sender, password):
        self.login_args = (sender, password)

    def sendmail(self, sender, receivers, message):
        self.sent = (sender, receivers, message)

    def quit(self):
        return None


class _FakeSmtpSsl(_FakeSmtp):
    pass


def _with_fake_smtp(func):
    original_plain, original_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP, smtplib.SMTP_SSL = _FakeSmtp, _FakeSmtpSsl
    _FakeSmtp.instances.clear()
    try:
        return func()
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = original_plain, original_ssl


def test_port_587_uses_starttls():
    """587 是 STARTTLS 端口，不能再按 SMTPS 直连。"""
    ok = _with_fake_smtp(lambda: send_mail(
        "SRA", "subj", "body", "smtp.qq.com", 587, "a@qq.com", "pwd", "b@qq.com"))
    instance = _FakeSmtp.instances[-1]

    assert ok is True
    assert instance.port == 587
    assert instance.started_tls is True
    assert instance.login_args == ("a@qq.com", "pwd")
    assert instance.sent[1] == ["b@qq.com"]


def test_port_465_uses_smtps():
    """465 保持原来的 SSL 直连，不受改动影响。"""
    ok = _with_fake_smtp(lambda: send_mail(
        "SRA", "subj", "body", "smtp.qq.com", 465, "a@qq.com", "pwd", "b@qq.com"))
    instance = _FakeSmtp.instances[-1]

    assert ok is True
    assert isinstance(instance, _FakeSmtpSsl)
    assert instance.started_tls is False


# ----------------------------------------------------------------------
# 告警投递
# ----------------------------------------------------------------------

def _mail_config(**overrides):
    config = SimpleNamespace(
        isEmailEnabled=True,
        smtpServer="smtp.qq.com",
        smtpPort=587,
        smtpSender="a@qq.com",
        smtpReceiver="b@qq.com",
        EncryptedSmtpAuthCode="ciphertext",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_alert_email_skipped_when_switch_is_off():
    """邮件开关关闭时，即使 SMTP 配置齐全也不发信，且不消耗节流名额。"""
    alerts.reset_throttle()

    def _run():
        return alerts.send_alert_email(
            _mail_config(isEmailEnabled=False), "标题", "内容",
            dedup_key="off", async_send=False)

    assert _with_fake_smtp(_run) is False
    assert _FakeSmtp.instances == []


def test_alert_email_sent_when_switch_is_on():
    """打开开关后，真实链路会走 SMTP 发信。"""
    alerts.reset_throttle()
    original = email_channel.encryption.decryptor
    email_channel.encryption.decryptor = lambda _cipher: "pwd"
    try:
        def _run():
            return alerts.send_alert_email(
                _mail_config(), "标题", "内容", dedup_key="on", async_send=False)

        assert _with_fake_smtp(_run) is True
    finally:
        email_channel.encryption.decryptor = original
        alerts.reset_throttle()

    assert len(_FakeSmtp.instances) == 1
    assert _FakeSmtp.instances[0].sent[1] == ["b@qq.com"]


def test_email_channel_respects_switch():
    """直接调用邮件通道也不能绕过开关（防止未来的新入口偷偷发信）。"""
    from SRACore.notification.models import NotificationContext

    context = NotificationContext(
        title="SRA", message="内容", payload={"title": "SRA", "message": "内容"},
        settings=_mail_config(isEmailEnabled=False),
    )

    def _run():
        return email_channel.EmailChannel().send(context)

    assert _with_fake_smtp(_run) is False
    assert _FakeSmtp.instances == []


def test_alert_email_skipped_without_smtp_config():
    """SMTP 不完整时直接跳过，不能抛出异常打断任务。"""
    alerts.reset_throttle()

    assert alerts.send_alert_email(_mail_config(smtpServer=""), "标题", "内容", async_send=False) is False
    assert alerts.send_alert_email(_mail_config(EncryptedSmtpAuthCode=""), "标题", "内容", async_send=False) is False


def test_alert_email_throttles_duplicates():
    """同一个 dedup_key 在时间窗内只发一封，避免重试刷屏。"""
    alerts.reset_throttle()
    sent: list[str] = []
    original = email_channel.EmailChannel.send

    email_channel.EmailChannel.send = lambda _self, ctx: sent.append(ctx.message) or True
    try:
        first = alerts.send_alert_email(_mail_config(), "标题", "内容", dedup_key="k", async_send=False)
        second = alerts.send_alert_email(_mail_config(), "标题", "内容", dedup_key="k", async_send=False)
    finally:
        email_channel.EmailChannel.send = original
        alerts.reset_throttle()

    assert first is True
    assert second is False
    assert len(sent) == 1


# ----------------------------------------------------------------------
# 货币战争监控
# ----------------------------------------------------------------------

def _bare_monitor(**attributes):
    task = CurrencyWars.__new__(CurrencyWars)
    now = time.monotonic()
    task.stopped_before_final_stage = False
    task.is_running = True
    task.selected_invest_environments = []
    task.selected_invest_strategies = []
    task.invest_strategy_misses = 0
    task._task_started_at = now
    task._game_started_at = now
    task._last_progress_at = now
    task._progress_note = "第2位面 2-3"
    task._current_round = (2, 3)
    task._round_since = now
    task._stall_alerted = False
    task._same_round_alerted = False
    task._long_game_alerted = False
    for key, value in attributes.items():
        setattr(task, key, value)
    return task


def _capture_alerts(task):
    captured: list[tuple[str, str]] = []
    task._send_health_alert = lambda title, message, **_kwargs: captured.append((title, message))
    return captured


def test_watchdog_alerts_stall_only_once():
    """15 分钟没有任何推进 => 发一封「长时间无进展」，恢复前不重复。"""
    task = _bare_monitor()
    captured = _capture_alerts(task)
    task._last_progress_at = time.monotonic() - STALL_ALERT_SECONDS - 1

    CurrencyWars._check_health(task)
    CurrencyWars._check_health(task)

    assert len(captured) == 1
    assert "长时间无进展" in captured[0][0]
    assert "2-3" in captured[0][1]

    # 有新的推进后重新武装，下一次卡住还能再提醒
    CurrencyWars._mark_progress(task)
    assert task._stall_alerted is False


def test_watchdog_alerts_same_round_too_long():
    """同一个备战关卡停留超过 20 分钟 => 提醒「长时间停留在同一关卡」。"""
    task = _bare_monitor()
    captured = _capture_alerts(task)
    task._round_since = time.monotonic() - SAME_ROUND_ALERT_SECONDS - 1

    CurrencyWars._check_health(task)

    assert len(captured) == 1
    assert "同一关卡" in captured[0][0]


def test_watchdog_alerts_long_game():
    """单局运行超过 60 分钟 => 提醒一次「运行时间过长」。"""
    task = _bare_monitor()
    captured = _capture_alerts(task)
    task._game_started_at = time.monotonic() - GAME_LONG_ALERT_SECONDS - 1

    CurrencyWars._check_health(task)
    CurrencyWars._check_health(task)

    assert len(captured) == 1
    assert "运行时间过长" in captured[0][0]


def test_watchdog_silent_after_final_stage_stop():
    """最终关卡前主动停止属于预期结束，不能当成卡住发告警。"""
    task = _bare_monitor(stopped_before_final_stage=True, is_running=False)
    captured = _capture_alerts(task)
    task._last_progress_at = time.monotonic() - STALL_ALERT_SECONDS * 3
    task._round_since = time.monotonic() - SAME_ROUND_ALERT_SECONDS * 3
    task._game_started_at = time.monotonic() - GAME_LONG_ALERT_SECONDS * 3

    CurrencyWars._check_health(task)

    assert captured == []


def test_note_round_resets_same_round_timer():
    """换关卡后重新计时，否则连续几关都会被算成「停在 2-3」。"""
    task = _bare_monitor(_current_round=None)
    CurrencyWars._note_round(task, (1, 2))
    first_at = task._round_since

    task._round_since = 0.0
    CurrencyWars._note_round(task, (1, 3))

    assert task._current_round == (1, 3)
    assert task._round_since > 0.0
    assert task._progress_note == "第1位面 1-3"
    assert first_at > 0.0


def test_health_monitor_thread_starts_and_stops():
    """监控线程必须能随任务启动，并在任务结束时干净退出（否则每局都会漏一个线程）。"""
    task = _bare_monitor()

    CurrencyWars._start_health_monitor(task)
    try:
        assert task._watchdog_thread is not None
        assert task._watchdog_thread.is_alive()
    finally:
        CurrencyWars._stop_health_monitor(task)
    task._watchdog_thread.join(timeout=3)

    assert not task._watchdog_thread.is_alive()


def test_exception_alert_contains_stage_and_investments():
    """异常邮件要带阶段、已选投资和异常原文，方便人不在电脑前也能判断。"""
    task = _bare_monitor()
    CurrencyWars._record_invest_environment(task, "联席决策")
    CurrencyWars._record_invest_strategy(task, "招财狗")
    captured = _capture_alerts(task)
    task.settings = SimpleNamespace()

    try:
        raise ValueError("未设置游戏启动路径")
    except ValueError as err:
        CurrencyWars._notify_exception(task, err)

    assert len(captured) == 1
    title, message = captured[0]
    assert "异常" in title
    assert "未设置游戏启动路径" in message
    assert "第2位面 2-3" in message
    assert "联席决策" in message and "招财狗" in message
