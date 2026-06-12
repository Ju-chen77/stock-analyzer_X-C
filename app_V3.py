# -*- coding: utf-8 -*-
"""
app_V3.py — A 股基本面分析 + PE/PB 估值
四段式：业绩检验 → 业绩归因 → 验证排雷 → 估值分析
数据源：AKShare（免费，无需注册）
启动后访问：http://127.0.0.1:5000
"""

import os
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import math
import time as _time
from datetime import datetime as _dt

import requests as _requests
import akshare as ak
import pandas as pd
from flask import Flask, request, jsonify, Response

PROJECT_NAME = "A 股基本面透视"
PROJECT_DESC = "业绩检验 · 业绩归因 · 验证排雷 · PE/PB 估值"

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════════
# 初始化：股票代码表
# ═══════════════════════════════════════════════════════════════
def _load_code_name():
    try:
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            if len(df.columns) == 2:
                df.columns = ["code", "name"]
            print(f"[启动] 已加载 {len(df)} 只 A 股 (主源)")
            return df
    except Exception as e:
        print(f"[启动] 主源失败: {e}")
    try:
        spot = ak.stock_zh_a_spot_em()
        df = spot[["代码", "名称"]].copy()
        df.columns = ["code", "name"]
        print(f"[启动] 已加载 {len(df)} 只 A 股 (备用源)")
        return df
    except Exception as e:
        print(f"[启动] 备用源失败: {e}")
    return pd.DataFrame(columns=["code", "name"])

_CODE_NAME = _load_code_name()


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════
def to_sina_code(code):
    code = str(code).strip()
    if code.startswith(("6", "9")): return "sh" + code
    if code.startswith(("0", "3")): return "sz" + code
    return "bj" + code

def sf(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None

def sdiv(a, b):
    if a is None or b is None or b == 0:
        return None
    try:
        return float(a) / float(b)
    except Exception:
        return None

def fcol(df, *candidates):
    for c in candidates:
        col = next((col for col in df.columns if c in str(col).strip()), None)
        if col:
            return col
    return None

def col_vals(df, col, periods):
    if col is None or df is None:
        return [None] * len(periods)
    d = dict(zip(df["_date"].astype(str).tolist(), df[col].tolist()))
    return [sf(d.get(p)) for p in periods]

def yoy_list(lst):
    result = []
    for i in range(len(lst)):
        if i + 1 < len(lst) and lst[i] is not None and lst[i + 1] is not None and lst[i + 1] != 0:
            result.append((lst[i] - lst[i + 1]) / abs(lst[i + 1]))
        else:
            result.append(None)
    return result

def _set_date(df):
    if df is None or df.empty:
        return df
    df = df.copy()
    date_col = fcol(df, "报表日期", "报告日")
    if date_col:
        df["_date"] = df[date_col].astype(str).str[:8]
        df = df.sort_values("_date", ascending=False).reset_index(drop=True)
    return df

def annual_periods(df, n=6):
    if df is None or "_date" not in df.columns:
        return []
    return df[df["_date"].str.endswith("1231")]["_date"].tolist()[:n]

def _percentile(current, series):
    if current is None or not series:
        return None
    clean = [x for x in series if x is not None and not math.isnan(x) and x > 0]
    if not clean:
        return None
    below = sum(1 for x in clean if x <= current)
    return round(below / len(clean) * 100, 1)


# ═══════════════════════════════════════════════════════════════
# 数据抓取层
# ═══════════════════════════════════════════════════════════════
def fetch_raw(code):
    sina = to_sina_code(code)
    out = {}
    for key, label in [("income", "利润表"), ("balance", "资产负债表"), ("cashflow", "现金流量表")]:
        try:
            df = ak.stock_financial_report_sina(stock=sina, symbol=label)
            out[key] = _set_date(df) if (df is not None and not df.empty) else None
        except Exception as e:
            print(f"[ERR] {label}: {e}")
            out[key] = None
    return out


# ═══════════════════════════════════════════════════════════════
# 第一段：业绩检验
# ═══════════════════════════════════════════════════════════════
def compute_performance(raw):
    income   = raw.get("income")
    cashflow = raw.get("cashflow")
    balance  = raw.get("balance")
    if income is None:
        return {}
    periods = annual_periods(income, 6)
    if not periods:
        return {}

    rev_col    = fcol(income, "营业收入")
    cost_col   = fcol(income, "营业成本")
    profit_col = fcol(income, "归属于母公司所有者的净利润", "净利润")

    revenue    = col_vals(income, rev_col,    periods)
    cogs       = col_vals(income, cost_col,   periods)
    net_profit = col_vals(income, profit_col, periods)

    gross_margin = [sdiv(r - c, r) if r and c else None for r, c in zip(revenue, cogs)]
    net_margin   = [sdiv(p, r) for p, r in zip(net_profit, revenue)]
    revenue_yoy  = yoy_list(revenue)
    profit_yoy   = yoy_list(net_profit)

    roe = []
    if balance is not None:
        eq_col = fcol(balance, "所有者权益", "股东权益")
        equity = col_vals(balance, eq_col, periods)
        for i, (p, e) in enumerate(zip(net_profit, equity)):
            if i + 1 < len(equity) and equity[i + 1] is not None and e is not None:
                roe.append(sdiv(p, (e + equity[i + 1]) / 2))
            else:
                roe.append(sdiv(p, e))
    else:
        roe = [None] * len(periods)

    cash_match = {}
    if cashflow is not None:
        cf_periods = annual_periods(cashflow, 6)
        common = [p for p in periods if p in cf_periods]

        sc_col  = fcol(cashflow, "销售商品、提供劳务收到的现金", "销售商品")
        pc_col  = fcol(cashflow, "购买商品、接受劳务支付的现金", "购买商品")
        ocf_col = fcol(cashflow, "经营活动产生的现金流量净额", "经营活动产生")

        sales_cash    = col_vals(cashflow, sc_col,  common)
        purchase_cash = col_vals(cashflow, pc_col,  common)
        ocf           = col_vals(cashflow, ocf_col, common)

        rev_c    = col_vals(income, rev_col,    common)
        cost_c   = col_vals(income, cost_col,   common)
        profit_c = col_vals(income, profit_col, common)

        cash_match = {
            "periods":       common,
            "revenue":       rev_c,
            "sales_cash":    sales_cash,
            "rev_ratio":     [sdiv(s, r) for s, r in zip(sales_cash, rev_c)],
            "cost":          cost_c,
            "purchase_cash": purchase_cash,
            "cost_ratio":    [sdiv(p, c) for p, c in zip(purchase_cash, cost_c)],
            "net_profit":    profit_c,
            "ocf":           ocf,
            "profit_ratio":  [sdiv(o, p) for o, p in zip(ocf, profit_c)],
        }

    return {
        "periods":      periods,
        "revenue":      revenue,
        "net_profit":   net_profit,
        "gross_margin": gross_margin,
        "net_margin":   net_margin,
        "roe":          roe,
        "revenue_yoy":  revenue_yoy,
        "profit_yoy":   profit_yoy,
        "cash_match":   cash_match,
    }


# ═══════════════════════════════════════════════════════════════
# 第二段：业绩归因
# ═══════════════════════════════════════════════════════════════
def compute_attribution(raw):
    income  = raw.get("income")
    balance = raw.get("balance")
    if income is None or balance is None:
        return {}
    periods = annual_periods(income, 6)
    if not periods:
        return {}

    rev_col    = fcol(income,  "营业收入")
    profit_col = fcol(income,  "归属于母公司所有者的净利润", "净利润")
    assets_col = fcol(balance, "资产总计")
    equity_col = fcol(balance, "所有者权益", "股东权益")

    revenue    = col_vals(income,  rev_col,    periods)
    net_profit = col_vals(income,  profit_col, periods)
    assets     = col_vals(balance, assets_col, periods)
    equity     = col_vals(balance, equity_col, periods)

    net_margin     = [sdiv(p, r) for p, r in zip(net_profit, revenue)]
    asset_turnover = [sdiv(r, a) for r, a in zip(revenue, assets)]
    equity_mult    = [sdiv(a, e) for a, e in zip(assets, equity)]
    roe_dupont = [
        m * t * eq if (m and t and eq) else None
        for m, t, eq in zip(net_margin, asset_turnover, equity_mult)
    ]

    def expense_rate(col_key):
        col = fcol(income, col_key)
        vals = col_vals(income, col, periods)
        return [sdiv(v, r) for v, r in zip(vals, revenue)]

    return {
        "periods":        periods,
        "net_margin":     net_margin,
        "asset_turnover": asset_turnover,
        "equity_mult":    equity_mult,
        "roe":            roe_dupont,
        "selling_rate":   expense_rate("销售费用"),
        "admin_rate":     expense_rate("管理费用"),
        "rd_rate":        expense_rate("研发费用"),
        "finance_rate":   expense_rate("财务费用"),
    }


# ═══════════════════════════════════════════════════════════════
# 第三段：验证排雷
# ═══════════════════════════════════════════════════════════════
def _traffic(value, green_thr, yellow_thr, higher_is_better=True):
    if value is None:
        return "grey"
    if higher_is_better:
        if value >= green_thr:  return "green"
        if value >= yellow_thr: return "yellow"
        return "red"
    else:
        if value <= green_thr:  return "green"
        if value <= yellow_thr: return "yellow"
        return "red"


def compute_risk(raw):
    income   = raw.get("income")
    balance  = raw.get("balance")
    cashflow = raw.get("cashflow")
    if income is None or balance is None:
        return {"dashboard": []}
    periods = annual_periods(income, 6)
    if not periods:
        return {"dashboard": []}

    rev_col    = fcol(income,  "营业收入")
    profit_col = fcol(income,  "归属于母公司所有者的净利润", "净利润")
    ar_col     = fcol(balance, "应收账款")
    inv_col    = fcol(balance, "存货")
    gw_col     = fcol(balance, "商誉")
    assets_col = fcol(balance, "资产总计")
    liab_col   = fcol(balance, "负债合计")
    equity_col = fcol(balance, "所有者权益", "股东权益")

    revenue     = col_vals(income,  rev_col,    periods)
    net_profit  = col_vals(income,  profit_col, periods)
    ar          = col_vals(balance, ar_col,     periods)
    inventory   = col_vals(balance, inv_col,    periods)
    goodwill    = col_vals(balance, gw_col,     periods)
    assets      = col_vals(balance, assets_col, periods)
    liabilities = col_vals(balance, liab_col,   periods)
    equity      = col_vals(balance, equity_col, periods)

    ocf = [None] * len(periods)
    if cashflow is not None:
        ocf_col = fcol(cashflow, "经营活动产生的现金流量净额", "经营活动产生")
        if ocf_col:
            cf_map = dict(zip(cashflow["_date"].astype(str).tolist(), cashflow[ocf_col].tolist()))
            ocf = [sf(cf_map.get(p)) for p in periods]

    dashboard = []

    pcq = sdiv(ocf[0], net_profit[0]) if ocf and net_profit else None
    dashboard.append({
        "name": "利润含金量", "value": round(pcq, 2) if pcq is not None else None,
        "unit": "×", "status": _traffic(pcq, 0.8, 0.5, higher_is_better=True),
        "desc": "经营现金流 / 净利润",
        "signal": "≥0.8 健康 ｜ 0.5–0.8 关注 ｜ <0.5 警示",
    })

    rev_yoy0 = sdiv(revenue[0] - revenue[1], abs(revenue[1])) if len(revenue) > 1 and revenue[0] and revenue[1] else None
    ar_yoy0  = sdiv(ar[0] - ar[1], abs(ar[1])) if len(ar) > 1 and ar[0] and ar[1] else None
    ar_gap   = (ar_yoy0 - rev_yoy0) if (ar_yoy0 is not None and rev_yoy0 is not None) else None
    dashboard.append({
        "name": "收入质量",
        "value": round(ar_gap * 100, 1) if ar_gap is not None else None,
        "unit": "pp（应收增速−营收增速）",
        "status": _traffic(ar_gap, 0.0, 0.1, higher_is_better=False) if ar_gap is not None else "grey",
        "desc": "应收账款增速 vs 营收增速差值",
        "signal": "<0 健康 ｜ 0–10pp 关注 ｜ >10pp 警示",
    })

    inv_yoy0 = sdiv(inventory[0] - inventory[1], abs(inventory[1])) if len(inventory) > 1 and inventory[0] and inventory[1] else None
    inv_gap  = (inv_yoy0 - rev_yoy0) if (inv_yoy0 is not None and rev_yoy0 is not None) else None
    dashboard.append({
        "name": "库存压力",
        "value": round(inv_gap * 100, 1) if inv_gap is not None else None,
        "unit": "pp（存货增速−营收增速）",
        "status": _traffic(inv_gap, 0.05, 0.15, higher_is_better=False) if inv_gap is not None else "grey",
        "desc": "存货增速 vs 营收增速差值",
        "signal": "<5pp 健康 ｜ 5–15pp 关注 ｜ >15pp 警示",
    })

    gw_ratio = sdiv(goodwill[0], equity[0]) if goodwill[0] is not None and equity and equity[0] else None
    dashboard.append({
        "name": "商誉风险",
        "value": round(gw_ratio * 100, 1) if gw_ratio is not None else None,
        "unit": "%（商誉 / 净资产）",
        "status": _traffic(gw_ratio, 0.1, 0.3, higher_is_better=False) if gw_ratio is not None else "grey",
        "desc": "商誉 / 净资产",
        "signal": "<10% 健康 ｜ 10–30% 关注 ｜ >30% 警示",
    })

    dr = sdiv(liabilities[0], assets[0]) if liabilities and assets and liabilities[0] and assets[0] else None
    dashboard.append({
        "name": "偿债风险",
        "value": round(dr * 100, 1) if dr is not None else None,
        "unit": "%（资产负债率）",
        "status": _traffic(dr, 0.5, 0.7, higher_is_better=False),
        "desc": "负债合计 / 资产总计（制造/消费行业参考）",
        "signal": "<50% 健康 ｜ 50–70% 关注 ｜ >70% 警示",
    })

    trend_periods = periods[:-1]
    rev_yoys = yoy_list(revenue)[:-1]
    ar_yoys  = yoy_list(ar)[:-1]
    inv_yoys = yoy_list(inventory)[:-1]

    return {
        "dashboard":     dashboard,
        "trend_periods": trend_periods,
        "rev_yoy":       rev_yoys,
        "ar_yoy":        ar_yoys,
        "inv_yoy":       inv_yoys,
    }


# ═══════════════════════════════════════════════════════════════
# 第四段：PE/PB 估值分析
# ═══════════════════════════════════════════════════════════════

def _get_stock_info_em(code):
    """通过东方财富直接 API 获取股票基本信息（绕过 akshare bug）"""
    secid = f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f57,f58,f84,f127,f116,f117"
    try:
        r = _requests.get(url, timeout=10)
        data = r.json().get("data", {})
        if data:
            return {
                "行业": str(data.get("f127", "")),
                "总股本": data.get("f84"),
                "总市值": data.get("f116"),
                "流通市值": data.get("f117"),
                "名称": str(data.get("f58", "")),
            }
    except Exception as e:
        print(f"[WARN] _get_stock_info_em: {e}")
    return {}


CYCLICAL_KEYWORDS = [
    "有色", "煤炭", "钢铁", "化工", "航运", "船舶",
    "证券", "房地产", "地产", "养殖", "猪", "鸡",
    "石油", "石化", "建材", "水泥", "铝", "铜", "锂",
]

_spot_cache = {"df": None, "ts": 0}
_forecast_cache = {"df": None, "ts": 0}

def _get_spot():
    now = _time.time()
    if _spot_cache["df"] is not None and now - _spot_cache["ts"] < 300:
        return _spot_cache["df"]
    try:
        df = ak.stock_zh_a_spot_em()
        _spot_cache.update(df=df, ts=now)
        return df
    except Exception as e:
        print(f"[ERR] spot: {e}")
        return _spot_cache["df"]


def _get_forecast():
    now = _time.time()
    if _forecast_cache["df"] is not None and now - _forecast_cache["ts"] < 600:
        return _forecast_cache["df"]
    try:
        df = ak.stock_profit_forecast_em()
        _forecast_cache.update(df=df, ts=now)
        return df
    except Exception as e:
        print(f"[ERR] forecast_all: {e}")
        return _forecast_cache["df"]


def _compute_pe_pb_history(code, eps_annual, bvps_annual):
    """
    用日线价格 + 年报 EPS/BVPS 手动计算 PE/PB 历史序列。
    eps_annual / bvps_annual: list of (date_str_YYYYMMDD, value) 按日期降序
    """
    chart_dates, chart_pe, chart_pb = [], [], []
    pe_pos, pb_pos = [], []

    try:
        start = str(_dt.now().year - 5) + "0101"
        end = _dt.now().strftime("%Y%m%d")
        hist = ak.stock_zh_a_hist(symbol=code, period="daily",
                                  start_date=start, end_date=end, adjust="qfq")
        if hist is None or hist.empty:
            return chart_dates, chart_pe, chart_pb, pe_pos, pb_pos

        date_col = fcol(hist, "日期")
        close_col = fcol(hist, "收盘")
        if not date_col or not close_col:
            return chart_dates, chart_pe, chart_pb, pe_pos, pb_pos

        hist = hist.sort_values(date_col).reset_index(drop=True)

        eps_map = sorted(eps_annual, key=lambda x: x[0])
        bvps_map = sorted(bvps_annual, key=lambda x: x[0])

        def _lookup(mapping, date_str):
            val = None
            for d, v in mapping:
                if d <= date_str:
                    val = v
                else:
                    break
            return val

        for i in range(0, len(hist), 5):
            row = hist.iloc[i]
            d = str(row[date_col]).replace("-", "")[:8]
            close = sf(row[close_col])
            if close is None or close <= 0:
                continue

            eps = _lookup(eps_map, d)
            bv  = _lookup(bvps_map, d)

            pe = round(close / eps, 2) if (eps and eps > 0) else None
            pb_v = round(close / bv, 2) if (bv and bv > 0) else None

            chart_dates.append(d[:4] + "-" + d[4:6] + "-" + d[6:8])
            chart_pe.append(pe)
            chart_pb.append(pb_v)

            if pe and pe > 0:
                pe_pos.append(pe)
            if pb_v and pb_v > 0:
                pb_pos.append(pb_v)
    except Exception as e:
        print(f"[ERR] pe_pb_hist: {e}")

    return chart_dates, chart_pe, chart_pb, pe_pos, pb_pos


def _is_cyclical(industry, raw):
    score = 0
    if industry:
        for kw in CYCLICAL_KEYWORDS:
            if kw in industry:
                score += 2
                break

    income = raw.get("income")
    if income is not None:
        periods = annual_periods(income, 7)
        if len(periods) >= 3:
            rev_col = fcol(income, "营业收入")
            profit_col = fcol(income, "归属于母公司所有者的净利润", "净利润")
            revenues = col_vals(income, rev_col, periods)
            profits  = col_vals(income, profit_col, periods)

            if any(y is not None and y <= -0.3 for y in yoy_list(revenues)):
                score += 1
            if any(p is not None and p < 0 for p in profits):
                score += 1
            if any(y is not None and y <= -0.5 for y in yoy_list(profits)):
                score += 1
    return score >= 3


def compute_valuation(code, industry, raw):
    """
    估值分析主入口。
    Returns: dict 包含 matrix / triangulation / scenario / history 等全部估值数据
    """
    result = {
        "is_cyclical": False, "stock_type": "普通股",
        "matrix": [], "triangulation": None, "scenario": None,
        "current": {}, "history": {}, "industry": {},
    }

    # ── 1. 基本信息（总股本等）──
    info_d = _get_stock_info_em(code)
    total_shares = sf(info_d.get("总股本"))

    # ── 2. 实时行情 ──
    spot = _get_spot()
    current_price, current_pe, current_pb, current_mv = None, None, None, None

    if spot is not None:
        hits = spot[spot["代码"].astype(str) == code]
        if not hits.empty:
            row = hits.iloc[0]
            current_price = sf(row.get("最新价"))
            pe_col = next((c for c in spot.columns if "市盈率" in c), None)
            pb_col = next((c for c in spot.columns if "市净率" in c), None)
            mv_col = next((c for c in spot.columns if "总市值" in c), None)
            if pe_col: current_pe = sf(row.get(pe_col))
            if pb_col: current_pb = sf(row.get(pb_col))
            if mv_col: current_mv = sf(row.get(mv_col))

    if current_mv is None:
        current_mv = sf(info_d.get("总市值"))

    result["current"] = {
        "price": current_price, "pe": current_pe, "pb": current_pb,
        "market_cap": current_mv, "total_shares": total_shares,
    }

    # ── 3. 财务指标（EPS / BVPS）── 先于历史计算，因为 PE/PB 序列依赖 EPS/BVPS
    eps_ttm, bvps = None, None
    eps_history_7y = []
    eps_annual_pairs = []
    bvps_annual_pairs = []

    try:
        _time.sleep(0.3)
        fi = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(_dt.now().year - 10))
        if fi is not None and not fi.empty:
            date_col = fcol(fi, "日期")
            eps_col  = fcol(fi, "摊薄每股收益", "每股收益")
            bvps_col = fcol(fi, "每股净资产")

            if date_col:
                fi["_d"] = fi[date_col].astype(str).str.replace("-", "").str[:8]
                annual = fi[fi["_d"].str.endswith("1231")].sort_values("_d", ascending=False)

                if eps_col:
                    eps_history_7y = [sf(x) for x in annual[eps_col].tolist()][:7]
                    if eps_history_7y:
                        eps_ttm = eps_history_7y[0]
                    eps_annual_pairs = [(str(r["_d"]), sf(r[eps_col])) for _, r in annual.iterrows()
                                        if sf(r[eps_col]) is not None]

                if bvps_col:
                    if not annual.empty:
                        bvps = sf(annual.iloc[0][bvps_col])
                    bvps_annual_pairs = [(str(r["_d"]), sf(r[bvps_col])) for _, r in annual.iterrows()
                                          if sf(r[bvps_col]) is not None]
    except Exception as e:
        print(f"[ERR] fi: {e}")

    # 实时 PE 反推仅作为 fallback（东财"动态PE"是季度年化，与年报 EPS 口径不同）
    if eps_ttm is None and current_pe and current_price and current_pe > 0:
        eps_ttm = round(current_price / current_pe, 4)
    if bvps is None and current_pb and current_price and current_pb > 0:
        bvps = round(current_price / current_pb, 4)

    result["eps_ttm"] = round(eps_ttm, 4) if eps_ttm else None
    result["bvps"] = round(bvps, 4) if bvps else None

    # ── 4. PE/PB 历史序列（5 年，手动计算：日线价格 / 年报 EPS·BVPS）──
    chart_dates, chart_pe, chart_pb, pe_pos, pb_pos = \
        _compute_pe_pb_history(code, eps_annual_pairs, bvps_annual_pairs)

    # 用与历史序列同口径的 PE/PB 计算百分位（年报 EPS/BVPS 基准）
    pe_annual = round(current_price / eps_ttm, 2) if (current_price and eps_ttm and eps_ttm > 0) else current_pe
    pb_annual = round(current_price / bvps, 2) if (current_price and bvps and bvps > 0) else current_pb
    pe_percentile = _percentile(pe_annual, pe_pos)
    pb_percentile = _percentile(pb_annual, pb_pos)
    pe_median = round(sorted(pe_pos)[len(pe_pos) // 2], 2) if pe_pos else None
    pb_median = round(sorted(pb_pos)[len(pb_pos) // 2], 2) if pb_pos else None

    def _q(lst, q):
        if not lst:
            return None
        s = sorted(lst)
        idx = int(len(s) * q)
        idx = min(idx, len(s) - 1)
        return round(s[idx], 2)

    result["history"] = {
        "dates": chart_dates, "pe": chart_pe, "pb": chart_pb,
        "pe_percentile": pe_percentile, "pb_percentile": pb_percentile,
        "pe_median": pe_median, "pb_median": pb_median,
        "pe_p25": _q(pe_pos, 0.25), "pe_p75": _q(pe_pos, 0.75),
        "pb_p25": _q(pb_pos, 0.25), "pb_p75": _q(pb_pos, 0.75),
        "pe_min": round(min(pe_pos), 2) if pe_pos else None,
        "pe_max": round(max(pe_pos), 2) if pe_pos else None,
        "pb_min": round(min(pb_pos), 2) if pb_pos else None,
        "pb_max": round(max(pb_pos), 2) if pb_pos else None,
    }

    # ── 5. 一致预期（全量拉取后按代码筛选）──
    eps_forecast, growth_forecast = None, None

    try:
        fc_all = _get_forecast()
        if fc_all is not None and not fc_all.empty:
            code_col = next((c for c in fc_all.columns if fc_all[c].astype(str).str.match(r'^\d{6}$').any()), None)
            if code_col:
                hit = fc_all[fc_all[code_col].astype(str) == code]
                if not hit.empty:
                    row = hit.iloc[0]
                    yr = str(_dt.now().year)
                    fc_eps_col = next((c for c in fc_all.columns if yr in str(c) and ("预测每股收益" in str(c) or "预测EPS" in str(c) or "每股" in str(c))), None)
                    if fc_eps_col is None:
                        fc_eps_col = next((c for c in fc_all.columns if "预测每股收益" in str(c) or "预测EPS" in str(c)), None)
                    if fc_eps_col:
                        eps_forecast = sf(row[fc_eps_col])
    except Exception as e:
        print(f"[WARN] forecast: {e}")

    if eps_forecast is None and len(eps_history_7y) >= 3:
        valid = [e for e in eps_history_7y[:3] if e is not None and e > 0]
        if len(valid) >= 2:
            cagr = (valid[0] / valid[-1]) ** (1 / (len(valid) - 1)) - 1
            growth_forecast = cagr
            eps_forecast = round(valid[0] * (1 + cagr), 4)

    if growth_forecast is None and eps_ttm and eps_forecast and eps_ttm > 0:
        growth_forecast = (eps_forecast - eps_ttm) / abs(eps_ttm)

    if growth_forecast is None and len(eps_history_7y) >= 4:
        e0, e3 = eps_history_7y[0], eps_history_7y[3]
        if e0 and e3 and e3 > 0 and e0 > 0:
            growth_forecast = (e0 / e3) ** (1 / 3) - 1

    result["eps_forecast"] = round(eps_forecast, 4) if eps_forecast else None
    result["growth_forecast"] = round(growth_forecast * 100, 2) if growth_forecast else None

    # ── 6. 行业同行 PE/PB ──
    industry_pe_median, industry_pb_median, peer_count = None, None, 0

    if spot is not None and industry:
        try:
            _time.sleep(0.3)
            peers = ak.stock_board_industry_cons_em(symbol=industry)
            if peers is not None and not peers.empty:
                pcodes = [c for c in peers["代码"].astype(str).tolist() if c != code][:60]
                ps = spot[spot["代码"].astype(str).isin(pcodes)]

                pe_c = next((c for c in ps.columns if "市盈率" in c), None)
                pb_c = next((c for c in ps.columns if "市净率" in c), None)

                if pe_c:
                    v = ps[pe_c].apply(sf).dropna()
                    v = v[v > 0]
                    if len(v) > 5:
                        lo, hi = v.quantile(0.1), v.quantile(0.9)
                        v = v[(v >= lo) & (v <= hi)]
                    if len(v):
                        industry_pe_median = round(float(v.median()), 2)
                    peer_count = len(v)

                if pb_c:
                    v = ps[pb_c].apply(sf).dropna()
                    v = v[v > 0]
                    if len(v) > 5:
                        lo, hi = v.quantile(0.1), v.quantile(0.9)
                        v = v[(v >= lo) & (v <= hi)]
                    if len(v):
                        industry_pb_median = round(float(v.median()), 2)
        except Exception as e:
            print(f"[WARN] peers: {e}")

    result["industry"] = {
        "pe_median": industry_pe_median,
        "pb_median": industry_pb_median,
        "peer_count": peer_count,
    }

    # ── 7. 判断周期股 ──
    cyclical = _is_cyclical(industry, raw)
    result["is_cyclical"] = cyclical
    result["stock_type"] = "强周期股" if cyclical else "普通股"

    # ── 8. 分路径估值 ──
    if cyclical:
        _compute_pb_valuation(result, eps_history_7y)
    else:
        _compute_pe_valuation(
            result, pe_annual, pe_percentile, pe_median,
            industry_pe_median, eps_ttm, eps_forecast,
            growth_forecast, current_price,
        )

    return result


def _compute_pe_valuation(result, current_pe, pe_pctl, pe_med,
                          ind_pe_med, eps_ttm, eps_fc, growth, price):
    """普通股 PE 六维矩阵 + 三角验证 + 情景分析"""
    matrix = []

    # ① TTM PE
    sig = current_pe < ind_pe_med if (current_pe and ind_pe_med) else None
    matrix.append({
        "name": "TTM PE", "value": round(current_pe, 1) if current_pe else None,
        "benchmark": f"行业 {ind_pe_med}x" if ind_pe_med else "—",
        "positive": sig, "desc": "低于行业中位数为正面",
    })

    # ② Forward PE
    fwd = round(price / eps_fc, 1) if (price and eps_fc and eps_fc > 0) else None
    sig = fwd < current_pe if (fwd and current_pe) else None
    matrix.append({
        "name": "Forward PE", "value": fwd,
        "benchmark": f"TTM {round(current_pe,1)}x" if current_pe else "—",
        "positive": sig, "desc": "低于 TTM PE 说明 EPS 预期增长",
    })

    # ③ 行业对比
    sig = current_pe < ind_pe_med if (current_pe and ind_pe_med) else None
    matrix.append({
        "name": "行业对比", "value": f"{round(current_pe,1)}x" if current_pe else None,
        "benchmark": f"行业 {ind_pe_med}x" if ind_pe_med else "—",
        "positive": sig, "desc": "估值低于行业为正面",
    })

    # ④ 历史分位
    sig = pe_pctl < 30 if pe_pctl is not None else None
    matrix.append({
        "name": "历史分位", "value": f"{pe_pctl}%" if pe_pctl is not None else None,
        "benchmark": "<30% 偏低 / 30–70% 合理 / >70% 偏高",
        "positive": sig, "desc": "5 年 PE 分位",
    })

    # ⑤ PEG
    peg = None
    sig = None
    if current_pe and growth and growth > 0:
        peg = round(current_pe / (growth * 100), 2)
        sig = peg < 1
    matrix.append({
        "name": "PEG", "value": peg,
        "benchmark": "<1 低估 / =1 合理 / >1 高估",
        "positive": sig, "desc": "PE / 预测增速（%）",
    })

    # ⑥ 目标 PE 测算
    target_space = None
    if pe_med and eps_fc and price and price > 0:
        target_space = round((pe_med * eps_fc - price) / price * 100, 1)
    sig = target_space > 50 if target_space is not None else None
    matrix.append({
        "name": "目标 PE 测算", "value": f"{target_space}%" if target_space is not None else None,
        "benchmark": ">50% 有吸引力",
        "positive": sig, "desc": "中性情景潜在空间",
    })

    pos = sum(1 for m in matrix if m["positive"] is True)
    result["matrix"] = matrix
    result["positive_count"] = pos
    result["matrix_verdict"] = "强信号" if pos >= 5 else ("中性" if pos >= 3 else "无吸引力")

    # 三角验证
    methods = []
    if growth and growth > 0 and eps_fc:
        peg_pe = growth * 100
        methods.append({"name": "PEG 法（PEG=1）", "target": round(peg_pe * eps_fc, 2), "pe_used": round(peg_pe, 1)})
    if pe_med and eps_fc:
        methods.append({"name": "历史分位法", "target": round(pe_med * eps_fc, 2), "pe_used": pe_med})
    if ind_pe_med and eps_fc:
        methods.append({"name": "行业相对法", "target": round(ind_pe_med * eps_fc, 2), "pe_used": ind_pe_med})

    tri = {"methods": methods}
    if methods:
        ts = [m["target"] for m in methods]
        tri["v_low"], tri["v_high"] = round(min(ts), 2), round(max(ts), 2)
        tri["v_mid"] = round(sum(ts) / len(ts), 2)
        if price and price > 0:
            tri["deviation"] = round((tri["v_mid"] - price) / price * 100, 1)
    result["triangulation"] = tri

    # 情景分析
    scenarios = []
    if methods and price and eps_fc:
        for label, mult in [("悲观", 0.8), ("中性", 1.0), ("乐观", 1.2)]:
            adj = eps_fc * mult
            ts = [m["pe_used"] * adj for m in methods]
            t = min(ts) if mult == 0.8 else (max(ts) if mult == 1.2 else sum(ts) / len(ts))
            sp = round((t - price) / price * 100, 1) if price > 0 else None
            scenarios.append({"label": label, "eps": round(adj, 4), "target": round(t, 2), "space": sp})
    result["scenario"] = scenarios


def _compute_pb_valuation(result, eps_7y):
    """周期股 PB 六维矩阵 + PB 三角验证 + 情景分析"""
    h = result.get("history", {})
    cur = result.get("current", {})
    ind = result.get("industry", {})
    price = cur.get("price")
    pb = cur.get("pb")
    pb_pctl = h.get("pb_percentile")
    pb_med  = h.get("pb_median")
    bvps    = result.get("bvps")
    ind_pb  = ind.get("pb_median")

    valid_eps = [e for e in eps_7y if e is not None and e > 0]
    norm_eps = round(sum(valid_eps) / len(valid_eps), 4) if valid_eps else None
    result["normalized_eps"] = norm_eps

    cycle = "未知"
    if pb_pctl is not None:
        if pb_pctl < 20:    cycle = "底部"
        elif pb_pctl < 50:  cycle = "复苏期"
        elif pb_pctl < 80:  cycle = "景气期"
        else:               cycle = "顶部"
    result["cycle_position"] = cycle

    matrix = []

    sig = pb < 1.5 if pb else None
    matrix.append({"name": "当前 PB", "value": round(pb, 2) if pb else None,
                    "benchmark": "<1 破净 / 1–2 偏低 / >2 正常", "positive": sig,
                    "desc": "PB < 1 常见于周期底部"})

    sig = pb_pctl < 30 if pb_pctl is not None else None
    matrix.append({"name": "PB 历史分位", "value": f"{pb_pctl}%" if pb_pctl is not None else None,
                    "benchmark": "<20% 底部 / 20–50% 复苏 / >80% 顶部", "positive": sig,
                    "desc": "5 年 PB 分位"})

    sig = pb < ind_pb if (pb and ind_pb) else None
    matrix.append({"name": "行业 PB 对比", "value": round(pb, 2) if pb else None,
                    "benchmark": f"行业 {ind_pb}x" if ind_pb else "—", "positive": sig,
                    "desc": "低于行业中位数为正面"})

    norm_pe = round(price / norm_eps, 1) if (price and norm_eps and norm_eps > 0) else None
    sig = norm_pe is not None and norm_pe < 15
    matrix.append({"name": "穿越周期 PE", "value": norm_pe,
                    "benchmark": "股价 / 5–7 年平均 EPS", "positive": sig if norm_pe else None,
                    "desc": "正常化 EPS 消除周期波动"})

    sig = cycle in ("底部", "复苏期") if cycle != "未知" else None
    matrix.append({"name": "周期位置", "value": cycle,
                    "benchmark": "底部/复苏 → 布局；顶部 → 警惕", "positive": sig,
                    "desc": "基于 PB 分位判断"})

    sig = pb is not None and pb < 1
    matrix.append({"name": "破净安全边际", "value": f"PB={round(pb,2)}" if pb else None,
                    "benchmark": "PB<1 有净资产支撑", "positive": sig if pb else None,
                    "desc": "跌破净资产提供下行保护"})

    pos = sum(1 for m in matrix if m["positive"] is True)
    result["matrix"] = matrix
    result["positive_count"] = pos
    result["matrix_verdict"] = "底部信号强" if pos >= 5 else ("中性偏低" if pos >= 3 else "非底部区域")

    # PB 三角验证
    methods = []
    if pb_med and bvps:
        methods.append({"name": "历史 PB 中位数法", "target": round(pb_med * bvps, 2), "mult": pb_med})
    pe_med = h.get("pe_median")
    if norm_eps and pe_med:
        methods.append({"name": "穿越周期 PE 法", "target": round(norm_eps * pe_med, 2), "mult": pe_med})
    if ind_pb and bvps:
        methods.append({"name": "行业 PB 相对法", "target": round(ind_pb * bvps, 2), "mult": ind_pb})

    tri = {"methods": methods}
    if methods:
        ts = [m["target"] for m in methods]
        tri["v_low"], tri["v_high"] = round(min(ts), 2), round(max(ts), 2)
        tri["v_mid"] = round(sum(ts) / len(ts), 2)
        if price and price > 0:
            tri["deviation"] = round((tri["v_mid"] - price) / price * 100, 1)
    result["triangulation"] = tri

    scenarios = []
    if tri.get("v_low") and price:
        for label, key in [("悲观（周期下行）", "v_low"), ("中性", "v_mid"), ("乐观（周期复苏）", "v_high")]:
            t = tri[key]
            sp = round((t - price) / price * 100, 1) if price > 0 else None
            scenarios.append({"label": label, "target": t, "space": sp})
    result["scenario"] = scenarios


# ═══════════════════════════════════════════════════════════════
# 价格获取
# ═══════════════════════════════════════════════════════════════
def fetch_price(code):
    try:
        df = ak.stock_zh_a_daily(symbol=to_sina_code(code), adjust="")
        if df is not None and not df.empty and "close" in df.columns:
            return sf(df.iloc[-1]["close"])
    except Exception:
        pass
    try:
        spot = _get_spot()
        if spot is not None:
            hits = spot[spot["代码"].astype(str) == code]
            if not hits.empty:
                return sf(hits.iloc[0].get("最新价"))
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# 搜索
# ═══════════════════════════════════════════════════════════════
def search_stocks(query, limit=12):
    q = query.strip()
    if not q:
        return []
    if _CODE_NAME.empty:
        return [{"code": q.zfill(6), "name": q}] if q.isdigit() else []
    df = _CODE_NAME
    hits = df[df["code"].astype(str).str.contains(q)] if q.isdigit() \
        else df[df["name"].astype(str).str.contains(q, na=False)]
    return [{"code": str(r["code"]), "name": str(r["name"])} for _, r in hits.head(limit).iterrows()]


# ═══════════════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════════════
@app.route("/api/search")
def api_search():
    return jsonify(search_stocks(request.args.get("q", "")))


@app.route("/api/analyze")
def api_analyze():
    code = request.args.get("code", "").strip()
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"error": "请提供 6 位股票代码"}), 400

    name, industry = code, ""
    hit = _CODE_NAME[_CODE_NAME["code"].astype(str) == code]
    if not hit.empty:
        name = str(hit.iloc[0]["name"])

    info_d = _get_stock_info_em(code)
    industry = str(info_d.get("行业", ""))
    if info_d.get("名称"):
        name = info_d["名称"]

    price = fetch_price(code)
    raw   = fetch_raw(code)
    val   = compute_valuation(code, industry, raw)

    return jsonify({
        "info":        {"code": code, "name": name, "industry": industry, "price": price},
        "performance": compute_performance(raw),
        "attribution": compute_attribution(raw),
        "risk":        compute_risk(raw),
        "valuation":   val,
    })


@app.route("/")
def index():
    html = PAGE.replace("__PROJECT_NAME__", PROJECT_NAME).replace("__PROJECT_DESC__", PROJECT_DESC)
    return Response(html, mimetype="text/html")


# ═══════════════════════════════════════════════════════════════
# 内嵌前端
# ═══════════════════════════════════════════════════════════════
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PROJECT_NAME__</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js" charset="utf-8"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#EBEEF1; --surface:#fff; --ink:#11151B; --muted:#6A7480;
  --line:#DCE0E6; --accent:#0B6E5D; --accent-soft:#E0F0EB;
  --red:#B23A2E; --red-soft:#FAE8E6;
  --yellow-ink:#7A5800; --yellow-soft:#FDF5D9;
  --green:#0B6E5D; --green-soft:#E0F0EB;
  --grey-soft:#F2F4F6;
  --display:'Space Grotesk',sans-serif;
  --body:'Inter',system-ui,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--ink);font-family:var(--body);
  line-height:1.5;-webkit-font-smoothing:antialiased}

.app{min-height:100vh;display:flex;flex-direction:column;justify-content:center;
  align-items:center;padding:32px 20px;transition:justify-content .3s}
.app.searched{justify-content:flex-start;padding-top:40px}

.hero{width:100%;max-width:600px;text-align:center}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--accent);margin-bottom:14px}
h1{font-family:var(--display);font-weight:700;font-size:clamp(36px,7vw,56px);
  letter-spacing:-.02em;margin:0 0 8px;line-height:1.05}
.tag{color:var(--muted);font-size:15px;margin:0 auto 28px;max-width:28em}

.searchbar{display:flex;gap:8px;background:var(--surface);
  border:1.5px solid var(--ink);border-radius:3px;padding:7px 7px 7px 18px}
.searchbar:focus-within{box-shadow:0 0 0 4px var(--accent-soft)}
.searchbar input{flex:1;border:0;outline:0;font-family:var(--body);
  font-size:17px;background:transparent;color:var(--ink)}
.searchbar input::placeholder{color:#A7AFB9}
.searchbar button{font-family:var(--display);font-weight:600;font-size:15px;
  border:0;background:var(--ink);color:#fff;padding:0 24px;border-radius:2px;cursor:pointer}
.searchbar button:hover{background:var(--accent)}
.hint{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:10px}

.candidates{margin-top:12px;border:1px solid var(--line);border-radius:3px;
  background:var(--surface);overflow:hidden;text-align:left}
.cand{display:flex;justify-content:space-between;align-items:center;
  padding:11px 16px;cursor:pointer;border-bottom:1px solid var(--line)}
.cand:last-child{border-bottom:0}
.cand:hover{background:var(--accent-soft)}
.cand .nm{font-weight:600}
.cand .cd{font-family:var(--mono);font-size:13px;color:var(--muted)}

.state-msg{margin-top:20px;font-family:var(--mono);font-size:14px;color:var(--muted)}
.state-msg.err{color:var(--red)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--accent);margin-right:8px;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}

#out{display:none;width:100%;max-width:980px;margin:40px auto 64px}

.stockhead{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;
  padding-bottom:18px;border-bottom:2px solid var(--ink);margin-bottom:28px}
.stockhead .sname{font-family:var(--display);font-weight:700;font-size:28px}
.stockhead .scode{font-family:var(--mono);color:var(--muted);font-size:14px}
.stockhead .smeta{margin-left:auto;font-family:var(--mono);font-size:13px;color:var(--muted)}
.stockhead .smeta b{color:var(--ink)}

.tabs{display:flex;gap:0;border-bottom:2px solid var(--line);margin-bottom:32px;flex-wrap:wrap}
.tab-btn{font-family:var(--display);font-weight:600;font-size:14px;
  padding:10px 22px;border:0;background:transparent;cursor:pointer;
  color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-2px}
.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-panel{display:none}
.tab-panel.active{display:block}

.sec-label{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.sec-title{font-family:var(--display);font-weight:600;font-size:18px;margin:32px 0 6px}
.sec-sub{color:var(--muted);font-size:14px;margin:0 0 18px}

.kpi-row{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:28px}
.kpi-card{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:16px}
.kpi-card .ktag{font-family:var(--mono);font-size:11px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
.kpi-card .kval{font-family:var(--display);font-weight:700;font-size:22px;letter-spacing:-.01em}
.kpi-card .kunit{font-family:var(--mono);font-size:12px;color:var(--muted);margin-left:3px}

.chart-wrap{background:var(--surface);border:1px solid var(--line);
  border-radius:3px;padding:20px;margin-bottom:20px}
.chart-title{font-family:var(--display);font-weight:600;font-size:15px;margin-bottom:14px}
.chart-note{font-size:12.5px;color:var(--muted);margin-top:8px}
.charts-3col{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px}

.dashboard-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:28px}
.dash-card{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:16px 14px}
.dash-card .d-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.dash-card .d-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.d-dot.green{background:#1DBF73} .d-dot.yellow{background:#E8A800}
.d-dot.red{background:var(--red)} .d-dot.grey{background:#CCC}
.dash-card .d-name{font-family:var(--display);font-weight:600;font-size:14px}
.dash-card .d-val{font-family:var(--mono);font-weight:600;font-size:22px;margin-bottom:4px}
.dash-card.green{background:var(--green-soft);border-color:#A8D9CE}
.dash-card.yellow{background:var(--yellow-soft);border-color:#E8D59A}
.dash-card.red{background:var(--red-soft);border-color:#E8BDBA}
.dash-card .d-unit{font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:8px}
.dash-card .d-desc{font-size:12.5px;color:var(--muted);line-height:1.4}
.dash-card .d-signal{font-family:var(--mono);font-size:11px;color:var(--muted);
  margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}

.dupont-table{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px}
.dupont-table th{background:#F4F6F8;font-family:var(--mono);font-size:12px;
  font-weight:500;color:var(--muted);padding:8px 12px;text-align:right;
  border-bottom:1px solid var(--line)}
.dupont-table th:first-child{text-align:left}
.dupont-table td{padding:8px 12px;text-align:right;font-family:var(--mono);
  border-bottom:1px solid var(--line)}
.dupont-table td:first-child{text-align:left;font-weight:600;font-family:var(--body)}
.dupont-table tr:last-child td{border-bottom:0}

/* ── 估值模块样式 ── */
.type-badge{display:inline-block;font-family:var(--mono);font-size:12px;font-weight:600;
  padding:4px 14px;border-radius:2px;margin-bottom:20px;letter-spacing:.04em}
.type-badge.normal{background:var(--accent-soft);color:var(--accent)}
.type-badge.cyclical{background:#EDE0F7;color:#6D3DB2}

.matrix-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.mx-card{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:14px}
.mx-card.pos{border-left:3px solid #1DBF73}
.mx-card.neg{border-left:3px solid var(--red)}
.mx-card.na{border-left:3px solid #CCC}
.mx-card .mx-name{font-family:var(--display);font-weight:600;font-size:13px;margin-bottom:4px}
.mx-card .mx-val{font-family:var(--mono);font-weight:700;font-size:20px;margin-bottom:2px}
.mx-card .mx-bench{font-family:var(--mono);font-size:11px;color:var(--muted)}
.mx-card .mx-desc{font-size:12px;color:var(--muted);margin-top:6px}

.signal-bar{display:flex;align-items:center;gap:12px;padding:14px 18px;
  border-radius:3px;margin-bottom:24px;font-family:var(--display);font-weight:600}
.signal-bar.strong{background:#D4F5E5;color:#0B6E5D}
.signal-bar.neutral{background:var(--yellow-soft);color:var(--yellow-ink)}
.signal-bar.weak{background:var(--red-soft);color:var(--red)}

.tri-table{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px}
.tri-table th{background:#F4F6F8;font-family:var(--mono);font-size:12px;
  font-weight:500;color:var(--muted);padding:8px 12px;text-align:right;
  border-bottom:1px solid var(--line)}
.tri-table th:first-child{text-align:left}
.tri-table td{padding:10px 12px;text-align:right;font-family:var(--mono);
  border-bottom:1px solid var(--line)}
.tri-table td:first-child{text-align:left;font-weight:600;font-family:var(--body)}

.dev-card{background:var(--surface);border:1px solid var(--line);border-radius:3px;
  padding:18px;margin-bottom:24px;display:flex;align-items:center;gap:24px}
.dev-card .dev-label{font-size:14px;color:var(--muted)}
.dev-card .dev-val{font-family:var(--display);font-weight:700;font-size:28px}
.dev-card .dev-val.up{color:var(--green)}
.dev-card .dev-val.down{color:var(--red)}

.scen-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:28px}
.scen-card{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:16px}
.scen-card .sc-label{font-family:var(--mono);font-size:11px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
.scen-card .sc-target{font-family:var(--display);font-weight:700;font-size:22px;margin-bottom:2px}
.scen-card .sc-space{font-family:var(--mono);font-size:13px}
.sc-space.up{color:var(--green)} .sc-space.down{color:var(--red)}
.scen-card .sc-note{font-size:12px;color:var(--muted);margin-top:6px}

footer{width:100%;max-width:980px;margin:0 auto;font-family:var(--mono);font-size:12px;
  color:var(--muted);text-align:center;padding:16px 0 32px;border-top:1px solid var(--line)}

@media(max-width:720px){
  .kpi-row{grid-template-columns:repeat(3,1fr)}
  .dashboard-grid{grid-template-columns:repeat(2,1fr)}
  .charts-3col{grid-template-columns:1fr}
  .matrix-grid{grid-template-columns:repeat(2,1fr)}
  .scen-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="app" id="app">

  <header class="hero">
    <div class="eyebrow">A 股基本面分析 · AKShare</div>
    <h1>__PROJECT_NAME__</h1>
    <p class="tag">__PROJECT_DESC__</p>
    <div class="searchbar">
      <input id="q" placeholder="例如：茅台 或 600519" autocomplete="off" autofocus>
      <button id="go">搜索</button>
    </div>
    <div class="hint">中文名称模糊搜索 / 6 位代码精确搜索</div>
    <div id="cands"></div>
    <div id="state" class="state-msg"></div>
  </header>

  <main id="out">
    <div class="stockhead">
      <span class="sname" id="o-name"></span>
      <span class="scode" id="o-code"></span>
      <span class="smeta" id="o-meta"></span>
    </div>

    <nav class="tabs">
      <button class="tab-btn active" data-tab="t1">① 业绩检验</button>
      <button class="tab-btn"        data-tab="t2">② 业绩归因</button>
      <button class="tab-btn"        data-tab="t3">③ 验证排雷</button>
      <button class="tab-btn"        data-tab="t4">④ 估值分析</button>
    </nav>

    <!-- ── Tab 1：业绩检验 ── -->
    <div class="tab-panel active" id="t1">
      <p class="sec-label">Part A · 常规盈利指标</p>
      <div class="kpi-row" id="kpi-row"></div>
      <div class="chart-wrap">
        <div class="chart-title">营业收入 & 归母净利润（亿元）及 YoY 增速</div>
        <div id="ch-rev-profit" style="height:280px"></div>
        <div class="chart-note">柱：金额（左轴 亿元） ｜ 线：同比增速（右轴 %）</div>
      </div>
      <div class="chart-wrap">
        <div class="chart-title">盈利能力趋势：毛利率 / 净利率 / ROE</div>
        <div id="ch-margins" style="height:240px"></div>
      </div>
      <p class="sec-label" style="margin-top:24px">Part B · 利润表 vs 现金流量表 三组对照</p>
      <div class="charts-3col">
        <div class="chart-wrap" style="margin-bottom:0">
          <div class="chart-title" style="font-size:13px">① 收入含金量</div>
          <div id="ch-cm1" style="height:220px"></div>
          <div class="chart-note">健康：销售收到现金 / 营收 ≈ 1.0–1.17</div>
        </div>
        <div class="chart-wrap" style="margin-bottom:0">
          <div class="chart-title" style="font-size:13px">② 成本含金量</div>
          <div id="ch-cm2" style="height:220px"></div>
          <div class="chart-note">关注：购买支付现金 vs 营业成本比值趋势</div>
        </div>
        <div class="chart-wrap" style="margin-bottom:0">
          <div class="chart-title" style="font-size:13px">③ 利润含金量</div>
          <div id="ch-cm3" style="height:220px"></div>
          <div class="chart-note">健康：经营现金流 / 净利润 ≥ 0.8</div>
        </div>
      </div>
    </div>

    <!-- ── Tab 2：业绩归因 ── -->
    <div class="tab-panel" id="t2">
      <p class="sec-label">杜邦三因子分解 · ROE = 净利率 × 总资产周转率 × 权益乘数</p>
      <div class="chart-wrap">
        <div class="chart-title">ROE 杜邦分解趋势</div>
        <div id="ch-dupont" style="height:280px"></div>
        <div class="chart-note">净利率（%）与权益乘数（×）用左轴；总资产周转率（×）用右轴</div>
      </div>
      <table class="dupont-table" id="dupont-table"></table>
      <p class="sec-label" style="margin-top:32px">费用率趋势（占营业收入 %）</p>
      <div class="chart-wrap">
        <div class="chart-title">销售 / 管理 / 研发 / 财务 费用率</div>
        <div id="ch-expense" style="height:250px"></div>
        <div class="chart-note">费用率上升可能压缩利润空间；研发费用率上升通常是战略性投入</div>
      </div>
    </div>

    <!-- ── Tab 3：验证排雷 ── -->
    <div class="tab-panel" id="t3">
      <p class="sec-label">排雷仪表盘 · 红黄绿三色标注当前状态</p>
      <div class="dashboard-grid" id="dash-grid"></div>
      <p class="sec-label" style="margin-top:8px">应收账款增速 vs 营收增速（历史趋势）</p>
      <div class="chart-wrap">
        <div class="chart-title">收入质量趋势</div>
        <div id="ch-ar" style="height:240px"></div>
        <div class="chart-note">应收增速持续高于营收 → 赊销冲量风险</div>
      </div>
      <p class="sec-label">存货增速 vs 营收增速（历史趋势）</p>
      <div class="chart-wrap">
        <div class="chart-title">库存压力趋势</div>
        <div id="ch-inv" style="height:240px"></div>
        <div class="chart-note">存货大幅领先营收 → 可能预示行业景气下滑</div>
      </div>
    </div>

    <!-- ── Tab 4：估值分析 ── -->
    <div class="tab-panel" id="t4">
      <div id="val-type"></div>
      <p class="sec-label">估值矩阵</p>
      <div class="matrix-grid" id="val-matrix"></div>
      <div id="val-signal"></div>

      <p class="sec-label" style="margin-top:8px">PE / PB 历史走势（5 年）</p>
      <div class="chart-wrap">
        <div class="chart-title" id="ch-hist-title">PE-TTM 历史走势</div>
        <div id="ch-pe-hist" style="height:300px"></div>
        <div class="chart-note">虚线为 25/50/75 分位线，阴影区间为中间 50% 范围</div>
      </div>

      <p class="sec-label" style="margin-top:8px">三角验证</p>
      <table class="tri-table" id="val-tri"></table>
      <div id="val-dev"></div>

      <p class="sec-label" style="margin-top:8px">情景分析</p>
      <div class="scen-grid" id="val-scen"></div>

      <div class="chart-wrap" style="background:var(--grey-soft)">
        <div class="chart-note" style="margin:0">
          ⚠ 以上估值仅基于公开数据与历史统计，不构成投资建议。关键假设变动可能导致结论反转，请自行判断风险。
        </div>
      </div>
    </div>
  </main>

  <footer id="footer" style="display:none">
    数据来自 AKShare 公开接口，可能存在延迟或缺失，仅供学习研究，不构成投资建议。
  </footer>
</div>

<script>
const $ = s => document.querySelector(s);
const appEl = $('#app'), stateEl = $('#state'), candsEl = $('#cands');
const outEl = $('#out'), footEl = $('#footer');

function setState(msg, isErr) {
  stateEl.className = 'state-msg' + (isErr ? ' err' : '');
  stateEl.innerHTML = msg ? (isErr ? msg : '<span class="dot"></span>' + msg) : '';
}

function fmtPeriod(d) {
  const y = d.slice(0,4), m = d.slice(4,6);
  if (m === '12') return y;
  const labels = {'03':'Q1','06':'H1','09':'Q3'};
  return y + (labels[m] || '');
}

const yi  = v => v == null ? null : v / 1e8;
const pct = v => v == null ? null : v * 100;
const fmt2 = v => v == null ? '—' : v.toFixed(2);
const fmtP = v => v == null ? '—' : v.toFixed(1) + '%';
const fmtX = v => v == null ? '—' : v.toFixed(2) + '×';

const PLOTLY_CFG = {responsive:true, displayModeBar:false};
const LAYOUT_BASE = {
  paper_bgcolor:'transparent', plot_bgcolor:'rgba(0,0,0,0)',
  font:{family:"'IBM Plex Mono',monospace", size:11, color:'#6A7480'},
  margin:{t:10, r:10, b:36, l:52},
  legend:{orientation:'h', y:-0.18, font:{size:11}},
  xaxis:{tickfont:{size:11}},
};

const C = {
  teal:'#0B6E5D', amber:'#C07B12', blue:'#1A5FAD', purple:'#6D3DB2',
  red:'#B23A2E', grey:'#A0AAB4',
  teal_a:'rgba(11,110,93,.18)', amber_a:'rgba(192,123,18,.18)',
};

// ── 搜索 ──
async function doSearch() {
  const q = $('#q').value.trim();
  candsEl.innerHTML = ''; candsEl.className = '';
  outEl.style.display = 'none'; footEl.style.display = 'none';
  if (!q) return;
  appEl.classList.add('searched');
  if (/^\d{6}$/.test(q)) { analyze(q); return; }
  setState('正在搜索…');
  try {
    const list = await (await fetch('/api/search?q=' + encodeURIComponent(q))).json();
    setState('');
    if (!list.length) { setState('没找到匹配的股票，换个关键词或直接输入 6 位代码。', true); return; }
    if (list.length === 1) { analyze(list[0].code); return; }
    candsEl.className = 'candidates';
    candsEl.innerHTML = list.map(s =>
      `<div class="cand" data-code="${s.code}">
         <span class="nm">${s.name}</span>
         <span class="cd">${s.code}</span>
       </div>`).join('');
    candsEl.querySelectorAll('.cand').forEach(el =>
      el.onclick = () => { candsEl.innerHTML=''; candsEl.className=''; analyze(el.dataset.code); });
  } catch(e) { setState('搜索失败，请检查后端是否运行。', true); }
}

async function analyze(code) {
  outEl.style.display = 'none'; footEl.style.display = 'none';
  setState('正在抓取财报数据与估值数据，约需 20–40 秒…');
  try {
    const data = await (await fetch('/api/analyze?code=' + code)).json();
    if (data.error) { setState(data.error, true); return; }
    render(data);
    setState('');
  } catch(e) { setState('抓取失败，数据源可能临时不可用，请重试。', true); }
}

function render(data) {
  renderHeader(data.info);
  renderPerformance(data.performance);
  renderAttribution(data.attribution);
  renderRisk(data.risk);
  renderValuation(data.valuation, data.info);
  outEl.style.display = 'block';
  footEl.style.display = 'block';
}

function renderHeader(info) {
  $('#o-name').textContent = info.name;
  $('#o-code').textContent = info.code;
  const price = info.price == null ? '—' : '¥' + Number(info.price).toFixed(2);
  $('#o-meta').innerHTML = `现价 <b>${price}</b>　·　${info.industry || '—'}`;
}

// Tab 切换
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('#' + btn.dataset.tab).classList.add('active');
    window.dispatchEvent(new Event('resize'));
  });
});

// ── Tab 1：业绩检验 ──
function renderPerformance(perf) {
  if (!perf || !perf.periods || !perf.periods.length) return;
  const xLabels = perf.periods.map(fmtPeriod);
  const rev  = perf.revenue.map(yi), prof = perf.net_profit.map(yi);
  const gm = perf.gross_margin.map(pct), nm = perf.net_margin.map(pct);
  const roe = perf.roe.map(pct);
  const ryoy = perf.revenue_yoy.map(pct), pyoy = perf.profit_yoy.map(pct);

  const kpis = [
    {tag:'营业收入', val: rev[0]==null?'—':rev[0].toFixed(1), unit:'亿元'},
    {tag:'归母净利润', val: prof[0]==null?'—':prof[0].toFixed(1), unit:'亿元'},
    {tag:'毛利率', val: gm[0]==null?'—':gm[0].toFixed(1), unit:'%'},
    {tag:'净利率', val: nm[0]==null?'—':nm[0].toFixed(1), unit:'%'},
    {tag:'ROE', val: roe[0]==null?'—':roe[0].toFixed(1), unit:'%'},
  ];
  $('#kpi-row').innerHTML = kpis.map(k =>
    `<div class="kpi-card"><div class="ktag">${k.tag}</div>
     <div class="kval">${k.val}<span class="kunit">${k.unit}</span></div></div>`).join('');

  Plotly.newPlot('ch-rev-profit', [
    {name:'营业收入', type:'bar', x:xLabels, y:rev, marker:{color:C.teal_a, line:{color:C.teal,width:1.5}}},
    {name:'净利润', type:'bar', x:xLabels, y:prof, marker:{color:C.amber_a, line:{color:C.amber,width:1.5}}},
    {name:'营收 YoY', type:'scatter', mode:'lines+markers', x:xLabels, y:ryoy,
      line:{color:C.teal,width:2}, marker:{size:5}, yaxis:'y2'},
    {name:'利润 YoY', type:'scatter', mode:'lines+markers', x:xLabels, y:pyoy,
      line:{color:C.amber,width:2,dash:'dot'}, marker:{size:5}, yaxis:'y2'},
  ], {...LAYOUT_BASE, barmode:'group',
    yaxis:{title:'亿元',gridcolor:'#F0F2F4'},
    yaxis2:{title:'增速 %',overlaying:'y',side:'right',showgrid:false,ticksuffix:'%'},
  }, PLOTLY_CFG);

  Plotly.newPlot('ch-margins', [
    {name:'毛利率', type:'scatter', mode:'lines+markers', x:xLabels, y:gm, line:{color:C.teal,width:2.5}, marker:{size:6}},
    {name:'净利率', type:'scatter', mode:'lines+markers', x:xLabels, y:nm, line:{color:C.amber,width:2.5}, marker:{size:6}},
    {name:'ROE',    type:'scatter', mode:'lines+markers', x:xLabels, y:roe, line:{color:C.blue,width:2.5}, marker:{size:6}},
  ], {...LAYOUT_BASE, yaxis:{title:'%',gridcolor:'#F0F2F4',ticksuffix:'%'}}, PLOTLY_CFG);

  const cm = perf.cash_match;
  if (cm && cm.periods && cm.periods.length) {
    const xl = cm.periods.map(fmtPeriod);
    renderCashMatch('ch-cm1', xl, cm.revenue.map(yi), cm.sales_cash.map(yi), cm.rev_ratio, '营业收入','销售收到现金');
    renderCashMatch('ch-cm2', xl, cm.cost.map(yi), cm.purchase_cash.map(yi), cm.cost_ratio, '营业成本','购买支付现金');
    renderCashMatch('ch-cm3', xl, cm.net_profit.map(yi), cm.ocf.map(yi), cm.profit_ratio, '净利润','经营现金流');
  }
}

function renderCashMatch(divId, xl, a, b, ratio, nameA, nameB) {
  Plotly.newPlot(divId, [
    {name:nameA, type:'bar', x:xl, y:a, marker:{color:C.teal_a, line:{color:C.teal,width:1.5}}},
    {name:nameB, type:'bar', x:xl, y:b, marker:{color:C.amber_a, line:{color:C.amber,width:1.5}}},
    {name:'比值', type:'scatter', mode:'lines+markers', x:xl, y:ratio,
      line:{color:C.red,width:2}, marker:{size:5}, yaxis:'y2'},
  ], {...LAYOUT_BASE, margin:{t:10,r:40,b:36,l:44}, barmode:'group',
    yaxis:{title:'亿元',gridcolor:'#F0F2F4'},
    yaxis2:{title:'比值',overlaying:'y',side:'right',showgrid:false},
    legend:{orientation:'h',y:-0.22,font:{size:10}},
  }, PLOTLY_CFG);
}

// ── Tab 2：业绩归因 ──
function renderAttribution(attr) {
  if (!attr || !attr.periods || !attr.periods.length) return;
  const xl = attr.periods.map(fmtPeriod);
  const nm = attr.net_margin.map(pct), at = attr.asset_turnover, em = attr.equity_mult;
  const roe = attr.roe.map(pct);

  Plotly.newPlot('ch-dupont', [
    {name:'净利率 %', type:'scatter', mode:'lines+markers', x:xl, y:nm, line:{color:C.teal,width:2.5}, marker:{size:6}},
    {name:'权益乘数 ×', type:'scatter', mode:'lines+markers', x:xl, y:em, line:{color:C.purple,width:2.5}, marker:{size:6}},
    {name:'资产周转 ×', type:'scatter', mode:'lines+markers', x:xl, y:at, line:{color:C.amber,width:2.5,dash:'dot'}, marker:{size:6}, yaxis:'y2'},
    {name:'ROE %', type:'scatter', mode:'lines+markers', x:xl, y:roe, line:{color:C.red,width:3}, marker:{size:7,symbol:'star'}},
  ], {...LAYOUT_BASE,
    yaxis:{title:'% 或 ×',gridcolor:'#F0F2F4'},
    yaxis2:{title:'总资产周转（×）',overlaying:'y',side:'right',showgrid:false},
  }, PLOTLY_CFG);

  const rows = xl.map((yr,i) => [yr, fmtP(nm[i]), fmtX(at[i]), fmtX(em[i]), fmtP(roe[i])]);
  const thead = `<tr>${['期间','净利率','总资产周转','权益乘数','ROE（验证）'].map(h=>`<th>${h}</th>`).join('')}</tr>`;
  const tbody = rows.map(r => `<tr>${r.map((v,i)=>`<td ${i===0?'style="text-align:left"':''}>${v}</td>`).join('')}</tr>`).join('');
  $('#dupont-table').innerHTML = `<thead>${thead}</thead><tbody>${tbody}</tbody>`;

  Plotly.newPlot('ch-expense', [
    {name:'销售费用率', type:'scatter', mode:'lines+markers', x:xl, y:attr.selling_rate.map(pct), line:{color:C.teal,width:2}, marker:{size:5}},
    {name:'管理费用率', type:'scatter', mode:'lines+markers', x:xl, y:attr.admin_rate.map(pct), line:{color:C.amber,width:2}, marker:{size:5}},
    {name:'研发费用率', type:'scatter', mode:'lines+markers', x:xl, y:attr.rd_rate.map(pct), line:{color:C.blue,width:2}, marker:{size:5}},
    {name:'财务费用率', type:'scatter', mode:'lines+markers', x:xl, y:attr.finance_rate.map(pct), line:{color:C.grey,width:2,dash:'dot'}, marker:{size:5}},
  ], {...LAYOUT_BASE, yaxis:{title:'%',gridcolor:'#F0F2F4',ticksuffix:'%'}}, PLOTLY_CFG);
}

// ── Tab 3：验证排雷 ──
function renderRisk(risk) {
  if (!risk) return;
  const dash = risk.dashboard || [];
  $('#dash-grid').innerHTML = dash.map(d => {
    const valStr = d.value == null ? '—' : String(d.value);
    return `<div class="dash-card ${d.status}">
      <div class="d-head"><span class="d-dot ${d.status}"></span><span class="d-name">${d.name}</span></div>
      <div class="d-val">${valStr}</div><div class="d-unit">${d.unit}</div>
      <div class="d-desc">${d.desc}</div><div class="d-signal">${d.signal}</div>
    </div>`;
  }).join('');

  const tp = (risk.trend_periods||[]).map(fmtPeriod);
  const rvy = (risk.rev_yoy||[]).map(pct), ary = (risk.ar_yoy||[]).map(pct), ivy = (risk.inv_yoy||[]).map(pct);
  if (tp.length) {
    Plotly.newPlot('ch-ar', [
      {name:'应收账款增速', type:'bar', x:tp, y:ary, marker:{color:C.amber_a, line:{color:C.amber,width:1.5}}},
      {name:'营收增速', type:'bar', x:tp, y:rvy, marker:{color:C.teal_a, line:{color:C.teal,width:1.5}}},
    ], {...LAYOUT_BASE, barmode:'group', yaxis:{title:'%',gridcolor:'#F0F2F4',ticksuffix:'%'}}, PLOTLY_CFG);
    Plotly.newPlot('ch-inv', [
      {name:'存货增速', type:'bar', x:tp, y:ivy, marker:{color:C.purple+'30', line:{color:C.purple,width:1.5}}},
      {name:'营收增速', type:'bar', x:tp, y:rvy, marker:{color:C.teal_a, line:{color:C.teal,width:1.5}}},
    ], {...LAYOUT_BASE, barmode:'group', yaxis:{title:'%',gridcolor:'#F0F2F4',ticksuffix:'%'}}, PLOTLY_CFG);
  }
}

// ── Tab 4：估值分析 ──
function renderValuation(val, info) {
  if (!val) return;

  // 类型徽章
  const isCyc = val.is_cyclical;
  $('#val-type').innerHTML = `<span class="type-badge ${isCyc?'cyclical':'normal'}">${val.stock_type}</span>`
    + (isCyc && val.cycle_position ? `<span style="margin-left:10px;font-family:var(--mono);font-size:13px;color:var(--muted)">周期位置：<b style="color:var(--ink)">${val.cycle_position}</b></span>` : '')
    + (val.current.pe ? `<span style="margin-left:16px;font-family:var(--mono);font-size:13px;color:var(--muted)">PE ${val.current.pe.toFixed(1)}x</span>` : '')
    + (val.current.pb ? `<span style="margin-left:10px;font-family:var(--mono);font-size:13px;color:var(--muted)">PB ${val.current.pb.toFixed(2)}x</span>` : '');

  // 矩阵卡片
  const mx = val.matrix || [];
  $('#val-matrix').innerHTML = mx.map(m => {
    const cls = m.positive === true ? 'pos' : (m.positive === false ? 'neg' : 'na');
    const valStr = m.value == null ? '—' : String(m.value);
    return `<div class="mx-card ${cls}">
      <div class="mx-name">${m.name}</div>
      <div class="mx-val">${valStr}</div>
      <div class="mx-bench">${m.benchmark}</div>
      <div class="mx-desc">${m.desc}</div>
    </div>`;
  }).join('');

  // 信号汇总
  const pc = val.positive_count || 0;
  const total = mx.length;
  const verd = val.matrix_verdict || '';
  const cls = pc >= 5 ? 'strong' : (pc >= 3 ? 'neutral' : 'weak');
  $('#val-signal').innerHTML = `<div class="signal-bar ${cls}">
    ${pc} / ${total} 正面信号　→　${verd}
  </div>`;

  // PE/PB 历史图
  const hist = val.history || {};
  if (hist.dates && hist.dates.length) {
    const usePB = isCyc;
    const yData = usePB ? hist.pb : hist.pe;
    const yLabel = usePB ? 'PB' : 'PE-TTM';
    const med = usePB ? hist.pb_median : hist.pe_median;
    const p25 = usePB ? hist.pb_p25 : hist.pe_p25;
    const p75 = usePB ? hist.pb_p75 : hist.pe_p75;
    const pctl = usePB ? hist.pb_percentile : hist.pe_percentile;

    $('#ch-hist-title').textContent = yLabel + ' 历史走势' + (pctl != null ? `（当前分位 ${pctl}%）` : '');

    const traces = [
      {name: yLabel, type:'scatter', mode:'lines', x:hist.dates, y:yData,
        line:{color:C.teal, width:1.5}, fill:'none'},
    ];
    const shapes = [];
    if (med != null) shapes.push({type:'line', y0:med, y1:med, x0:0, x1:1, xref:'paper',
      line:{color:C.amber, width:1.5, dash:'dash'}, label:{text:`中位数 ${med}`, font:{size:10}}});
    if (p25 != null) shapes.push({type:'line', y0:p25, y1:p25, x0:0, x1:1, xref:'paper',
      line:{color:C.grey, width:1, dash:'dot'}});
    if (p75 != null) shapes.push({type:'line', y0:p75, y1:p75, x0:0, x1:1, xref:'paper',
      line:{color:C.grey, width:1, dash:'dot'}});

    // 中间 50% 阴影
    if (p25 != null && p75 != null) {
      traces.push({
        name:'25–75%', type:'scatter', mode:'none', x:[hist.dates[0], hist.dates[hist.dates.length-1], hist.dates[hist.dates.length-1], hist.dates[0]],
        y:[p25, p25, p75, p75], fill:'toself', fillcolor:'rgba(11,110,93,0.07)', line:{width:0}, showlegend:false,
      });
    }

    // 当前值水平线
    const curVal = usePB ? val.current.pb : val.current.pe;
    if (curVal != null) shapes.push({type:'line', y0:curVal, y1:curVal, x0:0, x1:1, xref:'paper',
      line:{color:C.red, width:1.5}});

    Plotly.newPlot('ch-pe-hist', traces, {
      ...LAYOUT_BASE, margin:{t:10,r:10,b:36,l:52},
      yaxis:{title:yLabel, gridcolor:'#F0F2F4'},
      xaxis:{tickfont:{size:10}},
      shapes: shapes,
      annotations: curVal != null ? [{x:1, xref:'paper', y:curVal, text:`当前 ${curVal.toFixed(1)}`,
        showarrow:false, font:{size:10, color:C.red}, xanchor:'right'}] : [],
    }, PLOTLY_CFG);
  }

  // 三角验证表
  const tri = val.triangulation || {};
  const methods = tri.methods || [];
  if (methods.length) {
    const curPrice = val.current.price;
    const thead = `<tr><th style="text-align:left">方法</th><th>使用倍数</th><th>目标价（元）</th></tr>`;
    const tbody = methods.map(m =>
      `<tr><td style="text-align:left">${m.name}</td>
           <td>${m.pe_used != null ? m.pe_used + 'x' : (m.mult != null ? m.mult + 'x' : '—')}</td>
           <td>¥${m.target}</td></tr>`
    ).join('');
    const summary = `<tr style="background:#F4F6F8;font-weight:600">
      <td style="text-align:left">估值锚区间</td>
      <td>—</td>
      <td>¥${tri.v_low} — ¥${tri.v_high}</td></tr>`;
    $('#val-tri').innerHTML = `<thead>${thead}</thead><tbody>${tbody}${summary}</tbody>`;

    // 偏离度卡片
    if (tri.deviation != null) {
      const up = tri.deviation >= 0;
      $('#val-dev').innerHTML = `<div class="dev-card">
        <div><div class="dev-label">当前价 ¥${curPrice ? curPrice.toFixed(2) : '—'}　vs　估值锚中位 ¥${tri.v_mid}</div></div>
        <div class="dev-val ${up?'up':'down'}">${up?'+':''}${tri.deviation}%</div>
        <div style="font-size:13px;color:var(--muted);margin-left:8px">${up ? '低估空间' : '高估风险'}</div>
      </div>`;
    }
  }

  // 情景分析
  const scen = val.scenario || [];
  if (scen.length) {
    $('#val-scen').innerHTML = scen.map(s => {
      const up = s.space != null && s.space >= 0;
      const icons = {'悲观':'📉', '中性':'📊', '乐观':'📈',
                     '悲观（周期下行）':'📉', '乐观（周期复苏）':'📈'};
      return `<div class="scen-card">
        <div class="sc-label">${icons[s.label]||''} ${s.label}</div>
        <div class="sc-target">¥${s.target}</div>
        <div class="sc-space ${up?'up':'down'}">${s.space != null ? ((up?'+':'') + s.space + '%') : '—'}</div>
        ${s.eps ? `<div class="sc-note">调整后 EPS ${s.eps}</div>` : ''}
      </div>`;
    }).join('');
  }
}

// ── 事件绑定 ──
$('#go').onclick = doSearch;
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
