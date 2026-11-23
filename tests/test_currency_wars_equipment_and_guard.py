"""装备链路与投资策略屏蔽的回归测试。

只覆盖纯逻辑：用最小占位替换 OCR 运行时依赖（本机未装 rapidocr 时也能跑），
不启动游戏、不读界面。数据驱动的用例会在 BWIKI 资料刷新后自动提醒补名单。
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _install_ocr_stubs() -> None:
    """本机没有安装 OCR 运行时依赖时，为纯逻辑测试提供最小占位模块。"""
    try:
        import rapidocr  # noqa: F401
        return
    except ImportError:
        pass

    def stub(name: str, **attrs) -> None:
        module = types.ModuleType(name)
        module.__path__ = []
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

    stub("rapidocr", RapidOCR=object)
    stub("rapidocr.utils")
    stub("rapidocr.utils.output", RapidOCROutput=object)


_install_ocr_stubs()

import pytest  # noqa: E402

from tasks.currency_wars import shared_constants as guard  # noqa: E402
# 包的 __init__ 把 CurrencyWars 这个名字绑定成了类，显式取模块才能拿到模块级常量与类。
main_module = importlib.import_module("tasks.currency_wars.CurrencyWars")  # noqa: E402
from tasks.currency_wars.RerollStart import RerollStart  # noqa: E402
from extensions.currency_wars_plus.cw_plus import equipment_policy  # noqa: E402
from extensions.currency_wars_plus.cw_plus.CurrencyWars import CurrencyWars as PlusCurrencyWars  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INVESTMENT_DATA = ROOT / "tasks" / "currency_wars" / "data" / "currency_war_investments.json"


# --------------------------------------------------------------------------
# 共享常量：单一来源 + 分级判定
# --------------------------------------------------------------------------

def test_equipment_rounds_come_from_one_source():
    """主任务与 cw_plus 必须使用同一份装备节点常量。"""
    assert main_module.EQUIPMENT_ROUNDS is guard.EQUIPMENT_ROUNDS
    assert equipment_policy.EQUIPMENT_ROUNDS is guard.EQUIPMENT_ROUNDS
    assert guard.EQUIPMENT_ROUNDS == frozenset({(1, 4), (1, 9), (2, 4), (2, 7), (3, 4), (3, 7)})


def test_blocklists_come_from_one_source():
    """cw_plus 的屏蔽名单与共享模块一致，避免两边各自维护。"""
    assert PlusCurrencyWars.FIELD_REPLACING_INVESTMENTS == guard.FIELD_REPLACING_INVESTMENTS
    assert PlusCurrencyWars.ROSTER_CHURN_INVESTMENTS == guard.ROSTER_CHURN_INVESTMENTS


def test_invest_card_layout_comes_from_one_source():
    """主任务与 cw_plus 的三选一坐标、刷新次数、兜底顺序都取同一份定义。"""
    assert main_module.CurrencyWars.INVEST_CARD_CENTERS is guard.INVEST_CARD_CENTERS
    assert PlusCurrencyWars.INVEST_CARD_CENTERS is guard.INVEST_CARD_CENTERS
    assert main_module.CurrencyWars.INVEST_STRATEGY_REFRESH_TIMES == guard.INVEST_STRATEGY_REFRESH_TIMES
    assert PlusCurrencyWars.INVEST_STRATEGY_REFRESH_TIMES == guard.INVEST_STRATEGY_REFRESH_TIMES
    assert PlusCurrencyWars.LEAST_HARMFUL_INVESTMENTS is guard.LEAST_HARMFUL_INVESTMENTS
    assert PlusCurrencyWars.LEAST_HARMFUL_INVESTMENTS is not None


@pytest.mark.parametrize("name", [
    "全员晋升", "大裁员", "降本增效", "现金为王", "人力重组", "都是这家伙的错！",
])
def test_clearing_investments_are_level_two(name):
    assert guard.investment_risk(name) == 2


@pytest.mark.parametrize("name", [
    "乱成一锅粥", "乱成一锅粥+", "公司人才流动", "愚者恶作剧", "飞光•映月", "终身雇佣",
])
def test_roster_churn_investments_are_level_one(name):
    assert guard.investment_risk(name) == 1


@pytest.mark.parametrize("name", [
    "飞光•传剑", "淘金客", "星变", "扩张的代价", "搜打撤", "佩佩驾到", "",
])
def test_harmless_investments_are_level_zero(name):
    assert guard.investment_risk(name) == 0


def test_risk_matching_tolerates_ocr_noise():
    """全角标点、OCR 断字、多余空白都不应让屏蔽失效。"""
    assert guard.investment_risk(" 大 裁 员 ") == 2
    assert guard.investment_risk("都是这家伙的错！") == 2
    assert guard.investment_risk("乱成一锅粥＋") == 1
    assert guard.investment_risk(None) == 0


def test_risk_matching_uses_effect_description_when_title_is_missing():
    """标题没被 OCR 读到时，效果描述仍能识别清场/换人策略。"""
    assert guard.investment_risk("出售场上和备战席的所有角色，获得双倍价格的金币。") == 2
    assert guard.investment_risk("处在备战席最左侧6个位置的角色变为随机的同费用角色。") == 1
    assert guard.investment_risk("前台或后台角色参与战斗后，无法再移回备战席或出售。") == 1


def test_latest_investment_data_is_covered_by_blocklists():
    """BWIKI 资料刷新后，若新增同类策略而名单没跟上，这个用例会失败。"""
    data = json.loads(INVESTMENT_DATA.read_text(encoding="utf-8"))
    records = data["records"]
    assert len(records) == data["meta"]["count"]

    clearing = re.compile(r"出售场上和备战席|场上的所有角色会|出售1名.*上场角色")
    churning = re.compile(r"变为随机的同费用角色|去别处找乐子|出售1名.*上场角色|无法再移回备战席")
    checked = 0
    for record in records:
        content = record.get("content", "")
        name = record["name"]
        if clearing.search(content):
            assert guard.investment_risk(name) == 2, f"新增清场策略未屏蔽：{name}"
            checked += 1
        elif churning.search(content):
            assert guard.investment_risk(name) >= 1, f"新增换人策略未屏蔽：{name}"
            checked += 1
    assert checked >= 8, "数据文件可能换了格式，效果正则需要同步更新"


# --------------------------------------------------------------------------
# 主任务：以上游为基线的后台槽位去重
# --------------------------------------------------------------------------

class _RectBox:
    def __init__(self, center_x: float, center_y: float = 500.0):
        self.left = center_x - 10
        self.center = (center_x, center_y)

    def distance(self, other: "_RectBox") -> float:
        return abs(self.center[0] - other.center[0])


class _SyncOperator:
    window_context = SimpleNamespace(width=1000, height=1000)

    def __init__(self, boxes):
        self.boxes = boxes
        self.trace = None

    def rectangle_detect(self, *args, **kwargs):
        self.trace = kwargs.get("trace")
        return self.boxes


class _SyncStub:
    _sync_off_field_area = main_module.CurrencyWars._sync_off_field_area

    def __init__(self, boxes):
        self.operator = _SyncOperator(boxes)
        self.off_field_area = []
        self.off_field_character = []


def test_off_field_area_deduplicates_overlapping_rectangles():
    """上游 v2.25.2 的槽位去重必须在重做装备链路后保留。"""
    boxes = [_RectBox(120), _RectBox(125), _RectBox(420)]  # 前两个中心距 5，应合并
    stub = _SyncStub(boxes)

    stub._sync_off_field_area()

    assert len(stub.off_field_area) == 2
    assert stub.off_field_area[0] == (0.12, 0.5)
    assert stub.off_field_area[1] == (0.42, 0.5)
    assert len(stub.off_field_character) == 2
    assert stub.operator.trace is True


def test_off_field_area_keeps_single_rectangle():
    stub = _SyncStub([_RectBox(300)])

    stub._sync_off_field_area()

    assert stub.off_field_area == [(0.3, 0.5)]


# --------------------------------------------------------------------------
# 装备轮：缺少「装备推荐」模板时必须整轮跳过，不能动阵容缓存
# --------------------------------------------------------------------------

class _EquipOperator:
    def __init__(self):
        self.clicked: list[tuple] = []

    def click_point(self, *args, **kwargs):
        self.clicked.append(args)


class _EquipStub:
    equip_cached_characters = main_module.CurrencyWars.equip_cached_characters

    def __init__(self, character):
        self.operator = _EquipOperator()
        self.on_field_character = [character]
        self.off_field_character = [None]
        self.on_field_area = [(0.1, 0.1)]
        self.off_field_area = [(0.5, 0.5)]


def test_equip_round_is_skipped_without_recommend_template(monkeypatch, tmp_path):
    """模板缺失时 locate 恒返回 None，若继续执行会把角色缓存清空，
    随后 place_character 误判空位并把场上攻略角色换掉（20261121 复盘）。"""
    monkeypatch.setattr(main_module.CWIMG, "EQUIPMENT_RECOMMEND", str(tmp_path / "missing.png"))
    character = main_module.Characters.get_character("银狼LV.999")
    stub = _EquipStub(character)

    assert stub.equip_cached_characters(sync_layout=False) == 0
    assert stub.on_field_character == [character]
    assert stub.operator.clicked == []


# --------------------------------------------------------------------------
# cw_plus：三选一屏蔽与三列全屏蔽的兜底
# --------------------------------------------------------------------------

class _CardBox:
    def __init__(self, source: str, center_x: float):
        self.source = source
        self.width = 100
        self.left = center_x - self.width / 2
        self.top = 300
        self.height = 50
        self.center = (center_x, 325)


class _CardOperator:
    window_context = SimpleNamespace(width=1000, height=1000)

    def __init__(self, boxes, refresh_buttons=None):
        self.boxes = list(boxes)
        self.refresh_buttons = list(refresh_buttons or ())
        self.clicked = []
        self.refreshed = []
        self.located = []
        self.on_refresh = None

    def ocr_boxes(self, **kwargs):
        return self.boxes

    def locate_all(self, image, **kwargs):
        return self.refresh_buttons

    def locate(self, image, **kwargs):
        self.located.append(image)
        return None

    def click_img(self, image, **kwargs):
        self.clicked.append(("img", image))
        return True

    def click_box(self, box, **kwargs):
        self.clicked.append(box)
        if box in self.refresh_buttons:
            self.refreshed.append(box)
            self.refresh_buttons.remove(box)
            if self.on_refresh is not None:
                self.on_refresh()
        return True

    def click_point(self, *args, **kwargs):
        self.clicked.append(("point", args))
        return True


def _refresh_buttons(centers, count=3):
    return [_CardBox("刷新", 1000 * center[0]) for center in centers[:count]]


class _SelectStub:
    _select_invest_strategy_card = PlusCurrencyWars._select_invest_strategy_card
    _least_harmful_invest_column = PlusCurrencyWars._least_harmful_invest_column
    _read_invest_cards = PlusCurrencyWars._read_invest_cards
    _refresh_invest_card = PlusCurrencyWars._refresh_invest_card
    _pick_invest_target = PlusCurrencyWars._pick_invest_target
    _on_invest_ocr_failure = PlusCurrencyWars._on_invest_ocr_failure
    INVEST_CARD_CENTERS = PlusCurrencyWars.INVEST_CARD_CENTERS
    INVEST_CARDS_ROI = PlusCurrencyWars.INVEST_CARDS_ROI
    INVEST_STRATEGY_REFRESH_TIMES = PlusCurrencyWars.INVEST_STRATEGY_REFRESH_TIMES
    LEAST_HARMFUL_INVESTMENTS = PlusCurrencyWars.LEAST_HARMFUL_INVESTMENTS
    GOLD_DIGGER_STRATEGY = PlusCurrencyWars.GOLD_DIGGER_STRATEGY
    GOLD_DIGGER_MIN_COINS = PlusCurrencyWars.GOLD_DIGGER_MIN_COINS

    def __init__(self, sources, priorities=(), refresh_rounds=(), refresh_buttons=None):
        """sources: 初始三张卡；refresh_rounds: 每次刷新后整屏替换成的三张卡。"""
        self._rounds = [list(round_) for round_ in refresh_rounds]
        self.operator = _CardOperator(
            self._boxes_from(sources),
            _refresh_buttons(self.INVEST_CARD_CENTERS) if refresh_buttons is None else refresh_buttons,
        )
        self.operator.on_refresh = self._advance_round
        self._priorities = tuple(priorities)
        self._gold_digger_selected = False

    def _boxes_from(self, sources):
        return [
            _CardBox(source, 1000 * self.INVEST_CARD_CENTERS[index][0])
            for index, source in enumerate(sources)
        ]

    def _advance_round(self):
        if self._rounds:
            self.operator.boxes = self._boxes_from(self._rounds.pop(0))

    def _investment_priorities(self):
        return self._priorities

    def _check_stop(self):
        return None

    @property
    def refreshed_columns(self):
        return [
            min(range(len(self.INVEST_CARD_CENTERS)),
                key=lambda i: abs(box.center[0] / 1000 - self.INVEST_CARD_CENTERS[i][0]))
            for box in self.operator.refreshed
        ]


def test_prefers_safe_card_when_others_are_blocked():
    """2 级必避开、1 级降权，0 级优先。"""
    stub = _SelectStub(["大裁员", "乱成一锅粥", "淘金客"])

    stub._select_invest_strategy_card(None)

    assert len(stub.operator.clicked) == 1
    assert getattr(stub.operator.clicked[0], "source", None) == "淘金客"


def test_ocr_broken_name_is_still_blocked():
    """OCR 把「大裁员」断成「大裁 员」时仍要屏蔽。"""
    stub = _SelectStub(["大裁 员", "佩佩驾到", "招财狗"])

    stub._select_invest_strategy_card(None)

    assert getattr(stub.operator.clicked[0], "source", None) != "大裁 员"


def test_all_three_cards_blocked_picks_least_harmful_when_refresh_unavailable():
    """三列全屏蔽且没有刷新按钮时，挑最容易重建阵容的一张而不是静默点中间卡。"""
    sources = ["人力重组", "大裁员", "全员晋升"]  # 中间列是「大裁员」
    stub = _SelectStub(sources, refresh_buttons=[])

    stub._select_invest_strategy_card(None)

    assert len(stub.operator.clicked) == 1
    assert getattr(stub.operator.clicked[0], "source", None) == "人力重组"


def test_all_three_cards_blocked_refreshes_then_takes_safe_card():
    """三列全屏蔽时先刷新，刷出安全卡就选它，不再硬吃清场卡。"""
    stub = _SelectStub(["人力重组", "大裁员", "全员晋升"],
                       refresh_rounds=[["人力重组", "大裁员", "淘金客"]])

    stub._select_invest_strategy_card(None)

    assert len(stub.operator.refreshed) == 1
    # 最后一次点击才是选卡，前面是刷新
    assert getattr(stub.operator.clicked[-1], "source", None) == "淘金客"


def test_refresh_starts_from_the_most_harmful_non_fallback_card():
    """刷新顺序：先刷危害最高的非兜底卡，兜底卡留到后面再动。"""
    stub = _SelectStub(["人力重组", "大裁员", "全员晋升"],
                       refresh_rounds=[["人力重组", "大裁员", "全员晋升"]])

    stub._select_invest_strategy_card(None)

    assert stub.refreshed_columns, "三张都清场时至少刷新一次"
    assert stub.refreshed_columns[0] != 0, "第一张应刷非兜底卡"


def test_refresh_attempts_are_bounded_and_fall_back():
    """刷新次数有上限：刷满仍不安全就退回影响最小的一张，不会无限刷新。"""
    rounds = [["人力重组", "大裁员", "全员晋升"]] * 5
    stub = _SelectStub(["人力重组", "大裁员", "全员晋升"], refresh_rounds=rounds)

    stub._select_invest_strategy_card(None)

    assert len(stub.operator.refreshed) == PlusCurrencyWars.INVEST_STRATEGY_REFRESH_TIMES
    assert getattr(stub.operator.clicked[-1], "source", None) == "人力重组"


def test_recommended_clearing_strategy_is_skipped():
    """推荐项里混入清场卡时不能命中它。"""
    stub = _SelectStub(["佩佩驾到", "大裁员", "招财狗"], priorities=["大裁员"])

    stub._select_invest_strategy_card(None)

    assert getattr(stub.operator.clicked[0], "source", None) != "大裁员"


# --------------------------------------------------------------------------
# 主任务：选卡接入危害分级（收藏卡仅在自身安全时沿用）
# --------------------------------------------------------------------------

class _MainSelectStub:
    _select_invest_strategy_card = main_module.CurrencyWars._select_invest_strategy_card
    _read_invest_cards = main_module.CurrencyWars._read_invest_cards
    _refresh_invest_card = main_module.CurrencyWars._refresh_invest_card
    INVEST_CARD_CENTERS = main_module.CurrencyWars.INVEST_CARD_CENTERS
    INVEST_CARDS_ROI = main_module.CurrencyWars.INVEST_CARDS_ROI
    INVEST_STRATEGY_REFRESH_TIMES = main_module.CurrencyWars.INVEST_STRATEGY_REFRESH_TIMES

    def __init__(self, sources, collection_column=None, refresh_buttons=None, refresh_rounds=()):
        self._rounds = [list(round_) for round_ in refresh_rounds]
        self.operator = _CardOperator(
            self._boxes_from(sources),
            _refresh_buttons(self.INVEST_CARD_CENTERS) if refresh_buttons is None else refresh_buttons,
        )
        self.operator.on_refresh = self._advance_round
        self.collection_column = collection_column
        if collection_column is not None:
            self.operator.locate = self._locate_collection

    def _boxes_from(self, sources):
        return [
            _CardBox(source, 1000 * self.INVEST_CARD_CENTERS[index][0])
            for index, source in enumerate(sources)
        ]

    def _advance_round(self):
        if self._rounds:
            self.operator.boxes = self._boxes_from(self._rounds.pop(0))

    def _locate_collection(self, image, **kwargs):
        column = self.collection_column
        return _CardBox("收藏卡", 1000 * self.INVEST_CARD_CENTERS[column][0])


def test_main_task_keeps_collection_card_when_it_is_safe():
    """收藏卡本身安全时沿用原行为，直接点收藏卡。"""
    stub = _MainSelectStub(["佩佩驾到", "淘金客", "招财狗"], collection_column=1)

    assert stub._select_invest_strategy_card() is True
    assert stub.operator.clicked == [("img", main_module.CWIMG.COLLECTION)]


def test_main_task_abandons_blocked_collection_card():
    """收藏卡是会清场的那张时，必须改选安全卡，不能盲点收藏卡。"""
    stub = _MainSelectStub(["佩佩驾到", "大裁员", "招财狗"], collection_column=1,
                           refresh_buttons=[])

    stub._select_invest_strategy_card()

    assert stub.operator.clicked, "至少要点击一次"
    assert ("img", main_module.CWIMG.COLLECTION) not in stub.operator.clicked
    assert getattr(stub.operator.clicked[-1], "source", None) == "佩佩驾到"


def test_main_task_refreshes_when_all_cards_clear_the_team():
    stub = _MainSelectStub(["人力重组", "大裁员", "全员晋升"],
                           refresh_rounds=[["人力重组", "大裁员", "淘金客"]])

    stub._select_invest_strategy_card()

    assert stub.operator.refreshed, "三张都清场时应先刷新"
    assert getattr(stub.operator.clicked[-1], "source", None) == "淘金客"


def test_main_task_falls_back_when_ocr_reads_nothing():
    """OCR 读不到卡片时保持原有兜底：没有收藏卡就点中间卡。"""
    stub = _MainSelectStub([])

    assert stub._select_invest_strategy_card() is True
    assert stub.operator.clicked == [("point", stub.INVEST_CARD_CENTERS[1])]


def test_main_task_exposes_the_card_it_really_clicked():
    """实际选中的卡名要能被刷开局侧读到，用于「本局已选投资策略」记录。"""
    stub = _MainSelectStub(["佩佩驾到", "大裁员", "招财狗"], collection_column=1,
                           refresh_buttons=[])

    stub._select_invest_strategy_card()

    assert stub._selected_invest_card_title == "佩佩驾到"


# --------------------------------------------------------------------------
# 刷开局：兜底记录要与实际点击一致
# --------------------------------------------------------------------------

class _FallbackRecordStub:
    _record_fallback_strategy = RerollStart._record_fallback_strategy

    def __init__(self, chosen=None, collection=None, last_cards=()):
        self._selected_invest_card_title = chosen
        self._last_strategy_cards = list(last_cards)
        self.invest_strategy_misses = 0
        self.recorded = []
        self.operator = SimpleNamespace(locate=lambda image: collection)

    def _record_invest_strategy(self, name):
        self.recorded.append(name)


def test_fallback_record_uses_actual_selected_card():
    """基础逻辑改选了安全卡时，记录不能还是「收藏卡」。"""
    stub = _FallbackRecordStub(chosen="佩佩驾到", collection=object())

    stub._record_fallback_strategy()

    assert stub.recorded == ["佩佩驾到"]
    assert stub.invest_strategy_misses == 1


def test_fallback_record_still_falls_back_to_middle_card():
    """读不到卡名时沿用原逻辑：收藏卡优先，其次中间卡。"""
    stub = _FallbackRecordStub(chosen=None, collection=None, last_cards=["甲", "乙"])

    stub._record_fallback_strategy()

    assert stub.recorded == ["乙"]


# --------------------------------------------------------------------------
# 主任务刷开局：攻略推荐过滤
# --------------------------------------------------------------------------

class _RerollStub:
    _build_strategy_matcher = staticmethod(RerollStart._build_strategy_matcher)
    apply_strategy_recommendations = RerollStart.apply_strategy_recommendations
    recommend_invest_strategies = ()
    wanted_invest_strategies = None
    recommended_strategy_matcher = None


def test_apply_recommendations_filters_clearing_strategy():
    stub = _RerollStub()
    stub.recommend_invest_strategies = ("大裁员", "佩佩驾到")

    assert stub.apply_strategy_recommendations() is True
    matcher = stub.wanted_invest_strategies[0]
    assert matcher.match("佩佩驾到") is True
    assert matcher.match("大裁员") is False


def test_apply_recommendations_with_only_clearing_strategies_is_noop():
    stub = _RerollStub()
    stub.recommend_invest_strategies = ("全员晋升", "人力重组")

    assert stub.apply_strategy_recommendations() is False
    assert stub.wanted_invest_strategies is None
