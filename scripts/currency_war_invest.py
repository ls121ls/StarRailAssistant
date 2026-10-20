# -*- coding: utf-8 -*-
"""货币战争「投资策略一览」BWIKI 落地 + 解析 + 本地查询。

数据源：https://wiki.biligame.com/sr/投资策略一览
页面就是一张 4 列表格（名字 / 稀有度 / 内容 / 出现位面），每行形如：
    |-class="divsort" data-param1="3" data-param2="1,2"
    | {{图标/投资策略|银色|特战资金|放缩=50%}}<p>特战资金</p> || <div class="hide">3</div>银色
      || 进入首领节点时，获得7枚金币。 || 1,2
  data-param1 = 稀有度码（1=棱彩、2=金色、3=银色，取自页面筛选区）
  data-param2 = 出现位面（1/2/3，逗号分隔）
因此 **1 次请求拿全量**，不逐条翻页；原始 wikitext 落地缓存，默认复用不联网。

用法：
    python scripts/currency_war_invest.py                    # 抓取或复用缓存，生成数据文件
    python scripts/currency_war_invest.py --refresh           # 忽略缓存重新抓取
    python scripts/currency_war_invest.py --stats             # 稀有度/位面统计
    python scripts/currency_war_invest.py --rarity 棱彩 --plane 1
    python scripts/currency_war_invest.py --grep 利息         # 按关键词搜名字/内容
    python scripts/currency_war_invest.py --detail 孪生素数    # 打印单条完整内容
    python scripts/currency_war_invest.py --names --plane 3    # 只要名字列表（填刷新白名单用）

查询类命令只读本地 JSON，不碰网络。
缓存目录（仓库外，不入库）：%APPDATA%/SRA/cache/bwiki/invest_strategy.wikitext
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
CACHE_FILE = CACHE_DIR / "invest_strategy.wikitext"
DATA_DIR = REPO_ROOT / "tasks" / "currency_wars" / "data"
JSON_PATH = DATA_DIR / "currency_war_investments.json"
MD_PATH = DATA_DIR / "currency_war_investments.md"

API = "https://wiki.biligame.com/sr/api.php"
PAGE = "投资策略一览"
SOURCE_URL = f"https://wiki.biligame.com/sr/{urllib.parse.quote(PAGE)}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SRA-wiki-fetch/1.0"

# 页面筛选区 {{筛选项|1|<码>|<稀有度>}} 决定的稀有度编码
RARITY_BY_CODE = {"1": "棱彩", "2": "金色", "3": "银色"}
RARITY_ORDER = ["棱彩", "金色", "银色"]


class FetchFailed(RuntimeError):
    """重试耗尽（限流/网络异常），由调用方给出各自的缓存提示。"""


# --------------------------------------------------------------------------- 抓取

def api_get(params: dict, retries: int = 3) -> dict:
    """调用 MediaWiki API；限流（HTTP 567）/网络异常时按退避重试。"""
    query = urllib.parse.urlencode(params)
    last_err = ""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
            # wiki 对连续请求会返回 HTTP 567 限流，按退避重试
            last_err = str(e)
            if attempt < retries:
                wait = 8 * attempt
                print(f"[重试 {attempt}/{retries}] {last_err} -> {wait}s 后重试")
                time.sleep(wait)
    raise FetchFailed(last_err)


def fetch_wikitext(refresh: bool) -> str:
    """落地页面 wikitext；已有缓存直接复用（1 次请求拿全量策略）。"""
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
    if "error" in payload:
        raise SystemExit(f"[错误] 页面 {PAGE} 不存在: {payload['error'].get('info')}")

    text = payload["parse"]["wikitext"]
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(text, encoding="utf-8")
    print(f"[已落地] {len(text)} 字符 wikitext -> {CACHE_FILE}")
    return text


# ------------------------------------------------------------------------- 解析

def _resolve_template(body: str) -> str:
    """把一个模板体（不含外层花括号）解析成展示文字。

    规则：有 text= 参数用 text；图标类模板用第一个无名参数；
    其余（如 {{颜色|1费|2费角色}}）用最后一个无名参数（即真正显示的文字）。
    """
    parts = body.split("|")
    name = parts[0].strip()
    unnamed: list[str] = []
    named: dict[str, str] = {}
    for p in parts[1:]:
        key, sep, val = p.partition("=")
        # 只把 ASCII 标识符当作参数名，避免把“…+2星=3星…”这类正文里的 = 误判
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ ]*", key.strip()):
            named[key.strip()] = val.strip()
        else:
            unnamed.append(p)
    if "text" in named:
        return named["text"]
    if name.startswith("图标") and unnamed:
        return unnamed[0]
    return unnamed[-1] if unnamed else ""


def clean_markup(value: str) -> str:
    """剥掉 wikitext 模板/链接/标签，保留可读文本（支持模板嵌套）。"""
    text = value.strip()
    text = re.sub(r"<div class=\"hide\">\d+</div>", "", text)
    # 由内向外逐层展开模板：{{…{{…}}…}} 先算最内层
    for _ in range(12):
        new = re.sub(r"\{\{([^{}]*)\}\}", lambda m: _resolve_template(m.group(1)), text)
        if new == text:
            break
        text = new
    text = re.sub(r"\[\[[^\]]*文件:[^\]]*\]\]", "", text, flags=re.I)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_rarity_codes(text: str) -> dict[str, str]:
    """从筛选区 {{筛选项|1|<码>|<稀有度>}} 解析 {码: 稀有度}。"""
    pairs = re.findall(r"\{\{筛选项\|1\|(\d+)\|([^|}]+)", text)
    return {code: name.strip() for code, name in pairs} or RARITY_BY_CODE


def parse_rows(text: str, rarity_codes: dict[str, str]) -> list[dict]:
    """解析表格行 -> 投资策略记录列表。"""
    records: list[dict] = []
    for m in re.finditer(r'(?m)^\|-class="divsort"([^\n]*)\n(.*?)(?=^\|-|^\|\}|\Z)', text, flags=re.S):
        attrs, body = m.group(1), m.group(2)
        cells = [c.strip() for c in re.split(r"\|\|", body)]
        if len(cells) != 4:
            print(f"[警告] 列数不是 4，跳过：{cells[0][:40] if cells else body[:40]}")
            continue
        code = (re.search(r'data-param1="([^"]*)"', attrs) or [None, ""])[1]
        # 名字优先取 <p>…</p>（模板渲染出的显示名），退化到图标模板的第 2 参数
        name_m = re.search(r"<p>(.*?)</p>", cells[0], flags=re.S) or re.search(
            r"\{\{图标/投资策略\|[^|}]*\|([^|}]+)", cells[0])
        name = clean_markup(name_m.group(1)) if name_m else ""
        rarity = clean_markup(cells[1]) or rarity_codes.get(code, "")
        planes_m = re.search(r'data-param2="([^"]*)"', attrs)
        raw_planes = (planes_m.group(1) if planes_m else cells[3]).replace("%2C", ",")
        planes = [int(x) for x in re.findall(r"\d+", raw_planes)] or [
            int(x) for x in re.findall(r"\d+", cells[3])]
        records.append({
            "name": name,
            "rarity": rarity,
            "content": clean_markup(cells[2]),
            "planes": sorted(set(planes)),
        })
    return records


def build_index(records: list[dict]) -> dict:
    """位面 -> 稀有度 -> 名字列表，方便按位面挑刷新白名单。"""
    index: dict[str, dict[str, list[str]]] = {}
    for r in records:
        for p in r["planes"]:
            bucket = index.setdefault(str(p), {k: [] for k in RARITY_ORDER})
            bucket.setdefault(r["rarity"], []).append(r["name"])
    for bucket in index.values():
        for names in bucket.values():
            names.sort()
    return index


# --------------------------------------------------------------------- Markdown

def render_md(data: dict) -> str:
    meta, recs = data["meta"], data["records"]
    lines = [
        "# 货币战争投资策略一览（BWIKI）",
        "",
        f"- 来源：<{meta['source']}>",
        f"- 抓取时间：{meta['fetched_at']}｜策略数：{meta['count']}",
        f"- 完整数据：`{JSON_PATH.relative_to(REPO_ROOT).as_posix()}`",
        f"- 稀有度分布：{'、'.join(f'{k} {v}' for k, v in meta['rarity_counts'].items())}",
        f"- 出现位面分布：{'、'.join(f'位面{k} {v}条' for k, v in meta['plane_counts'].items())}",
        "",
        "## 按稀有度分组",
        "",
    ]
    for rarity in RARITY_ORDER:
        subset = [r for r in recs if r["rarity"] == rarity]
        if not subset:
            continue
        lines += [f"### {rarity}（{len(subset)} 条）", "",
                  "| 名字 | 出现位面 | 内容 |", "| --- | --- | --- |"]
        for r in sorted(subset, key=lambda x: (x["planes"], x["name"])):
            content = r["content"].replace("|", "\\|")
            lines.append(f"| {r['name']} | {'、'.join(map(str, r['planes']))} | {content} |")
        lines.append("")

    lines += ["## 位面索引（只要名字）", ""]
    for plane, bucket in sorted(data["index"].items(), key=lambda x: int(x[0])):
        lines.append(f"### 位面 {plane}")
        lines.append("")
        for rarity in RARITY_ORDER:
            names = bucket.get(rarity) or []
            if names:
                lines.append(f"- **{rarity}**（{len(names)}）：{'、'.join(names)}")
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------------- 本地

def rebuild(refresh: bool) -> dict:
    text = fetch_wikitext(refresh)
    codes = parse_rarity_codes(text)
    records = parse_rows(text, codes)
    if not records:
        raise SystemExit(f"[错误] 没解析到任何策略行，页面结构可能变了: {CACHE_FILE}")
    names = [r["name"] for r in records]
    if len(set(names)) != len(names):
        dup = sorted({n for n in names if names.count(n) > 1})
        print(f"[提示] 存在重名策略（{len(dup)} 个）: {'、'.join(dup[:8])}")
    data = {
        "meta": {
            "source": SOURCE_URL,
            "page": PAGE,
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "count": len(records),
            "rarity_counts": {k: sum(1 for r in records if r["rarity"] == k) for k in RARITY_ORDER},
            "plane_counts": {str(p): sum(1 for r in records if p in r["planes"]) for p in (1, 2, 3)},
        },
        "index": build_index(records),
        "records": sorted(records, key=lambda r: (RARITY_ORDER.index(r["rarity"])
                                                  if r["rarity"] in RARITY_ORDER else 9,
                                                  r["planes"], r["name"])),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MD_PATH.write_text(render_md(data), encoding="utf-8")
    print(f"[生成] {JSON_PATH.relative_to(REPO_ROOT)}  ({data['meta']['count']} 条策略)")
    print(f"[生成] {MD_PATH.relative_to(REPO_ROOT)}")
    print("[统计] " + "、".join(f"{k}{v}" for k, v in data["meta"]["rarity_counts"].items())
          + " | " + "、".join(f"位面{k}:{v}条" for k, v in data["meta"]["plane_counts"].items()))
    return data


def load_local() -> dict:
    if not JSON_PATH.exists():
        sys.exit(f"[错误] 本地数据不存在: {JSON_PATH}\n请先运行: python scripts/currency_war_invest.py")
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def query(args: argparse.Namespace) -> None:
    records = load_local()["records"]

    def ok(r: dict) -> bool:
        if args.rarity and r["rarity"] != args.rarity:
            return False
        if args.plane and args.plane not in r["planes"]:
            return False
        if args.grep and args.grep not in (r["name"] + r["content"]):
            return False
        return True

    rows = [r for r in records if ok(r)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    cond = " / ".join(x for x in [args.rarity or "稀有度不限",
                                  f"位面{args.plane}" if args.plane else "位面不限",
                                  f"含“{args.grep}”" if args.grep else ""] if x)
    print(f"命中 {len(rows)} 条（{cond}）")
    for r in rows:
        if args.names:
            print(f"  [{r['rarity']}|{'、'.join(map(str, r['planes']))}] {r['name']}")
        else:
            print(f"  [{r['rarity']}|{'、'.join(map(str, r['planes']))}] {r['name']}：{r['content']}")


def show_detail(name: str) -> None:
    for r in load_local()["records"]:
        if r["name"] == name or name in r["name"]:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return
    sys.exit(f"[错误] 没有投资策略: {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="落地 BWIKI 货币战争投资策略一览并生成本地查询数据")
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存重新抓取")
    ap.add_argument("--stats", action="store_true", help="打印稀有度/位面统计")
    ap.add_argument("--rarity", choices=RARITY_ORDER, help="按稀有度筛选")
    ap.add_argument("--plane", type=int, choices=[1, 2, 3], help="按出现位面筛选")
    ap.add_argument("--grep", help="关键词搜索名字与内容")
    ap.add_argument("--detail", help="打印单条策略完整信息")
    ap.add_argument("--names", action="store_true", help="只列名字（配合 --plane/--rarity 填刷新白名单）")
    ap.add_argument("--json", action="store_true", help="结果输出 JSON")
    args = ap.parse_args()

    if args.detail:
        show_detail(args.detail)
        return
    if args.stats:
        meta = load_local()["meta"]
        print(f"共 {meta['count']} 条，抓取于 {meta['fetched_at']}")
        for k, v in meta["rarity_counts"].items():
            print(f"  {k}: {v}")
        for k, v in meta["plane_counts"].items():
            print(f"  位面{k}: {v}")
        return
    if args.rarity or args.plane or args.grep:
        query(args)
        return
    rebuild(args.refresh)


if __name__ == "__main__":
    main()
