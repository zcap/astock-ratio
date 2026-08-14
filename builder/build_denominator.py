#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日盘后构建"分母"数据：
    分母 = Min(历史最低价[前复权], 首发价格)

输出一份 denominator.json 供 Cloudflare Workers 上的前端页面使用。
前端只需要用 实时最新价 / 分母 即可得到排序指标。

用法：
    # 快速试跑（只取前 80 只，几分钟）
    python build_denominator.py --limit 80 --out ../worker/public/denominator.json

    # 全量（约 5400 只，1~2 小时，支持断点续跑：中断后重新执行即可）
    python build_denominator.py --out ../worker/public/denominator.json

    # 调试个股
    python build_denominator.py --codes 301332,600519 --out /tmp/test.json
"""
import argparse
import datetime as dt
import json
import time
from pathlib import Path

import akshare as ak
import pandas as pd


# ---------------------------------------------------------------- 板块判定
def board_of(code: str) -> str:
    """根据代码前缀判定所属板块（不需要额外接口）。"""
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    if code.startswith(("600", "601", "603", "605")):
        return "上证主板"
    if code.startswith(("000", "001", "002", "003")):
        return "深证主板"
    if code.startswith(("920", "43", "83", "87", "88")):
        return "北交所"
    return "其他"


# ---------------------------------------------------------------- 数据获取
def fetch_stock_list() -> pd.DataFrame:
    """全市场股票列表（沪深京 A 股），来自东财实时快照接口。"""
    print("[1/3] 拉取全市场股票列表 ...")
    spot = ak.stock_zh_a_spot_em()
    df = spot[["代码", "名称"]].copy()
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    df = df.drop_duplicates(subset="代码").reset_index(drop=True)
    print(f"      共 {len(df)} 只")
    return df


def fetch_ipo_prices() -> dict:
    """批量拉取首发价格（东财新股数据库，老股票可能缺失 -> 缺失时分母只用历史最低价）。"""
    print("[2/3] 拉取首发价格（新股数据库，一次性批量）...")
    try:
        df = ak.stock_xgsglb_em(symbol="全部股票")
    except Exception as e:
        print(f"      !! 首发价格接口失败，本次全部按缺失处理: {e}")
        return {}
    result = {}
    for _, row in df.iterrows():
        code = str(row.get("股票代码", "")).zfill(6)
        try:
            price = float(row.get("发行价格"))
        except (TypeError, ValueError):
            continue
        if price and price > 0:
            result[code] = round(price, 4)
    print(f"      拿到 {len(result)} 条首发价格")
    return result


def fetch_hist_low(code: str, retries: int = 3):
    """单只股票的前复权历史最低价。失败重试，最终失败返回 None。"""
    for attempt in range(retries):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
            if df is None or df.empty:
                return None
            low = pd.to_numeric(df["最低"], errors="coerce").min()
            if pd.isna(low):
                return None
            return round(float(low), 4)
        except Exception:
            time.sleep(min(2 ** attempt * 2, 20))
    return None


# ---------------------------------------------------------------- 断点缓存
class Checkpoint:
    """
    按日期命名的 jsonl 缓存：同一天内中断后重跑可以跳过已完成的股票。
    只缓存成功结果；失败的下次重跑会再试。
    """

    def __init__(self, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        today = dt.date.today().strftime("%Y%m%d")
        self.path = cache_dir / f"lows-{today}.jsonl"
        self.done = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    self.done[rec["c"]] = rec["l"]
                except (json.JSONDecodeError, KeyError):
                    continue
            print(f"      发现断点缓存 {self.path.name}，已完成 {len(self.done)} 只")
        self._fh = self.path.open("a", encoding="utf-8")

    def save(self, code: str, low: float) -> None:
        self.done[code] = low
        self._fh.write(json.dumps({"c": code, "l": low}, ensure_ascii=False) + "\n")
        self._fh.flush()


# ---------------------------------------------------------------- 主流程
def main() -> None:
    parser = argparse.ArgumentParser(description="构建 分母 = Min(前复权历史最低, 首发价) 数据")
    parser.add_argument("--out", default="../worker/public/denominator.json", help="输出 JSON 路径")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试跑用），0 = 全量")
    parser.add_argument("--codes", default="", help="只处理指定代码，逗号分隔（调试用）")
    parser.add_argument("--sleep", type=float, default=0.35, help="每只股票之间的间隔秒数（防限流）")
    parser.add_argument("--cache", default=".cache", help="断点缓存目录")
    args = parser.parse_args()

    stocks = fetch_stock_list()
    if args.codes:
        wanted = {c.strip().zfill(6) for c in args.codes.split(",") if c.strip()}
        stocks = stocks[stocks["代码"].isin(wanted)].reset_index(drop=True)
    elif args.limit > 0:
        stocks = stocks.head(args.limit)

    ipo_map = fetch_ipo_prices()
    ckpt = Checkpoint(Path(__file__).resolve().parent / args.cache)

    print(f"[3/3] 逐只拉取前复权历史最低价，共 {len(stocks)} 只 ...")
    items = {}
    failed = []
    t0 = time.time()

    for i, row in stocks.iterrows():
        code, name = row["代码"], row["名称"]

        if code in ckpt.done:
            low = ckpt.done[code]
        else:
            low = fetch_hist_low(code)
            if low is not None:
                ckpt.save(code, low)
            time.sleep(args.sleep)

        if low is None or low <= 0:
            failed.append(code)
        else:
            ipo = ipo_map.get(code)
            den = min(low, ipo) if ipo else low
            items[code] = {"n": name, "b": board_of(code), "l": low, "i": ipo, "d": round(den, 4)}

        n = i + 1
        if n % 100 == 0 or n == len(stocks):
            speed = n / max(time.time() - t0, 1)
            eta = (len(stocks) - n) / max(speed, 0.01)
            print(f"      {n}/{len(stocks)}  成功 {len(items)}  失败 {len(failed)}  预计剩余 {eta/60:.0f} 分钟")

    payload = {
        "build_date": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "count": len(items),
        "failed": failed,
        "items": items,
    }
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"\n完成：{len(items)} 只写入 {out_path}")
    print(f"失败 {len(failed)} 只（停牌新股/退市/接口异常，前端会自动忽略）: {failed[:20]}{' ...' if len(failed) > 20 else ''}")


if __name__ == "__main__":
    main()
