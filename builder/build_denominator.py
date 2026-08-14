#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日盘后构建"分母"数据：
    分母 = Min(历史最低价[前复权], 首发价格)

专为在 GitHub Actions（海外 IP）上运行设计，自带多重容错：
  - 只依赖 requests，不依赖 akshare
  - 东财接口带浏览器请求头 + 多镜像域名轮换 + 被断连后自动换新连接重试
  - 个股历史 K 线：东财失败自动切换腾讯行情接口兜底
  - 全市场列表：东财失败时退回仓库里上一份 denominator.json 的名单
  - 断点续跑：同一天内中断后重新执行会跳过已完成的股票
  - 全量模式下有效结果太少会拒绝写出，防止把残缺数据部署上线

用法：
    # 快速试跑（只取前 80 只，几分钟）
    python build_denominator.py --limit 80 --out ../worker/public/denominator.json

    # 全量（约 5400 只，1~2 小时）
    python build_denominator.py --out ../worker/public/denominator.json

    # 调试个股
    python build_denominator.py --codes 301332,600519 --out /tmp/test.json
"""
import argparse
import datetime as dt
import json
import math
import random
import time
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

EM_PUSH2 = ["push2.eastmoney.com", "82.push2.eastmoney.com", "21.push2.eastmoney.com", "45.push2.eastmoney.com"]
EM_HIS = ["push2his.eastmoney.com", "23.push2his.eastmoney.com", "42.push2his.eastmoney.com"]
EM_DATA = ["datacenter-web.eastmoney.com", "datacenter.eastmoney.com"]
TX_HOSTS = ["web.ifzq.gtimg.cn"]
EM_REF = "https://quote.eastmoney.com/"
FS_ALL = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"  # 沪深京全部 A 股


# ---------------------------------------------------------------- 请求层
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


_BOX = {"s": None}


def _session() -> requests.Session:
    if _BOX["s"] is None:
        _BOX["s"] = new_session()
    return _BOX["s"]


def _reset_session() -> None:
    try:
        if _BOX["s"] is not None:
            _BOX["s"].close()
    except Exception:
        pass
    _BOX["s"] = new_session()


def get_json(hosts, path, params, referer, tries=4, timeout=15):
    """带镜像轮换与重试的 GET → JSON。被远端断连时换全新连接再试。"""
    last = None
    for attempt in range(tries):
        host = hosts[attempt % len(hosts)]
        try:
            r = _session().get(f"https://{host}{path}", params=params,
                               timeout=timeout, headers={"Referer": referer})
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"HTTP {r.status_code} from {host}")
        except Exception as e:
            last = e
            _reset_session()
        time.sleep(0.6 * (attempt + 1) + random.random() * 0.6)
    raise last


# ---------------------------------------------------------------- 代码规则
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


def em_secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def tx_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


# ---------------------------------------------------------------- 全市场列表
def fetch_stock_list():
    print("[1/3] 拉取全市场股票列表（东财）...")
    params = {"pn": 1, "pz": 100, "po": 1, "np": 1,
              "ut": "bd1d9ddb04089700cf9c27f6f7426281",
              "fltt": 2, "invt": 2, "fid": "f12", "fs": FS_ALL, "fields": "f12,f14"}
    first = get_json(EM_PUSH2, "/api/qt/clist/get", params, EM_REF)
    data = (first or {}).get("data") or {}
    diff, total = data.get("diff") or [], data.get("total", 0)
    if not diff:
        raise RuntimeError("列表接口返回为空")
    out = [(str(d["f12"]).zfill(6), d["f14"]) for d in diff]
    pages = math.ceil(total / len(diff))
    for pn in range(2, pages + 1):
        params["pn"] = pn
        j = get_json(EM_PUSH2, "/api/qt/clist/get", params, EM_REF)
        out += [(str(d["f12"]).zfill(6), d["f14"]) for d in ((j or {}).get("data") or {}).get("diff") or []]
        time.sleep(0.3 + random.random() * 0.5)
    seen, uniq = set(), []
    for c, n in out:
        if c not in seen:
            seen.add(c)
            uniq.append((c, n))
    print(f"      共 {len(uniq)} 只")
    return uniq


def fetch_stock_list_with_fallback(prev_json_path: str):
    try:
        return fetch_stock_list()
    except Exception as e:
        print(f"      !! 东财列表接口失败：{e}")
    p = Path(prev_json_path)
    if p.exists():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
            if not prev.get("sample") and prev.get("items"):
                lst = [(c, v["n"]) for c, v in prev["items"].items()]
                print(f"      退回上一份数据的名单：{len(lst)} 只（当天新上市的会缺席，列表接口恢复后自动补上）")
                return lst
        except Exception:
            pass
    raise SystemExit("股票列表获取失败，且没有可用的历史名单。"
                     "建议：到 Actions 页 Re-run 一次（换个 runner IP），或稍后再试。")


# ---------------------------------------------------------------- 首发价格
def fetch_ipo_prices() -> dict:
    """东财新股数据库（覆盖 2010 年以后申购的新股；更老的股票缺失时分母只用历史最低价）。"""
    print("[2/3] 拉取首发价格（东财新股数据库）...")
    params = {"sortColumns": "APPLY_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
              "pageSize": 5000, "pageNumber": 1,
              "reportName": "RPTA_APP_IPOAPPLY",
              "columns": "SECURITY_CODE,ISSUE_PRICE",
              "filter": "(APPLY_DATE>'2010-01-01')",
              "source": "WEB", "client": "WEB"}
    result = {}

    def eat(j):
        for rec in ((j or {}).get("result") or {}).get("data") or []:
            code = str(rec.get("SECURITY_CODE", "")).zfill(6)
            try:
                price = float(rec.get("ISSUE_PRICE"))
            except (TypeError, ValueError):
                continue
            if code and price > 0:
                result[code] = round(price, 4)

    try:
        first = get_json(EM_DATA, "/api/data/v1/get", params, "https://data.eastmoney.com/")
        pages = int(((first or {}).get("result") or {}).get("pages") or 0)
        eat(first)
        for pn in range(2, pages + 1):
            params["pageNumber"] = pn
            eat(get_json(EM_DATA, "/api/data/v1/get", params, "https://data.eastmoney.com/"))
            time.sleep(0.2 + random.random() * 0.3)
    except Exception as e:
        print(f"      !! 首发价格接口失败（{e}），本次按缺失处理：分母只用历史最低价")
        return {}
    print(f"      拿到 {len(result)} 条首发价格")
    return result


# ---------------------------------------------------------------- 历史最低价
def em_hist_low(code: str):
    """东财 K 线（前复权），一次请求返回全部历史，只取日期+最低价两个字段。"""
    params = {"secid": em_secid(code), "klt": 101, "fqt": 1,
              "beg": "19900101", "end": "20500101", "lmt": 1000000,
              "ut": "7eea3edcaed734bea9cbfc24409ed989",
              "fields1": "f1,f2,f3", "fields2": "f51,f55"}
    j = get_json(EM_HIS, "/api/qt/stock/kline/get", params, EM_REF, tries=3)
    klines = ((j or {}).get("data") or {}).get("klines")
    if not klines:
        return None
    low = min(float(s.split(",")[1]) for s in klines if "," in s)
    return round(low, 4)


def tx_hist_low(code: str):
    """腾讯行情兜底（前复权日K，单次最多 640 根，向前翻页拿全历史）。"""
    sym = tx_symbol(code)
    lows, end = [], ""
    for _ in range(40):  # 40 × 640 根，远超任何 A 股的历史长度
        j = get_json(TX_HOSTS, "/appstock/app/fqkline/get",
                     {"param": f"{sym},day,,{end},640,qfq"}, "https://gu.qq.com/", tries=3)
        node = ((j or {}).get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        if not arr:
            break
        lows += [float(bar[4]) for bar in arr if len(bar) > 4]
        if len(arr) < 640:
            break
        first_day = dt.date.fromisoformat(arr[0][0])
        end = (first_day - dt.timedelta(days=1)).isoformat()
        time.sleep(0.15)
    return round(min(lows), 4) if lows else None


def fetch_hist_low(code: str, stats: dict):
    try:
        v = em_hist_low(code)
        if v is not None:
            stats["em"] += 1
            return v
    except Exception:
        pass
    try:
        v = tx_hist_low(code)
        if v is not None:
            stats["tx"] += 1
            return v
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 断点缓存
class Checkpoint:
    """按日期命名的 jsonl 缓存：同一天内中断后重跑可以跳过已完成的股票。只缓存成功结果。"""

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
    parser.add_argument("--min-count", type=int, default=4000,
                        help="全量模式下有效结果低于该数则报错退出（防止部署残缺数据）")
    args = parser.parse_args()

    full_run = not args.codes and args.limit == 0

    stocks = fetch_stock_list_with_fallback(args.out)
    if args.codes:
        wanted = {c.strip().zfill(6) for c in args.codes.split(",") if c.strip()}
        stocks = [(c, n) for c, n in stocks if c in wanted]
    elif args.limit > 0:
        stocks = stocks[: args.limit]

    ipo_map = fetch_ipo_prices()
    ckpt = Checkpoint(Path(__file__).resolve().parent / args.cache)
    stats = {"em": 0, "tx": 0}

    print(f"[3/3] 逐只拉取前复权历史最低价，共 {len(stocks)} 只 ...")
    items, failed = {}, []
    t0 = time.time()

    for i, (code, name) in enumerate(stocks):
        if code in ckpt.done:
            low = ckpt.done[code]
        else:
            low = fetch_hist_low(code, stats)
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

    if full_run and len(items) < args.min_count:
        raise SystemExit(f"有效结果只有 {len(items)} 只（< {args.min_count}），疑似接口大面积失败，"
                         "拒绝写出以免部署残缺数据。可到 Actions 页 Re-run 重试。")

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
    print(f"数据源使用：东财 {stats['em']} 次，腾讯兜底 {stats['tx']} 次")
    print(f"失败 {len(failed)} 只（停牌新股/退市/接口异常，前端会自动忽略）: "
          f"{failed[:20]}{' ...' if len(failed) > 20 else ''}")


if __name__ == "__main__":
    main()
