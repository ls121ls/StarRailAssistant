"""货币战争刷开局：投资策略候选表与「攻略推荐带入」的单元测试。"""

from tasks.currency_wars.RerollStart import PriorityMatcher, RerollStart
from tasks.img import CWIMG


class _RerollStub:
    """只保留解析逻辑所需属性的最小对象，避免构造完整任务与操作器。"""

    _build_strategy_matcher = staticmethod(RerollStart._build_strategy_matcher)
    recommend_invest_strategies = ()
    wanted_invest_strategies = None
    recommended_strategy_matcher = None


def _stub(**attributes) -> _RerollStub:
    stub = _RerollStub()
    for key, value in attributes.items():
        setattr(stub, key, value)
    return stub


def test_priority_matcher_returns_leftmost_candidate():
    """`|` 分隔的候选表按书写顺序排优先级，与 OCR 顺序无关。"""
    matcher = PriorityMatcher("佩佩驾到｜这么大的钻石")

    assert matcher.match("佩佩驾到") is True
    assert matcher.priority("这么大的钻石") == 1
    assert matcher.priority("招财狗") is None


def test_priority_matcher_tolerates_missing_exclamation():
    """卡名末尾的叹号被 OCR 漏掉时仍应命中。"""
    matcher = PriorityMatcher("艾丝妲的猛犬！")

    assert matcher.match("艾丝妲的猛犬") is True
    assert matcher.match("艾丝妲的猛犬!") is True


def test_set_invest_strategy_keeps_stage_mode_and_adds_candidates():
    """分号仍是阶段分隔，单阶段内可用 `|` 表示任一命中。"""
    stub = _stub()

    RerollStart.set_invest_strategy(stub, "采购专员·彩;这么大的钻石|佩佩驾到")

    stages = stub.wanted_invest_strategies
    assert len(stages) == 2
    assert not isinstance(stages[0], PriorityMatcher)
    assert stages[0].match("采购专员彩")
    assert isinstance(stages[1], PriorityMatcher)
    # 显式配置的候选表同样用于开局达成后的三选一
    assert stub.recommended_strategy_matcher is stages[1]


def test_apply_strategy_recommendations_when_unconfigured():
    """未填写投资策略时，沿用攻略推荐的候选表作为阶段1要求。"""
    stub = _stub(recommend_invest_strategies=("这么大的钻石", "佩佩驾到"))

    assert RerollStart.apply_strategy_recommendations(stub) is True
    assert len(stub.wanted_invest_strategies) == 1
    assert stub.wanted_invest_strategies[0].match("佩佩驾到")
    assert stub.recommended_strategy_matcher is stub.wanted_invest_strategies[0]


def test_explicit_invest_strategy_wins_over_recommendations():
    """显式配置优先：不会再被攻略推荐覆盖。"""
    stub = _stub(recommend_invest_strategies=("这么大的钻石",))
    RerollStart.set_invest_strategy(stub, "阿哈大悦")

    assert RerollStart.apply_strategy_recommendations(stub) is False
    assert stub.wanted_invest_strategies[0].match("阿哈大悦")


def test_no_recommendations_keeps_previous_behaviour():
    """攻略没有推荐投资策略时保持原样：不做任何限制。"""
    stub = _stub()

    assert RerollStart.apply_strategy_recommendations(stub) is False
    assert stub.wanted_invest_strategies is None
    assert stub.recommended_strategy_matcher is None


class _OcrItem:
    """模拟 OCR 结果：只需要 source 字段。"""

    def __init__(self, source: str):
        self.source = source
        self.points = [(0, 0)]


class _FakeOperator:
    """记录调用行为的假操作器：没有刷新按钮，也没有收藏卡。"""

    def __init__(self, texts: list[str]):
        self.texts = texts
        self.images_clicked: list[str] = []
        self.points_clicked: list[tuple[float, float]] = []
        self.clicked_cards: list[str] = []
        self.refreshed = 0

    def ocr_boxes(self, **_kwargs):
        return [_OcrItem(text) for text in self.texts]

    def locate_all(self, image):
        # 这里模拟「已无刷新次数」：界面上找不到刷新按钮
        return []

    def locate(self, image):
        return None

    def click_img(self, image, **_kwargs):
        self.images_clicked.append(image)
        return False  # 没有收藏卡，基础逻辑会退化为点击中间位置

    def click_point(self, x, y, **_kwargs):
        self.points_clicked.append((x, y))
        return True

    def click_box(self, box, **_kwargs):
        source = getattr(box, "source", "")
        self.clicked_cards.append(source)
        if source == _ScriptedOperator.REFRESH_MARK:
            self.refreshed += 1
        return True


def _bare_task(operator, **attributes):
    """绕过 __init__ 装配一个只带本次逻辑所需属性的任务对象。"""
    task = RerollStart.__new__(RerollStart)
    task.operator = operator
    task.opening_satisfied = False
    task.wanted_invest_strategies = None
    task.invest_strategy_stage = 0
    task.recommended_strategy_matcher = None
    task.strategy_special_events = {}
    task.invest_strategy_misses = 0
    task.selected_invest_environments = []
    task.selected_invest_strategies = []
    task._last_strategy_cards = []
    task._detected_env_names = []
    for key, value in attributes.items():
        setattr(task, key, value)
    return task


def test_missing_strategy_continues_instead_of_rerolling():
    """刷新后仍没有指定投资策略时不再重开，改为随便选一张把对局打下去。"""
    operator = _FakeOperator(["招财狗", "环保大使叽米"])
    task = _bare_task(
        operator,
        wanted_invest_strategies=[PriorityMatcher("这么大的钻石")],
    )

    aborted: list[bool] = []
    original_abort = RerollStart.abort_and_return
    RerollStart.abort_and_return = lambda _self: aborted.append(True)
    try:
        assert RerollStart.handle_invest_strategy(task, None) is False
    finally:
        RerollStart.abort_and_return = original_abort

    # 没有重开，并且已标记理想开局达成，后续按攻略打完这一局
    assert aborted == []
    assert CWIMG.BACK_PREPARE_PAGE not in operator.images_clicked
    assert task.opening_satisfied is True
    # 兜底走了基础逻辑：点不到收藏卡就点中间那张
    assert operator.points_clicked == [(0.5, 0.3)]
    # 兜底选择也记进投资策略清单，方便暂停邮件里说明
    assert task.invest_strategy_misses == 1
    assert task.selected_invest_strategies == ["环保大使叽米"]


class _ScriptedOperator(_FakeOperator):
    """三张卡各带一个刷新按钮（用掉就消失），OCR 按脚本逐轮返回不同结果。"""

    REFRESH_MARK = "__refresh__"

    def __init__(self, script: list[list[str]]):
        super().__init__([])
        self.script = script
        self.ocr_calls = 0

    def ocr_boxes(self, **_kwargs):
        texts = self.script[min(self.ocr_calls, len(self.script) - 1)]
        self.ocr_calls += 1
        return [_OcrItem(text) for text in texts]

    def locate_all(self, image):
        remaining = 3 - self.refreshed
        return [_OcrItem(self.REFRESH_MARK) for _ in range(max(0, remaining))]


def test_last_card_refresh_result_is_checked_before_giving_up():
    """三张卡都刷完后必须再识别一次：右卡刷出来的候选也要能选中。"""
    operator = _ScriptedOperator([
        ["绕口令", "群攻星徽", "如有神助"],    # 初始
        ["借力打力", "群攻星徽", "如有神助"],  # 刷完左卡
        ["借力打力", "装备党", "如有神助"],    # 刷完中卡
        ["借力打力", "装备党", "溜佩佩"],      # 刷完右卡：修复前这轮永远不会发生
    ])
    task = _bare_task(operator, wanted_invest_strategies=[PriorityMatcher("溜佩佩")])

    assert RerollStart.handle_invest_strategy(task, None) is True

    # 3 次刷新 + 4 次识别，最后选中右卡刷出来的「溜佩佩」
    assert operator.refreshed == 3
    assert operator.ocr_calls == 4
    assert operator.clicked_cards[-1] == "溜佩佩"
    assert task.opening_satisfied is True
    # 命中的策略要记进清单，暂停邮件里要用
    assert task.selected_invest_strategies == ["溜佩佩"]


def test_after_opening_satisfied_later_screens_still_refresh():
    """开局达成后，后续每次三选一没命中候选表时也要刷新，而不是直接点收藏/中间卡。"""
    operator = _ScriptedOperator([
        ["专家招募", "狸职手续", "加拉赫顾问"],  # 初始
        ["专家招募", "白银投资", "加拉赫顾问"],
        ["专家招募", "白银投资", "红钻闪耀"],
        ["专家招募", "白银投资", "招财狗"],  # 刷完右卡后出现候选
    ])
    task = _bare_task(
        operator,
        opening_satisfied=True,
        invest_strategy_stage=1,
        recommended_strategy_matcher=PriorityMatcher("招财狗"),
    )

    # 开局达成后不再触发重开，也不进入终止状态
    assert RerollStart.handle_invest_strategy(task, None) is False

    assert operator.refreshed == 3
    assert operator.ocr_calls == 4
    assert operator.clicked_cards[-1] == "招财狗"
    # 走的是「命中候选」而不是基础逻辑的收藏卡/中间卡
    assert operator.points_clicked == []
    assert task.selected_invest_strategies == ["招财狗"]


