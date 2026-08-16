#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日盘后构建"分母"数据：
    分母 = Min(历史最低价[前复权·等比], 首发价格)

v11 核心思路 —— 不再依赖任何第三方的"复权后数据"，自己算等比复权：
    等比前复权只需要两样东西：
      1. 不复权原始日K（腾讯行情，实测对 GitHub 海外 IP 永远可达）
      2. 分红送配事件表（东财数据中心，与首发价格同一域名，实测同样永远可达）
    对每个除权除息日，按交易所公式算除权除息参考价：
        ref = (前收盘 - 每股现金红利) / (1 + 每股送转比例)
        当日等比因子 r = ref / 前收盘   （恒有 0 < r <= 1，绝不产生负价格）
    历史某日的前复权价 = 原始价 × ∏(该日之后所有事件的 r)，全序列取最低即得分母。
    与东财口径一致；已知细微差异：纯配股事件未纳入（三十年仅数百例，影响可忽略）。

数据链路：
    股票名单：东财直连 → 东财·经 Worker 代理 → 腾讯（沪深京）→ 新浪 → 上一份名单
    历史K线：东财K线直连(半开探测) → 东财K线·代理 → 自算(腾讯原始价×事件表)
    首发价格 / 分红送配事件：东财数据中心
    口径优先级：东财新鲜 > 自算(等比) > 等比滞后顶替(≤15日) > 缺席

其他：多进程分片（--workers）、半开熔断、断点续跑、收尾二次重试、
      结果过少拒绝写出、事件表拉取失败则禁用自算通道（宁缺毋错）。

用法：
    python build_denominator.py --limit 80 --out ../worker/public/denominator.json
    python build_denominator.py --out ../worker/public/denominator.json --workers 4
"""
import argparse
import bisect
import datetime as dt
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

socket.setdefaulttimeout(25)

# 所有打印强制 flush，进度实时可见
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
STALE_MAX = 15        # 等比口径历史数据最多沿用多少个交易日


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
    print("[1/4] 拉取全市场股票列表 ...")
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
    print("[2/4] 拉取首发价格（东财数据中心）...")
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


# ---------------------------------------------------------------- 分红送配事件表（自算复权的原料）
def _num(rec, keys, default=0.0):
    for k in keys:
        v = rec.get(k)
        if v is None or v == "-":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return default


def fetch_bonus_events() -> dict:
    """全市场分红送配事件：{code: [(除权除息日'YYYY-MM-DD', 每股现金红利税前, 每股送转比例), ...]}
    与首发价格同一个数据中心域名（实测对 GitHub 可达）。"""
    print("[3/4] 拉取全市场分红送配事件表（东财数据中心）...")
    base = {"sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "-1",
            "pageSize": 5000, "pageNumber": 1,
            "reportName": "RPT_SHAREBONUS_DET",
            "quoteColumns": "", "source": "WEB", "client": "WEB", "filter": ""}
    events, rows = {}, 0

    def eat(j):
        nonlocal rows
        for rec in ((j or {}).get("result") or {}).get("data") or []:
            ex = str(rec.get("EX_DIVIDEND_DATE") or "")[:10]
            if len(ex) != 10:
                continue  # 尚未实施、没有除权除息日的预案，跳过
            code = str(rec.get("SECURITY_CODE", "")).zfill(6)
            cash10 = _num(rec, ["PRETAX_BONUS_RMB", "PRETAX_BONUS_AMOUNT"])
            gift10 = _num(rec, ["BONUS_IT_RATIO"])
            if gift10 == 0.0:
                gift10 = _num(rec, ["BONUS_RATIO"]) + _num(rec, ["IT_RATIO"])
            if cash10 <= 0 and gift10 <= 0:
                continue
            events.setdefault(code, []).append((ex, cash10 / 10.0, gift10 / 10.0))
            rows += 1

    for columns in ("SECURITY_CODE,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB,BONUS_IT_RATIO", "ALL"):
        try:
            params = dict(base, columns=columns)
            first = get_json(EM_DATA, "/api/data/v1/get", params, "https://data.eastmoney.com/", timeout=30)
            pages = int(((first or {}).get("result") or {}).get("pages") or 0)
            eat(first)
            if not events:
                raise RuntimeError("首页无有效事件")
            for pn in range(2, min(pages, 60) + 1):
                params["pageNumber"] = pn
                eat(get_json(EM_DATA, "/api/data/v1/get", params, "https://data.eastmoney.com/", timeout=30))
                time.sleep(0.15 + random.random() * 0.25)
            print(f"      拿到 {rows} 条事件，覆盖 {len(events)} 只股票")
            return events
        except Exception as e:
            events, rows = {}, 0
            print(f"      !! 事件表拉取失败（columns={columns[:12]}...）：{e}")
    return {}   # 空 = 自算通道将被禁用（宁缺毋错）


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


# ---------------------------------------------------------------- 东财 K 线（等比，直连/代理，漏风就赚）
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


# ---------------------------------------------------------------- 自算等比复权（腾讯原始价 × 事件表）
def tx_raw_daily(code: str):
    """腾讯不复权日K全历史，返回升序 [(date, low, close), ...]。"""
    sym = tx_symbol(code)
    bars, end = [], ""
    for _ in range(30):   # 30 × 640 根，覆盖任何 A 股历史
        j = get_json(TX_IFZQ, "/appstock/app/fqkline/get",
                     {"param": f"{sym},day,,{end},640,"}, "https://gu.qq.com/")
        node = ((j or {}).get("data") or {}).get(sym) or {}
        arr = node.get("day") or []
        if not arr:
            break
        chunk = []
        for b in arr:
            try:
                chunk.append((str(b[0])[:10], float(b[4]), float(b[2])))
            except (ValueError, TypeError, IndexError):
                continue
        bars = chunk + bars
        if len(arr) < 640:
            break
        first_day = dt.date.fromisoformat(str(arr[0][0])[:10])
        end = (first_day - dt.timedelta(days=1)).isoformat()
        time.sleep(0.08)
    return bars


def compute_qfq_low(bars, stock_events):
    """等比前复权历史最低：
    对每个除权除息日算 r = (前收盘 - 每股现金) / (前收盘 × (1 + 每股送转))，
    历史价 × 之后所有 r 的连乘，全序列取正值最低。r ∈ (0,1]，绝无负价格。"""
    if not bars:
        return None
    dates = [b[0] for b in bars]
    last_date = dates[-1]
    ratios = []
    for ex, cash, gift in sorted(stock_events or []):
        if ex > last_date:
            continue   # 未来/未生效的事件不影响当前锚定
        i = bisect.bisect_left(dates, ex) - 1   # 除权除息日前最后一个交易日
        if i < 0:
            continue
        pc = bars[i][2]
        if pc <= 0:
            continue
        ref = (pc - cash) / (1.0 + gift)
        if ref <= 0:
            continue
        ratios.append((ex, ref / pc))
    F, j, best = 1.0, len(ratios) - 1, None
    for k in range(len(bars) - 1, -1, -1):
        d, low, _close = bars[k]
        while j >= 0 and ratios[j][0] > d:
            F *= ratios[j][1]
            j -= 1
        if low > 0:
            v = low * F
            if best is None or v < best:
                best = v
    return round(best, 4) if best is not None else None


def computed_hist_low(code: str, events: dict):
    bars = tx_raw_daily(code)
    if not bars:
        return None
    return compute_qfq_low(bars, events.get(code))


def fetch_hist_low(code: str, stats: dict, brks: dict, events, events_ok: bool):
    """返回 (low, src)。src: em/px/c；全失败 (None, None)。"""
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
    if events_ok:
        try:
            v = computed_hist_low(code, events)
            if v is not None:
                stats["c"] += 1
                return v, "c"
        except Exception:
            pass
    return None, None


# ---------------------------------------------------------------- 断点缓存
def load_done(cache_dir: Path) -> dict:
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
    # em/px/c 都是等比口径，一视同仁，不打标（历史遗留的 bs 标记仍可被顶替沿用）
    return {"n": name, "b": board_of(code), "l": low, "i": ipo, "d": round(den, 4)}


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
                  items, failed, stale_used, sleep, events, events_ok):
    if code in done:
        low, src = done[code]
    else:
        low, src = fetch_hist_low(code, stats, brks, events, events_ok)
        if low is not None and src in ("em", "px", "c"):
            writer.save(code, low, src)
        time.sleep(sleep)

    if low is not None and low > 0 and src in ("em", "px", "c"):
        items[code] = make_record(code, name, low, src, ipo_map)
        return
    carried = carry_from_prev(prev_ok, code, name)
    if carried is not None:
        items[code] = carried
        stale_used.append(code)
    else:
        failed.append(code)


def shard_worker(shard, codes, done, ipo_map, prev_ok, sleep, cache_dir, result_file,
                 events, events_ok):
    time.sleep(shard * 1.5)
    socket.setdefaulttimeout(25)
    _reset_session()
    writer = ShardWriter(Path(cache_dir), shard)
    stats = {"em": 0, "px": 0, "c": 0}
    brks = {"em": Breaker(f"[w{shard}]东财直连"), "px": Breaker(f"[w{shard}]东财代理")}
    items, failed, stale_used = {}, [], []
    t0 = time.time()
    for i, (code, name) in enumerate(codes):
        process_stock(code, name, done, writer, ipo_map, prev_ok, stats, brks,
                      items, failed, stale_used, sleep, events, events_ok)
        n = i + 1
        if n % 100 == 0 or n == len(codes):
            speed = n / max(time.time() - t0, 1)
            eta = (len(codes) - n) / max(speed, 0.01)
            print(f"      [w{shard}] {n}/{len(codes)}  成功 {len(items)}  失败 {len(failed)}  "
                  f"预计剩余 {eta/60:.0f} 分钟")
    Path(result_file).write_text(json.dumps(
        {"items": items, "failed": failed, "stale_used": stale_used, "stats": stats},
        ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 主流程
def main() -> None:
    parser = argparse.ArgumentParser(description="构建 分母 = Min(前复权历史最低, 首发价) 数据")
    parser.add_argument("--out", default="../worker/public/denominator.json")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试跑），0 = 全量")
    parser.add_argument("--codes", default="", help="只处理指定代码，逗号分隔")
    parser.add_argument("--sleep", type=float, default=0.15, help="每只股票之间的间隔秒数")
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
    events = fetch_bonus_events()
    events_ok = bool(events)
    if not events_ok:
        print("      !! 事件表不可用，本次禁用自算通道（依赖东财漏风 + 滞后顶替，宁缺毋错）")

    prev_ok = {}
    try:
        prev = json.loads(Path(args.out).read_text(encoding="utf-8"))
        if not prev.get("sample"):
            for c, rec in (prev.get("items") or {}).items():
                if rec.get("s") in (None, "bs"):   # 等比口径（东财/自算/历史bs）可顶替
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
    print(f"[4/4] 计算前复权历史最低价，共 {len(stocks)} 只，{n_workers} 进程并行 ...")
    t0 = time.time()

    items, failed, stale_used = {}, [], []
    stats = {"em": 0, "px": 0, "c": 0}

    if n_workers == 1:
        writer = ShardWriter(cache_dir, 0)
        brks = {"em": Breaker("东财直连"), "px": Breaker("东财代理")}
        for i, (code, name) in enumerate(stocks):
            process_stock(code, name, done, writer, ipo_map, prev_ok, stats, brks,
                          items, failed, stale_used, args.sleep, events, events_ok)
            n = i + 1
            if n % 100 == 0 or n == len(stocks):
                speed = n / max(time.time() - t0, 1)
                eta = (len(stocks) - n) / max(speed, 0.01)
                print(f"      {n}/{len(stocks)}  成功 {len(items)}  失败 {len(failed)}  "
                      f"预计剩余 {eta/60:.0f} 分钟")
    else:
        shards = [stocks[i::n_workers] for i in range(n_workers)]
        result_files = [cache_dir / f"result-w{i}.json" for i in range(n_workers)]
        procs = []
        for i in range(n_workers):
            p = mp.Process(target=shard_worker,
                           args=(i, shards[i], done, ipo_map, prev_ok, args.sleep,
                                 str(cache_dir), str(result_files[i]), events, events_ok))
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
            stale_used += r["stale_used"]
            for k in stats:
                stats[k] += r["stats"].get(k, 0)
            rf.unlink()

    # 收尾二次重试
    if failed:
        print(f"      对失败的 {len(failed)} 只做二次重试 ...")
        name_map = dict(stocks)
        writer = ShardWriter(cache_dir, 99)
        brks = {"em": Breaker("东财直连(重试)"), "px": Breaker("东财代理(重试)")}
        still = []
        for code in failed:
            low, src = fetch_hist_low(code, stats, brks, events, events_ok)
            time.sleep(args.sleep)
            if low is not None and low > 0 and src in ("em", "px", "c"):
                writer.save(code, low, src)
                items[code] = make_record(code, name_map.get(code, ""), low, src, ipo_map)
            else:
                still.append(code)
        failed = still

    if full_run and len(items) < args.min_count:
        raise SystemExit(f"有效结果只有 {len(items)} 只（< {args.min_count}），疑似接口大面积失败，"
                         "拒绝写出以免部署残缺数据。可到 Actions 页 Re-run 重试。")

    payload = {
        "build_date": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "count": len(items),
        "failed": failed,
        "neg_excluded": [],
        "items": items,
    }
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    mins = (time.time() - t0) / 60
    print(f"\n完成：{len(items)} 只写入 {out_path}（K线阶段耗时 {mins:.0f} 分钟）")
    print(f"股票名单来源：{list_src}；K 线来源：东财直连 {stats['em']}，东财代理 {stats['px']}，"
          f"自算等比(腾讯原始价×事件表) {stats['c']}")
    if stale_used:
        st_max = max(int(items[c].get("st", 0)) for c in stale_used if c in items)
        print(f"用等比口径历史数据顶替 {len(stale_used)} 只（最长滞后 {st_max} 个交易日）")
    print(f"失败 {len(failed)} 只（停牌新股/退市/接口异常，前端会自动忽略）: "
          f"{failed[:20]}{' ...' if len(failed) > 20 else ''}")


if __name__ == "__main__":
    main()
