"""货币战争主任务与 cw_plus 扩展共用的常量和文本判定。

这里只放无副作用的常量与纯函数，两个实现都从这里取值，避免各自维护一份
「装备节点」和「会改动阵容的投资策略」名单而在版本更新后悄悄不同步。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

# 当前对局节点数：位面1 共9个（1-9首领），位面2/3 各7个（2-7/3-7首领）。
# 装备只在每个位面的第4个节点和最后一个节点处理，其余节点不打开装备面板。
EQUIPMENT_ROUNDS = frozenset({(1, 4), (1, 9), (2, 4), (2, 7), (3, 4), (3, 7)})

# 会清空或替换「已上阵」阵容的投资策略，三选一时必须避开。
# 名称使用去掉标点后的公共子串，便于容忍 OCR 漏掉叹号、间隔号。
FIELD_REPLACING_INVESTMENTS = (
    "全员晋升",          # 场上所有角色永久升级成高 1 费的随机角色
    "大裁员",            # 出售场上和备战席的所有角色
    "降本增效",
    "现金为王",
    "人力重组",
    "都是这家伙的错",     # 未完美通关时出售 1 名携带星徽的上场角色，可生效 3 次
)

# 会周期性改动备战席角色、条件性替换单个角色，或锁死阵容流动性的投资策略。
# 危害低于上一组：优先避开，但没有更安全选项时可以作为退让选择。
ROSTER_CHURN_INVESTMENTS = (
    "乱成一锅粥",        # 同时覆盖「乱成一锅粥+」
    "公司人才流动",
    "愚者恶作剧",
    "映月",              # 「飞光•映月」；不用「飞光」以免误伤无害的「飞光•传剑」
    # 参战过的角色无法再移回备战席或出售：不清场，但换阵容、腾格子会持续失败。
    "终身雇佣",
)

# 卡片效果描述里的兜底特征：OCR 没读出标题时仍能识别这类策略。
_FIELD_REPLACING_EFFECT_MARKERS = (
    "场上和备战席的所有角色",
    "场上的所有角色会永久升级",
    "上场角色",
)
_ROSTER_CHURN_EFFECT_MARKERS = (
    "变为随机的同费用角色",
    "去别处找乐子",
    "无法再移回备战席",
)

# 三张卡都会清空/替换阵容时的兜底顺序：优先选最容易重建阵容的一张。
LEAST_HARMFUL_INVESTMENTS = (
    "人力重组", "大裁员", "降本增效", "现金为王", "全员晋升", "都是这家伙的错",
)

# 投资策略三选一界面的卡片坐标：三列等距，click 用比例坐标，OCR 用整块区域。
INVEST_CARD_CENTERS = ((0.27, 0.30), (0.50, 0.30), (0.73, 0.30))
INVEST_CARDS_ROI = (0.05, 0.10, 0.95, 0.62)
# 三张卡各带一个一次性刷新按钮，一屏最多 3 次刷新机会。
INVEST_STRATEGY_REFRESH_TIMES = 3
# 卡片默认偏好顺序：中间 → 左 → 右（沿用原有「点中间卡」的兜底习惯）。
INVEST_COLUMN_ORDER = (1, 0, 2)


def normalize_invest_text(value: object) -> str:
    """归一化投资策略文本：全角转半角、去空白、小写，便于子串匹配。"""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


_FIELD_REPLACING_NORM = tuple(normalize_invest_text(name) for name in FIELD_REPLACING_INVESTMENTS)
_ROSTER_CHURN_NORM = tuple(normalize_invest_text(name) for name in ROSTER_CHURN_INVESTMENTS)


def investment_risk(text: object) -> int:
    """返回策略对现有阵容的危害等级。

    0 - 不改阵容；1 - 周期性改动备战席或条件性换人；2 - 清空/替换已上阵阵容。
    参数可以只给策略名，也可以给 OCR 出来的整段卡片文字。
    """
    value = normalize_invest_text(text)
    if not value:
        return 0
    if any(word in value for word in _FIELD_REPLACING_NORM):
        return 2
    if "出售" in value and any(marker in value for marker in _FIELD_REPLACING_EFFECT_MARKERS):
        return 2
    if any(word in value for word in _ROSTER_CHURN_NORM):
        return 1
    if any(marker in value for marker in _ROSTER_CHURN_EFFECT_MARKERS):
        return 1
    return 0


def is_field_replacing(text: object) -> bool:
    """是否会清空/替换已上阵阵容。"""
    return investment_risk(text) == 2


def invest_column_of(center_x: float, width: float | int, centers=None) -> int | None:
    """把卡片文字的像素横坐标映射到列号；窗口宽度未知时返回 None。"""
    centers = INVEST_CARD_CENTERS if centers is None else centers
    if not width:
        return None
    ratio = center_x / width
    return min(range(len(centers)), key=lambda index: abs(ratio - centers[index][0]))


def rank_invest_columns(risk_by_column: Mapping[int, int]) -> tuple[list[int], list[int]]:
    """按危害等级把三列分成（安全列，可退让列）。

    安全列是 0 级（不改阵容），可退让列含 1 级（只动备战席）；2 级一律不进候选。
    两列都按 INVEST_COLUMN_ORDER 排序，调用方可以稳定地取第一个作为默认目标。
    """
    safe: list[int] = []
    acceptable: list[int] = []
    for column in INVEST_COLUMN_ORDER:
        risk = risk_by_column.get(column, 0)
        if risk >= 2:
            continue
        acceptable.append(column)
        if risk == 0:
            safe.append(column)
    return safe, acceptable


def least_harmful_invest_column(titles: Mapping[int, Sequence[str]]) -> int:
    """三张卡都要避开时，按 LEAST_HARMFUL_INVESTMENTS 的顺序挑一列，找不到则取中间列。"""
    for name in LEAST_HARMFUL_INVESTMENTS:
        normalized = normalize_invest_text(name)
        for column in INVEST_COLUMN_ORDER:
            if any(normalized in normalize_invest_text(text) for text in titles.get(column, ())):
                return column
    return 1
