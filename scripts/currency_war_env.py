# -*- coding: utf-8 -*-
"""货币战争「投资环境一览」BWIKI 落地 + 解析 + 本地查询。

数据源：https://wiki.biligame.com/sr/投资环境一览
该页正文只有一段 SMW 动态查询：
    {{#ask:[[分类:投资环境]] |?名称 |?效果 |?角色 |?装备 |limit=1000 }}
所以直接调 `action=ask` 一次拿全量（投资环境没有稀有度/位面维度，字段是
名字 / 效果 / 关联角色 / 关联装备），不抓渲染 HTML、更不逐条翻页。

复用 scripts/currency_war_invest.py 的 API 重试与 wikitext 清洗逻辑。

用法：
    python scripts/currency_war_env.py                      # 抓取或复用缓存，生成数据文件
    python scripts/currency_war_env.py --refresh            # 忽略缓存重新抓取
    python scripts/currency_war_env.py --stats              # 条数 / 角色 / 装备统计
    python scripts/currency_war_env.py --character 佩佩     # 查与某角色相关的环境
    python scripts/currency_war_env.py --equip 星徽         # 查与某装备相关的环境
    python scripts/currency_war_env.py --grep 金币          # 关键词搜名字与效果
    python scripts/currency_war_env.py --detail 二手市场     # 打印单条完整信息
    python scripts/currency_war_env.py --names              # 只要名字列表（填刷新白名单）

查询类命令只读本地 JSON，不碰网络。
缓存目录（仓库外，不入库）：%APPDATA%/SRA/cache/bwiki/investment_environment.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from currency_war_invest import FetchFailed, api_get, clean_markup  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "SRA" / "cache" / "bwiki"
CACHE_FILE = CACHE_DIR / "investment_environment.json"
DATA_DIR = REPO_ROOT / "tasks" / "currency_wars" / "data"
JSON_PATH = DATA_DIR / "currency_war_environments.json"
MD_PATH = DATA_DIR / "currency_war_environments.md"

PAGE = "投资环境一览"
CATEGORY = "投资环境"
SOURCE_URL = f"https://wiki.biligame.com/sr/{urllib.parse.quote(PAGE)}"
PROPERTIES = ["名称", "效果", "角色", "装备"]


# --------------------------------------------------------------------------- 抓取

def fetch_roster(refresh: bool) -> dict:
    """一次 ask 拿全量投资环境；原始响应落地缓存。"""
    if CACHE_FILE.exists() and not refresh:
        print(f"[缓存命中] 分类:{CATEGORY} <- {CACHE_FILE}")
        return json.loads(CACHE_FILE.read_text(encoding="utf-8-sig"))["results"]

    query = f"[[分类:{CATEGORY}]]" + "".join(f"|?{p}" for p in PROPERTIES) + "|limit=1000"
    print(f"[抓取] {query}")
    try:
        payload = api_get({"action": "ask", "query": query, "format": "json", "formatversion": 2})
    except FetchFailed as e:
        raise SystemExit(f"[错误] 抓取失败: {e}\n已有缓存仍在 {CACHE_FILE}，可去掉 --refresh 复用。") from None
    results = dict((payload.get("query") or {}).get("results") or {})
    if not results:
        raise SystemExit(f"[错误] ask 没返回数据，分类可能改名: {CATEGORY}")
    if payload.get("query-continue-offset"):
        print("[警告] 结果被截断，可能有环境未抓到")

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[已落地] {len(results)} 条投资环境 -> {CACHE_FILE}")
    return results


# ------------------------------------------------------------------------- 解析

def parse_records(roster: dict) -> list[dict]:
    """ask 结果 -> 投资环境记录。"""
    records: list[dict] = []
    for page, item in roster.items():
        printouts = item.get("printouts") or {}

        def vals(key: str) -> list[str]:
            return [clean_markup(str(v)) for v in (printouts.get(key) or []) if str(v).strip()]

        names = vals("名称")
        records.append({
            "name": names[0] if names else page,
            "effect": clean_markup(" ".join(vals("效果"))),
            "characters": vals("角色"),
            "equipment": vals("装备"),
            "wiki_page": page,
        })
    return sorted(records, key=lambda r: r["name"])


def build_index(records: list[dict], key: str) -> dict[str, list[str]]:
    """角色/装备 -> 相关环境名列表。"""
    index: dict[str, list[str]] = {}
    for r in records:
        for item in r[key]:
            index.setdefault(item, []).append(r["name"])
    return {k: sorted(v) for k, v in sorted(index.items())}


# --------------------------------------------------------------------- Markdown

def render_md(data: dict) -> str:
    meta, recs = data["meta"], data["records"]
    lines = [
        "# 货币战争投资环境一览（BWIKI）",
        "",
        f"- 来源：<{meta['source']}>（分类 {meta['category']}）",
        f"- 抓取时间：{meta['fetched_at']}｜环境数：{meta['count']}",
        f"- 完整数据：`{JSON_PATH.relative_to(REPO_ROOT).as_posix()}`",
        f"- 关联角色的环境 {meta['with_characters']} 条、关联装备的环境 {meta['with_equipment']} 条",
        "- 投资环境没有稀有度/出现位面维度（每局开局三选一），故只记录 名字/效果/关联角色/关联装备",
        "",
        "## 环境总表",
        "",
        "| 名字 | 效果 | 关联角色 | 关联装备 |",
        "| --- | --- | --- | --- |",
    ]
    for r in recs:
        effect = r["effect"].replace("|", "\\|")
        lines.append(f"| {r['name']} | {effect} | {'、'.join(r['characters'])} | {'、'.join(r['equipment'])} |")

    for title, key in (("角色", "characters"), ("装备", "equipment")):
        index = data[f"{key}_index"]
        lines += ["", f"## 按{title}索引", ""]
        if not index:
            lines.append("- 无")
            continue
        for item, names in index.items():
            lines.append(f"- **{item}**（{len(names)}）：{'、'.join(names)}")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------------- 本地

def rebuild(refresh: bool) -> None:
    records = parse_records(fetch_roster(refresh))
    data = {
        "meta": {
            "source": SOURCE_URL,
            "category": CATEGORY,
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "count": len(records),
            "with_characters": sum(1 for r in records if r["characters"]),
            "with_equipment": sum(1 for r in records if r["equipment"]),
        },
        "characters_index": build_index(records, "characters"),
        "equipment_index": build_index(records, "equipment"),
        "records": records,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MD_PATH.write_text(render_md(data), encoding="utf-8")
    print(f"[生成] {JSON_PATH.relative_to(REPO_ROOT)}  ({data['meta']['count']} 条投资环境)")
    print(f"[生成] {MD_PATH.relative_to(REPO_ROOT)}")
    print(f"[统计] 关联角色 {data['meta']['with_characters']} 条、关联装备 {data['meta']['with_equipment']} 条；"
          f"角色键 {len(data['characters_index'])} 个、装备键 {len(data['equipment_index'])} 个")


def load_local() -> dict:
    if not JSON_PATH.exists():
        sys.exit(f"[错误] 本地数据不存在: {JSON_PATH}\n请先运行: python scripts/currency_war_env.py")
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def query(args: argparse.Namespace) -> None:
    rows = load_local()["records"]
    if args.character:
        rows = [r for r in rows if args.character in "、".join(r["characters"])]
    if args.equip:
        rows = [r for r in rows if args.equip in "、".join(r["equipment"])]
    if args.grep:
        rows = [r for r in rows if args.grep in (r["name"] + r["effect"])]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    cond = " / ".join(x for x in [
        f"角色含“{args.character}”" if args.character else "",
        f"装备含“{args.equip}”" if args.equip else "",
        f"关键词“{args.grep}”" if args.grep else ""] if x) or "全部"
    print(f"命中 {len(rows)} 条（{cond}）")
    for r in rows:
        tail = " / ".join(filter(None, ["、".join(r["characters"]), "、".join(r["equipment"])]))
        if args.names:
            print(f"  {r['name']}")
        else:
            print(f"  {r['name']}：{r['effect']}" + (f"  [{tail}]" if tail else ""))


def show_detail(name: str) -> None:
    for r in load_local()["records"]:
        if r["name"] == name or name in r["name"]:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return
    sys.exit(f"[错误] 没有投资环境: {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="落地 BWIKI 货币战争投资环境一览并生成本地查询数据")
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存重新抓取")
    ap.add_argument("--stats", action="store_true", help="打印条数与索引统计")
    ap.add_argument("--character", help="按关联角色筛选")
    ap.add_argument("--equip", help="按关联装备筛选")
    ap.add_argument("--grep", help="关键词搜索名字与效果")
    ap.add_argument("--detail", help="打印单条投资环境完整信息")
    ap.add_argument("--names", action="store_true", help="只列名字（填刷新白名单用）")
    ap.add_argument("--json", action="store_true", help="结果输出 JSON")
    args = ap.parse_args()

    if args.detail:
        show_detail(args.detail)
        return
    if args.stats:
        meta = load_local()["meta"]
        print(f"共 {meta['count']} 条，抓取于 {meta['fetched_at']}")
        print(f"  关联角色: {meta['with_characters']} 条")
        print(f"  关联装备: {meta['with_equipment']} 条")
        for key, label in (("characters_index", "角色"), ("equipment_index", "装备")):
            data = load_local()[key]
            print(f"  {label}键 {len(data)} 个: {'、'.join(list(data)[:12])}{' …' if len(data) > 12 else ''}")
        return
    if args.character or args.equip or args.grep:
        query(args)
        return
    rebuild(args.refresh)


if __name__ == "__main__":
    main()
