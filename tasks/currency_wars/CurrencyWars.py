import enum
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from SRACore.operators.model import Box
from SRACore.task import Executable
from SRACore.util.errors import ErrorCode, SRAError
from tasks.currency_wars.characters import Character, Characters, Positioning
from tasks.currency_wars.shared_constants import (
    EQUIPMENT_ROUNDS,
    INVEST_CARD_CENTERS as INVEST_CENTERS,
    INVEST_CARDS_ROI as INVEST_ROI,
    INVEST_STRATEGY_REFRESH_TIMES as INVEST_REFRESH_TIMES,
    invest_column_of, least_harmful_invest_column,
)
from tasks.currency_wars.invest_cards import (
    read_invest_cards, refresh_blocked_invest_card, select_invest_column,
)
from tasks.img import CWIMG, IMG, DUIMG


class Difficulty(enum.IntEnum):
    """难度模式枚举"""
    LOWEST = 0
    HIGHEST = 1
    CURRENT = 2


class CurrencyWarsPage(enum.IntEnum):
    """货币战争页面枚举"""
    START = 1
    PREPARATION = 2
    UNKNOWN = -1


# 装备节点（EQUIPMENT_ROUNDS）与 cw_plus 共用 shared_constants 中的同一份定义
# 每个角色最多穿戴3件装备
MAX_EQUIPMENT_PER_CHARACTER = 3
# 启动时若游戏正处于战斗中，等待战斗结束的最长时间（秒）
BATTLE_WAIT_TIMEOUT = 600
# 「第三位面最后一个关卡」的备战阶段：进入该关卡前直接停止任务，并邮件汇报本局投资情况
FINAL_STAGE_ROUND = (3, 7)
# 是否启用「最终关卡前停止任务」。需要全程托管时改成 False 即可
STOP_BEFORE_FINAL_STAGE = True
# ---- 无人值守告警阈值（秒），触发后发邮件提醒 ----
# 监控线程检查间隔
WATCHDOG_INTERVAL_SECONDS = 30
# 连续这么久没有任何推进 => 判定为「长时间等待/疑似卡住」（单场战斗超时是 600 秒，这里留出余量）
STALL_ALERT_SECONDS = 900
# 同一个备战关卡停留这么久 => 判定为「长时间执行同一任务」
SAME_ROUND_ALERT_SECONDS = 1200
# 单局运行这么久 => 提醒一次「本局耗时过长」，避免多局配置被误报
GAME_LONG_ALERT_SECONDS = 3600


def _looks_like_character_name(name: str) -> bool:
    """判断 OCR 文本是否可能为角色名，过滤道具数量等界面噪声。

    未打开角色面板时，名称识别区域可能读到右上角道具栏的数量角标（如“1”），
    这类文本既不是已收录角色，也不含汉字，不能当作角色处理，
    否则会在备战席中产生无法清除的幽灵角色。
    """
    if Characters.get_character(name) is not None:
        return True
    return len(name) >= 2 and re.search(r"[\u4e00-\u9fff]", name) is not None


class StageName(enum.StrEnum):
    """关卡阶段名称枚举"""
    REPLENISH_STAGE = '补给阶段'
    ENCOUNTER_NODE = '遭遇节点'
    FOLD = '无'
    SELECT_INVEST_STRATEGY = '选择投资策略'
    CLICK_BLANK = '点击空白处关闭'
    GAME_OVER = '游戏结束'
    MAIN_MENU = '主界面'
    SELECT_EQUIPMENT = '选择装备'



def _merge_ocr_lines(fragments: list[tuple[list[tuple[int, int]], str, float]]) -> list[str]:
    """按文本框坐标把 OCR 片段拼成文本行，去掉检测框重叠处被重复识别的字。

    RapidOCR 有时会把同一行文本切成多个互相重叠的文本框（例如“知更鸟·晴歌”被切成
    “知更鸟”和“鸟·晴歌”），直接按顺序拼接会多出重叠的字。这里按片段的横向重叠范围
    估算可重复字数上限，再比对文本实际重复的前缀（检测框边界不精确，且“·”等窄字符
    会拉偏平均字宽，所以只按几何估算会误删有效字符）；纵向不重叠的片段视为新的一行。
    """
    ordered = sorted(fragments, key=lambda item: (min(p[1] for p in item[0]), min(p[0] for p in item[0])))
    lines: list[str] = []
    line_right = line_top = line_bottom = 0
    for points, fragment, _ in ordered:
        left = min(point[0] for point in points)
        right = max(point[0] for point in points)
        top = min(point[1] for point in points)
        bottom = max(point[1] for point in points)
        same_line = bool(lines) and top < line_bottom and bottom > line_top
        if same_line:
            text = lines[-1]
            if left < line_right:
                char_width = (right - left) / len(fragment) if fragment else 0
                if char_width > 0:
                    # 几何重叠字数只作为上限（再多容一个字符），实际去掉多少由重复文本决定
                    limit = min(int(round((line_right - left) / char_width)) + 1, len(fragment), len(text))
                    for k in range(limit, 0, -1):
                        if fragment[:k] == text[-k:]:
                            fragment = fragment[k:]
                            break
            lines[-1] = text + fragment
            line_right = max(line_right, right)
            line_top = min(line_top, top)
            line_bottom = max(line_bottom, bottom)
        else:
            lines.append(fragment)
            line_right, line_top, line_bottom = right, top, bottom
    return lines


def _merge_ocr_text(fragments: list[tuple[list[tuple[int, int]], str, float]]) -> str:
    """按文本框坐标拼接 OCR 片段（多行直接相连），去掉重叠处被重复识别的字。"""
    return ''.join(_merge_ocr_lines(fragments))


class CurrencyWars(Executable):
    # 投资策略三选一的卡片坐标、刷新次数与「会清场/换人」的名单都和 cw_plus 共用 shared_constants，
    # 避免两个实现在资料刷新后各自维护一份而悄悄不同步。
    INVEST_CARD_CENTERS = INVEST_CENTERS
    INVEST_CARDS_ROI = INVEST_ROI
    INVEST_STRATEGY_REFRESH_TIMES = INVEST_REFRESH_TIMES

    # FOLD（无）状态下需要连续识别到多少次才认定为真的回到备战界面。
    # 战斗结束后画面还在过渡时，偶尔只有「收起」图标能被匹配到，单次命中就终止
    # 会抢跑跳过刚弹出的遭遇节点/补给阶段界面。
    FOLD_CONFIRM_TIMES = 3

    def __init__(self, operator, runtimes, isPointRewardsEnabled: bool = False):
        super().__init__(operator)
        self.runtimes = runtimes
        self.isPointRewardsEnabled = isPointRewardsEnabled
        self.special_events: dict[str, tuple[str, Callable[[], bool]]] = {
            "领航员": ("选择伙伴", self.handle_choose_partner),
            "盛会之星": ("盛会之星", self.handle_the_planet_of_festivities),
            "命运圣杯": ("祈愿试炼", self.handle_wish_trial),
            "头号玩家": ("我来当策划", self.handle_silver_wolf_lv999),
            "开拓银轨": ("开拓商店", self.handle_trailblaze_store),
            "奇兽花园": ("奇兽花园", self.handle_beast_garden),
            "梦境交织": ("梦境交织", self.handle_dream_interference),
            "巡海游侠": ("巡海游侠", self.handle_sea_trader),
            "黑市主理人": ("黑市主理人", self.handle_black_market_manager),
            "双龙吟": ("双龙吟", self.handle_double_dragon),
            "苍龙濯世": ("苍龙濯世", self.handle_world_cleansing_dragon),
            "从师": ("从师", self.handle_apprentice_selection),
        }
        self.is_continue = False  # 是否是继续挑战
        self.is_game_over = False
        self.skip_current_round = False  # 前台无角色时跳过本轮剩余流程，直接刷新角色并开始新一轮
        self.stopped_before_final_stage = False  # 是否已在最终关卡前主动停止任务
        self.selected_invest_environments: list[str] = []  # 本局选过的投资环境
        self.selected_invest_strategies: list[str] = []  # 本局选过的投资策略
        self.invest_strategy_misses = 0  # 未命中候选、按收藏卡/中间卡兜底的次数
        # ---- 无人值守监控（异常 / 长时间等待 / 同一关卡过久 / 单局过久 都会发邮件）----
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._task_started_at = time.monotonic()
        self._game_started_at = 0.0
        self._last_progress_at = time.monotonic()
        self._progress_note = "任务启动"
        self._current_round: tuple[int, int] | None = None
        self._round_since = 0.0
        self._stall_alerted = False
        self._same_round_alerted = False
        self._long_game_alerted = False
        self.difficulty: int = Difficulty.LOWEST
        self.on_field_character: list[Character | None] = [None, None, None, None]  # 前台角色列表
        self.on_field_area: list[tuple[float, float]] = [
            (0.386, 0.365),
            (0.464, 0.365),
            (0.536, 0.365),
            (0.611, 0.365)
        ]
        self.off_field_character: list[Character | None] = [None, None, None, None, None, None]  # 后台角色列表
        self.off_field_area: list[tuple[float, float]] = [
            (0.3056, 0.620),
            (0.3806, 0.620),
            (0.4525, 0.620),
            (0.5264, 0.620),
            (0.6004, 0.620),
            (0.6738, 0.620)
        ]
        self.in_hand_character: list[Character | None] = [None, None, None, None, None, None, None, None,
                                                          None]  # 备战席角色列表
        self.in_hand_area: list[tuple[float, float]] = [
            (0.229, 0.844),
            (0.297, 0.844),
            (0.358, 0.844),
            (0.426, 0.844),
            (0.488, 0.844),
            (0.556, 0.844),
            (0.618, 0.844),
            (0.684, 0.844),
            (0.749, 0.844)
        ]  # 备战席区域
        self.store_area: list[tuple[float, float]] = [
            (0.25, 0.18),
            (0.40, 0.18),
            (0.55, 0.18),
            (0.68, 0.18),
            (0.80, 0.18)
        ]
        self.max_team_size = 3  # 最大队伍人数
        self.is_running = False
        self.min_coins = 40  # 最小金币数
        self.min_level = 7  # 商店等级
        self.mid_level = 7  # 商店等级
        self.strategy_characters: dict[str, int] = dict()  # 在攻略中的角色及其预期星级
        self.strategy_code = ""  # 当前使用的攻略代码
        self.strategy_special_events: dict[str, str] = dict()  # 攻略中的特殊事件处理
        self.recommend_invest_strategies: tuple[str, ...] = ()  # 攻略推荐的投资策略（分号分隔，越靠前越优先）
        self.is_overclock = False  # 超频博弈
        self.is_complex_invest_strategy_selection_enabled = False  # 是否启用复杂投资策略选择逻辑

    def run(self):
        self._start_health_monitor()
        try:
            return self._run()
        finally:
            self._stop_health_monitor()

    def _run(self):
        self.is_running = True
        if self.runtimes == 0:
            logger.warning("货币战争运行次数为 0 ，本次将跳过运行。")
            return True
        
        for i in range(self.runtimes):
            if not self.is_running:
                break
            if self.isPointRewardsEnabled:
                page = self.page_locate()
                if page == CurrencyWarsPage.START and self.check_point_rewards():
                    logger.info("旷宇纷争积分奖励已满，跳过货币战争任务")
                    self.receive_point_rewards()
                    break
            logger.info(f"第{i + 1}次进入货币战争，剩余{self.runtimes - i - 1}次")
            self._game_started_at = time.monotonic()
            self._long_game_alerted = False
            self._mark_progress(f"第{i + 1}局开始")
            try:
                if self.start_game():
                    self.game_loop()
                    if self.stopped_before_final_stage:
                        break
                    if not self.should_continue_next_round():
                        break
            except Exception as e:
                logger.error(e)
                self._notify_exception(e)
                return False
        if self.stopped_before_final_stage:
            # 最终关卡前已按配置停止任务：保留现场、不做任何收尾操作
            logger.info("已按最终关卡停止要求结束任务，游戏停留在备战界面")
            # 已置位停止事件时返回 False，任务队列不会再执行后续任务，界面状态显示为「已停止」
            return not self._is_stop_requested()
        self.handle_ending()
        return True

    def _is_stop_requested(self) -> bool:
        """停止事件是否已置位（界面点「停止任务」或本任务主动停止）"""
        stop_event = getattr(getattr(self, "operator", None), "stop_event", None)
        return stop_event is not None and stop_event.is_set()

    def should_continue_next_round(self) -> bool:
        """ 一局结束后是否继续进入下一局。默认继续，子类可重写
        （如刷开局在达成理想开局并打完后不再重开）。 """
        return True

    def handle_ending(self):
        """ 处理游戏结束逻辑，默认退出货币战争页面 """
        self.operator.wait_img(CWIMG.START_CURRENCY_WARS, timeout=30)
        self.operator.press_key("esc")

    def check_point_rewards(self) -> bool:
        """检查旷宇纷争积分奖励是否已满（需处于货币战争开始页面）"""
        result = self.operator.ocr(from_x=0.1, from_y=0.89, to_x=0.21, to_y=0.96)
        # 格式 x/18000，OCR可能将结果切断成多个部分或产生噪声，用正则匹配开头和结尾都是18000
        if result:
            current = "".join(item[1] for item in result)
            logger.info(f"当前积分奖励: {current}")
            if re.match(r'^18000.*18000$', current):
                return True
        return False

    def receive_point_rewards(self):
        self.operator.click_point(0.15, 0.93, tag="领取积分奖励", after_sleep=1)
        self.operator.click_img(DUIMG.RECEIVE, after_sleep=1)
        self.operator.press_key("esc")

    def reset_character(self):
        """ 重置所有角色信息 """
        for i, c in enumerate(self.on_field_character):
            if c is not None:
                c.is_placed = False  # 重置放置状态
                c.stars = 1  # 重置星级
                c.equip_count = 0  # 重置装备计数
            self.on_field_character[i] = None  # 重置角色信息
        for i, c in enumerate(self.off_field_character):
            if c is not None:
                c.is_placed = False
                c.stars = 1
                c.equip_count = 0
            self.off_field_character[i] = None
        for i, c in enumerate(self.in_hand_character):
            if c is not None:
                c.is_placed = False
                c.stars = 1
                c.equip_count = 0
            self.in_hand_character[i] = None

    def set_difficulty(self, mode: int):
        """设置难度模式：0=最低难度，1=最高难度，2=当前难度（不切换）"""
        self.difficulty = mode

    def set_overclock(self, overclock: bool):
        self.is_overclock = overclock

    def page_locate(self, depth = 0) -> CurrencyWarsPage:
        """
        定位到货币战争页面

        :return: CurrencyWarsPage 枚举值，表示当前所在页面；如果未识别到任何目标页面，则返回 CurrencyWarsPage.UNKNOWN
        """
        if depth>5:
            raise RuntimeError("页面定位递归过深，可能存在无限循环")
        page, _ = self.operator.wait_any_img([IMG.ENTER,
                                              CWIMG.START_CURRENCY_WARS,
                                              CWIMG.PREPARATION_STAGE,
                                              DUIMG.BONUS_POINTS], interval=0.5)
        if page == 0:
            if self.operator.locate(CWIMG.CURRENCY_WARS): # 货币战争门口
                self.operator.press_key("f")
            else:
                logger.info("[页面定位] 正在定位到货币战争开始页面")
                guide_hotkey = self.settings.General.hotkeyF4
                self.operator.press_key(str(guide_hotkey).lower())
                if not self.operator.wait_img(IMG.F4, timeout=20):
                    logger.error(SRAError(ErrorCode.WAIT_TIMEOUT, "等待指南界面超时"))
                    self.operator.press_key("esc")
                box = self.operator.wait_img(IMG.COSMIC_STRIFE, timeout=10)
                self.operator.click_box(box, after_sleep=1)  # 旷宇纷争
                self.operator.click_point(0.242, 0.30, after_sleep=0.8)  # 货币战争
                self.operator.click_point(0.7786, 0.8194, after_sleep=1)  # 前往参与
            return self.page_locate(depth + 1)
        elif page == 1:
            logger.info("[页面定位] 已定位到货币战争开始页面")
            return CurrencyWarsPage.START
        elif page == 2:
            logger.info("[页面定位] 已定位到货币战争准备阶段")
            return CurrencyWarsPage.PREPARATION
        elif page == 3:
            self.operator.press_key("esc")
            return CurrencyWarsPage.START
        else:
            logger.error("[页面定位] 检测超时，未识别到任何目标页面")
            return CurrencyWarsPage.UNKNOWN

    def _wait_battle_finish(self) -> bool:
        """启动时若游戏正处于战斗中，等待战斗结束并推进战后流程。

        仅在页面定位失败时调用。战斗画面不一定出现 Q 键提示，因此只要画面
        持续无法识别就保持轮询，直到战斗结算出现或超时。

        :return: True-当前战斗已结束且战后流程已处理，可重新定位页面；
                 False-等待超时，未识别到战斗结束
        """
        page_templates = [IMG.ENTER, CWIMG.START_CURRENCY_WARS,
                          CWIMG.PREPARATION_STAGE, DUIMG.BONUS_POINTS]
        battle_templates = [IMG.Q, CWIMG.SETTLE, CWIMG.CONTINUE]
        templates = page_templates + battle_templates
        logger.info("[启动流程] 未识别到已知页面，检查游戏是否正处于战斗中")
        start_time = time.monotonic()
        next_log_time = start_time + 30
        combat_logged = False
        while time.monotonic() - start_time < BATTLE_WAIT_TIMEOUT:
            index, _ = self.operator.locate_any(templates, trace=False)
            if 0 <= index < len(page_templates):
                logger.info("[启动流程] 重新识别到已知页面，结束战斗等待")
                return True
            if index == len(page_templates):  # IMG.Q：入战提示
                if not combat_logged:
                    logger.info("[启动流程] 检测到游戏正处于战斗中，等待战斗结束")
                    combat_logged = True
            elif index != -1:  # CWIMG.SETTLE / CWIMG.CONTINUE：战斗结算界面
                logger.info("[启动流程] 检测到战斗已结束，继续战后流程")
                return self._continue_after_battle()
            self.operator.sleep(1)
            if time.monotonic() >= next_log_time:
                logger.info(f"[启动流程] 仍在等待战斗结束（已等待{int(time.monotonic() - start_time)}秒）")
                next_log_time += 30
        logger.error(f"[启动流程] 等待战斗结束超时（{BATTLE_WAIT_TIMEOUT}秒）")
        return False

    def _continue_after_battle(self) -> bool:
        """点击战斗结算的继续按钮，并按正常节奏处理战后的关卡切换与商店流程。"""
        self.operator.sleep(0.5)
        if not self.operator.click_point(0.5, 0.824, after_sleep=1, tag="点击继续按钮"):
            logger.error("[启动流程] 点击战斗结算继续按钮失败")
            return False
        self.stage_transition()
        if self.is_game_over:
            logger.info("[启动流程] 当前对局已结束，重新进入对局")
        else:
            self.shopping()
        return True

    def start_game(self):
        """开始一局货币战争。
        拆分原复杂流程为多个独立步骤，提升可读性与可维护性。
        返回 True 表示进入对局并完成初始策略与备战席识别
            False 表示流程中断或失败。
        """
        page = self.page_locate()
        if page == CurrencyWarsPage.UNKNOWN and self._wait_battle_finish():
            # 游戏正在战斗中：等待结算并推进战后流程后重新定位
            page = self.page_locate()
        if page == CurrencyWarsPage.UNKNOWN:
            logger.error("[启动流程] 页面定位失败，无法开始游戏")
            return False
        self.is_running = True  # 标记任务运行中
        if not self._handle_page_enter(page):
            logger.error("[启动流程] 进入对局流程失败")
            self.is_running = False
            return False

        if not self.handle_game_entered():
            return False
        # 对局初始化
        init_success = self._initialize_game()
        if not init_success:
            logger.error("[启动流程] 对局初始化失败")
            self.is_running = False
            return False
        if not self.handle_game_initialized():
            return False
        logger.info("[启动流程] 对局启动成功")
        self.is_game_over = False
        return True

    def _handle_page_enter(self, page: CurrencyWarsPage) -> bool:
        """
        根据页面类型处理进入逻辑
        :param page: 页面枚举值
        :return: True-进入成功，False-进入失败
        """
        if page == CurrencyWarsPage.START:
            # 开始页面 → 执行进入流程
            return self._enter_from_start_page()
        elif page == CurrencyWarsPage.PREPARATION:
            # 准备阶段 → 跳过进入流程
            self.is_continue = True
            logger.info("[进入流程] 已处于准备阶段，跳过进入流程")
            return True
        else:
            return False

    # ==================== 进入流程辅助方法 ====================
    def _enter_from_start_page(self) -> bool:
        """
        从开始页面进入对局
        :return: True-进入成功，False-进入失败
        """
        # 等待开始按钮
        index, box = self.operator.wait_any_img(
            [CWIMG.START_CURRENCY_WARS, CWIMG.INSTRUCTIONS],
            timeout=30,
            interval=0.5
        )
        if index == -1:
            logger.error("[进入流程] 未识别到开始按钮")
            return False
        if index == 1:
            self.operator.press_key("esc")  # 赛季扩充说明
            box = self.operator.wait_img(CWIMG.START_CURRENCY_WARS, timeout=30)  # 重新获取开始游戏按钮
        # 重试点击开始按钮直到LOGO消失
        self.operator.do_while(
            lambda: self.operator.click_box(box),  # NOQA  # pyright: ignore[reportArgumentType]
            lambda: self.operator.locate(CWIMG.LOGO) is None,
            interval=2,
            max_iterations=10
        )
        self.operator.sleep(1)  # 等待界面响应
        if self.is_overclock:
            self.operator.click_point(0.15625, 0.4167, after_sleep=1, tag="点击超频博弈")
        else:
            self.operator.click_point(0.15625, 0.2315, after_sleep=1, tag="点击标准博弈")

        # 选择进入方式（标准进入/继续进度）
        enter_type, enter_box = self.operator.wait_any_img(
            [CWIMG.ENTER_GAME, CWIMG.CONTINUE_PROGRESS],
            timeout=10
        )
        if enter_box is None:
            logger.error("[进入流程] 未识别到任何进入方式（标准/继续）")
            return False

        # 执行对应进入逻辑
        if enter_type == 0:  # 标准进入
            return self._enter_new_game(enter_box)
        elif enter_type == 1:  # 继续进度
            return self._enter_continue_game(enter_box)

        return False

    def _enter_new_game(self, enter_button_box) -> bool:
        """
        新游戏进入流程
        :param enter_button_box: 进入按钮坐标框
        :return: True-成功，False-失败
        """
        # 1. 点击进入按钮
        if not self.operator.click_box(enter_button_box, after_sleep=1.5):
            logger.error("[新游戏] 点击进入按钮失败")
            return False

        # 2. 难度选择
        self._select_difficulty()

        # 3. 点击开始对局
        if not self.operator.click_img(CWIMG.START_GAME, after_sleep=1):
            logger.error("[新游戏] 点击开始对局按钮失败")
            return False

        # 4. 处理Boss信息
        if not self._handle_boss_info_flow():
            return False

        # 5. 处理投资环境
        self._handle_invest_environment_flow()
        self.operator.sleep(8)  # 等待开局补给
        return True

    def _select_difficulty(self) -> None:
        """难度选择逻辑（抽离后更易维护）"""
        if self.difficulty == Difficulty.HIGHEST:
            # 最高难度：点击返回最高职级
            logger.info("[难度选择] 选择最高难度")
            self.operator.click_img(CWIMG.RETURN_HIGHEST_RANK, after_sleep=0.8)
        elif self.difficulty == Difficulty.LOWEST:
            # 最低难度：循环点击下拉箭头
            logger.info("[难度选择] 选择最低难度")
            while self.operator.click_img(CWIMG.DOWN_ARROW, after_sleep=0.5):
                pass
        elif self.difficulty == Difficulty.CURRENT:
            # 当前难度：不操作
            logger.info("[难度选择] 保持当前难度")
        else:
            logger.warning(f"[难度选择] 未知难度模式：{self.difficulty}，使用当前难度")

    def _handle_boss_info_flow(self) -> bool:
        """处理Boss信息流程（抽离独立方法）"""
        # 等待下一步提示
        next_step_box = self.operator.wait_img(
            CWIMG.NEXT_STEP,
            timeout=10
        )
        if not next_step_box:
            logger.error("[Boss信息] 未识别到下一步提示")
            return False

        # 处理Boss信息 + 点击下一步
        self.operator.sleep(0.5)
        self.handle_boss_info()
        if not self.operator.do_while(
            lambda: self.operator.click_box(next_step_box, after_sleep=1),
            lambda: self.operator.locate(CWIMG.NEXT_STEP) is not None,
            interval=1,
            max_iterations=10
        ):
            logger.error("[Boss信息] 点击下一步按钮失败")
            return False
        # 点击空白处
        self.operator.click_point(0.6, 0.5, after_sleep=0.5, tag="点击空白处")
        return True

    def _handle_invest_environment_flow(self) -> bool:
        # 检查投资环境界面
        if not self.operator.wait_img(CWIMG.INVEST_ENVIRONMENT):
            logger.error("[投资环境] 未识别到投资环境界面")
            return False
        self.operator.sleep(1)  # 等待界面稳定
        self.handle_invest_environment()
        # 有些情况下点击投资环境界面后会再次弹出投资环境界面，需要再次处理
        if self.operator.locate(CWIMG.INVEST_ENVIRONMENT):
            self.handle_invest_environment()
        return True

    # ==================== 继续进度进入逻辑（重构后） ====================
    def _enter_continue_game(self, continue_button_box) -> bool:
        """
        继续进度进入流程（重构后，异常处理更规范）
        :param continue_button_box: 继续按钮坐标框
        :return: True-成功，False-失败
        """
        self.is_continue = True
        # 点击继续按钮
        if not self.operator.click_box(continue_button_box, after_sleep=1):
            logger.error("[继续进度] 点击继续按钮失败")
            return False

        try:
            # 等待空白提示并点击
            self.operator.sleep(2)
            click_blank_box = self.operator.wait_img(
                CWIMG.CLICK_BLANK,
                timeout=10
            )
            if not click_blank_box:
                logger.error("[继续进度] 未识别到点击空白提示")
                return False
            self.operator.click_box(click_blank_box, after_sleep=2)
            logger.info("[继续进度] 进入对局成功")
            return True
        except Exception as e:
            logger.error(f"[继续进度] 流程异常：{str(e)}", exc_info=True)
            return False

    # ==================== 对局初始化（重构后） ====================
    def _initialize_game(self) -> bool:
        """
        对局初始化（重构后，区分继续进度和新游戏）
        :return: True-初始化成功，False-初始化失败
        """
        self.operator.sleep(1)
        # 新对局重新统计每个角色的装备件数
        for character in Characters.characters.values():
            character.equip_count = 0
        if self.is_continue:
            # 继续进度：获取前台角色信息
            logger.info("[初始化] 继续进度")
            self.refresh_character()
        # 新游戏：没有初始化逻辑
        return True

    def handle_boss_info(self) -> None:
        """处理进入对局后可能出现的Boss信息提示框。

        如果需要额外的操作（如记录Boss信息、调整策略等），可以在此方法中实现。"""
        ...

    def handle_invest_environment(self) -> None:
        """处理投资环境界面

        标准模式下，简单地选择中间的投资环境或选择尚未收集的投资环境，点击确定即可。
        """
        if not self.operator.click_img(CWIMG.COLLECTION):
            self.operator.click_point(0.5, 0.5, tag="选择中间的投资环境")
        self.operator.click_img(IMG.ENSURE2, after_sleep=1)
        if self.operator.locate(CWIMG.INVEST_ENVIRONMENT):
            self.operator.click_point(0.5, 0.5, after_sleep=1)
            self.operator.click_img(IMG.ENSURE2, after_sleep=1)

    def handle_game_entered(self) -> bool:  # NOQA
        """进入游戏的后续处理逻辑（如有）。默认实现为无操作，子类可重写此方法以添加额外逻辑。

        调用时机：在完成进入流程并进入对局后、初始化角色信息前调用
        Returns:
            True-继续执行后续初始化和游戏循环；False-中止进入流程
        """
        if self.is_continue or not self.strategy_code:
            return True
        self.operator.click_img(CWIMG.STRATEGY, after_sleep=1, retry=3)
        self.operator.click_img(CWIMG.HOT_STRATEGIES, after_sleep=0.5)
        self.operator.move_to(0.5, 0.5)
        self.operator.sleep(0.5)
        self.operator.click_img(CWIMG.ENTER_STRATEGY_CODE, after_sleep=1)
        self.operator.click_point(0.5, 0.5, after_sleep=1, tag="攻略码输入框")
        self.operator.copy(self.strategy_code)
        self.operator.paste()
        self.operator.sleep(1)
        self.operator.click_img(IMG.ENSURE2, after_sleep=1)
        box = self.operator.wait_img(CWIMG.APPLY_STRATEGY)
        if box:
            self.operator.click_box(box, after_sleep=1)
        else:
            logger.warning("[应用策略] 未识别到应用策略按钮")
        
        # 恢复到主界面
        self.operator.do_while(
            lambda: self.operator.press_key('esc'),
            lambda: self.operator.locate(CWIMG.QUIT) is None,
            interval=0.5,            max_iterations=5)
        return True

    def handle_game_initialized(self) -> bool:  # NOQA
        """对局初始化完成后的处理逻辑（如有）。默认实现为无操作，子类可重写此方法以添加额外逻辑。

        调用时机：在完成角色放置和备战席识别后、正式进入游戏循环前调用
        Returns:
            True-继续执行游戏循环；False-中止进入流程
        """
        return True

    def abort_and_return(self, retries: int = 3) -> bool:
        """中止当前对局并返回开始页面

        退出流程常因确认弹窗延迟弹出或被其他弹窗遮挡而识别失败，
        因此整体重试：每次失败先按 ESC 关闭残留弹窗，再重新定位退出按钮。
        """
        for attempt in range(1, retries + 1):
            logger.info("中止当前对局并返回开始页面")
            if self._abort_current_game():
                return True
            if attempt < retries:
                self.operator.press_key("esc")
                self.operator.sleep(1)
        logger.error(f"退出对局失败，已重试{retries}次")
        return False

    def _abort_current_game(self) -> bool:
        """执行一次退出对局流程，成功返回 True"""
        # 已经回到开始页面时无需再退出
        if self.operator.locate(CWIMG.START_CURRENCY_WARS) is not None:
            return True

        index, box = self.operator.wait_any_img([CWIMG.QUIT, CWIMG.WITHDRAW_AND_SETTLE])
        if index < 0:
            return False
        if index == 0:
            self.operator.click_box(box, after_sleep=0.5)
            # 确认按钮可能延迟弹出，先等待再点击
            confirm_box = self.operator.wait_img(CWIMG.WITHDRAW_AND_SETTLE, timeout=8)
            if confirm_box is None:
                return False
            self.operator.click_box(confirm_box, after_sleep=1)
        else:
            self.operator.click_box(box, after_sleep=1)

        # 结算页面出现较慢，延长等待时间
        if self.operator.wait_img(CWIMG.NEXT_STEP, timeout=120) is None:
            return False
        self.handle_game_over(None)
        return True

    def game_loop(self):
        run_times = 0
        # 从存档继续时，第一场战斗前不会走到循环末尾的备战关卡读取，这里先补一次判断
        first_round = self._read_preparation_round()
        if first_round is not None:
            self._note_round(first_round)
        if first_round is not None and self._should_stop_before_final_stage(first_round):
            self.stop_before_final_stage(first_round)
            return True
        while self.is_running:
            self._mark_progress()
            self.harvest_crystals()
            self.get_in_hand_area(True)  # 更新备战席信息
            self.handle_characters_updated()
            self.update_max_team_size()
            self.place_character()
            self.sell_character()
            if not self.battle():
                if self.skip_current_round:  # 前台无角色，跳过本轮剩余流程
                    self.skip_current_round = False
                    self.refresh_character()
                    self.sort_all_areas_by_priority()  # 优先级排序
                    continue
                break
            self.handle_special_event()
            if not self.stage_transition():
                break
            if self.is_game_over:  # 任务已被标记为停止, 需要退出循环
                break
            self.handle_special_event()
            self.shopping()

            run_times += 1
            current_round = self._read_preparation_round()
            if current_round is not None:
                self._note_round(current_round)
                is_equipment_round = current_round in EQUIPMENT_ROUNDS
            else:
                # 备战关卡未识别时按战斗间隔兜底，避免整局不检查装备
                logger.debug(f"备战关卡未识别（第{run_times}轮），按战斗间隔兜底检查装备")
                is_equipment_round = run_times % 2 == 0
            if is_equipment_round:  # 装备只在每个位面的第4个节点和最后一个节点处理
                self.refresh_character()
                self.sort_all_areas_by_priority()  # 优先级排序
            elif run_times % 2 == 0:  # 每2轮更新一次前台角色信息，不打开装备面板
                self.refresh_character(equip=False)

            # 装备、商店都已处理完，下一轮就是要打的关卡：第三位面最终关卡前停止任务并通知
            if current_round is not None and self._should_stop_before_final_stage(current_round):
                self.stop_before_final_stage(current_round)
                break
        # 游戏循环结束，重置角色状态
        self.reset_character()
        return True

    def _should_stop_before_final_stage(self, current_round: tuple[int, int]) -> bool:
        """是否应在「第三位面最后一个关卡」的备战阶段停止任务"""
        if not STOP_BEFORE_FINAL_STAGE or self.stopped_before_final_stage:
            return False
        return tuple(current_round) == FINAL_STAGE_ROUND

    # ------------------------------------------------------------------
    # 无人值守监控：异常 / 长时间等待 / 长时间停留在同一关卡 / 单局过久
    # ------------------------------------------------------------------

    def _start_health_monitor(self) -> None:
        """启动后台监控线程（只在 run() 期间存活）。"""
        now = time.monotonic()
        self._task_started_at = now
        self._game_started_at = now
        self._last_progress_at = now
        self._round_since = now
        self._current_round = None
        self._progress_note = "任务启动，等待进入对局"
        self._stall_alerted = False
        self._same_round_alerted = False
        self._long_game_alerted = False
        self._watchdog_stop = threading.Event()
        thread = threading.Thread(target=self._health_monitor_loop, name="cw-watchdog", daemon=True)
        self._watchdog_thread = thread
        thread.start()
        logger.debug("无人值守监控已启动（阈值：无进展 15 分钟 / 同一关卡 20 分钟 / 单局 60 分钟）")

    def _stop_health_monitor(self) -> None:
        stop = getattr(self, "_watchdog_stop", None)
        if stop is not None:
            stop.set()

    def _mark_progress(self, note: str = "") -> None:
        """任务心跳：每完成一轮可观测的推进时调用，用于区分「在推进」和「卡住」。"""
        self._last_progress_at = time.monotonic()
        self._stall_alerted = False
        if note:
            self._progress_note = note

    def _note_round(self, current_round: tuple[int, int]) -> None:
        """记录当前备战关卡，用于检测「长时间停在同一个关卡」。"""
        current_round = (int(current_round[0]), int(current_round[1]))
        if current_round != self._current_round:
            self._current_round = current_round
            self._round_since = time.monotonic()
            self._same_round_alerted = False
        self._progress_note = f"第{current_round[0]}位面 {current_round[0]}-{current_round[1]}"

    def _health_monitor_loop(self) -> None:
        while not self._watchdog_stop.wait(WATCHDOG_INTERVAL_SECONDS):
            try:
                self._check_health()
            except Exception as err:  # 监控自身异常不影响任务
                logger.debug(f"监控检查异常（忽略）：{err}")

    def _check_health(self) -> None:
        now = time.monotonic()
        # 人工停止 / 任务已结束属于预期状态，不告警，并把基准时间推后
        if self.stopped_before_final_stage or not self.is_running:
            self._last_progress_at = now
            self._game_started_at = now
            return

        idle = now - self._last_progress_at
        if idle >= STALL_ALERT_SECONDS and not self._stall_alerted:
            self._stall_alerted = True
            self._send_health_alert(
                "货币战争长时间无进展",
                f"已经 {int(idle // 60)} 分钟没有检测到任何推进，可能卡在等待界面或图片识别。\n"
                f"当前阶段：{self._progress_note}\n"
                f"{self._invest_summary()}\n"
                "如果游戏界面正常，请人工确认后决定是否继续。",
                dedup_key=f"cw-stall|{self._progress_note}",
            )

        if (self._current_round is not None and not self._same_round_alerted
                and now - self._round_since >= SAME_ROUND_ALERT_SECONDS):
            self._same_round_alerted = True
            self._send_health_alert(
                "货币战争长时间停留在同一关卡",
                f"已经在「{self._progress_note}」停留 {int((now - self._round_since) // 60)} 分钟，"
                "可能在反复重试或战斗没有正常结束。\n"
                f"本局已运行：{int((now - self._game_started_at) // 60)} 分钟\n"
                f"{self._invest_summary()}",
                dedup_key=f"cw-round|{self._current_round}",
            )

        if not self._long_game_alerted and now - self._game_started_at >= GAME_LONG_ALERT_SECONDS:
            self._long_game_alerted = True
            self._send_health_alert(
                "货币战争单局运行时间过长",
                f"本局已连续运行 {int((now - self._game_started_at) // 60)} 分钟。\n"
                f"当前阶段：{self._progress_note}\n"
                f"{self._invest_summary()}\n"
                "如果这不是预期行为，请检查游戏界面或停止任务。",
                dedup_key="cw-long-game",
                result="running",
            )

    def _notify_exception(self, error: BaseException) -> None:
        """任务异常终止时发邮件：带上阶段、已选投资和最近的调用栈。"""
        import traceback

        summary = "".join(traceback.format_exception_only(type(error), error)).strip()
        stack = traceback.format_exc(limit=8)
        message = (
            "货币战争任务异常终止，自动化已停止。\n"
            f"当前阶段：{self._progress_note}\n"
            f"{self._invest_summary()}\n"
            f"异常：{summary}\n"
            f"最近调用栈：\n{stack[-1500:]}"
        )
        logger.error(f"任务异常：{summary}")
        self._send_health_alert(
            "货币战争任务异常",
            message,
            dedup_key=f"cw-exception|{type(error).__name__}|{str(error)[:60]}",
        )

    def _send_health_alert(self, title: str, message: str, *, dedup_key: str, result: str = "timeout") -> None:
        """统一出口：日志 + 邮件（后台线程发送，不阻塞任务）。"""
        logger.warning(f"{title}：{message.splitlines()[0] if message else ''}")
        try:
            from SRACore.notification.alerts import send_alert_email

            send_alert_email(
                self.settings,
                title,
                message,
                result=result,
                dedup_key=dedup_key,
                async_send=True,
            )
        except Exception as err:  # 邮件失败不影响任务
            logger.warning(f"提醒邮件发送异常：{err}")

    def _record_invest_environment(self, name: str) -> None:
        """记录本局选中的投资环境（去重）"""
        name = (name or "").strip()
        if not name or name in self.selected_invest_environments:
            return
        self.selected_invest_environments.append(name)
        logger.info(f"本局已选投资环境：{name}")

    def _record_invest_strategy(self, name: str) -> None:
        """记录本局选中的投资策略（去重）"""
        name = (name or "").strip()
        if not name or name in self.selected_invest_strategies:
            return
        self.selected_invest_strategies.append(name)
        logger.info(f"本局已选投资策略：{name}")

    def _invest_summary(self) -> str:
        """本局已选投资环境与投资策略的文字摘要"""
        lines = [
            "投资环境：" + ("、".join(self.selected_invest_environments) or "未记录"),
            "投资策略：" + ("、".join(self.selected_invest_strategies) or "未记录"),
        ]
        if self.invest_strategy_misses:
            lines.append(f"备注：有 {self.invest_strategy_misses} 次没刷到候选策略，按收藏卡/中间卡兜底选择")
        return "\n".join(lines)

    def stop_before_final_stage(self, current_round: tuple[int, int]) -> None:
        """第三位面最终关卡前直接停止任务：发邮件告知已选的投资环境与投资策略，然后结束自动化

        停止后游戏停留在备战界面，不做任何退出/按键操作，可直接人工接手。
        """
        self.stopped_before_final_stage = True
        self.is_running = False  # 让 game_loop / run 的循环条件立即结束
        plane, node = current_round
        message = (
            f"货币战争即将进入第{plane}位面的最后一个关卡（{plane}-{node} 首领），已按要求停止任务。\n"
            f"{self._invest_summary()}\n"
            "游戏仍停留在备战界面，未做任何退出操作，可直接人工接手。"
        )
        logger.warning("=" * 64)
        for line in message.splitlines():
            logger.warning(line)
        logger.warning("=" * 64)
        self._send_stop_mail(message)
        # 置位停止事件：任务队列不会继续执行后续任务，界面状态显示为「已停止」
        stop_event = getattr(getattr(self, "operator", None), "stop_event", None)
        if stop_event is not None:
            stop_event.set()
        logger.warning("已在最终关卡前停止任务，游戏停留在备战界面")

    def _send_stop_mail(self, message: str) -> None:
        """发送停止提醒（受「设置 → 通知 → 邮件」开关控制）：邮件为主，桌面通知兜底。

        开关关闭时整条提醒链路静默，投资摘要仍会逐行写进日志，不影响停止本身。
        """
        try:
            from SRACore.notification.channels.email import EmailChannel
            from SRACore.notification.channels.system import SystemChannel
            from SRACore.notification.models import NotificationContext
            from SRACore.notification.service import build_notification_payload

            config = self.settings.Notification
            if not getattr(config, "isEmailEnabled", False):
                logger.info("邮件提醒已关闭，本次停止提醒仅记录日志（设置 → 通知 → 邮件 可打开）")
                return

            payload = build_notification_payload("货币战争停止", message, "info")
            context = NotificationContext(
                title="SRA",
                message=message,
                payload=payload,
                settings=config,
            )
            if EmailChannel().send(context):
                logger.info("停止提醒邮件已发送")
            else:
                logger.warning("停止提醒邮件发送失败：请在 设置 → 通知 → 邮件 里填写 SMTP 服务器、发件人、授权码和收件人")
            # 桌面通知兜底：邮件通道没配置时也能看到提醒
            try:
                SystemChannel().send(context)
            except Exception as err:
                logger.debug(f"系统通知发送失败（忽略）：{err}")
        except Exception as err:  # 通知失败不能影响停止本身
            logger.warning(f"停止提醒发送异常：{err}")

    def _read_preparation_round(self) -> tuple[int, int] | None:
        """OCR识别左上角"备战阶段 X-Y"标签，返回(位面, 节点)；无法识别时返回None。"""
        rois = ((0.105, 0.02, 0.18, 0.135), (0.205, 0.02, 0.285, 0.105))
        for from_x, from_y, to_x, to_y in rois:
            rows = self.operator.ocr(from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y)
            if not rows:
                continue
            normalized = re.sub(r"\s", "", " ".join(str(row[1]) for row in rows))
            if "备战阶段" not in normalized:
                continue
            match = re.search(r"([1-9])-([1-9])", normalized)
            if match:
                logger.debug(f"当前备战关卡：{match[1]}-{match[2]}")
                return int(match[1]), int(match[2])
        return None

    def refresh_character(self, equip: bool = True):
        """ 刷新角色信息，确保备战席中未放置的角色状态正确。

        扫描骨架与上游 v2.25.2 保持一致（前台扫描 -> 后台槽位识别含矩形去重 -> 后台扫描），
        OCR 统一走 _merge_ocr_lines 逐行拼接；在此基础上把「穿戴」从扫描里剥离：
        完整扫描后只按缓存给可装备的出场角色穿戴/合成，不再为装备重扫一遍所有槽位。
        """
        self.get_on_field_area(True)
        self.operator.sleep(0.3)
        self._sync_off_field_area()
        self.get_off_field_area(True)
        self.operator.sleep(0.3)
        if equip:
            self.equip_cached_characters(sync_layout=False)
        field_characters = self.on_field_character + self.off_field_character
        for c in self.in_hand_character:
            if c is not None and c not in field_characters:
                c.is_placed = False

    def _sync_off_field_area(self):
        """只识别后台槽位坐标，不点击任何槽位。

        矩形去重沿用上游 v2.25.2 的实现（中心距离小于 100 视为同一个位置）：
        定点穿戴按缓存下标点槽位，重复坐标会让它点到同一位置或越界。
        """
        boxes = self.operator.rectangle_detect(120, 150, 130, 150,
                                               from_x=0.1, from_y=0.52, to_x=0.86, to_y=0.7,
                                               trace=True)
        if not boxes:
            return
        unique_boxes: list[Box] = []
        for b in sorted(boxes, key=lambda b: b.left):
            if all(b.distance(u) >= 100 for u in unique_boxes):
                unique_boxes.append(b)
        self.off_field_area = [
            (b.center[0] / self.operator.window_context.width, b.center[1] / self.operator.window_context.height)
            for b in unique_boxes
        ]
        # 同步调整角色列表长度
        if len(self.off_field_area) > len(self.off_field_character):
            self.off_field_character.extend(
                [None] * (len(self.off_field_area) - len(self.off_field_character))
            )
        logger.info(f"当前后台共有 {len(self.off_field_area)} 个位置")

    def equip_cached_characters(self, *, sync_layout: bool = True) -> int:
        """按缓存的出场角色穿戴/合成装备。

        只点击缓存中已确认身份、属于攻略且仍可装备的角色位置，按攻略优先级
        排序后逐个处理；不依次点击所有槽位，也不点击空位。
        """
        if sync_layout:
            self._sync_off_field_area()
        targets: list[tuple[str, int, Character, tuple[float, float]]] = []
        for area_name, characters, areas in (
                ("前台", self.on_field_character, self.on_field_area),
                ("后台", self.off_field_character, self.off_field_area),
        ):
            for index, character in enumerate(characters):
                if index >= len(areas):
                    break
                if self._can_equip(character):
                    targets.append((area_name, index, character, areas[index]))
        # 攻略优先级高的角色先穿，避免低优先级角色占用装备
        targets.sort(key=lambda item: item[2].priority if item[2].priority is not None else -1,
                     reverse=True)
        logger.info(f"装备轮：缓存中可装备角色 {len(targets)} 个："
                    f"{'、'.join(c.name for _, _, c, _ in targets) or '无'}")
        equipped = 0
        for area_name, index, character, point in targets:
            if self._equip_cached_character(area_name, index, character, point):
                equipped += 1
            self.operator.sleep(0.3)
        logger.info(f"装备轮完成：成功穿戴/合成 {equipped} 件")
        return equipped

    def _can_equip(self, character: Character | None) -> bool:
        """缓存中的角色是否还需要并且允许穿戴装备。"""
        if character is None or character.is_npc or character.is_locked:
            return False
        if character.name not in self.strategy_characters:
            return False  # 不给不在攻略中的角色穿戴装备
        return character.equip_count < MAX_EQUIPMENT_PER_CHARACTER

    def _equip_cached_character(self, area_name: str, index: int,
                                character: Character, point: tuple[float, float]) -> bool:
        """点击缓存角色所在槽位，核实面板身份后执行一次穿戴/合成。"""
        self.operator.click_point(*point, after_sleep=0.5, trace=False)
        try:
            recommend = self.operator.locate(CWIMG.EQUIPMENT_RECOMMEND)
            if not recommend:
                # 面板可能尚未弹出，短暂重试后再判定缓存失效
                self.operator.sleep(0.4)
                recommend = self.operator.locate(CWIMG.EQUIPMENT_RECOMMEND)
            if not recommend:
                # 槽位已不是缓存中的角色或已为空，交给下次完整扫描校正
                logger.info(f"{area_name}[{index}] 未打开 {character.name} 的角色面板，缓存已过期，跳过装备")
                self._set_cached_character(area_name, index, None)
                return False
            panel_character = self._read_panel_character()
            if panel_character is not None and panel_character.name != character.name:
                logger.warning(f"{area_name}[{index}] 缓存角色 {character.name} 与实际 "
                               f"{panel_character.name} 不符，跳过装备并更新缓存")
                self._set_cached_character(area_name, index, panel_character)
                return False
            self.operator.click_box(recommend, after_sleep=1)
            _, box = self.operator.locate_any([CWIMG.EQUIP, CWIMG.SYNTHESIS], confidence=0.8)
            if not box:
                logger.info(f"{area_name}[{index}] {character.name} 暂无可穿戴或可合成装备")
                return False
            if not self.operator.click_box(box, after_sleep=0.5):
                logger.warning(f"{area_name}[{index}] {character.name} 穿戴/合成点击失败")
                return False
            character.equip_count += 1
            logger.info(f"{area_name}[{index}] {character.name} 已穿戴/合成 1 件装备"
                        f"（{character.equip_count}/{MAX_EQUIPMENT_PER_CHARACTER}）")
            return True
        finally:
            self.operator.click_point(0.5, 0.5, trace=False)  # 关闭角色面板

    def _read_panel_character(self) -> Character | None:
        """读取当前角色面板中的角色，只用于复核缓存身份。"""
        rows = self.operator.ocr(from_x=0.775, from_y=0.175, to_x=0.880, to_y=0.2315)
        if not rows:
            # 狸猫/佩佩等角色名称可能显示在另一个位置
            rows = self.operator.ocr(from_x=0.775, from_y=0.445, to_x=0.880, to_y=0.486)
        if not rows:
            return None
        name = "".join(str(row[1]) for row in rows)
        return Characters.get_character(name)

    def _set_cached_character(self, area_name: str, index: int, character: Character | None):
        """更新前后台缓存中的槽位内容，供后续装备判断和扫描使用。"""
        target = self.on_field_character if area_name == "前台" else self.off_field_character
        if 0 <= index < len(target):
            target[index] = character

    def get_coins(self):
        # 实现获取金币逻辑
        logger.info("获取当前金币数量")
        coins = self.operator.ocr(from_x=0.84, from_y=0.81, to_x=0.89, to_y=0.89)
        if coins:
            try:
                return int(coins[0][1])
            except (IndexError, ValueError):
                return 0
        else:
            return 0

    def _get_character_in_area(self,
                               areas,
                               target_character_list,
                               force=False,
                               count_stars=False,
                               use_backup_ocr=False):
        """
        通用方法：获取指定区域的角色信息
        :param areas: 区域坐标列表（如 off_field_area）
        :param target_character_list: 目标角色列表（如 off_field_character，将被更新）
        :param force: 强制更新角色信息，默认False（即已有角色则跳过）
        :param count_stars: 是否获取角色星级
        :return: 更新后的角色列表
        """

        def get_stars(merge: int) -> int:
            # 获取角色星级 merge: 当 box 之间的距离小于此值时，将它们视为同一个box
            boxes = self.operator.locate_all(CWIMG.STAR, from_x=0.83, from_y=0.15, to_x=0.87, to_y=0.17)
            if not boxes:
                return 0
            valid_boxes: list[Box] = []
            for b in boxes:
                for vb in valid_boxes:
                    if vb.distance(b) < merge:
                        break
                else:
                    valid_boxes.append(b)
            return len(valid_boxes)

        # 角色名称OCR区域
        ocr_from_x, ocr_from_y, ocr_to_x, ocr_to_y = (0.775, 0.185, 0.880, 0.258)

        def ocr_fragments(rows):
            # 空位背景常被误识别为「1」等数字/符号，只保留包含文字的片段
            return [item for item in rows or () if re.search(r"[^\W\d_]", item[1])]

        def ocr_name(rows) -> str:
            """按上游 v2.25.2 的逐行拼接得到角色名，主/备 OCR 区域统一走这条路径。"""
            fragments = ocr_fragments(rows)
            return "".join(_merge_ocr_lines(fragments)) if fragments else ""

        for index, area in enumerate(areas):
            try:
                # 点击区域（带延迟，确保界面响应）
                if target_character_list[index] is not None and not force:
                    continue  # 已有角色则跳过
                self.operator.mouse_down(*area, trace=False)
                self.operator.sleep(0.2)
                self.operator.mouse_up(*area, trace=False)

                # OCR识别角色名称
                character_names = self.operator.ocr(
                    from_x=ocr_from_x,
                    from_y=ocr_from_y,
                    to_x=ocr_to_x,
                    to_y=ocr_to_y
                )
                name = ocr_name(character_names)

                if use_backup_ocr and not _looks_like_character_name(name):
                    # 主区域未读到有效名称时尝试备用区域，狸猫/佩佩等角色名称可能显示在其他位置
                    backup_names = self.operator.ocr(
                        from_x=0.775,
                        from_y=0.445,
                        to_x=0.880,
                        to_y=0.486
                    )
                    if backup_names:
                        character_names = backup_names
                        name = ocr_name(backup_names)

                # 更新角色列表（与 ocr_name 使用同一套片段过滤，避免主/备路径判定不一致）
                fragments = ocr_fragments(character_names)
                if fragments:
                    # OCR 可能把名字切成多个重叠的文本框，也可能把下方标签一起识别进来：
                    # 逐行累加，优先采用能匹配角色表的组合（如“银狼LV.999”而不是“银狼LV.999前台”）
                    lines = _merge_ocr_lines(fragments)
                    name, char = '', None
                    for i in range(len(lines)):
                        candidate = ''.join(lines[:i + 1])
                        char = Characters.get_character(candidate)
                        if char is not None:
                            name = candidate
                            break
                    if char is None:
                        name = ''.join(lines)
                        logger.warning(f"OCR识别到角色名称：{name}，但未在角色列表中找到匹配项")
                        target_character_list[index] = Characters.new_character(name)  # 新增临时角色
                        self.operator.click_point(0.5, 0.5, trace=False)  # 关闭信息框，避免影响下一个槽位
                        continue
                    if count_stars and not char.is_npc:
                        char.stars = get_stars(merge=3)
                    target_character_list[index] = char
                    self.operator.click_point(0.5, 0.5, trace=False)  # 点击空白处关闭信息框
                else:
                    target_character_list[index] = None  # 未识别到角色

            except Exception as e:
                # 捕获点击或OCR异常（如坐标错误、识别失败）
                logger.error(f"获取区域[{index}]角色失败：{str(e)}")

        return target_character_list

    def get_off_field_area(self, force=False):
        """获取后台角色信息"""
        self._get_character_in_area(areas=self.off_field_area, target_character_list=self.off_field_character,
                                    force=force,
                                    count_stars=True,
                                    use_backup_ocr=True)
        for char in self.off_field_character:
            if char is not None:
                char.is_placed = True
        logger.info(f"当前后台角色：{self.off_field_character}")

    def get_on_field_area(self, force=False):
        """获取前台角色信息"""
        self._get_character_in_area(areas=self.on_field_area, target_character_list=self.on_field_character,
                                    force=force,
                                    count_stars=True)
        for char in self.on_field_character:
            if char is not None:
                char.is_placed = True
        logger.info(f"当前前台角色：{self.on_field_character}")

    def get_in_hand_area(self, force=False):
        """获取备战席角色信息"""
        # 打开武器箱或专家邀请函
        target = self.operator.locate(CWIMG.OPEN)
        while target:
            self.operator.mouse_down(target.center[0], target.center[1])
            self.operator.mouse_up()
            self.operator.sleep(1)
            self.operator.click_point(0.8, 0.25, after_sleep=1)
            target = self.operator.locate(CWIMG.OPEN)
        self._get_character_in_area(areas=self.in_hand_area, target_character_list=self.in_hand_character, force=force)
        logger.info(f"当前备战席角色：{self.in_hand_character}")

    def harvest_crystals(self):
        # 实现水晶收集逻辑
        logger.info("收集水晶")
        if self.operator.type == "Local":
            self.operator.drag_to(0.68, 0.20, 0.82, 0.18, trace=False)
            self.operator.sleep(0.3)
            self.operator.drag_to(0.68, 0.25, 0.82, 0.25, trace=False)
            self.operator.sleep(0.3)
            self.operator.drag_to(0.68, 0.30, 0.83, 0.30, trace=False)
            self.operator.sleep(0.3)
            self.operator.drag_to(0.68, 0.35, 0.84, 0.35, trace=False)
            self.operator.sleep(0.3)
            self.operator.drag_to(0.68, 0.40, 0.83, 0.40, trace=False)
            self.operator.sleep(0.3)
        else:
            # 浏览器不支持滑动，拖动可能无法正确触发收集
            for x in range(66, 85, 2):
                for y in range(20, 41, 5):
                    self.operator.click_point(x / 100, y / 100, trace=False)

    def sell_character(self):
        """
        实现角色出售逻辑，确保备战席不超过8个角色
        :return: bool - 出售操作是否成功
        """
        max_attempts = 5
        attempt = 0

        while attempt < max_attempts:
            if self.in_hand_character_count < 8:
                logger.info("备战席未满，跳过出售角色")
                return True
            logger.info("备战席已满，尝试出售角色")
            if not self._handle_sell_character():
                # 常规条件下没有可出售目标，重复扫描只会得到同样结果，直接进入强制出售
                logger.info("没有符合出售条件的角色，提前进入强制出售流程")
                break
            self.get_in_hand_area()  # 检测空位，确保没有新插入的角色
            attempt += 1

        # 超过最大尝试次数仍未成功出售时的逻辑
        logger.warning(f"已尝试{attempt}次出售角色，备战席仍为满状态，将强制执行出售")
        self._handle_sell_character(force=True)
        self.get_in_hand_area()  # 最后更新备战席状态

        if self.in_hand_character_count < 8:
            logger.info("强制出售角色成功")
            return True
        else:
            logger.error("强制出售角色失败，备战席仍为满状态")
            return False

    def _handle_sell_character(self, force=False) -> bool:
        """
        角色出售逻辑
        :param force: bool - 是否强制出售所有角色（包括已放置的角色）
        :return: 是否存在可出售角色
        """
        # 创建需要出售的角色索引列表
        characters_to_sell: list[tuple[int, Character]] = []
        for i, character in enumerate(self.in_hand_character):
            if character is None:
                continue
            if force:
                # 强制出售
                logger.info(f"强制出售角色：{character.name}")
                characters_to_sell.append((i, character))
            elif character.name not in self.strategy_characters.keys():
                # 不在攻略中的角色
                logger.info(f"出售非攻略角色：{character.name}")
                characters_to_sell.append((i, character))
            elif character.stars >= self.strategy_characters[character.name] and character.is_placed:
                # 已满足攻略要求的角色
                logger.info(f"出售满足攻略要求的角色：{character.name}")
                characters_to_sell.append((i, character))

        # 执行出售操作
        for i, character in characters_to_sell:
            # 由于商店区域没有角色列表，直接执行拖拽
            sell_area = (0.05, 0.86)  # 出售区域
            source = self.in_hand_area[i]
            self.operator.drag_to(source[0], source[1], sell_area[0], sell_area[1])
            # 更新备战席状态
            self.in_hand_character[i] = None
            if character:
                character.is_placed = False
            logger.info(f"已出售角色：{character.name}")
            self.operator.sleep(0.5)
        logger.info("出售操作完成")
        return len(characters_to_sell) > 0

    # 人口计数（当前/上限）的识别区域：某些事件会让原本只占 1 个人口的角色改为占 2 个，
    # 原本刚好满编的阵容就会超出上限，此时游戏会静默拒绝出战（点了没有任何反应）。
    POPULATION_AREA = {"from_x": 0.44, "from_y": 0.19, "to_x": 0.57, "to_y": 0.29}

    def get_population(self) -> tuple[int, int] | None:
        """读取人口计数（当前/上限），读数不完整时返回 None。"""
        result = self.operator.ocr(**self.POPULATION_AREA)
        if not result:
            return None
        text = _merge_ocr_text(result)
        match = re.search(r"(\d{1,2})\s*[/／]\s*(\d{1,2})", text)
        if match is None:
            logger.debug(f"人口计数无法解析：{text}")
            return None
        current, capacity = int(match.group(1)), int(match.group(2))
        # 上限最大为等级上限，当前人数最多是每个位置都占 2 个人口
        if not 1 <= capacity <= 12 or not 0 <= current <= 2 * capacity:
            logger.debug(f"人口计数超出合理范围：{text}")
            return None
        logger.debug(f"人口计数：{current}/{capacity}")
        return current, capacity

    def ensure_population_allows_battle(self) -> bool:
        """确认人口未超上限，超限时撤下角色腾出人口。

        :return: 处理后可出战（人口未超限，或读数不可用无法判断）返回 True，仍超限返回 False
        """
        population = self.get_population()
        if population is None:
            logger.debug("未读取到人口计数，跳过人口超限处理")
            return True
        current, capacity = population
        if current <= capacity:
            return True
        logger.warning(f"人口超出上限：{current}/{capacity}，撤下角色腾出人口后再出战")
        for area_type, index in self._population_withdraw_candidates():
            if not self._withdraw_character(area_type, index):
                continue
            population = self.get_population()
            if population is None:
                return True
            current, capacity = population
            logger.info(f"腾出人口后：{current}/{capacity}")
            if current <= capacity:
                return True
        logger.warning(f"未能腾出足够人口：{current}/{capacity}")
        return False

    def _population_withdraw_candidates(self) -> list[tuple[str, int]]:
        """按撤离代价从低到高列出场上角色：先后台、后前台，优先级低的先撤。"""
        def by_priority(characters: list[Character | None]) -> list[int]:
            present = [(c.priority or 0, i) for i, c in enumerate(characters)
                       if c is not None and not c.is_locked]
            return [i for _, i in sorted(present)]

        candidates = [('off_field', i) for i in by_priority(self.off_field_character)]
        on_field = by_priority(self.on_field_character)
        # 前台至少要留一名角色，否则无法出战
        if len(on_field) > 1:
            candidates += [('on_field', i) for i in on_field[:-1]]
        return candidates

    def _withdraw_character(self, area_type: str, index: int) -> bool:
        """把场上角色撤回备战席；备战席已满时改为出售该角色。"""
        if area_type == 'off_field':
            characters, areas, area_name = self.off_field_character, self.off_field_area, "后台"
        else:
            characters, areas, area_name = self.on_field_character, self.on_field_area, "前台"
        character = characters[index]
        if character is None:
            return False
        free_index = self._free_in_hand_index()
        if free_index is not None:
            logger.info(f"将{area_name}角色 {character.name} 撤回备战席以腾出人口")
            return self.swap_character_between_areas(area_type, index, 'in_hand', free_index)
        logger.info(f"备战席已满，出售{area_name}角色 {character.name} 以腾出人口")
        self.operator.drag_to(areas[index][0], areas[index][1], 0.05, 0.86)
        self.operator.sleep(0.5)
        characters[index] = None
        character.is_placed = False
        return True

    def _free_in_hand_index(self) -> int | None:
        """找一个空的备战席位置；本地数据没有空位时重新识别一次。"""
        free = next((i for i, c in enumerate(self.in_hand_character) if c is None), None)
        if free is not None:
            return free
        self.get_in_hand_area(True)
        return next((i for i, c in enumerate(self.in_hand_character) if c is None), None)

    def battle(self) -> bool:
        # 按钮偶现点击不生效：点击后需确认按钮消失，未消失则重试
        button_labels = ["出战", "跳过", "幕后茶歇", "选择主角"]
        button_region = {"from_x": 0.9, "from_y": 0.65, "to_x": 0.98, "to_y": 0.75}
        # 限制处理次数，避免识别异常时无限递归
        max_protagonist_selections = 3
        for _ in range(max_protagonist_selections + 1):
            # 「从师」等主角光环会直接盖在备战界面上且不替换「出战」按钮，
            # 此时点击出战不会生效，先处理掉事件再继续
            self.handle_special_event()
            index, box = self.operator.wait_ocr_any(button_labels, **button_region)
            if index == -1:
                # 没有识别到任何已知按钮
                raise RuntimeError("未识别到战斗按钮或跳过按钮")
            if index == 0:
                # 出战前复核人口：超出上限时点击出战不会生效
                self.ensure_population_allows_battle()
            for _ in range(3):
                self.operator.click_box(box, after_sleep=1.5)
                retry_box = self.operator.ocr_match(button_labels[index], **button_region)  # pyright: ignore[reportArgumentType]
                if retry_box is None:  # 按钮已消失，点击生效
                    break
                box = retry_box  # 用重新定位的位置重试
            else:
                logger.warning(f"点击「{button_labels[index]}」后按钮未消失，点击可能未生效")
                if index == 0:
                    if not self.ensure_population_allows_battle():
                        raise RuntimeError(f"人口超出上限且无法腾出：{self.get_population()}")
                    # 腾出人口后再补点一次
                    retry_box = self.operator.ocr_match(button_labels[index], **button_region)
                    if retry_box is not None:
                        self.operator.click_box(retry_box, after_sleep=1.5)
            if index != 3:  # 不是选择主角，继续后续战斗流程
                break
            self.handle_protagonist_selection()  # 选择完主角后重新进入战斗流程
        else:
            raise RuntimeError("战斗流程异常")
        if index == 2:  # 幕后茶歇
            self.handle_tea_break()
            return True
        if index == 0:  # 出战：确认前台角色后等待战斗结束
            ocr_i, _ = self.operator.ocr_match_any(["无角色", "未达上限"], from_x=0.36, from_y=0.44, to_x=0.64, to_y=0.5)
            # 检查前台区域是否有角色
            if ocr_i == 0:
                logger.error("前台区域无角色，无法出战，跳过本轮剩余流程")
                self.skip_current_round = True
                self.operator.click_img(IMG.ENSURE)
                return False
            elif ocr_i == 1:
                logger.info("前台区域未达上限，继续战斗")
                self.operator.click_img(IMG.ENSURE)
            logger.info("战斗开始，等待挑战结束")
            # 战斗中持续检测“未开启自动战斗”图标（Q），出现即按 V 开启；
            # IMG.Q 放在结算/继续之后，保证战斗结束时优先判定为结束
            waiting_templates = [CWIMG.SETTLE, CWIMG.CONTINUE, IMG.Q]
        else:  # 跳过战斗：对局已结束，直接等待结算界面
            logger.info("跳过战斗")
            waiting_templates = [CWIMG.SETTLE, CWIMG.CONTINUE]
        deadline = time.time() + 600
        while True:
            result, _ = self.operator.wait_any_img(waiting_templates, timeout=max(deadline - time.time(), 0),
                                                   interval=1, trace=False)
            if result != 2:  # 结算/继续按钮或超时（跳过战斗时没有索引 2）
                break
            self.operator.press_key("v")  # 按 V 开启自动战斗，继续等待
            self.operator.sleep(1)
        if result == -1:
            logger.warning("等待挑战结束超时")
            for _ in range(5):
                self.operator.press_key('esc')
                self.operator.sleep(1)
                if self.operator.click_img(CWIMG.RETREAT):
                    break
            else:
                raise RuntimeError("挑战结束超时，未能成功中止对局")
            self.abort_and_return() # 挑战结束超时，尝试中止并返回开始页面
            return False
        logger.info("挑战结束")
        self.operator.sleep(0.5)
        self.operator.click_point(0.5, 0.824, tag="点击继续按钮")
        self.operator.sleep(3)
        return True

    def stage_transition(self):
        """实现关卡切换逻辑，通过识别图片状态执行对应操作"""
        # 定义状态配置：(图片路径, 状态名称, 处理函数, 是否为终止状态)
        stage_config = [
            (
                CWIMG.REPLENISH_STAGE,
                StageName.REPLENISH_STAGE,
                self.handle_replenish_stage
            ),
            (
                CWIMG.ENCOUNTER_NODE,
                StageName.ENCOUNTER_NODE,
                self.handle_encounter_node
            ),
            (
                CWIMG.FOLD,
                StageName.FOLD,
                lambda prev_stage: True,  # 目标状态，无需处理
            ),
            (
                CWIMG.SELECT_INVEST_STRATEGY,
                StageName.SELECT_INVEST_STRATEGY,
                self.handle_invest_strategy
            ),
            (
                CWIMG.CLICK_BLANK,
                StageName.CLICK_BLANK,
                self.handle_click_blank
            ),
            (
                CWIMG.NEXT_STEP,
                StageName.GAME_OVER,
                self.handle_game_over
            ),
            (
                CWIMG.START_CURRENCY_WARS,
                StageName.MAIN_MENU,
                self.handle_main_menu
            ),
            (
                CWIMG.SELECT_EQUIPMENT,
                StageName.SELECT_EQUIPMENT,
                lambda prev_stage: self.operator.click_point(0.5, 0.2, after_sleep=0.5, tag="选择装备")
            ),
        ]

        img_list = [cfg[0] for cfg in stage_config]

        prev_stage_name: StageName | None = None
        fold_hits = 0  # 连续识别到 FOLD（无）的次数
        while True:  # 用无限循环 + 内部break控制退出
            stage_index, _ = self.operator.wait_any_img(img_list, timeout=15, interval=1)
            self.operator.sleep(1.5)
            # 检查是否超时（未识别到任何状态）
            if stage_index == -1:
                if self.operator.click_img(CWIMG.STORE):
                    continue
                raise RuntimeError("关卡状态检测超时，未识别到任何图片")

            # 获取当前状态配置
            img_path, stage_name, handle_func = stage_config[stage_index]

            # FOLD（无）表示已回到备战界面、本轮没有待处理的关卡；
            # 要求连续命中才认定，避免过渡画面里的「收起」抢跑跳过真正的关卡界面
            if stage_name == StageName.FOLD:
                fold_hits += 1
                if fold_hits < self.FOLD_CONFIRM_TIMES:
                    logger.debug(f"识别到无状态（{fold_hits}/{self.FOLD_CONFIRM_TIMES}），继续等待确认")
                    continue
            else:
                fold_hits = 0

            logger.info(f"检测到状态：{stage_name}({img_path})")

            # 执行状态处理函数
            try:
                is_terminal = handle_func(prev_stage_name)
                logger.info(f"状态 {stage_name} 处理完成")
            except Exception as e:
                raise RuntimeError(f"处理状态 {stage_name} 时发生异常: {str(e)}") from e

            # 更新上一阶段名称
            prev_stage_name = stage_name

            # 若当前是终止状态，直接退出循环
            if is_terminal:
                logger.info(f"达到终止状态：{stage_name}，退出关卡切换流程")
                return self.handle_stage_transitioned()

    def handle_main_menu(self, _: StageName | None) -> bool:
        """处理主菜单界面的逻辑"""
        self.is_game_over = True
        return True

    def handle_stage_transitioned(self) -> bool:  # NOQA
        """关卡切换完成后的处理逻辑（如有）。默认实现为无操作，子类可重写此方法以添加额外逻辑。
        调用时机：在完成关卡切换流程后、正式进入下一轮游戏循环前调用
        Returns:
            True-继续执行游戏循环；False-中止任务
        """
        return True

    def handle_characters_updated(self):
        """角色信息更新后的处理逻辑（如有）。默认实现为无操作，可重写此方法以添加额外逻辑。"""
        if all(c is None for c in self.on_field_character + self.off_field_character + self.in_hand_character):
            # 如果所有角色信息均为None，打开商店界面购买角色
            self.operator.click_point(0.8438, 0.8481, after_sleep=1, tag="打开商店界面")
            self.shopping()
            # 重新获取备战席信息
            self.get_in_hand_area()

    def handle_game_over(self, _: StageName | None) -> bool:
        """处理游戏结束后的逻辑，如点击继续、返回主界面等"""
        self.operator.click_img(CWIMG.NEXT_STEP)
        self.operator.move_to(0.5, 0.5)  # 移动鼠标避免遮挡
        next_page_box = self.operator.wait_img(CWIMG.NEXT_PAGE)
        if next_page_box is None:
            logger.warning("未识别到下一页按钮，无法继续")
            self.operator.click_point(0.5, 0.82, after_sleep=1, tag="点击下一页")  # 兜底方案
        else:
            self.operator.click_box(next_page_box, after_sleep=1)
        self.operator.click_point(0.5, 0.82, after_sleep=1, tag="点击返回货币战争")
        self.is_game_over = True  # 标记游戏结束
        return True

    def handle_click_blank(self, _: StageName | None) -> bool:
        """处理Boss预览界面的逻辑"""
        self.operator.click_point(0.5, 0.70, after_sleep=1)
        return False

    def _read_invest_cards(self):
        """OCR 三张投资策略卡，返回 (titles, card_boxes, risk_by_column)。"""
        return read_invest_cards(self.operator)

    def _refresh_invest_card(self, titles, risk_by_column, width) -> bool:
        """三张卡都要避开时刷新危害最高的一张，兜底卡保持不动。"""
        return refresh_blocked_invest_card(
            self.operator, CWIMG.INVEST_STRATEGY_REFRESH, titles, risk_by_column, width)

    def _select_invest_strategy_card(self) -> bool:
        """按危害等级选择投资策略：安全卡优先，收藏卡仅在其自身安全时沿用。

        三张卡都会清空/替换阵容时，先按每张卡自带的刷新按钮刷掉危害最高的一张
        并重新识别，仍然没有安全选项才退回「影响最小的一张」。
        """
        centers = self.INVEST_CARD_CENTERS
        context = getattr(self.operator, "window_context", None)
        width = getattr(context, "width", 0) or 0
        collection = self.operator.locate(CWIMG.COLLECTION)
        collection_column = (invest_column_of(collection.center[0], width, centers)
                             if collection is not None else None)
        titles, card_boxes, risk_by_column = {}, {}, {}
        target, forced, refreshed = 1, False, 0
        # 记录本次实际选中的卡名，供刷开局侧把兜底选择写进「本局已选投资策略」。
        self._selected_invest_card_title = None
        for attempt in range(self.INVEST_STRATEGY_REFRESH_TIMES + 1):
            titles, card_boxes, risk_by_column = self._read_invest_cards()
            chosen, forced = select_invest_column(risk_by_column, collection_column)
            if not forced:
                target = chosen
                break
            if attempt == self.INVEST_STRATEGY_REFRESH_TIMES:
                break
            if not self._refresh_invest_card(titles, risk_by_column, width):
                break
            refreshed += 1
        if forced:
            target = least_harmful_invest_column(titles)
        title = next(iter(titles.get(target, ())), None)
        self._selected_invest_card_title = title
        label = f"第{target + 1}张「{title}」" if title else f"第{target + 1}张（卡名未识别）"
        if forced:
            logger.warning(f"三张投资策略均会清空/替换阵容，已刷新 {refreshed} 次仍无安全选项；"
                           f"选择影响最小的一张：{label}")
        elif collection is not None and target == collection_column:
            logger.info(f"投资策略沿用收藏卡：{label}")
        else:
            logger.info(f"投资策略选择：{label}")
        if collection is not None and target == collection_column and self.operator.click_img(CWIMG.COLLECTION):
            return True
        box = card_boxes.get(target)
        if box is not None and self.operator.click_box(box):
            return True
        return bool(self.operator.click_point(*centers[target], after_sleep=0.5,
                                              tag=f"选择第{target + 1}张投资策略"))

    def handle_invest_strategy(self, prev_stage: StageName | None) -> bool:
        """处理投资策略选择界面的逻辑：先避开会清空/替换阵容的卡，收藏卡仅在其安全时沿用"""
        self._select_invest_strategy_card()
        self.operator.click_img(IMG.ENSURE2, after_sleep=1, retry=3)
        self.post_invest_strategy()
        return False

    def post_invest_strategy(self, strategy_name: str | None = None):
        """投资策略选择后的后续处理逻辑"""
        if strategy_name == "阿哈大悦" or self.operator.locate(CWIMG.SELECT_SIMPLE_EQUIPMENT):
            self.operator.click_point(0.8, 0.25, after_sleep=1, tag="阿哈大悦选择装备4")

    def handle_replenish_stage(self, _: StageName | None):
        """处理补给阶段的逻辑"""
        self.operator.click_point(0.53, 0.52, after_sleep=1)  # 选择固定位置
        self.operator.click_point(0.88, 0.91, after_sleep=1)  # 点击确认按钮
        return False

    def handle_encounter_node(self, _: StageName | None) -> bool:
        """处理遭遇节点的逻辑"""
        self.operator.click_point(0.35, 0.50, after_sleep=1)  # 简单难度
        self.operator.click_point(0.50, 0.84, after_sleep=1)  # 点击确认钮
        return False

    def _handle_selection_event(self, ocr_region: tuple[float, float, float, float],
                                keyword: str, count: int = 1):
        """
        通用事件选择处理
        :param ocr_region: OCR识别区域 (from_x, from_y, to_x, to_y)
        :param keyword: 目标关键词（空格分隔的多个关键词按顺序各匹配一个选项）
        :param count: 需要同时选中的选项数量（如“梦境交织”需要选择两个）
        """
        boxes = self.operator.ocr_boxes(from_x=ocr_region[0], from_y=ocr_region[1],
                                        to_x=ocr_region[2], to_y=ocr_region[3])
        if not boxes:
            return False
        # 每个关键词各匹配一个尚未选中的选项
        chosen: list[int] = []
        for candidate in keyword.split() or [""]:
            for index, box in enumerate(boxes):
                if index not in chosen and candidate in box.source.strip():
                    chosen.append(index)
                    break
        if not chosen:
            # 如果没有匹配的关键词，则默认选择最后一个选项
            chosen = [len(boxes) - 1]
        if len(chosen) < count:
            # 需要多选时，按 OCR 顺序补齐未选中的选项
            chosen += [index for index in range(len(boxes)) if index not in chosen][:count - len(chosen)]
        if len(chosen) < count:
            logger.warning(f"事件选项不足：需要选择 {count} 个，仅识别到 {len(chosen)} 个")
        for index in chosen:
            self.operator.click_box(boxes[index], after_sleep=1)
        # 确认按钮偶现点击不生效：点击后按钮应消失，仍在则重试
        for _ in range(3):
            self.operator.click_img(IMG.ENSURE2, after_sleep=1)
            if not self.operator.locate(IMG.ENSURE2):
                break
        else:
            raise RuntimeError("确认事件选择失败")
        return True

    def handle_the_planet_of_festivities(self):
        """处理盛会之星事件的逻辑"""
        return self._handle_selection_event((0.22, 0.29, 0.85, 0.35),
                                            self.strategy_special_events.get("盛会之星", ""))

    def handle_choose_partner(self):
        """处理领航员选择事件的逻辑"""
        return self._handle_selection_event((0.25, 0.32, 0.85, 0.39),
                                            self.strategy_special_events.get("领航员", ""))

    def handle_wish_trial(self):
        """处理命运圣杯事件的逻辑"""
        return self._handle_selection_event((0.22, 0.42, 0.85, 0.465), self.strategy_special_events.get("命运圣杯", ""))

    def handle_trailblaze_store(self):
        """处理开拓银轨事件的逻辑"""
        self.operator.click_point(0.5, 0.2)
        self.operator.click_point(0.5, 0.5)
        return True

    def handle_beast_garden(self):
        """处理奇兽花园事件的逻辑"""
        return self._handle_selection_event((0.22, 0.34, 0.85, 0.4),
                                            self.strategy_special_events.get("奇兽花园", ""))

    def handle_dream_interference(self):
        """处理梦境交织事件的逻辑（该事件需要同时选择两个选项）"""
        return self._handle_selection_event((0.22, 0.3, 0.85, 0.36),
                                            self.strategy_special_events.get("梦境交织", ""),
                                            count=2)

    def handle_sea_trader(self):
        """处理巡海游侠事件的逻辑"""
        return self._handle_selection_event((0.22, 0.30, 0.85, 0.36),
                                            self.strategy_special_events.get("巡海游侠", ""))

    def handle_black_market_manager(self):
        """处理黑市主理人事件的逻辑"""
        return self.operator.press_key("esc")

    def handle_double_dragon(self):
        """处理双龙吟事件的逻辑"""
        return self._handle_selection_event((0.22, 0.30, 0.85, 0.35),
                                            self.strategy_special_events.get("双龙吟", ""))

    def handle_world_cleansing_dragon(self):
        """处理苍龙濯世事件的逻辑"""
        return self._handle_selection_event((0.22, 0.12, 0.85, 0.17),
                                            self.strategy_special_events.get("苍龙濯世", ""))

    def detect_special_event(self) -> str:
        """检测当前是否有特殊事件发生，并返回事件名称"""
        # 标题按字数居中，两字标题（如“从师”）会超出 0.49 左边界，识别区域需要留出余量
        result = self.operator.ocr(from_x=0.42, from_y=0.038, to_x=0.60, to_y=0.11)
        if not result:
            return ""
        # OCR 可能把标题切成多个片段，括号等符号也容易被识别成噪声（如“我来当(?策划”），
        # 因此先按坐标拼接，再只保留汉字匹配标题
        text = re.sub(r"[^\u4e00-\u9fff]", "", _merge_ocr_text(result))
        # 遍历特殊事件列表，匹配标题
        for event, (title, handler) in self.special_events.items():
            if title in text:
                return event
        return ""

    def handle_silver_wolf_lv999(self):
        return self._handle_selection_event((0.22, 0.34, 0.85, 0.4),
                                            self.strategy_special_events.get("头号玩家", ""))

    def handle_special_event(self):
        """处理特殊事件的逻辑"""
        logger.debug("检测特殊事件")
        event = self.detect_special_event()

        if event:
            logger.info(f"检测到特殊事件：{event}")
            handler = self.special_events[event][1]
            if not handler():
                raise RuntimeError("特殊事件处理失败")
            self.handle_special_event()  # 递归处理
        # 未检测到事件，退出

    # ==================== 幕后茶歇节点 ====================
    def handle_tea_break(self) -> bool:
        """处理「幕后茶歇」奇遇节点。

        该节点出现时界面右侧不会显示出战按钮，而是显示「幕后茶歇」按钮。
        点击后在「与奇遇伙伴对话（触发特殊事件）」与「享用茶歇（回复 20 点小队生命值）」中二选一，
        并推进后续对话、奖励选择与开箱流程，直到回到备战界面。

        :return: True-已处理完茶歇事件并回到备战界面；False-当前不是茶歇节点或处理失败
        """

        def _is_tea_break_health_low() -> bool:
            """读取茶歇界面顶部的生命值，判断是否需要优先回复生命"""
            result = self.operator.ocr(from_x=0.49, from_y=0.03, to_x=0.57, to_y=0.12)
            for item in result or []:
                text = str(item[1]).strip()
                if text.isdigit():
                    health = int(text)
                    logger.info(f"[幕后茶歇] 当前生命值：{health}")
                    return health <= 40
            return False

        def _drive_tea_break(_option: tuple[float, float]) -> bool:
            """推进茶歇事件：处理对话、特殊事件奖励与开箱，直到回到备战界面"""
            self.operator.do_while(
                lambda : self.operator.click_point(*_option, after_sleep=1),
                lambda : self.operator.ocr_match("幕后", from_x=0.46, from_y=0.13, to_x=0.53, to_y=0.18) is not None,
                max_iterations=3
            )
            logger.info("[幕后茶歇] 已选择茶歇选项，推进对话")
            self.operator.do_while(
                lambda : self.operator.click_point(0.5, 0.5, after_sleep=1),
                lambda : self.operator.ocr_match("返回", from_x=0.58, from_y=0.38, to_x=0.96, to_y=0.8) is None,
                max_iterations=5
            )
            return_box = self.operator.wait_ocr("返回", from_x=0.58, from_y=0.38, to_x=0.96, to_y=0.8, timeout=3)
            if return_box is None:
                logger.error("[幕后茶歇] 未识别到返回按钮，茶歇事件推进失败")
                return False
            self.operator.click_box(return_box, y_offset=-0.2, after_sleep=0.5)  # 点击返回选项的上一个选项
            ensure_box = self.operator.wait_ocr("确认", from_x=0.58, from_y=0.38, to_x=0.96, to_y=0.8, timeout=3)
            if ensure_box is None:
                logger.error("[幕后茶歇] 未识别到确认按钮，茶歇事件推进失败")
                return False
            self.operator.click_box(ensure_box, after_sleep=0.5)
            self.operator.do_while(
                lambda: self.operator.locate(CWIMG.CLICK_BLANK, from_x=0.65, from_y=0.8, to_x=0.9, to_y=0.9) is None,
                lambda: self.operator.click_point(0.5, 0.9, after_sleep=1),
                interval=1,
                max_iterations=5
            )  # 点击空白处推进对话，直到出现「点击空白处继续」提示
            return True

        if self.operator.wait_ocr("幕后", from_x=0.46, from_y=0.13, to_x=0.53, to_y=0.18) is None:
            logger.error("[幕后茶歇] 未识别到茶歇事件界面")
            return False
        # 生命值偏低时优先享用茶歇回复生命，否则与奇遇伙伴对话获取特殊奖励
        option = (0.8, 0.5) if _is_tea_break_health_low() else (0.2, 0.5)
        return _drive_tea_break(option)

    def handle_protagonist_selection(self) -> bool:
        """处理「选择主角」奇遇节点。"""
        logger.info("选择主角")
        self.operator.wait_ocr("选择", from_x=0.42, from_y=0.06, to_x=0.58, to_y=0.11)
        self.operator.click_point(0.2, 0.3, after_sleep=1, tag="选择左侧效果")
        target = (0.5, 0.4)
        # 从前台角色中按优先级从高到低依次尝试
        candidates = sorted(((i, c) for i, c in enumerate(self.on_field_character) if c is not None),
                            key=lambda item: item[1].priority or 0, reverse=True)
        if not candidates:
            raise RuntimeError("未检测到前台角色，无法选择主角")
        for index, character in candidates:
            logger.info(f"选择主角：{character.name}（优先级 {character.priority}）")
            origin = (0.42+index*0.05, 0.56)
            self.operator.drag_to(origin[0], origin[1], target[0], target[1])  # 拖动角色到主角位置
            self.operator.sleep(0.5)
            self.operator.click_point(0.5, 0.888, after_sleep=1, tag="点击确认选择主角")
            # 该角色已有主角效果时会弹出替换光环提示，只识别提示文字所在的小块区域
            if self.operator.ocr_match("替换", from_x=0.38, from_y=0.43, to_x=0.65, to_y=0.53):
                # 该角色已有主角效果：关闭替换提示，换下一个角色重试
                logger.warning(f"角色 {character.name} 已有主角效果，换下一个角色重试")
                self.operator.press_key("esc")
                self.operator.sleep(1)
                continue
            return True
        raise RuntimeError("未能选择主角：所有前台角色都已有主角效果")

    def handle_apprentice_selection(self) -> bool:
        """处理「从师」节点的逻辑（候选为角色卡牌，名字在同一行）"""
        return self._handle_selection_event((0.24, 0.31, 0.85, 0.37),
                                            self.strategy_special_events.get("从师", ""))

    def shopping(self):
        def scan_characters_in_store() -> list[Character] | None:
            """扫描商店中的角色"""
            results = self.operator.ocr(from_x=0.19, from_y=0.26, to_x=0.88, to_y=0.31)
            chars = []
            if not results:
                return []
            for item in results:
                name_raw = item[1]
                if not isinstance(name_raw, str):
                    continue
                name: str = name_raw.strip()
                if not name:
                    continue
                if name.isdecimal():  # 跳过价格识别结果
                    continue
                if '备' in name:  # 备战席已满
                    return None
                char = Characters.get_character(name)
                if char is not None:
                    chars.append(char)
                    logger.info(f"商店中发现角色：{char.name}，P: {char.priority}")
                else:
                    logger.info(f"商店中发现未知角色：{name}")
            return chars

        def purchase_target_character(characters: list[Character]) -> bool:
            """
            尝试购买目标角色（策略内的角色）
            :param characters: 商店中扫描到的角色列表
            :return: True-购买成功，False-无目标角色可买
            """
            for i, c in enumerate(characters):
                if c.name in self.strategy_characters.keys():  # 角色在攻略列表中
                    if c.stars >= self.strategy_characters[c.name]:
                        continue  # 已达目标星级
                    # 执行购买操作
                    target = self.store_area[i]
                    self.operator.click_point(*target, after_sleep=0.5)
                    logger.info(f"购买角色：{c.name}")
                    # 特殊处理
                    if c.name == Characters.SilverWolfLV999.name:
                        self.handle_special_event()
                    self.operator.move_to(0.5, 0.5)  # 移动鼠标避免遮挡

            logger.info("当前商店无目标角色可购买")
            return False

        cs = scan_characters_in_store()
        if cs:
            purchase_target_character(cs)
        prev_coins = -1
        while True:
            purchased = False

            level = self.get_level()
            coins = self.get_coins()

            # 前后两次循环识别到的金币数量相同则结束循环
            if coins == prev_coins:
                logger.info(f"当前金币{coins}与上一轮相同，退出购物循环")
                break
            prev_coins = coins

            # 基础退出条件：金币不足4
            if coins < 4:
                logger.info(f"金币不足4（当前{coins}），退出购物循环")
                break
            # 等级大于3且小于最低等级：升级
            if 3 < level < self.min_level:
                logger.info(f"等级{level}大于3且小于最低等级{self.min_level}，执行升级操作")
                self.operator.press_key('f')
                self.operator.sleep(0.5)
                continue

            # 扫描商店角色并尝试购买
            cs = scan_characters_in_store()
            if cs:
                purchased = purchase_target_character(cs)

            # 非超频模式：检查金币是否满足保留要求（预判刷新后结果）
            if not self.is_overclock:
                # 最小保留数校验：当前金币 < 最小保留数 → 直接退出
                if coins < self.min_coins:
                    logger.info(f"当前金币 {coins} 小于最低保留数量 {self.min_coins}，退出购物循环")
                    break

            # 执行刷新（仅当预判后金币足够时才会走到这里）
            self.operator.press_key('d')
            self.operator.sleep(0.5)

            # 6. 等级<中级等级且未购买：升级（原有逻辑）
            if level < self.mid_level and not purchased:
                logger.info(f"等级{level}<中级等级{self.mid_level}且未购买角色，执行升级操作")
                self.operator.press_key('f')
                self.operator.sleep(0.5)

        # 关闭商店
        self.operator.click_point(0.5, 0.55, after_sleep=1.5)
        logger.info("购物流程结束，关闭商店")

    @property
    def current_team_size(self) -> int:
        """获取当前队伍人数"""
        size = 0
        for character in self.on_field_character + self.off_field_character:
            if character is not None:
                size += 1
        return size

    @property
    def in_hand_character_count(self) -> int:
        """获取当前备战席角色数量"""
        count = 0
        for character in self.in_hand_character:
            if character is not None:
                count += 1
        return count

    def get_level(self):
        logger.info("获取当前等级")
        level = self.operator.ocr(from_x=0.05, from_y=0.815, to_x=0.3, to_y=0.87)
        try:
            if level:
                return int(level[0][1].split('.')[1])
            else:
                return self.max_team_size
        except (ValueError, IndexError):
            return self.max_team_size

    def update_max_team_size(self):
        result = self.operator.ocr(from_x=0.505, from_y=0.18, to_x=0.608, to_y=0.27)
        if result:
            try:
                self.max_team_size = int(str(result[-1][1]).strip())
                logger.info(f"最大队伍人数已更新为 {self.max_team_size}")
            except ValueError:
                logger.warning(f"无法解析最大队伍人数：{result[-1][1]}")

    def load_strategy(self, name: str):
        """加载攻略文件"""
        if not name.strip():
            logger.warning("未指定攻略文件名，将使用默认策略")
            name = "template"
        if ".json" in name:
            path = name
        else:
            path = f"tasks/currency_wars/strategies/{name}.json"
        with open(path, "r", encoding="utf-8") as f:
            strategy_data: dict[str, Any] = json.load(f)
        strategy_title = strategy_data.get("title", "")
        description = strategy_data.get("description", "")
        self.min_coins = strategy_data.get("min_coins", 40)
        self.min_level = strategy_data.get("min_level", 7)
        self.mid_level = strategy_data.get("mid_level", 7)
        self.strategy_code = strategy_data.get("share_code", "")
        self.strategy_special_events = strategy_data.get("special_events", {})
        # 攻略推荐的投资策略：分号（含全角）分隔、按优先级排列，与 cw_plus 的解析口径保持一致
        self.recommend_invest_strategies = tuple(
            name
            for name in (re.sub(r"\s+", "", item) for item in
                         re.split(r"[;；\n]+", str(strategy_data.get("recommend_invest_strategies") or "")))
            if name
        )
        # 在攻略中的角色设置成最高优先级
        strategy_on_field: dict[str, int] = strategy_data.get("on_field", {})
        strategy_off_field: dict[str, int] = strategy_data.get("off_field", {})
        # 攻略里的角色名可能带 OCR 无法输入的字符（如“知更鸟•晴歌”），统一按角色表的标准名称记录
        on_field_names: set[str] = set()
        for i, cn in enumerate(strategy_on_field.keys()):
            c = Characters.get_character(cn)
            if c is None:
                logger.warning(f"攻略角色不存在，跳过：{cn}")
                continue
            c.priority = 99 - i  # 确保攻略中的前台角色优先级都大于其他角色，同时有所差别
            c.position = Positioning.OnField
            on_field_names.add(c.name)
            self.strategy_characters[c.name] = strategy_on_field[cn]
        for i, cn in enumerate(strategy_off_field):
            c = Characters.get_character(cn)
            if c is None:
                logger.warning(f"攻略角色不存在，跳过：{cn}")
                continue
            c.priority = 99 - i  # 确保攻略中的后台角色优先级都大于其他角色
            if c.name in on_field_names:
                c.position = Positioning.OnOffField
            else:
                c.position = Positioning.OffField
                self.strategy_characters[c.name] = strategy_off_field[cn]
        # 兼容旧配置: 1 -> 1星，3 -> 2星，9 -> 3星
        for sc in self.strategy_characters.keys():
            if 3 < self.strategy_characters[sc] < 9:
                self.strategy_characters[sc] = 2
            elif self.strategy_characters[sc] >= 9:
                self.strategy_characters[sc] = 3
        logger.info(f"已加载攻略: {strategy_title}: {description}")

    def unload_strategy(self):
        """卸载当前攻略，重置角色"""
        for c in Characters.characters.values():
            c.reset()
        self.min_coins = 40
        self.min_level = 7
        self.mid_level = 7
        self.strategy_characters.clear()
        self.strategy_code = ""
        self.recommend_invest_strategies = ()
        logger.info("已卸载当前攻略，角色优先级已重置")

    def place_character(self) -> bool:
        """
        将手中的角色放置到前台或后台（严格先放前台角色，再放后台角色，确保前台优先占位）
        队伍不满时优先放空位，队伍满时仅替换低priority角色
        :return: 至少有一个角色放置成功返回 True，否则返回 False
        """
        # 第一次遍历：仅处理前台角色（Positioning.OnField / OnOffField），优先占满编队
        logger.info("=== 放置前台角色 ===")
        for i, character in enumerate(self.in_hand_character):
            if character is None or character.is_placed:
                continue
            if character.position == Positioning.OffField:
                continue
            if self.place_on_field_character(i):
                self.operator.sleep(2)
                self.handle_special_event()

        # 第二次遍历：仅处理后台角色（Positioning.OffField），填充剩余空位或替换
        logger.info("=== 放置后台角色 ===")
        for i, character in enumerate(self.in_hand_character):
            if character is None or character.is_placed:
                continue
            if character.position == Positioning.OnField:
                continue
            if self.place_off_field_character(i):
                self.operator.sleep(2)
                self.handle_special_event()

        # 若前台为空，则从后台提升一个角色到前台第一个位置，避免出现前台全空的情况
        self.ensure_on_field_not_empty()

        logger.info("角色放置完成")
        return True

    def place_on_field_character(self, character_in_hand: int) -> bool:
        """将手中的角色放置到前台（前台角色专用，优先占位）"""
        return self._place_to_target(
            character_in_hand,
            target_characters=self.on_field_character,
            area_type="前台"
        )

    def place_off_field_character(self, character_in_hand: int) -> bool:
        """将手中的角色放置到后台（后台角色专用）"""
        return self._place_to_target(
            character_in_hand,
            target_characters=self.off_field_character,
            area_type="后台"
        )

    def ensure_on_field_not_empty(self) -> bool:
        """校验前台角色列表是否为空。

        若前台（on_field_character）全部为空，则从后台（off_field_character）中按顺序取第一个非空角色，
        将其移动到前台第一个位置（索引 0）。

        :return: 若无需处理或移动成功返回 True；若前台为空且后台也无角色可移动则返回 False。
        """
        if any(c is not None for c in self.on_field_character):
            return True

        for off_index, char in enumerate(self.off_field_character):
            if char is None:
                continue
            logger.info(f"前台为空，提升后台角色 {char.name} (off_field[{off_index}]) 到前台 on_field[0]")
            return self.swap_character_between_areas(
                source_area_type='off_field',
                source_index=off_index,
                target_area_type='on_field',
                target_index=0,
            )

        logger.warning("前台为空且后台无角色可提升")
        return False

    def _place_to_target(
            self,
            character_in_hand: int,
            target_characters: list[Character | None],
            area_type: str
    ) -> bool:
        """通用放置逻辑：适配前台/后台角色，处理空位放置和满员替换"""
        full_team = self.current_team_size >= self.max_team_size

        # 1. 基础有效性检查
        if not (0 <= character_in_hand < len(self.in_hand_character)):
            logger.error(f"无效的备战席索引：{character_in_hand}（范围：0~{len(self.in_hand_character) - 1}）")
            return False

        character = self.in_hand_character[character_in_hand]
        if not character or character.is_placed:
            logger.warning(f"备战席索引 {character_in_hand} 无有效角色或角色已放置")
            return False

        # 确定目标区域类型
        target_area_type = 'on_field' if area_type == "前台" else 'off_field'

        # 2. 队伍不满时：优先填充空位（前台角色优先占名额）
        if not full_team:
            for index, existing_char in enumerate(target_characters):
                if existing_char is None:
                    # 使用通用对换方法实现放置
                    success = self.swap_character_between_areas(
                        source_area_type='in_hand',
                        source_index=character_in_hand,
                        target_area_type=target_area_type,
                        target_index=index
                    )
                    if success:
                        logger.info(
                            f"角色 {character.name} 放置到{area_type}索引 {index}（空位）")
                    return success
            logger.info(f"{area_type}无空位可放置")

        # 3. 队伍已满时：仅替换低priority角色（不占用新名额）
        min_priority = character.priority if character.priority is not None else -1
        min_priority_index = -1
        # 查找目标区域中priority最低的角色
        for index, existing_char in enumerate(target_characters):
            # if existing_char is None:
            if existing_char is None or existing_char.priority is None:
                continue
            # existing_priority = existing_char.priority if existing_char.priority is not None else -1
            # if existing_priority < min_priority:
            if existing_char.priority < min_priority:
                # min_priority = existing_priority
                min_priority = existing_char.priority
                min_priority_index = index

        if min_priority_index != -1:
            existing_char = target_characters[min_priority_index]
            if existing_char is None:  # 类型保护
                return False
            # 使用通用对换方法实现替换
            success = self.swap_character_between_areas(
                source_area_type='in_hand',
                source_index=character_in_hand,
                target_area_type=target_area_type,
                target_index=min_priority_index
            )
            if success:
                logger.info(
                    f"角色 {character.name} 替换{area_type}索引 {min_priority_index} 的 {existing_char.name} "
                    f"(priority {existing_char.priority} → {character.priority})"
                )
            return success

        logger.info(f"{character.name}无低priority角色可替换")
        return False

    def swap_character_between_areas(
            self,
            source_area_type: str,
            source_index: int,
            target_area_type: str,
            target_index: int
    ) -> bool:
        """
        通用方法：将两个任意区域的任意位置的内容进行对换

        :param source_area_type: 源区域类型 ('on_field', 'off_field', 'in_hand')
        :param source_index: 源区域中的位置索引
        :param target_area_type: 目标区域类型 ('on_field', 'off_field', 'in_hand')
        :param target_index: 目标区域中的位置索引
        :return: 对换操作是否成功
        """
        # 区域映射：区域类型 -> (角色列表, 坐标列表, 区域名称)
        area_mapping = {
            'on_field': (self.on_field_character, self.on_field_area, "前台"),
            'off_field': (self.off_field_character, self.off_field_area, "后台"),
            'in_hand': (self.in_hand_character, self.in_hand_area, "备战席")
        }

        # 验证区域类型
        if source_area_type not in area_mapping or target_area_type not in area_mapping:
            logger.error(f"无效的区域类型: source={source_area_type}, target={target_area_type}")
            return False

        # 获取源区域信息
        source_chars, source_coords, source_name = area_mapping[source_area_type]
        # 获取目标区域信息
        target_chars, target_coords, target_name = area_mapping[target_area_type]

        # 验证索引范围
        if not (0 <= source_index < len(source_coords)):
            logger.error(f"源区域索引超出范围: {source_index} (0~{len(source_coords) - 1})")
            return False
        if not (0 <= target_index < len(target_coords)):
            logger.error(f"目标区域索引超出范围: {target_index} (0~{len(target_coords) - 1})")
            return False

        # 获取源和目标角色
        source_char = source_chars[source_index]
        target_char = target_chars[target_index]

        # 记录操作前的状态
        logger.info(f"开始对换操作: {source_name}[{source_index}] <-> {target_name}[{target_index}]")
        logger.info(f"源角色: {source_char.name if source_char else 'None'}")
        logger.info(f"目标角色: {target_char.name if target_char else 'None'}")

        # 执行拖拽对换操作
        source_coord = source_coords[source_index]
        target_coord = target_coords[target_index]

        # 执行拖拽
        self.operator.drag_to(source_coord[0], source_coord[1], target_coord[0], target_coord[1], trace=False)
        self.operator.sleep(0.5)  # 等待操作完成
        if self.operator.locate(CWIMG.CANNOT_BE_FIELDED, from_x=0.25, from_y=0.25, to_x=0.75, to_y=0.75):
            logger.warning("对换操作失败：目标位置无法放置该角色")
            self.operator.sleep(1)
            self.operator.click_point(0.5, 0.5, after_sleep=1)  # 点击空白处关闭提示.
            return False

        # 交换角色列表中的内容
        source_chars[source_index], target_chars[target_index] = target_chars[target_index], source_chars[source_index]

        # 更新角色的放置状态
        if source_char:
            source_char.is_placed = (target_area_type != 'in_hand')
        if target_char:
            target_char.is_placed = (source_area_type != 'in_hand')

        logger.info(f"对换操作完成: {source_name}[{source_index}] <-> {target_name}[{target_index}]")
        return True

    def sort_characters_by_priority(self, area_type: str) -> bool:
        """
        对指定区域的角色按优先级进行排序，高优先级角色靠前

        :param area_type: 区域类型 ('on_field', 'off_field')
        :return: 排序操作是否成功
        """
        # 验证区域类型
        if area_type not in ['on_field', 'off_field']:
            logger.error(f"无效的区域类型: {area_type}，只支持 'on_field' 或 'off_field'")
            return False

        # 获取区域信息
        if area_type == 'on_field':
            characters = self.on_field_character
            area_name = "前台"
        else:  # off_field
            characters = self.off_field_character
            area_name = "后台"

        logger.info(f"开始对{area_name}区域角色按优先级排序")

        # 创建包含角色、索引和优先级的列表
        char_info_list = []
        for i, char in enumerate(characters):
            if char is not None:
                char_info_list.append({
                    'character': char,
                    'index': i,
                    'priority': char.priority
                })
            else:
                char_info_list.append({
                    'character': None,
                    'index': i,
                    'priority': -1  # 空位置优先级设为-1，排在最后
                })

        # 按优先级降序排序（高优先级在前）
        sorted_char_info = sorted(char_info_list, key=lambda x: x['priority'], reverse=True)

        # 执行排序交换
        success_count = 0
        for target_pos, target_info in enumerate(sorted_char_info):
            current_pos = target_info['index']

            # 如果已经在正确位置，跳过
            if current_pos == target_pos:
                continue

            # 找到目标位置当前的角色信息
            target_current_info: dict[str, Any] | None = None
            for info in char_info_list:
                if info['index'] == target_pos:
                    target_current_info = info
                    break

            # 如果目标位置为空或优先级较低，执行交换
            if (target_current_info is None or
                    target_current_info['character'] is None or
                    target_current_info['priority'] <= target_info['priority']):

                # 执行交换
                if self.swap_character_between_areas(
                        source_area_type=area_type,
                        source_index=current_pos,
                        target_area_type=area_type,
                        target_index=target_pos
                ):
                    success_count += 1
                    logger.info(f"{area_name}区域位置交换: [{current_pos}] <-> [{target_pos}]")

                    # 更新char_info_list中的索引信息
                    for info in char_info_list:
                        if info['index'] == current_pos:
                            info['index'] = target_pos
                        elif info['index'] == target_pos:
                            info['index'] = current_pos

        logger.info(f"{area_name}区域优先级排序完成，成功交换 {success_count} 次")
        return success_count > 0

    def sort_all_areas_by_priority(self) -> bool:
        """
        对所有区域（前台和后台）的角色按优先级进行排序

        :return: 排序操作是否成功
        """
        logger.info("开始对所有区域进行优先级排序")

        field_success = self.sort_characters_by_priority('on_field')
        offfield_success = self.sort_characters_by_priority('off_field')

        overall_success = field_success or offfield_success

        if overall_success:
            logger.info("所有区域优先级排序完成")
        else:
            logger.info("无需进行排序操作")

        return overall_success
