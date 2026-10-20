# -*- coding: utf-8 -*-
"""货币战争 BWIKI 角色一览落地 + HTML 解析 + 羁绊/费用速查。

思路：索引页 https://wiki.biligame.com/sr/角色一览（货币） 一张表就是全量角色，
每个角色行的 <tr> 上带 data-param1..8，列依次是
    1=标签(输出/辅助/治疗/护盾)  2=站位(前/后/前后)  3~5=羁绊(同值重复三遍供三类筛选)
    6=费用  7=实装版本  8=专家顾问(是/否)
筛选区里的 data-option="<组>|<羁绊名>" 给出羁绊归类（3=阵营羁绊、4=流派羁绊、5=独立羁绊）。
因此 **只需 1 次请求、解析 1 份 HTML**，不逐角色翻页。

设计目标：
- 原始 HTML 落地缓存，默认命中缓存不请求网络；解析结果写进仓库供后续查表组队。
- 顺带和 tasks/currency_wars/characters.py 的注册表对一遍名/费用/站位，暴露差异。

用法：
    python scripts/currency_war_wiki.py                      # 抓取或复用缓存，生成数据文件
    python scripts/currency_war_wiki.py --refresh            # 忽略缓存重新抓取索引页
    python scripts/currency_war_wiki.py --bonds              # 羁绊索引（人数从多到少）
    python scripts/currency_war_wiki.py --bond 昼之半神 --cost 1-3
    python scripts/currency_war_wiki.py --bond 能量 --bond 群攻 --mode all
    python scripts/currency_war_wiki.py --tag 治疗 --position 后
    python scripts/currency_war_wiki.py --detail 阿格莱雅    # 打印单角色记录
    python scripts/currency_war_wiki.py --stale              # 与 characters.py 不一致的角色

筛选类命令（--bonds/--bond/--tag/--position/--detail/--stale）只读本地 JSON，不碰网络。

缓存目录（仓库外，不入库）：%APPDATA%/SRA/cache/bwiki/index.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "SRA" / "cache" / "bwiki"
INDEX_CACHE = CACHE_DIR / "index.html"
DATA_DIR = REPO_ROOT / "tasks" / "currency_wars" / "data"
JSON_PATH = DATA_DIR / "currency_war_characters.json"
MD_PATH = DATA_DIR / "currency_war_characters.md"

API = "https://wiki.biligame.com/sr/api.php"
INDEX_PAGE = "角色一览（货币）"
SOURCE_URL = f"https://wiki.biligame.com/sr/{urllib.parse.quote(INDEX_PAGE)}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SRA-wiki-fetch/1.0"

# 索引页筛选区 data-option 的组号 -> 羁绊归类
BOND_GROUP_KIND = {"3": "阵营羁绊", "4": "流派羁绊", "5": "独立羁绊"}
POSITION_MAP = {"前": "前台", "后": "后台", "前后": "前后台"}
WIKI_TO_ENUM = {"前台": "OnField", "后台": "OffField", "前后台": "OnOffField"}

# 让脚本能 import 项目内的角色注册表做名称/数值校验（绕开包 __init__ 的重量依赖）
Characters = None
try:
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "cw_characters", REPO_ROOT / "tasks" / "currency_wars" / "characters.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)  # type: ignore
    Characters = _mod.Characters
except Exception as e:  # noqa: BLE001
    print(f"[警告] 角色注册表加载失败，跳过名称校验: {e}")


# --------------------------------------------------------------------------- 抓取

def fetch_index_html(refresh: bool) -> str:
    """落地索引页渲染 HTML；已有缓存直接复用（1 次请求拿全量角色）。"""
    if INDEX_CACHE.exists() and not refresh:
        print(f"[缓存命中] {INDEX_PAGE} <- {INDEX_CACHE}")
        return INDEX_CACHE.read_text(encoding="utf-8-sig")

    print(f"[抓取] {SOURCE_URL}")
    query = urllib.parse.urlencode({
        "action": "parse",
        "page": INDEX_PAGE,
        "prop": "text",
        "format": "json",
        "formatversion": 2,
    })
    last_err = ""
    payload = None
    for attempt in range(1, 4):
        req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            break
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
            # wiki 对连续请求会返回 HTTP 567 限流，按退避重试
            last_err = str(e)
            wait = 8 * attempt
            print(f"[重试 {attempt}/3] {last_err} -> {wait}s 后重试")
            time.sleep(wait)
    if payload is None:
        raise SystemExit(f"[错误] 索引页抓取失败: {last_err}\n已有缓存仍在 {INDEX_CACHE}，可去掉 --refresh 复用。")

    text = payload["parse"]["text"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_CACHE.write_text(text, encoding="utf-8")
    print(f"[已落地] {len(text)} 字符 HTML -> {INDEX_CACHE}")
    return text


# ------------------------------------------------------------------------- 解析

def _attr(row: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', row)
    return html.unescape(m.group(1)).strip() if m else ""


def _split_list(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，、]", value) if x.strip()]


def parse_bond_kinds(text: str) -> dict[str, str]:
    """从筛选区解析 {羁绊名: 阵营羁绊|流派羁绊|独立羁绊}。"""
    kinds: dict[str, str] = {}
    for group, name in re.findall(r'data-option="(\d)\|([^"]+)"', text):
        kind = BOND_GROUP_KIND.get(group)
        if kind:
            kinds.setdefault(html.unescape(name).strip(), kind)
    return kinds


def parse_rows(text: str, bond_kinds: dict[str, str]) -> list[dict]:
    """解析角色行 -> 记录列表。"""
    records: list[dict] = []
    for m in re.finditer(r'<tr class="divsort"([^>]*)>(.*?)</tr>', text, re.S):
        attrs, body = m.group(1), m.group(2)
        param = dict(re.findall(r'data-param(\d)="([^"]*)"', attrs))
        page = _attr(body, "href").split("/sr/")[-1]
        page = urllib.parse.unquote(page.split("#")[0])
        wiki_name = page.split("/")[-1] if "/" in page else _attr(body, "title")
        bonds = _split_list(html.unescape(param.get("3", "")))
        position = POSITION_MAP.get(param.get("2", ""), param.get("2", ""))
        # 费用一般是单值；个别角色（如银狼LV.999）在不同阶段费用不同，写作 "3, 4, 5"
        costs = sorted({int(x) for x in re.findall(r"\d+", param.get("6", ""))})
        cost = costs[0] if costs else None
        registry = match_registry(wiki_name)
        record = {
            # 优先用 characters.py 的规范名，方便直接写进 strategies json
            "name": registry["name"] if registry else wiki_name,
            "wiki_name": wiki_name,
            "wiki_page": page,
            "cost": cost,
            "position": position,
            "tags": _split_list(html.unescape(param.get("1", ""))),
            "bonds": bonds,
            "bond_kinds": {b: bond_kinds.get(b, "") for b in bonds},
            "version": param.get("7", ""),
            "expert": param.get("8", "") == "是",
            "registry": registry,
        }
        if len(costs) > 1:
            record["costs"] = costs
        if registry:
            diff = []
            if registry["cost"] not in (costs or [None]):
                diff.append(f"费用 注册表{registry['cost']} != wiki{'/'.join(map(str, costs)) or '未知'}")
            if registry["positioning"] != WIKI_TO_ENUM.get(position, ""):
                diff.append(f"站位 注册表{registry['positioning']} != wiki{position}")
            record["registry_diff"] = diff
        records.append(record)
    return sorted(records, key=lambda c: (c["cost"] or 99, c["name"]))


def match_registry(name: str) -> dict | None:
    """与 tasks/currency_wars/characters.py 的注册表对齐。"""
    if Characters is None:
        return None
    c = Characters.get_character(name.replace("•", "·"))
    if c is None:
        return None
    return {"name": c.name, "cost": c.cost, "positioning": c.positioning.name}


def build_bond_index(characters: list[dict]) -> dict:
    index: dict[str, dict] = {}
    for c in characters:
        for b in c["bonds"]:
            entry = index.setdefault(b, {"kind": c["bond_kinds"].get(b, ""), "members": []})
            entry["members"].append({
                "name": c["name"],
                "cost": c["cost"],
                "position": c["position"],
                "tags": c["tags"],
            })
    for entry in index.values():
        entry["members"].sort(key=lambda m: (m["cost"] or 99, m["name"]))
        entry["count"] = len(entry["members"])
    return index


# --------------------------------------------------------------------- Markdown

def render_md(data: dict) -> str:
    meta, bonds, chars = data["meta"], data["bonds"], data["characters"]
    lines = [
        "# 货币战争角色数据（BWIKI 角色一览）",
        "",
        f"- 来源：<{meta['source']}>",
        f"- 抓取时间：{meta['fetched_at']}｜角色数：{meta['count']}｜羁绊数：{len(bonds)}",
        f"- 完整记录：`{JSON_PATH.relative_to(REPO_ROOT).as_posix()}`",
        "- 站位/费用/羁绊/标签直接取自索引页表格；赋能、推荐装备等角色详情未纳入（数据量与请求成本不划算，需要时另行抓子页）",
        "",
        "## 羁绊索引",
        "",
    ]
    for kind in ("阵营羁绊", "流派羁绊", "独立羁绊", ""):
        subset = {k: v for k, v in bonds.items() if v["kind"] == kind}
        if not subset:
            continue
        lines += [f"### {kind or '未归类'}", ""]
        for name, entry in sorted(subset.items(), key=lambda x: -x[1]["count"]):
            lines += [f"#### {name}（{entry['count']} 人）", "",
                      "| 名称 | 费用 | 站位 | 标签 |", "| --- | --- | --- | --- |"]
            for m in entry["members"]:
                lines.append(f"| {m['name']} | {m['cost']} | {m['position']} | {'、'.join(m['tags'])} |")
            lines.append("")

    lines += [
        "## 角色速查（按费用）",
        "",
        "| 费用 | 名称 | 站位 | 标签 | 羁绊 | 版本 | 顾问 | 注册表 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in chars:
        lines.append(
            f"| {'/'.join(map(str, c.get('costs') or [c['cost']]))} | {c['name']} | {c['position']} | "
            f"{'、'.join(c['tags'])} | "
            f"{'、'.join(c['bonds'])} | {c['version']} | {'是' if c['expert'] else ''} | "
            f"{'✓' if c['registry'] else '未收录'} |"
        )

    lines += ["", "## 与 characters.py 注册表的差异", ""]
    missing = [c for c in chars if c["registry"] is None]
    diff = [c for c in chars if c.get("registry_diff")]
    for c in missing:
        lines.append(f"- 未收录：{c['name']}（wiki 页 {c['wiki_page']}，{c['cost']}费 {c['position']} "
                     f"{'、'.join(c['bonds'])}）")
    for c in diff:
        lines.append(f"- {c['name']}：{'；'.join(c['registry_diff'])}")
    if not missing and not diff:
        lines.append("- 无差异")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- 查询模式

def load_local() -> dict:
    if not JSON_PATH.exists():
        sys.exit(f"[错误] 本地数据不存在: {JSON_PATH}\n请先运行: python scripts/currency_war_wiki.py")
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def parse_cost_range(text: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)(?:-(\d+))?$", text)
    if not m:
        sys.exit(f"[错误] 费用写法不对: {text}（示例 1 或 1-3）")
    lo = int(m.group(1))
    return lo, int(m.group(2) or lo)


def query(args: argparse.Namespace) -> None:
    chars = load_local()["characters"]
    want = args.bond or []
    cost = parse_cost_range(args.cost) if args.cost else None

    def ok(c: dict) -> bool:
        if want:
            hit = set(c["bonds"]) & set(want)
            if args.mode == "all" and len(hit) != len(set(want)):
                return False
            if args.mode == "any" and not hit:
                return False
        if cost:
            values = [v for v in (c.get("costs") or [c["cost"]]) if v is not None]
            if not any(cost[0] <= v <= cost[1] for v in values):
                return False
        if args.tag and args.tag not in c["tags"]:
            return False
        if args.position and args.position not in c["position"]:
            return False
        if args.missing_registry and c["registry"] is not None:
            return False
        if args.stale and c["registry"] is not None and not c.get("registry_diff"):
            return False
        return True

    rows = sorted((c for c in chars if ok(c)), key=lambda c: (c["cost"] or 99, c["name"]))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    bond_text = "+".join(want) if want else "不限"
    print(f"命中 {len(rows)} 个角色（羁绊 {bond_text} / {args.mode}）")
    for c in rows:
        share = [b for b in c["bonds"] if b in want]
        extra = f"｜与注册表不一致: {'；'.join(c['registry_diff'])}" if c.get("registry_diff") else ""
        print(f"  {c['cost']}费 {c['name']}（{c['position']}｜{'、'.join(c['tags'])}｜{c['version']}）"
              f" 命中: {'、'.join(share) or '-'} | 全部: {'、'.join(c['bonds'])}{extra}")


def show_detail(name: str) -> None:
    for c in load_local()["characters"]:
        if c["name"] == name or c["wiki_name"] == name or c["wiki_page"].endswith("/" + name):
            print(json.dumps(c, ensure_ascii=False, indent=2))
            return
    sys.exit(f"[错误] 本地数据里没有角色: {name}")


# ------------------------------------------------------------------------- main

def rebuild(refresh: bool) -> None:
    text = fetch_index_html(refresh)
    bond_kinds = parse_bond_kinds(text)
    characters = parse_rows(text, bond_kinds)
    if not characters:
        raise SystemExit(f"[错误] 索引页里没解析到角色行，页面结构可能变了: {INDEX_CACHE}")

    data = {
        "meta": {
            "source": SOURCE_URL,
            "index_page": INDEX_PAGE,
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "count": len(characters),
            "bond_kinds": bond_kinds,
        },
        "bonds": build_bond_index(characters),
        "characters": characters,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    MD_PATH.write_text(render_md(data), encoding="utf-8")
    print(f"[生成] {JSON_PATH.relative_to(REPO_ROOT)}  ({data['meta']['count']} 个角色, {len(data['bonds'])} 个羁绊)")
    print(f"[生成] {MD_PATH.relative_to(REPO_ROOT)}")
    missing = [c["name"] for c in characters if c["registry"] is None]
    if missing:
        print(f"[提示] 未在 characters.py 收录（共 {len(missing)}）: {', '.join(missing)}")
    for c in characters:
        if c.get("registry_diff"):
            print(f"[差异] {c['name']}: {'；'.join(c['registry_diff'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description="落地 BWIKI 货币战争角色一览并生成羁绊/费用速查数据")
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存重新抓取索引页")
    ap.add_argument("--bonds", action="store_true", help="列出本地数据里的羁绊索引")
    ap.add_argument("--bond", action="append", default=[], help="按羁绊筛选（可多次）")
    ap.add_argument("--mode", choices=["any", "all"], default="any", help="多羁绊：任一/全部命中")
    ap.add_argument("--cost", help="费用筛选，如 1 或 1-3")
    ap.add_argument("--tag", help="标签筛选，如 输出/辅助/治疗/护盾")
    ap.add_argument("--position", help="站位筛选：前台/后台/前后台")
    ap.add_argument("--detail", help="打印单个角色记录（读本地数据）")
    ap.add_argument("--missing-registry", action="store_true", help="只看未在 characters.py 收录的角色")
    ap.add_argument("--stale", action="store_true", help="只看未收录或费用/站位与注册表不一致的角色")
    ap.add_argument("--json", action="store_true", help="筛选结果输出 JSON")
    args = ap.parse_args()

    if args.detail:
        show_detail(args.detail)
        return
    if args.bonds:
        for name, entry in sorted(load_local()["bonds"].items(), key=lambda x: -x[1]["count"]):
            print(f"{entry['count']:>3}  {entry['kind']:<5} {name}")
        return
    if args.bond or args.cost or args.tag or args.position or args.missing_registry or args.stale:
        query(args)
        return
    rebuild(args.refresh)


if __name__ == "__main__":
    main()
