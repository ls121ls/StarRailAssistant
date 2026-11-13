"""货币战争：第三位面最终关卡前停止任务 + 投资环境/策略摘要的单元测试。"""

import threading
from types import SimpleNamespace

from SRACore.notification.channels import email as email_channel
from tasks.currency_wars.CurrencyWars import CurrencyWars, FINAL_STAGE_ROUND


class _FakeOperator:
    """只提供停止事件，用来断言「停止任务」确实被触发。"""

    def __init__(self):
        self.stop_event = threading.Event()


def _bare_cw(**attributes):
    """绕过 __init__ 装配一个只带停止逻辑所需属性的任务对象。"""
    task = CurrencyWars.__new__(CurrencyWars)
    task.operator = _FakeOperator()
    task.stopped_before_final_stage = False
    task.is_running = True
    task.selected_invest_environments = []
    task.selected_invest_strategies = []
    task.invest_strategy_misses = 0
    for key, value in attributes.items():
        setattr(task, key, value)
    return task


def test_stop_only_before_third_plane_final_stage():
    """只在 3-7 的备战阶段停止，2-7、3-6 都不能触发。"""
    task = _bare_cw()

    assert FINAL_STAGE_ROUND == (3, 7)
    assert CurrencyWars._should_stop_before_final_stage(task, (3, 7)) is True
    assert CurrencyWars._should_stop_before_final_stage(task, (3, 6)) is False
    assert CurrencyWars._should_stop_before_final_stage(task, (2, 7)) is False
    assert CurrencyWars._should_stop_before_final_stage(task, (1, 9)) is False

    # 停止过一次后不再重复触发
    task.stopped_before_final_stage = True
    assert CurrencyWars._should_stop_before_final_stage(task, (3, 7)) is False


def test_invest_summary_lists_environments_and_strategies():
    """摘要里要能看到选过的投资环境、投资策略，以及兜底次数。"""
    task = _bare_cw()
    CurrencyWars._record_invest_environment(task, "联席决策")
    CurrencyWars._record_invest_environment(task, "联席决策")  # 重复不重复记录
    CurrencyWars._record_invest_environment(task, "彩虹时代")
    CurrencyWars._record_invest_strategy(task, "艾丝妲的猛犬！")
    task.invest_strategy_misses = 2

    summary = CurrencyWars._invest_summary(task)

    assert "联席决策、彩虹时代" in summary
    assert "艾丝妲的猛犬！" in summary
    assert "2 次" in summary
    assert task.selected_invest_environments == ["联席决策", "彩虹时代"]


def test_invest_summary_when_nothing_selected():
    """一局都没选到任何投资环境/策略时给出明确提示，而不是空白。"""
    summary = CurrencyWars._invest_summary(_bare_cw())

    assert "投资环境：未记录" in summary
    assert "投资策略：未记录" in summary
    assert "备注" not in summary


def test_stop_before_final_stage_stops_task_without_waiting():
    """停止时标记状态、发邮件、置位停止事件，并且立刻返回（不再原地等待人工）。"""
    task = _bare_cw()
    CurrencyWars._record_invest_environment(task, "联席决策")
    CurrencyWars._record_invest_strategy(task, "招财狗")

    sent: list[str] = []
    original = CurrencyWars._send_stop_mail
    CurrencyWars._send_stop_mail = lambda _self, message: sent.append(message)
    try:
        # 函数若仍在等待人工，这里会一直阻塞；用超时保护把「阻塞」暴露成测试失败
        worker = threading.Thread(target=CurrencyWars.stop_before_final_stage, args=(task, (3, 7)))
        worker.start()
        worker.join(timeout=5)
        assert worker.is_alive() is False, "stop_before_final_stage 仍在阻塞等待人工"
    finally:
        CurrencyWars._send_stop_mail = original

    assert task.stopped_before_final_stage is True
    assert task.is_running is False  # 主循环条件立即失效
    assert task.operator.stop_event.is_set() is True  # 整个任务被停止，队列不会继续
    assert len(sent) == 1
    assert "3-7" in sent[0]
    assert "联席决策" in sent[0]
    assert "招财狗" in sent[0]
    assert "已按要求停止任务" in sent[0]


def test_stop_without_stop_event_still_works():
    """没有停止事件（例如测试桩）时也不能抛异常，只是少了队列级停止。"""
    task = _bare_cw(operator=None)
    original = CurrencyWars._send_stop_mail
    CurrencyWars._send_stop_mail = lambda _self, _message: None
    try:
        CurrencyWars.stop_before_final_stage(task, (3, 7))
    finally:
        CurrencyWars._send_stop_mail = original

    assert task.stopped_before_final_stage is True
    assert task.is_running is False
    assert CurrencyWars._is_stop_requested(task) is False


def test_stop_mail_skipped_when_switch_is_off():
    """邮件开关关闭时，最终关卡的停止提醒也不会发信。"""
    task = _bare_cw()
    task.settings = SimpleNamespace(
        Notification=SimpleNamespace(isEmailEnabled=False))
    sent: list[str] = []
    original = email_channel.EmailChannel.send
    email_channel.EmailChannel.send = lambda _self, ctx: sent.append(ctx.message) or True
    try:
        CurrencyWars._send_stop_mail(task, "测试提醒")
    finally:
        email_channel.EmailChannel.send = original

    assert sent == []


def test_run_stops_and_skips_ending():
    """停止后整局直接结束：不按 esc 收尾、不再进下一局，返回「已停止」。"""
    task = _bare_cw()
    task.runtimes = 2
    task.isPointRewardsEnabled = False
    task._start_health_monitor = lambda: None
    task._stop_health_monitor = lambda: None
    task.start_game = lambda: True
    task.should_continue_next_round = lambda: True

    ended: list[bool] = []
    task.handle_ending = lambda: ended.append(True)

    def fake_game_loop():
        CurrencyWars.stop_before_final_stage(task, (3, 7))
        return True

    task.game_loop = fake_game_loop

    original = CurrencyWars._send_stop_mail
    CurrencyWars._send_stop_mail = lambda _self, _message: None
    try:
        result = CurrencyWars.run(task)
    finally:
        CurrencyWars._send_stop_mail = original

    assert result is False  # 交给任务队列按「已停止」处理，不触发失败重试
    assert ended == []  # 没有调用 handle_ending，不会按 esc 退出对局
    assert task.stopped_before_final_stage is True
