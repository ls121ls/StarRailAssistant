"""投资策略三选一的读卡与刷新流程。

主任务（``tasks/currency_wars/CurrencyWars.py``）和 cw_plus 扩展各自持有一份
任务类、互不继承，但「读取三张卡 → 判定危害等级 → 刷新 → 兜底」这段流程完全
相同。放在这里共用，避免两边各写一份后在资料刷新时行为不一致。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from .shared_constants import (
    INVEST_CARD_CENTERS, INVEST_CARDS_ROI,
    invest_column_of, investment_risk, least_harmful_invest_column,
    normalize_invest_text, rank_invest_columns,
)


def _card_text_and_column(box, width, height) -> tuple[str | None, int | None]:
    """把一条 OCR 结果映射成 (归一化文本, 卡片列号)；无法归属到卡片时返回 (None, None)。"""
    # 全角标点、OCR 断字、卡名漏叹号都要能对上屏蔽名单，先归一化再匹配。
    text = normalize_invest_text(getattr(box, "source", "") or "")
    left = getattr(box, "left", None)
    if not text or left is None:
        # 没有坐标的 OCR 结果无法判断属于哪张卡，不能拿来选卡。
        return None, None
    column = invest_column_of(left + (getattr(box, "width", 0) or 0) / 2, width, INVEST_CARD_CENTERS)
    if column is None:
        return None, None
    # 标题栏等页面文字不属于卡片，不能当作选卡依据。
    top = getattr(box, "top", 0) or 0
    box_height = getattr(box, "height", 0) or 0
    if height and not 0.16 <= (top + box_height / 2) / height <= 0.62:
        return None, None
    return text, column


def read_invest_cards(operator, *, on_error: Callable[[Exception], None] | None = None,
                      reraise: tuple[type[BaseException], ...] = ()) -> tuple[dict, dict, dict]:
    """读取三张投资策略卡，返回 (titles, card_boxes, risk_by_column)。

    :param operator: 操作器，使用它的 ocr_boxes 读取整块卡片区域
    :param on_error: OCR 失败时的回调（cw_plus 用它做停止检查）
    :param reraise: 需要原样向上抛出的异常类型（线程停止类异常不能吞掉）
    """
    roi = INVEST_CARDS_ROI
    context = getattr(operator, "window_context", None)
    width = getattr(context, "width", 0) or 0
    height = getattr(context, "height", 0) or 0
    titles: dict[int, list[str]] = {}
    card_boxes: dict[int, Any] = {}
    risk_by_column: dict[int, int] = {}
    try:
        boxes = operator.ocr_boxes(from_x=roi[0], from_y=roi[1],
                                   to_x=roi[2], to_y=roi[3], trace=False)
    except Exception as error:  # noqa: BLE001 - OCR 失败不应中断选卡流程
        if reraise and isinstance(error, reraise):
            raise
        if on_error is not None:
            on_error(error)
        else:
            logger.debug(f"投资策略卡片文字读取失败：{type(error).__name__}")
        return titles, card_boxes, risk_by_column

    for box in boxes or ():
        text, column = _card_text_and_column(box, width, height)
        if text is None or column is None:
            continue
        titles.setdefault(column, [])
        if len(titles[column]) < 3:
            titles[column].append(text[:40])
        card_boxes.setdefault(column, box)
        # 名称与效果描述都参与判定：标题没读出来时仍能靠「出售场上和备战席」等文字识别。
        risk = investment_risk(text)
        if risk > risk_by_column.get(column, 0):
            risk_by_column[column] = risk
    return titles, card_boxes, risk_by_column


def select_invest_column(risk_by_column, collection_column: int | None = None) -> tuple[int | None, bool]:
    """没有推荐策略时的默认选卡：0 级优先、1 级退让、2 级避开。

    :return: (目标列, 是否三张卡都必须避开)；全部要避开时目标列为 None
    """
    safe, acceptable = rank_invest_columns(risk_by_column)
    if safe or acceptable:
        preferred = safe or acceptable
        if collection_column is not None and collection_column in preferred:
            return collection_column, False
        return preferred[0], False
    return None, True


def refresh_blocked_invest_card(operator, refresh_image, titles, risk_by_column, width) -> bool:
    """三张卡都要避开时刷新危害最高的一张，兜底卡保持不动。

    每张卡各带一个一次性刷新按钮，刷新后必须重新识别整屏，因此这里只负责
    「点一次刷新」，是否继续刷新由调用方按次数上限控制。

    :return: True-已点击刷新按钮；False-没有可用刷新按钮
    """
    buttons = operator.locate_all(refresh_image)
    if not buttons:
        return False
    # 刷新按钮和卡片同列：先把按钮映射到列，避免把 A 列的按钮当成 B 列的。
    buttons_by_column = {}
    for box in buttons:
        column = invest_column_of(box.center[0], width, INVEST_CARD_CENTERS) if width else None
        if column is not None:
            buttons_by_column.setdefault(column, box)
    available = [column for column in sorted(risk_by_column) if column in buttons_by_column]
    if not available:
        available = sorted(risk_by_column)
    keep = least_harmful_invest_column(titles)
    # 优先刷掉危害更高、且不是兜底卡的那张；只剩兜底卡可刷时不再挑剔。
    preferred = [column for column in available if column != keep] or available
    if not preferred:
        return False
    target_column = max(preferred, key=lambda column: (risk_by_column.get(column, 0), -column))
    button = buttons_by_column.get(target_column)
    if button is None:
        button = min(buttons, key=lambda box: abs(
            box.center[0] / width - INVEST_CARD_CENTERS[target_column][0])) if width else buttons[0]
    logger.warning(f"投资策略三张卡都会清空/替换阵容，刷新第{target_column + 1}张后重新识别")
    return bool(operator.click_box(button, after_sleep=1))
