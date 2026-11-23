# -*- coding: utf-8 -*-
"""货币战争赛季页（「货币战争•入画奇航」等）BWIKI 落地 + 解析 + 本地查询。

数据源：https://wiki.biligame.com/sr/货币战争•入画奇航

页面主体是奖励表：羁绊链路（阵营/流派两栏，每档含「标准博弈限定」的高阶任务）、
职级奖励、创业指南（4 章，含「完成标准博弈 / 超频博弈 / 难度3及以上」类目标）。
「赛季优势 / 职级效果 / 预期收益 / 图鉴奖励」当前页面为空，按 WIP 记录，不臆造。

羁绊的类别与成员数取自 tasks/currency_wars/data/currency_war_characters.json，
避免在脚本里再维护一份羁绊名单。

用法：
    python scripts/currency_war_season.py                 # 抓取或复用缓存，生成数据文件
    python scripts/currency_war_season.py --refresh       # 忽略缓存重新抓取
    python scripts/currency_war_season.py --stats         # 条数 / 限制统计
    python scripts/currency_war_season.py --grep 标准博弈  # 关键词检索目标与说明

查询类命令只读本地 JSON，不碰网络。
缓存目录（仓库外，不入库）：%APPDATA%/SRA/cache/bwiki/season_ru_hua_qi_hang.wikitext
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from currency_war_invest import FetchFailed, api_get  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "SRA" / "cache" / "bwiki"
CACHE_FILE = CACHE_DIR / "season_ru_hua_qi_hang.wikitext"
DATA_DIR = REPO_ROOT / "tasks" / "currency_wars" / "data"
JSON_PATH = DATA_DIR / "currency_war_season_s2.json"
MD_PATH = DATA_DIR / "currency_war_season_s2.md"
CHARACTERS_PATH = DATA_DIR / "currency_war_characters.json"

PAGE = "货币战争•入画奇航"
SEASON = "入画奇航"
SOURCE_URL = f"https://wiki.biligame.com/sr/{urllib.parse.quote(PAGE)}"
CST = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- 抓取

def fetch_wikitext(refresh: bool) -> str:
    """落地页面 wikitext；已有缓存直接复用。"""
    if CACHE_FILE.exists() and not refresh:
        print(f"[缓存命中] {PAGE} <- {CACHE_FILE}")
        return CACHE_FILE.read_text(encoding="utf-8-sig")

    print(f"[抓取] {SOURCE_URL}")
    try:
        payload = api_get({
            "action": "parse", "page": PAGE, "prop": "wikitext",
            "format": "json", "formatversion": 2,
        })
    except FetchFailed as e:
        raise SystemExit(f"[错误] 抓取失败: {e}\n已有缓存仍在 {CACHE_FILE}，可去掉 --refresh 复用。") from None
    text = payload["parse"]["wikitext"]
    if isinstance(text, dict):
        text = text["*"]
    if "羁绊链路" not in text:
        raise SystemExit("[错误] 页面结构变化：未找到『羁绊链路』，请检查解析规则")
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(text, encoding="utf-8")
    print(f"[已落地] wikitext {len(text)} 字符 -> {CACHE_FILE}")
    return text


# ------------------------------------------------------------------------- 解析工具

def section(text: str, start: str, end: str | None) -> str:
    """截取标题 start 到 end 之间的正文。"""
    begin = text.find(start)
    if begin < 0:
        return ""
    begin += len(start)
    stop = text.find(end, begin) if end else len(text)
    return text[begin:stop if stop >= 0 else len(text)]


def strip_markup(text: str) -> str:
    """把 {{图标|名称|数量}} 等模板压成可读文本。"""
    text = re.sub(r"\{\{图标\|([^|}]+)\|([^|}]+)\}\}", r"\1×\2", text)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rewards(text: str) -> dict[str, str]:
    """从 {{图标|星琼|20}}、{{图标|晋升点|200}} 提取奖励字典。"""
    rewards: dict[str, str] = {}
    for name, amount in re.findall(r"\{\{图标\|([^|}]+)\|([^|}]+)\}\}", text):
        rewards[name.strip()] = amount.strip()
    return rewards


def parse_tables(text: str) -> list[list[list[str]]]:
    """解析页面中所有 wikitable -> [[[单元格, ...], ...], ...]。"""
    tables: list[list[list[str]]] = []
    rows: list[list[str]] = []
    cells: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("{|"):
            inside, rows, cells = True, [], []
            continue
        if not inside:
            continue
        if line == "|}":
            if cells:
                rows.append(cells)
            tables.append(rows)
            inside, cells = False, []
            continue
        if line.startswith("|-"):
            if cells:
                rows.append(cells)
            cells = []
            continue
        if line.startswith("!") or line.startswith("|"):
            # 本页用 `! 目标 !! 奖励` / `| 目标 || 奖励 || 备注` 的单行多列写法
            content = line.lstrip("!|").strip()
            # 保留原始模板供 parse_rewards 解析，展示时再 strip_markup
            cells.extend(part.strip() for part in re.split(r"!!|\|\|", content))
    return tables


def restrictions(text: str) -> list[str]:
    """从目标文字提取关卡/职级限制。"""
    found: list[str] = []
    for token in ("标准博弈", "超频博弈", "投资大师及以上", "难度3及以上"):
        if token in text:
            found.append(token)
    return found


# ------------------------------------------------------------------------- 解析

def load_bond_meta() -> dict[str, dict]:
    data = json.loads(CHARACTERS_PATH.read_text(encoding="utf-8"))
    return data.get("bonds", {})


def parse_bond_chain(text: str, bond_meta: dict[str, dict]) -> list[dict]:
    body = section(text, "===羁绊链路===", "===职级奖励===")
    result: list[dict] = []
    # 前两个羁绊用四级标题，其余用三级标题，两种都认
    matches = list(re.finditer(r"^(====([^=\n]+)====|===([^=\n]+)===)$", body, flags=re.M))
    for index, match in enumerate(matches):
        name = (match.group(2) or match.group(3)).strip()
        chunk = body[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(body)]
        tables = parse_tables(chunk)
        if not tables:
            continue
        meta = bond_meta.get(name, {})
        for row in tables[0][1:]:  # 跳过表头
            if len(row) < 2:
                continue
            goal, reward_text = strip_markup(row[0]), row[1]
            note = strip_markup(row[2]) if len(row) > 2 else ""
            match = re.search(r"激活(\d+)(.+)$", goal)
            if not match:
                continue
            count = int(match.group(1))
            rank = "投资大师及以上" if "投资大师及以上" in goal else ""
            result.append({
                "bond": name,
                "kind": meta.get("kind", "未分类"),
                "count": count,
                "rank_requirement": rank,
                "goal": goal,
                "reward": parse_rewards(reward_text) or {"原文": reward_text},
                "note": note,
                "difficulty_restriction": restrictions(goal + note),
                "s2_members": [m["name"] for m in meta.get("members", [])
                               if any(m.get("name") == c.get("name") and c.get("variant") == SEASON
                                      for c in _characters_cache)],
            })
    return result


_characters_cache: list[dict] = []


def parse_rank_rewards(text: str) -> dict:
    body = section(text, "===职级奖励===", "===创业指南===")
    entries = []
    for line in body.splitlines():
        if not line.strip().startswith("*") or "职级达到" not in line:
            continue
        span = re.search(r"'''(.+?)'''", line)
        ranks = [r.strip() for r in span.group(1).split("/")] if span else []
        entries.append({
            "ranks": ranks,
            "reward": parse_rewards(line),
            "text": strip_markup(line.lstrip("*")),
        })
    total_line = next((ln for ln in body.splitlines() if "奖励总计" in ln), "")
    return {"entries": entries, "total": parse_rewards(total_line)}


def parse_guide(text: str) -> list[dict]:
    body = section(text, "===创业指南===", "===预期收益===")
    chapters: list[dict] = []
    for index, chunk in enumerate(re.split(r'<div class="resp-tab-content"', body)[1:], start=1):
        tables = parse_tables(chunk)
        if not tables:
            continue
        objectives = []
        for row in tables[0][1:]:
            if len(row) < 2:
                continue
            objectives.append({
                "goal": strip_markup(row[0]),
                "reward": parse_rewards(row[1]) or {"原文": strip_markup(row[1])},
                "difficulty_restriction": restrictions(strip_markup(row[0])),
            })
        completion = {"reward": {}, "text": ""}
        if len(tables) > 1 and tables[1]:
            row = tables[1][0]
            completion = {
                "reward": parse_rewards(row[1]) if len(row) > 1 else {},
                "text": strip_markup(row[0]) if row else "",
            }
        chapters.append({"chapter": index, "objectives": objectives, "completion": completion})
    return chapters


def parse_page(text: str) -> dict:
    global _characters_cache
    _characters_cache = json.loads(CHARACTERS_PATH.read_text(encoding="utf-8"))["characters"]
    wip = {}
    for heading in ("赛季优势", "职级效果", "预期收益", "图鉴奖励"):
        body = section(text, f"==={heading}===", None).split("==")[0].strip()
        wip[heading] = body or "（页面当前为空，施工中）"
    return {
        "meta": {
            "source": SOURCE_URL,
            "index_page": PAGE,
            "season": SEASON,
            "installed_version": (re.search(r"实装版本=([^\s}\n]+)", text) or [None, ""])[1],
            "page_status": "施工中" if "施工中" in text else "正式",
            "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
            "character_data": "currency_war_characters.json",
        },
        "season_gameplay": wip,
        "rewards": {
            "point_rewards": "参见「货币战争」页面（本页以 #section-h 引用）",
            "promotion_notes": [strip_markup(ln.lstrip("*")) for ln in
                                section(text, "===晋升等级===", "===羁绊链路===").splitlines()
                                if ln.strip().startswith("*")],
            "bond_chain": parse_bond_chain(text, load_bond_meta()),
            "rank_rewards": parse_rank_rewards(text),
            "startup_guide": parse_guide(text),
        },
    }


# --------------------------------------------------------------------- Markdown

def reward_text(reward: dict) -> str:
    return "、".join(f"{k}×{v}" for k, v in reward.items()) if reward else "—"


def render_md(data: dict) -> str:
    m, r = data["meta"], data["rewards"]
    lines = [
        f"# 货币战争·{m['season']}赛季资料（BWIKI）", "",
        f"- 来源：<{m['source']}>｜实装版本：{m['installed_version']}｜状态：{m['page_status']}",
        f"- 抓取时间：{m['fetched_at']}｜羁绊链路任务：{len(r['bond_chain'])} 条",
        "- 羁绊类别与 S2 成员取自 `currency_war_characters.json`。", "",
    ]
    lines += ["## 赛季玩法", ""]
    for name, body in data["season_gameplay"].items():
        lines += [f"### {name}", "", body, ""]
    lines += ["## 奖励", "", "### 积分奖励", "", r["point_rewards"], "", "### 晋升等级", ""]
    lines += [f"- {n}" for n in r["promotion_notes"]]
    lines += ["", "### 羁绊链路", ""]
    lines += ["| 类别 | 羁绊 | 目标 | 奖励 | 限制 | S2 成员 |", "|---|---|---|---|---|---|"]
    for item in r["bond_chain"]:
        limit = "、".join(item["difficulty_restriction"]) or "—"
        lines.append("| {kind} | {bond} | {goal} | {reward} | {limit} | {members} |".format(
            kind=item["kind"], bond=item["bond"], goal=item["goal"],
            reward=reward_text(item["reward"]), limit=limit,
            members="、".join(item["s2_members"]) or "—"))
    lines += ["", "> 注：带「标准博弈限定」的第三档奖励还需职级达到投资大师及以上，超频博弈不计。", ""]
    lines += ["### 职级奖励", ""]
    for entry in r["rank_rewards"]["entries"]:
        lines.append(f"- {'/'.join(entry['ranks'])}：{reward_text(entry['reward'])}")
    lines += [f"- 总计：{reward_text(r['rank_rewards']['total'])}", "", "### 创业指南", ""]
    for chapter in r["startup_guide"]:
        lines += [f"#### 第{chapter['chapter']}章", "", "| 目标 | 奖励 | 限制 |", "|---|---|---|"]
        for obj in chapter["objectives"]:
            lines.append(f"| {obj['goal']} | {reward_text(obj['reward'])} | "
                         f"{'、'.join(obj['difficulty_restriction']) or '—'} |")
        lines += ["", f"完成全部：{reward_text(chapter['completion']['reward'])}", ""]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="货币战争赛季页资料落地与查询")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存重新抓取")
    parser.add_argument("--stats", action="store_true", help="统计条数与限制")
    parser.add_argument("--grep", metavar="关键词", help="检索目标与说明")
    args = parser.parse_args()

    if args.stats or args.grep:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        items = data["rewards"]["bond_chain"] + [o for c in data["rewards"]["startup_guide"]
                                                 for o in c["objectives"]]
        if args.stats:
            print(f"羁绊链路 {len(data['rewards']['bond_chain'])} 条，创业指南 "
                  f"{sum(len(c['objectives']) for c in data['rewards']['startup_guide'])} 条")
            limited = [i for i in items if i.get("difficulty_restriction")]
            print(f"含关卡/职级限制 {len(limited)} 条")
            for item in limited:
                print(f"  {item.get('bond') or item.get('goal')}: "
                      f"{'/'.join(item['difficulty_restriction'])}")
            return
        hits = [i for i in items if args.grep in json.dumps(i, ensure_ascii=False)]
        print(f"命中 {len(hits)} 条")
        for item in hits:
            print(json.dumps(item, ensure_ascii=False))
        return

    data = parse_page(fetch_wikitext(args.refresh))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MD_PATH.write_text(render_md(data), encoding="utf-8")
    print(f"[已落地] {len(data['rewards']['bond_chain'])} 条羁绊链路任务 -> {JSON_PATH}")
    print(f"[已落地] Markdown -> {MD_PATH}")


if __name__ == "__main__":
    main()
