# -*- coding: utf-8 -*-
"""
Wind 数据通道（第一通道）—— 直连 Wind MCP HTTPS 端点（JSON-RPC 2.0）
================================================================
定位：估值（现价 / 总市值 / PE-TTM / PB）与 名称 / 申万行业 的**首选**数据源；
调用失败、超时或**每日额度用尽（QUOTA）**时返回 None，由上层回退到现有
akshare / emweb / 年报解析通道。三大报表暂不走 Wind（省额度，按用户选择）。

为什么不走 wind-mcp-skill 的 node CLI：该 CLI 在本机（用户目录含非 ASCII）经 Python
subprocess 派生时 stdout 恒为空（node 侧 spawn 怪异），故直接复刻其 HTTP 逻辑——
CLI 本质只是对 https://mcp.wind.com.cn/vserver_*/mcp/ 的 JSON-RPC 裸封装。

额度：Wind 云 API 按积分计费，返回体不含剩余额度 → 只能在报错/限流时判定；判定后本
进程内后续调用直接短路（`_QUOTA_EXHAUSTED`），避免反复空试。
安全：WIND_API_KEY 从 ~/.wind-aifinmarket/config 运行时读取，仅驻内存；不写日志、不入库、
不硬编码。结果进本地缓存（省额度）：行情 12h、名称/行业 30d。
"""

import os
import re
import json
import time

import requests

# ── 端点 / 配置 ──────────────────────────────────────────────
_ENDPOINTS = {
    "stock_data": "https://mcp.wind.com.cn/vserver_stock_data/mcp/",
}
_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".wind-aifinmarket", "config")
_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
os.makedirs(_CACHE, exist_ok=True)

# 额度/限流关键词（源自 skill cli.mjs 的 QUOTA_ERROR 模式）
_QUOTA_RE = re.compile(
    r"单日请求次数超限|daily.*limit|余额不足|请先充值|积分不足|insufficient.*balance"
    r"|请求过于频繁|qps.*limit|too.*frequent|限流|blocked_quota|quota",
    re.I)

_QUOTA_EXHAUSTED = False       # 进程内额度熔断
_INITED = False                # 进程内 MCP initialize 只做一次
_KEY = None                    # 缓存密钥（仅内存）


def quota_exhausted():
    return _QUOTA_EXHAUSTED


# ── 密钥 ─────────────────────────────────────────────────────
def _read_key():
    """从 ~/.wind-aifinmarket/config 读取 WIND_API_KEY（dotenv 风格）。仅内存缓存。"""
    global _KEY
    if _KEY is not None:
        return _KEY or None
    _KEY = ""
    key = None
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            for raw in fh:
                s = raw.strip().lstrip("﻿")
                if not s or s.startswith("#") or "=" not in s:
                    continue
                if s.startswith("export "):
                    s = s[7:]
                k, v = s.split("=", 1)
                if k.strip() == "WIND_API_KEY":
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    key = v.strip()
    except Exception:
        key = os.environ.get("WIND_API_KEY")
    _KEY = key or ""
    return _KEY or None


# ── 缓存 ─────────────────────────────────────────────────────
def _cache_get(key, ttl):
    p = os.path.join(_CACHE, key + ".json")
    try:
        if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < ttl:
            return json.load(open(p, encoding="utf-8"))
    except Exception:
        pass
    return None


def _cache_set(key, val):
    try:
        json.dump(val, open(os.path.join(_CACHE, key + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:
        pass


# ── HTTP / JSON-RPC ──────────────────────────────────────────
def _headers(key):
    return {"Authorization": "Bearer " + key,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}


def _parse_sse(resp):
    """解析 SSE / 纯 JSON 响应 → payload dict。用 utf-8 解码，按 \\n 切分，取末个 data: 行。"""
    raw = resp.content.decode("utf-8", "replace").strip()
    if raw.startswith("{"):
        return json.loads(raw)
    last = None
    for line in raw.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("data: "):
            last = line[6:]
    if last is None:
        raise ValueError("响应非 SSE 也非 JSON")
    return json.loads(last)


def _rpc(server, method, params, timeout):
    ep = _ENDPOINTS[server]
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return requests.post(ep, headers=_headers(_read_key()), json=body, timeout=timeout)


def _ensure_init(server):
    global _INITED
    if _INITED:
        return True
    try:
        r = _rpc(server, "initialize",
                 {"protocolVersion": "2025-03-26", "capabilities": {},
                  "clientInfo": {"name": "stock-analyzer", "version": "5"}}, 30)
        if r.status_code == 200:
            _INITED = True
            return True
    except Exception as e:
        print(f"[Wind] initialize 异常: {str(e)[:100]}")
    return False


def _call(server, tool, args, timeout=60):
    """调 Wind MCP tools/call。返回 (inner_dict, err)；err ∈ {None,'QUOTA','ERR'}。"""
    global _QUOTA_EXHAUSTED
    if _QUOTA_EXHAUSTED:
        return None, "QUOTA"
    if not _read_key():
        return None, "ERR"
    if server not in _ENDPOINTS:
        return None, "ERR"
    if not _ensure_init(server):
        return None, "ERR"
    try:
        r = _rpc(server, "tools/call",
                 {"name": tool, "arguments": args, "_meta": {}}, timeout)
    except Exception as e:
        print(f"[Wind] 调用异常: {str(e)[:100]}")
        return None, "ERR"

    if r.status_code != 200:
        if r.status_code == 429 or _QUOTA_RE.search(r.text or ""):
            _QUOTA_EXHAUSTED = True
            print("[Wind] 额度/限流(HTTP) → 回退原通道")
            return None, "QUOTA"
        return None, "ERR"

    try:
        payload = _parse_sse(r)
    except Exception:
        return None, "ERR"

    # JSON-RPC 层错误 / 工具层 isError
    err_msg = None
    if isinstance(payload.get("error"), dict):
        err_msg = payload["error"].get("message") or json.dumps(payload["error"], ensure_ascii=False)
    result = payload.get("result") or {}
    if not err_msg and result.get("isError"):
        try:
            err_msg = result["content"][0]["text"]
        except Exception:
            err_msg = json.dumps(result, ensure_ascii=False)

    inner = None
    try:
        inner = json.loads(result["content"][0]["text"])
    except Exception:
        inner = None

    # 工具内嵌业务错误
    if not err_msg and isinstance(inner, dict):
        if isinstance(inner.get("mcp_tool_error_code"), (int, float)) and inner["mcp_tool_error_code"] != 0:
            err_msg = inner.get("mcp_tool_error_msg") or "tool error"
        elif isinstance(inner.get("error"), dict) and (inner["error"].get("code") or inner["error"].get("message")):
            err_msg = f"{inner['error'].get('code','')}: {inner['error'].get('message','')}"

    if err_msg:
        if _QUOTA_RE.search(err_msg):
            _QUOTA_EXHAUSTED = True
            print("[Wind] 额度/限流 → 回退原通道")
            return None, "QUOTA"
        return None, "ERR"

    if inner is None:
        return None, "ERR"
    return inner, None


# ── 结果解析 ─────────────────────────────────────────────────
def _rows(inner):
    """兼容 price_indicators(data.rows) 与 NL 工具(data.data[0].rows) 两种结构。"""
    d = (inner or {}).get("data") or {}
    if isinstance(d, dict) and "rows" in d:
        return d.get("columns") or [], d.get("rows") or []
    if isinstance(d, dict) and isinstance(d.get("data"), list) and d["data"]:
        b = d["data"][0]
        return b.get("columns") or [], b.get("rows") or []
    return [], []


def _row_dict(inner):
    cols, rows = _rows(inner)
    if not rows:
        return None
    return {cols[i]["name"]: rows[0][i] for i in range(min(len(cols), len(rows[0])))}


def _f(v):
    try:
        x = float(v)
        return None if x != x else x
    except Exception:
        return None


def windcode(code, market=None):
    """6 位代码 → Wind 代码。market='NEEQ' → .NQ；否则按段：0/3=SZ，4/8/920=BJ，其余 SH。"""
    c = str(code).strip()
    if market == "NEEQ":
        return c + ".NQ"
    if c.startswith(("0", "3")):
        return c + ".SZ"
    if c.startswith(("4", "8", "920")):
        return c + ".BJ"
    return c + ".SH"


# ── 对外接口 ─────────────────────────────────────────────────
def market_indicators(code, market=None):
    """
    现价 / 总市值 / 三视角 PE / PB。命中缓存或 Wind；失败 / 额度 / 无数据 → None。

    三视角 PE 同一次调用取回（零额外额度）：
      pe_ttm = 市盈率(TTM)   当前实况
      pe_lyr = 市盈率(LYR)   静态（上年报口径，去年基准）
      pe_fwd = 市盈率(预测)  前瞻（Wind 一致预期口径）
    """
    ck = f"wind_mkt_{code}_{market or ''}"
    c = _cache_get(ck, 12 * 3600)
    if c is not None:
        return c or None
    inner, err = _call("stock_data", "get_stock_price_indicators",
                       {"windcode": windcode(code, market),
                        "indexes": "中文简称,最新成交价,总市值2,"
                                   "市盈率(TTM),市盈率(LYR),市盈率(预测),市净率(LF)"})
    if err:
        return None
    m = _row_dict(inner)
    if not m:
        _cache_set(ck, {})
        return None
    res = {"name": m.get("中文简称"), "price": _f(m.get("最新成交价")),
           "mktcap": _f(m.get("总市值2")),
           "pe_ttm": _f(m.get("市盈率(TTM)")), "pe_lyr": _f(m.get("市盈率(LYR)")),
           "pe_fwd": _f(m.get("市盈率(预测)")), "pb": _f(m.get("市净率(LF)"))}
    _cache_set(ck, res)
    return res


def consensus(code, market=None):
    """
    一致预期覆盖度：评级机构家数（Forward PE 的覆盖广度代理）。

    Wind 行情指标里没有「一致预期机构数」字段，改用 get_stock_fundamentals 的
    「评级机构家数」——即对该标的出具评级 / 盈利预测的机构数量。无覆盖 / 小盘股
    多为空。返回 {coverage:int|None}；失败 / 额度 → None。缓存 12h。
    """
    ck = f"wind_cons_{code}_{market or ''}"
    c = _cache_get(ck, 12 * 3600)
    if c is not None:
        return c or None
    inner, err = _call("stock_data", "get_stock_fundamentals",
                       {"question": windcode(code, market) + " 评级机构家数"})
    if err:
        return None
    m = _row_dict(inner)
    if not m:
        _cache_set(ck, {})
        return None
    cov = None
    for k, v in m.items():
        if "机构家数" in k or "评级机构" in k:
            cov = _f(v)
            break
    res = {"coverage": int(cov) if cov is not None else None}
    _cache_set(ck, res)
    return res


# 行业对比用指标（最近完整会计年度，年度口径以匹配阈值/分位设计）
_METRICS_Q = (
    " 最近一个完整会计年度的 净资产收益率ROE、总资产净利率ROA、销售毛利率、销售净利率、"
    "资产负债率、总资产周转率、营业总收入同比增长率、归属母公司股东的净利润同比增长率、"
    "应收账款周转率、存货周转率、流动比率、总资产、基本每股收益、每股净资产、"
    "经营活动产生的现金流量净额与净利润之比"
)


def stock_metrics(code, market=None):
    """
    行业对比用的单只标准化指标（最近完整会计年度），走 get_stock_fundamentals。

    返回与 industry_compare._extract_metrics 同构的 dict（roe/roa/gross_margin/net_margin/
    debt_ratio/asset_turnover/rev_growth/profit_growth/ar_turnover/inv_turnover/current_ratio/
    interest_cover/ocf_to_ni/total_assets/eps/bvps），total_assets 归一到元；失败/额度 → None。
    缓存 30d（年度指标变动慢）。
    """
    ck = f"wind_metrics_{code}_{market or ''}"
    c = _cache_get(ck, 30 * 86400)
    if c is not None:
        return c or None
    inner, err = _call("stock_data", "get_stock_fundamentals",
                       {"question": windcode(code, market) + _METRICS_Q})
    if err:
        return None
    m = _row_dict(inner)
    if not m:
        _cache_set(ck, {})
        return None

    def pick(req, exclude=()):
        reqs = [req] if isinstance(req, str) else list(req)
        for col, v in m.items():
            if all(r in str(col) for r in reqs) and not any(e in str(col) for e in exclude):
                return _f(v)
        return None

    total = pick("总资产", exclude=("周转", "净利率", "报酬", "ROA", "收益"))
    ocf = pick(["现金", "净利润"], exclude=("增长",))
    if ocf is not None and abs(ocf) <= 5:      # 倍数(0.95) → 折算百分比
        ocf = round(ocf * 100, 2)
    res = {
        "roe":            pick("ROE"),
        "roa":            pick("ROA"),
        "gross_margin":   pick("毛利率"),
        "net_margin":     pick("销售净利率"),
        "debt_ratio":     pick("资产负债率"),
        "asset_turnover": pick("总资产周转"),
        "rev_growth":     pick(["营业总收入", "增长"]),
        "profit_growth":  pick(["净利润", "增长"]),
        "ar_turnover":    pick("应收账款周转"),
        "inv_turnover":   pick("存货周转"),
        "current_ratio":  pick("流动比率"),
        "interest_cover": None,
        "ocf_to_ni":      ocf,
        "total_assets":   (total * 1e8 if total is not None else None),   # 亿元 → 元
        "eps":            pick("每股收益"),
        "bvps":           pick("每股净资产"),
    }
    if res["roe"] is None and res["net_margin"] is None:   # 基本没取到 → 失败
        _cache_set(ck, {})
        return None
    _cache_set(ck, res)
    return res


def basic_info(code, market=None):
    """证券简称 + 申万行业。返回 {name, sw_l2(二级名), sw_full(全链)}；失败 / 额度 → None。"""
    ck = f"wind_basic_{code}_{market or ''}"
    c = _cache_get(ck, 30 * 86400)
    if c is not None:
        return c or None
    inner, err = _call("stock_data", "get_stock_basicinfo",
                       {"question": windcode(code, market) + "的证券简称和所属申万行业"})
    if err:
        return None
    m = _row_dict(inner)
    if not m:
        _cache_set(ck, {})
        return None
    sw_full = str(m.get("所属申万行业明细") or "").strip()
    parts = [p for p in sw_full.split("--") if p]
    sw_l2 = parts[1] if len(parts) >= 2 else (parts[0] if parts else None)
    res = {"name": m.get("证券简称") or m.get("中文简称"), "sw_l2": sw_l2, "sw_full": sw_full}
    _cache_set(ck, res)
    return res


# 自测：python wind_data.py 600519 874628
if __name__ == "__main__":
    import sys
    codes = sys.argv[1:] or ["600519"]
    for c in codes:
        mk = "NEEQ" if c.startswith(("4", "8", "9")) and len(c) == 6 else None
        print(f"\n=== {c} (market={mk}) ===")
        print("market:", json.dumps(market_indicators(c, mk), ensure_ascii=True))
        print("basic :", json.dumps(basic_info(c, mk), ensure_ascii=True))
