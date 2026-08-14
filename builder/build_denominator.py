#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日盘后构建"分母"数据：
    分母 = Min(历史最低价[前复权·等比], 首发价格)

口径说明：
  前复权有两个流派——减法复权（同花顺/通达信/腾讯，高分红老股会出现负价格）
  和等比复权（东方财富/Baostock，永远为正）。"最新价÷历史最低"只有在等比口径
  下才有"距底部多少倍"的含义。历史最低用周K线取 min，与日K线取 min 数学等价
  （每根周线的 low 就是那一周的最低价），数据量降 5 倍。

数据链路（专为 GitHub Actions 海外 IP 设计）：
  股票名单：东财直连 → 东财·经 Worker 代理 → 腾讯（沪深京）→ 新浪 → 上一份名单
  历史K线：东财直连 → 东财·经 Worker 代理 → Baostock（等比，沪深，T+1）
           → 腾讯（减法口径近似，含非正值则剔除）
  首发价格：东财数据中心；失败则本次缺失
  口径优先级：东财新鲜 > 等比滞后顶替(≤10日) > Baostock > 腾讯近似 > 剔除

性能与稳健：
  - 多进程分片并行（--workers，默认 4），每个进程独立连接与熔断器
  - Baostock 断线自动重连、每 250 次查询主动换连接、库刷屏输出静音
  - 东财半开熔断：熔断后定期探测，抓住间歇性放行的时间窗
  - 断点续跑（同日重跑跳过已完成）、收尾二次重试、结果过少拒绝写出

用法：
    python build_denominator.py --limit 80 --out ../worker/public/denominator.json
    python build_denominator.py --out ../worker/public/denominator.json --workers 4
"""
import argparse
import contextlib
import datetime as dt
import io
import json
import math
import multiprocessing as mp
import os
import random
import re
import socket
import time
from pathlib import Path

import requests

# baostock 的 socket 不带超时，服务器抽风时会永久阻塞——全局兜底
socket.setdefaulttimeout(25)

# 所有打印强制 flush，进度实时可见（与 PYTHONUNBUFFERED 双保险）
_print = print
def print(*args, **kwargs):  # noqa: A001
    kwargs.setdefault("flush", True)
    _print(*args, **kwargs)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

EM_PUSH2 = ["push2.eastmoney.com", "82.push2.eastmoney.com", "21.push2.eastmoney.com"]
EM_HIS = ["push2his.eastmoney.com", "23.push2his.eastmoney.com", "42.push2his.eastmoney.com",
          "5.push2his.eastmoney.com", "64.push2his.eastmoney.com", "91.push2his.eastmoney.com"]
EM_DATA = ["datacenter-web.eastmoney.com", "datacenter.eastmoney.com"]
TX_QQ = ["proxy.finance.qq.com"]
TX_IFZQ = ["web.ifzq.gtimg.cn"]
SINA = ["vip.stock.finance.sina.com.cn"]
EM_REF = "https://quote.eastmoney.com/"
FS_ALL = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"  # 沪深京全部 A 股

EM_PROXY = (os.environ.get("EM_PROXY") or "").strip().rstrip("/")

PROBE_EVERY = 25      # 熔断后每隔多少只股票探测一次
BREAK_AFTER = 5       # 连续失败多少次触发熔断
STALE_MAX = 10        # 等比口径历史数据最多沿用多少个交易日
BS_RELOGIN_EVERY = 250


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


def _get(hosts, path, params, referer, tries, timeout, encoding=None):
    last = None
    for attempt in range(tries):
        host = hosts[attempt % len(hosts)]
        try:
            r = _session().get(f"https://{host}{path}", params=params,
                               timeout=timeout, headers={"Referer": referer})
            if r.status_code == 200:
                if encoding:
                    r.encoding = encoding
                return r
            last = RuntimeError(f"HTTP {r.status_code} from {host}")
        except Exception as e:
            last = e
            _reset_session()
        time.sleep(0.5 * (attempt + 1) + random.random() * 0.5)
    raise last


def get_json(hosts, path, params, referer, tries=3, timeout=15):
    return _get(hosts, path, params, referer, tries, timeout).json()


def get_text(hosts, path, params, referer, tries=3, timeout=15, encoding=None):
    return _get(hosts, path, params, referer, tries, timeout, encoding).text


def proxy_json(path, params, tries=2, timeout=25):
    if not EM_PROXY:
        raise RuntimeError("未配置 EM_PROXY")
    host = EM_PROXY.replace("https://", "").replace("http://", "")
    return get_json([host], path, params, EM_PROXY + "/", tries=tries, timeout=timeout)


# ---------------------------------------------------------------- 代码规则
def board_of(code: str) -> str:
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


def _dedup(pairs):
    seen, uniq = set(), []
    for c, n in pairs:
        if c and c not in seen:
            seen.add(c)
            uniq.append((c, n))
    return uniq


def _min_positive(vals):
    pos = [v for v in vals if v > 0]
    return round(min(pos), 4) if pos else None


# ---------------------------------------------------------------- 全市场列表（多源）
def _parse_clist(j):
    return [(str(d["f12"]).zfill(6), d["f14"]) for d in ((j or {}).get("data") or {}).get("diff") or []]


def list_em():
    params = {"pn": 1, "pz": 100, "po": 1, "np": 1,
              "ut": "bd1d9ddb04089700cf9c27f6f7426281",
              "fltt": 2, "invt": 2, "fid": "f12", "fs": FS_ALL, "fields": "f12,f14"}
    first = get_json(EM_PUSH2, "/api/qt/clist/get", params, EM_REF)
    data = (first or {}).get("data") or {}
    total, out = data.get("total", 0), _parse_clist(first)
    if not out:
        raise RuntimeError("东财列表返回为空")
    for pn in range(2, math.ceil(total / len(out)) + 1):
        params["pn"] = pn
        out += _parse_clist(get_json(EM_PUSH2, "/api/qt/clist/get", params, EM_REF))
        time.sleep(0.3 + random.random() * 0.4)
    return _dedup(out)


def list_em_proxy():
    first = proxy_json("/api/quotes", {"pn": 1, "pz": 100})
    data = (first or {}).get("data") or {}
    total, out = data.get("total", 0), _parse_clist(first)
    if not out:
        raise RuntimeError("代理列表返回为空")
    per = len(out)
    for pn in range(2, math.ceil(total / per) + 1):
        out += _parse_clist(proxy_json("/api/quotes", {"pn": pn, "pz": per}))
        time.sleep(0.15 + random.random() * 0.2)
    return _dedup(out)


def list_tx():
    params = {"_appver": "11.17.0", "board_code": "aStock",
              "sort_type": "price", "direct": "down", "offset": 0, "count": 200}
    out = []

    def eat(d):
        for rec in (d or {}).get("rank_list") or []:
            code = re.sub(r"^(sh|sz|bj)", "", str(rec.get("code", "")))
            if code.isdigit() and len(code) == 6:
                out.append((code, str(rec.get("name", ""))))

    j = get_json(TX_QQ, "/cgi/cgi-bin/rank/hs/getBoardRankList", params, "https://gu.qq.com/", timeout=30)
    data = (j or {}).get("data") or {}
    total = int(data.get("total") or 0)
    eat(data)
    for pg in range(1, math.ceil(total / 200)):
        params["offset"] = pg * 200
        j = get_json(TX_QQ, "/cgi/cgi-bin/rank/hs/getBoardRankList", params, "https://gu.qq.com/", timeout=30)
        eat((j or {}).get("data") or {})
        time.sleep(0.2 + random.random() * 0.3)
    uniq = _dedup(out)
    if len(uniq) < 1000:
        raise RuntimeError(f"腾讯列表只返回 {len(uniq)} 只，疑似异常")
    return uniq


def list_sina():
    out = []
    for page in range(1, 120):
        params = {"page": page, "num": 80, "sort": "symbol", "asc": 1,
                  "node": "hs_a", "symbol": "", "_s_r_a": "page"}
        text = get_text(SINA, "/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                        params, "https://vip.stock.finance.sina.com.cn/mkt/", encoding="gbk").strip()
        if not text or text in ("null", "[]"):
            break
        arr = json.loads(re.sub(r'([{,])\s*([A-Za-z_]\w*)\s*:', r'\1"\2":', text))
        if not arr:
            break
        out += [(str(rec.get("code", "")).zfill(6), str(rec.get("name", ""))) for rec in arr]
        if len(arr) < 80:
            break
        time.sleep(0.3 + random.random() * 0.4)
    uniq = _dedup(out)
    if len(uniq) < 1000:
        raise RuntimeError(f"新浪列表只返回 {len(uniq)} 只，疑似异常")
    return uniq


def fetch_stock_list(prev_json_path: str):
    print("[1/3] 拉取全市场股票列表 ...")
    sources = [("东财", list_em)]
    if EM_PROXY:
        sources.append(("东财(代理)", list_em_proxy))
    sources += [("腾讯", list_tx), ("新浪", list_sina)]
    for name, fn in sources:
        try:
            lst = fn()
            print(f"      使用 {name} 列表：共 {len(lst)} 只")
            return lst, name
        except Exception as e:
            print(f"      !! {name} 列表失败：{e}")
    p = Path(prev_json_path)
    if p.exists():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
            if not prev.get("sample") and prev.get("items"):
                lst = [(c, v["n"]) for c, v in prev["items"].items()]
                print(f"      退回上一份数据的名单：{len(lst)} 只（当天新上市的会缺席）")
                return lst, "历史名单"
        except Exception:
            pass
    raise SystemExit("所有列表来源都失败了。建议：到 Actions 页 Re-run 一次（换 runner IP），或稍后再试。")


# ---------------------------------------------------------------- 首发价格
def fetch_ipo_prices() -> dict:
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
        print(f"      拿到 {len(result)} 条首发价格")
    except Exception as e:
        print(f"      !! 首发价格接口失败（{e}），本次按缺失处理：分母只用历史最低价")
    return result


# ---------------------------------------------------------------- 半开熔断
class Breaker:
    def __init__(self, name: str, threshold: int = None, probe_every: int = None):
        self.name = name
        self.threshold = threshold or BREAK_AFTER
        self.probe_every = probe_every or PROBE_EVERY
        self.fails, self.open, self.calls = 0, False, 0

    def should_try(self) -> bool:
        self.calls += 1
        if not self.open:
            return True
        return self.calls % self.probe_every == 0

    def ok(self):
        self.fails = 0
        if self.open:
            self.open = False
            print(f"      >> {self.name} 探测成功，通道恢复")

    def fail(self):
        self.fails += 1
        if not self.open and self.fails >= self.threshold:
            self.open = True
            print(f"      !! {self.name} 连续失败，熔断（此后每 {self.probe_every} 只探测一次）")


# ---------------------------------------------------------------- 东财 K 线（日线，精确）
def _parse_em_kline(j):
    klines = ((j or {}).get("data") or {}).get("klines")
    if not klines:
        return None
    lows = []
    for s in klines:
        try:
            lows.append(float(s.split(",")[1]))
        except (ValueError, IndexError):
            continue
    return _min_positive(lows)


def em_hist_low(code: str):
    params = {"secid": em_secid(code), "klt": 101, "fqt": 1,
              "beg": "19900101", "end": "20500101", "lmt": 1000000,
              "ut": "7eea3edcaed734bea9cbfc24409ed989",
              "fields1": "f1,f2,f3", "fields2": "f51,f55"}
    return _parse_em_kline(get_json(EM_HIS, "/api/qt/stock/kline/get", params, EM_REF, tries=2))


def em_hist_low_proxy(code: str):
    return _parse_em_kline(proxy_json("/api/kline", {"secid": em_secid(code)}, tries=2))


# ---------------------------------------------------------------- Baostock（周线等比，断线自愈）
_BS = {"ok": None, "uses": 0, "denied": 0}
BS_LOGIN_RETRY_EVERY = 200


def _bs_quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _bs_login() -> bool:
    try:
        import baostock as bs
        try:
            _bs_quiet(bs.logout)
        except Exception:
            pass
        lg = _bs_quiet(bs.login)
        _BS["uses"] = 0
        return getattr(lg, "error_code", "1") == "0"
    except Exception:
        return False


def _bs_ready() -> bool:
    if _BS["ok"] is None:
        _BS["ok"] = _bs_login()
        if not _BS["ok"]:
            print("      !! Baostock 暂不可用（登录失败/超时），稍后自动重试")
    elif _BS["ok"] is False:
        _BS["denied"] += 1
        if _BS["denied"] % BS_LOGIN_RETRY_EVERY == 0:
            _BS["ok"] = _bs_login()
            if _BS["ok"]:
                print("      >> Baostock 重新登录成功，通道恢复")
    return bool(_BS["ok"])


def _bs_query_low(code: str):
    import baostock as bs
    sym = ("sh." if code.startswith("6") else "sz.") + code
    # 周K取 min 与日K取 min 等价（每根周线的 low 就是当周最低），数据量降 5 倍
    rs = _bs_quiet(bs.query_history_k_data_plus, sym, "low",
                   start_date="1990-01-01", frequency="w", adjustflag="2")
    if getattr(rs, "error_code", "1") != "0":
        raise RuntimeError(f"baostock error_code={rs.error_code}")
    lows = []
    with contextlib.redirect_stdout(io.StringIO()):
        while rs.next():
            row = rs.get_row_data()
            try:
                lows.append(float(row[0]))
            except (ValueError, TypeError, IndexError):
                continue
    if not lows:
        raise RuntimeError("baostock 返回空序列")
    return _min_positive(lows)


def bs_hist_low(code: str):
    _BS["uses"] += 1
    if _BS["uses"] >= BS_RELOGIN_EVERY:
        _bs_login()
    try:
        return _bs_query_low(code)
    except Exception:
        if not _bs_login():
            raise
        return _bs_query_low(code)


# ---------------------------------------------------------------- 腾讯（周线，减法口径近似）
def tx_hist_low(code: str):
    """返回 (正值最低, 是否含非正值)。含非正值 = 减法复权失真，不可用。"""
    sym = tx_symbol(code)
    lows, dirty, end = [], False, ""
    for _ in range(12):
        j = get_json(TX_IFZQ, "/appstock/app/fqkline/get",
                     {"param": f"{sym},week,,{end},640,qfq"}, "https://gu.qq.com/")
        node = ((j or {}).get("data") or {}).get(sym) or {}
        arr = node.get("qfqweek") or node.get("week") or []
        if not arr:
            break
        for bar in arr:
            try:
                v = float(bar[4])
            except (ValueError, TypeError, IndexError):
                continue
            if v <= 0:
                dirty = True
            else:
                lows.append(v)
        if len(arr) < 640:
            break
        first_day = dt.date.fromisoformat(arr[0][0])
        end = (first_day - dt.timedelta(days=1)).isoformat()
        time.sleep(0.1)
    return (_min_positive(lows), dirty)


def fetch_hist_low(code: str, stats: dict, brks: dict):
    """返回 (low, src)。src: em/px/bs/tx；减法复权失真 (None,'neg')；全失败 (None,None)。"""
    if brks["em"].should_try():
        try:
            v = em_hist_low(code)
            brks["em"].ok()
            if v is not None:
                stats["em"] += 1
                return v, "em"
        except Exception:
            brks["em"].fail()
    if EM_PROXY and brks["px"].should_try():
        try:
            v = em_hist_low_proxy(code)
            brks["px"].ok()
            if v is not None:
                stats["px"] += 1
                return v, "px"
        except Exception:
            brks["px"].fail()
    if not code.startswith(("43", "83", "87", "88", "92")) and _bs_ready() and brks["bs"].should_try():
        try:
            v = bs_hist_low(code)
            brks["bs"].ok()
            if v is not None:
                stats["bs"] += 1
                return v, "bs"
        except Exception:
            brks["bs"].fail()
    try:
        v, dirty = tx_hist_low(code)
        if dirty:
            return None, "neg"
        if v is not None:
            stats["tx"] += 1
            return v, "tx"
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------- 断点缓存
def load_done(cache_dir: Path) -> dict:
    """加载今天所有分片的断点缓存（含单进程模式的）。"""
    today = dt.date.today().strftime("%Y%m%d")
    done = {}
    for p in cache_dir.glob(f"lows-{today}*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                done[rec["c"]] = (rec["l"], rec.get("s"))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


class ShardWriter:
    def __init__(self, cache_dir: Path, shard: int):
        cache_dir.mkdir(parents=True, exist_ok=True)
        today = dt.date.today().strftime("%Y%m%d")
        self._fh = (cache_dir / f"lows-{today}-w{shard}.jsonl").open("a", encoding="utf-8")

    def save(self, code: str, low: float, src: str) -> None:
        self._fh.write(json.dumps({"c": code, "l": low, "s": src}, ensure_ascii=False) + "\n")
        self._fh.flush()


# ---------------------------------------------------------------- 分片工作进程
def make_record(code, name, low, src, ipo_map):
    ipo = ipo_map.get(code)
    den = min(low, ipo) if ipo else low
    rec = {"n": name, "b": board_of(code), "l": low, "i": ipo, "d": round(den, 4)}
    if src in ("tx", "bs"):
        rec["s"] = src
    return rec


def carry_from_prev(prev_ok, code, name):
    rec = prev_ok.get(code)
    if not rec:
        return None
    st = int(rec.get("st", 0)) + 1
    if st > STALE_MAX:
        return None
    new = dict(rec)
    new["n"] = name or new.get("n", "")
    new["st"] = st
    return new


def process_stock(code, name, done, writer, ipo_map, prev_ok, stats, brks,
                  items, failed, neg, stale_used, sleep):
    if code in done:
        low, src = done[code]
    else:
        low, src = fetch_hist_low(code, stats, brks)
        if low is not None and src in ("em", "px", "bs", "tx"):
            writer.save(code, low, src)
        time.sleep(sleep)

    if src in ("em", "px") and low is not None and low > 0:
        items[code] = make_record(code, name, low, src, ipo_map)
        return
    carried = carry_from_prev(prev_ok, code, name)   # 等比滞后数据优先于腾讯近似
    if src == "bs" and low is not None and low > 0:
        items[code] = make_record(code, name, low, src, ipo_map)
    elif carried is not None:
        items[code] = carried
        stale_used.append(code)
    elif src == "tx" and low is not None and low > 0:
        items[code] = make_record(code, name, low, src, ipo_map)
    elif src == "neg":
        neg.append(code)
    else:
        failed.append(code)


def shard_worker(shard, codes, done, ipo_map, prev_ok, sleep, cache_dir, result_file):
    time.sleep(shard * 2.0)   # 错峰启动，避免多进程同时轰击
    socket.setdefaulttimeout(25)
    _reset_session()
    writer = ShardWriter(Path(cache_dir), shard)
    stats = {"em": 0, "px": 0, "tx": 0, "bs": 0}
    brks = {"em": Breaker(f"[w{shard}]东财直连"), "px": Breaker(f"[w{shard}]东财代理"),
            "bs": Breaker(f"[w{shard}]Baostock")}
    items, failed, neg, stale_used = {}, [], [], []
    t0 = time.time()
    for i, (code, name) in enumerate(codes):
        process_stock(code, name, done, writer, ipo_map, prev_ok, stats, brks,
                      items, failed, neg, stale_used, sleep)
        n = i + 1
        if n % 100 == 0 or n == len(codes):
            speed = n / max(time.time() - t0, 1)
            eta = (len(codes) - n) / max(speed, 0.01)
            print(f"      [w{shard}] {n}/{len(codes)}  成功 {len(items)}  失败 {len(failed)}  "
                  f"剔除 {len(neg)}  预计剩余 {eta/60:.0f} 分钟")
    Path(result_file).write_text(json.dumps(
        {"items": items, "failed": failed, "neg": neg, "stale_used": stale_used, "stats": stats},
        ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 主流程
def main() -> None:
    parser = argparse.ArgumentParser(description="构建 分母 = Min(前复权历史最低, 首发价) 数据")
    parser.add_argument("--out", default="../worker/public/denominator.json")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试跑），0 = 全量")
    parser.add_argument("--codes", default="", help="只处理指定代码，逗号分隔")
    parser.add_argument("--sleep", type=float, default=0.2, help="每只股票之间的间隔秒数")
    parser.add_argument("--workers", type=int, default=4, help="并行进程数（1 = 单进程）")
    parser.add_argument("--cache", default=".cache")
    parser.add_argument("--min-count", type=int, default=4000)
    args = parser.parse_args()

    full_run = not args.codes and args.limit == 0
    if EM_PROXY:
        print(f"东财代理已配置：{EM_PROXY}")

    stocks, list_src = fetch_stock_list(args.out)
    if args.codes:
        wanted = {c.strip().zfill(6) for c in args.codes.split(",") if c.strip()}
        stocks = [(c, n) for c, n in stocks if c in wanted]
    elif args.limit > 0:
        stocks = stocks[: args.limit]

    ipo_map = fetch_ipo_prices()

    prev_ok = {}
    try:
        prev = json.loads(Path(args.out).read_text(encoding="utf-8"))
        if not prev.get("sample"):
            for c, rec in (prev.get("items") or {}).items():
                if rec.get("s") in (None, "bs"):   # 东财或 Baostock = 等比口径，可顶替
                    prev_ok[c] = rec
    except Exception:
        pass
    if prev_ok:
        print(f"      上一份数据中有 {len(prev_ok)} 条等比口径记录可作滞后顶替")

    cache_dir = Path(__file__).resolve().parent / args.cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    done = load_done(cache_dir)
    if done:
        print(f"      发现断点缓存：已完成 {len(done)} 只")

    n_workers = max(1, min(args.workers, 8))
    print(f"[3/3] 拉取前复权历史最低价，共 {len(stocks)} 只，{n_workers} 进程并行 ...")
    t0 = time.time()

    items, failed, neg, stale_used = {}, [], [], []
    stats = {"em": 0, "px": 0, "tx": 0, "bs": 0}

    if n_workers == 1:
        writer = ShardWriter(cache_dir, 0)
        brks = {"em": Breaker("东财直连"), "px": Breaker("东财代理"), "bs": Breaker("Baostock")}
        for i, (code, name) in enumerate(stocks):
            process_stock(code, name, done, writer, ipo_map, prev_ok, stats, brks,
                          items, failed, neg, stale_used, args.sleep)
            n = i + 1
            if n % 100 == 0 or n == len(stocks):
                speed = n / max(time.time() - t0, 1)
                eta = (len(stocks) - n) / max(speed, 0.01)
                print(f"      {n}/{len(stocks)}  成功 {len(items)}  失败 {len(failed)}  "
                      f"剔除 {len(neg)}  预计剩余 {eta/60:.0f} 分钟")
    else:
        shards = [stocks[i::n_workers] for i in range(n_workers)]
        result_files = [cache_dir / f"result-w{i}.json" for i in range(n_workers)]
        procs = []
        for i in range(n_workers):
            p = mp.Process(target=shard_worker,
                           args=(i, shards[i], done, ipo_map, prev_ok, args.sleep,
                                 str(cache_dir), str(result_files[i])))
            p.start()
            procs.append(p)
        for p in procs:
            p.join()
        for i, rf in enumerate(result_files):
            if not rf.exists():
                raise SystemExit(f"分片 {i} 未产出结果（进程异常退出），请 Re-run")
            r = json.loads(rf.read_text(encoding="utf-8"))
            items.update(r["items"])
            failed += r["failed"]
            neg += r["neg"]
            stale_used += r["stale_used"]
            for k in stats:
                stats[k] += r["stats"].get(k, 0)
            rf.unlink()

    # 收尾二次重试（主进程内串行，量小）
    if failed:
        print(f"      对失败的 {len(failed)} 只做二次重试 ...")
        name_map = dict(stocks)
        writer = ShardWriter(cache_dir, 99)
        brks = {"em": Breaker("东财直连(重试)"), "px": Breaker("东财代理(重试)"),
                "bs": Breaker("Baostock(重试)")}
        still = []
        for code in failed:
            low, src = fetch_hist_low(code, stats, brks)
            time.sleep(args.sleep)
            if src == "neg":
                carried = carry_from_prev(prev_ok, code, name_map.get(code, ""))
                if carried is not None:
                    items[code] = carried
                    stale_used.append(code)
                else:
                    neg.append(code)
            elif low is not None and low > 0:
                writer.save(code, low, src)
                items[code] = make_record(code, name_map.get(code, ""), low, src, ipo_map)
            else:
                carried = carry_from_prev(prev_ok, code, name_map.get(code, ""))
                if carried is not None:
                    items[code] = carried
                    stale_used.append(code)
                else:
                    still.append(code)
        failed = still

    if full_run and len(items) < args.min_count:
        raise SystemExit(f"有效结果只有 {len(items)} 只（< {args.min_count}），疑似接口大面积失败，"
                         "拒绝写出以免部署残缺数据。可到 Actions 页 Re-run 重试。")

    payload = {
        "build_date": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "count": len(items),
        "failed": failed,
        "neg_excluded": neg,
        "items": items,
    }
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    mins = (time.time() - t0) / 60
    print(f"\n完成：{len(items)} 只写入 {out_path}（K线阶段耗时 {mins:.0f} 分钟）")
    print(f"股票名单来源：{list_src}；K 线来源：东财直连 {stats['em']}，东财代理 {stats['px']}，"
          f"Baostock {stats['bs']}，腾讯 {stats['tx']}")
    if stale_used:
        st_max = max(int(items[c].get("st", 0)) for c in stale_used if c in items)
        print(f"用等比口径历史数据顶替 {len(stale_used)} 只（最长滞后 {st_max} 个交易日）")
    if neg:
        print(f"减法复权失真剔除 {len(neg)} 只（高分红老股，暂无等比数据可用）: "
              f"{neg[:15]}{' ...' if len(neg) > 15 else ''}")
    print(f"失败 {len(failed)} 只（停牌新股/退市/接口异常，前端会自动忽略）: "
          f"{failed[:20]}{' ...' if len(failed) > 20 else ''}")


if __name__ == "__main__":
    main()
