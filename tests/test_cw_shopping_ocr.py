"""货币战争：商店槽位换算与人口读数兜底的单元测试。"""

from tasks.currency_wars.CurrencyWars import _split_population_digits, _store_slot_index

STORE_AREA = [(0.25, 0.18), (0.40, 0.18), (0.55, 0.18), (0.68, 0.18), (0.80, 0.18)]
SCAN_REGION = (0.19, 0.26, 0.88, 0.31)


def _box(left: int, right: int, top: int = 6, bottom: int = 40):
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def test_store_slot_index_matches_name_position_among_price_boxes():
    """价格框同样占 OCR 结果位，槽位必须按名称框的横向位置判定。"""
    names = [_box(13, 94), _box(282, 340), _box(551, 608), _box(817, 875), _box(1089, 1211)]

    assert [_store_slot_index(box, SCAN_REGION, STORE_AREA, 1920) for box in names] == [0, 1, 2, 3, 4]


def test_store_slot_index_keeps_position_when_left_slot_is_empty():
    """左侧槽位空着时，第 1 个识别结果属于 4 号槽，不能按下标点成 0 号槽。"""
    assert _store_slot_index(_box(817, 875), SCAN_REGION, STORE_AREA, 1920) == 3


def test_store_slot_index_scales_with_window_width():
    """窗口更宽时框坐标同比放大，换算结果不变。"""
    box = _box(1452, 1615)  # 1920 下 1089~1211 的框按 2560 等比放大

    assert _store_slot_index(box, SCAN_REGION, STORE_AREA, 2560) == 4


def test_store_slot_index_rejects_empty_box():
    assert _store_slot_index([], SCAN_REGION, STORE_AREA, 1920) == -1


def test_split_population_digits_restores_missing_slash():
    """小字号下斜杠常被吞掉，按位数还原当前/上限。"""
    assert _split_population_digits('i717') == (7, 7)
    assert _split_population_digits('87') == (8, 7)
    assert _split_population_digits('1010') == (10, 10)
    assert _split_population_digits('107') == (10, 7)
    assert _split_population_digits('710') == (7, 10)


def test_split_population_digits_returns_none_when_unreadable():
    assert _split_population_digits('ABC') == (None, None)
    assert _split_population_digits('0') == (None, None)
