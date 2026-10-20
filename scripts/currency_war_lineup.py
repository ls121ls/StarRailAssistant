# -*- coding: utf-8 -*-
"""货币战争米游社 lineup 抓取 + 落地缓存 + 生成 strategies。

设计目标（对应用户诉求）：
- 复用本地保存的请求配置（headers/Cookie），避免每次重新粘 cURL。
- 每个 lineup id 抓到的原始响应落地缓存，下次直接命中缓存、不再请求网络。
- 解析 tourn_detail，按 title 生成 tasks/currency_wars/strategies/{title}.json。

用法：
    python scripts/currency_war_lineup.py <lineup_id> [<lineup_id> ...] [--refresh]
    python scripts/currency_war_lineup.py --from-url "<lineup 页面或接口 URL>"

配置与缓存目录（仓库外，不入库）：%APPDATA%/SRA/cache/miyoushe/
    request_config.json  接口模板 + 请求头(含 Cookie) + fetched_ids
    {id}.json            对应 lineup 的原始响应缓存
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "SRA" / "cache" / "miyoushe"
CONFIG_PATH = CACHE_DIR / "request_config.json"
STRATEGY_DIR = REPO_ROOT / "tasks" / "currency_wars" / "strategies"

# 让脚本能 import 项目内的角色注册表做名称校验。
# 注意：不能用 `from tasks.currency_wars.characters import ...`，因为包的 __init__
# 会连带 import CurrencyWars（依赖 plyer 等运行时库）。这里按文件路径直接加载
# characters.py（它只依赖 dataclasses/enum），绕开包初始化。
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
    print(f"[警告] 角色注册表加载失败，改用本地归一化兜底: {e}")


def _normalize_name(name: str) -> str:
    """与 Characters.get_character 一致的常见字符归一化（• -> · 等）。"""
    return name.replace("•", "·").replace("傑", "杰").replace("姐", "妲")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"[错误] 未找到请求配置: {CONFIG_PATH}\n请先用浏览器 Copy as cURL 生成该文件。")
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def extract_id(arg: str) -> str:
    if arg.startswith("http"):
        m = re.search(r"/detail/\?id=([0-9a-zA-Z]+)", arg) or re.search(r"/lineup/([0-9a-zA-Z]+)", arg)
        if not m:
            sys.exit(f"[错误] 无法从 URL 解析 lineup id: {arg}")
        return m.group(1)
    return arg


def fetch(cfg: dict, lineup_id: str, refresh: bool) -> dict:
    cache_file = CACHE_DIR / f"{lineup_id}.json"
    if cache_file.exists() and not refresh:
        print(f"[缓存命中] {lineup_id} 来自 {cache_file.name}（未请求网络）")
        with open(cache_file, encoding="utf-8-sig") as f:
            return json.load(f)

    url = cfg["endpoint_template"].format(id=lineup_id)
    req = urllib.request.Request(url, headers=cfg["headers"])
    print(f"[抓取] {url}")
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (可信内网域名)
        raw = resp.read().decode("utf-8")  # 直接按 UTF-8 解字节，规避字符集乱码
    data = json.loads(raw)
    if data.get("retcode") != 0:
        sys.exit(f"[接口错误] retcode={data.get('retcode')} msg={data.get('message')}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write(raw)
    ids = cfg.setdefault("fetched_ids", [])
    if lineup_id not in ids:
        ids.append(lineup_id)
    save_config(cfg)
    print(f"[已落地] 响应缓存 -> {cache_file}")
    return data


def sanitize_filename(name: str) -> str:
    # Windows 文件名非法字符 \/:*?"<>| 以及控制字符
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip().strip(".")
    return name[:120]


def build_strategy(data: dict) -> dict:
    lineup = data["data"]["lineup"]
    td = lineup.get("tourn_detail") or {}
    stages = td.get("role_stages") or []
    final = next((s for s in stages if s.get("stage") == "Final"), stages[-1] if stages else {})

    def roles(key: str) -> dict:
        out = {}
        for r in sorted(final.get(key, []) or [], key=lambda x: x.get("board_index", 0)):
            name = _normalize_name(r.get("name", ""))
            if Characters is not None:
                c = Characters.get_character(name)
                if c is not None:
                    name = c.name  # 采用注册表里的规范名
                else:
                    print(f"[警告] 角色未在注册表命中，原样保留: {r.get('name')}")
            out[name] = int(r.get("star", 1))
        return out

    labels = ",".join(x.get("text", "") for x in td.get("labels", []))
    m = re.search(r"(\d+)\s*级", labels)
    min_level = int(m.group(1)) if m else 7

    share = td.get("share_code", "")
    if share and not (share.startswith("##") and share.endswith("##")):
        share = f"##{share}##"

    return {
        "title": lineup.get("title", ""),
        "description": lineup.get("description", ""),
        "author": lineup.get("nickname", ""),
        "uploader": f"米游社lineup({lineup.get('id', '')})",
        "share_code": share,
        "min_coins": 40,
        "min_level": min_level,
        "mid_level": 9,
        "on_field": roles("front_roles"),
        "off_field": roles("back_roles"),
    }


def write_strategy(strategy: dict) -> Path:
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    path = STRATEGY_DIR / f"{sanitize_filename(strategy['title'])}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(strategy, f, ensure_ascii=False, indent=2)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取米游社货币战争 lineup 并生成 strategies")
    ap.add_argument("targets", nargs="*", help="lineup id 或含 id 的 URL")
    ap.add_argument("--from-url", action="append", default=[], help="页面/接口 URL（可多次）")
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存强制重新抓取")
    args = ap.parse_args()

    raw_targets = list(args.targets) + list(args.from_url)
    if not raw_targets:
        ap.print_help()
        sys.exit(0)

    cfg = load_config()
    for t in raw_targets:
        lid = extract_id(t)
        data = fetch(cfg, lid, args.refresh)
        strategy = build_strategy(data)
        path = write_strategy(strategy)
        print(f"[生成] {path.name}  前台={strategy['on_field']}  后台={strategy['off_field']}")


if __name__ == "__main__":
    main()
