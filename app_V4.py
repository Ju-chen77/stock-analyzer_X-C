# -*- coding: utf-8 -*-
"""
app_V4.py — A 股四段式财务分析
四段式：业绩检验 → 业绩归因 → 验证排雷 → PE/PB 估值区间
数据源：AKShare（免费，无需注册）
启动后访问：http://127.0.0.1:5000
"""

import os
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import math
import akshare as ak
import requests as _req
import pandas as pd
from flask import Flask, request, jsonify, render_template

PROJECT_NAME = "三段式财报透视"
PROJECT_DESC = "业绩检验 · 业绩归因 · 验证排雷"

app = Flask(__name__)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ═══════════════════════════════════════════════════════════════
# 初始化：股票代码表
# ═══════════════════════════════════════════════════════════════
def _load_code_name():
    try:
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            if len(df.columns) == 2:
                df.columns = ["code", "name"]
            print(f"[启动] 已加载 {len(df)} 只 A 股")
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
    """safe_float：失败或 NaN 返回 None"""
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None

def sdiv(a, b):
    """safe_divide：分母为 0 / None 返回 None"""
    if a is None or b is None or b == 0:
        return None
    try:
        return float(a) / float(b)
    except Exception:
        return None

def fcol(df, *candidates):
    """在 DataFrame 中找第一个包含候选关键字的列名（精确优先，再做子串匹配）"""
    for c in candidates:
        # 精确匹配
        if c in df.columns:
            return c
    for c in candidates:
        # 子串匹配
        col = next((col for col in df.columns if c in str(col).strip()), None)
        if col:
            return col
    return None

def col_vals(df, col, periods):
    """按 periods 顺序从 df 提取列值（_date 已设置）"""
    if col is None or df is None:
        return [None] * len(periods)
    d = dict(zip(df["_date"].astype(str).tolist(), df[col].tolist()))
    return [sf(d.get(p)) for p in periods]

def yoy_list(lst):
    """
    对降序列表计算每期 YoY 增速。
    lst[0] 是最新期，lst[1] 是上一年，以此类推。
    返回同长度列表，最后一期无法计算为 None。
    """
    result = []
    for i in range(len(lst)):
        if i + 1 < len(lst) and lst[i] is not None and lst[i + 1] is not None and lst[i + 1] != 0:
            result.append((lst[i] - lst[i + 1]) / abs(lst[i + 1]))
        else:
            result.append(None)
    return result

def _set_date(df):
    """
    提取日期列，写入 _date（8位纯数字字符串），并按降序排列。
    兼容两种格式：'20161231' 和 '2016-12-31'（后者先去掉横线）。
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    date_col = fcol(df, "报表日期", "报告日")
    if date_col:
        df["_date"] = df[date_col].astype(str).str.replace("-", "", regex=False).str[:8]
        df = df.sort_values("_date", ascending=False).reset_index(drop=True)
    return df

def annual_periods(df, n=6):
    """从 df 中取最近 n 个年报期（以 1231 结尾）"""
    if df is None or "_date" not in df.columns:
        return []
    return df[df["_date"].str.endswith("1231")]["_date"].tolist()[:n]


def _get_equity(balance, periods):
    """
    提取股东权益，兼容多种列名。
    找不到直接列时用「资产总计 − 负债合计」反推，确保杜邦和 ROE 不因列名问题缺失。
    """
    # 尝试多种新浪/东财列名写法
    eq_col = fcol(balance,
                  "所有者权益(或股东权益)合计",
                  "归属于母公司所有者权益合计",
                  "所有者权益合计",
                  "股东权益合计",
                  "所有者权益",
                  "股东权益")
    if eq_col:
        return col_vals(balance, eq_col, periods)

    # 兜底：资产 − 负债
    assets_col = fcol(balance, "资产总计")
    liab_col   = fcol(balance, "负债合计")
    if assets_col and liab_col:
        assets = col_vals(balance, assets_col, periods)
        liabs  = col_vals(balance, liab_col,   periods)
        return [a - l if (a is not None and l is not None) else None
                for a, l in zip(assets, liabs)]

    return [None] * len(periods)


# ═══════════════════════════════════════════════════════════════
# 数据抓取层
# ═══════════════════════════════════════════════════════════════
def fetch_raw(code):
    """
    抓取三大报表原始 DataFrame（AKShare → 新浪财经）。
    Returns: dict {income, balance, cashflow}
    """
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
# 主营构成（按产品）— East Money API
# ═══════════════════════════════════════════════════════════════
def _fetch_segment_revenue(code):
    """
    从东方财富获取主营构成（按产品），仅取年报数据。
    Returns: dict {periods: [...], segments: [{name, revenue, cost, gross_margin}, ...]}
    按年份分组，每年的各产品线收入、成本、毛利率。
    """
    try:
        prefix = "SH" if code.startswith(("6", "9")) else "SZ"
        url = f"https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax?code={prefix}{code}"
        r = _req.get(url, timeout=15,
                     headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://emweb.securities.eastmoney.com"})
        data = r.json()
        items = data.get("zygcfx", [])
        if not items:
            return {}

        # 筛选：按产品(MAINOP_TYPE=2)、年报(含12-31)
        annual = [it for it in items
                  if it.get("MAINOP_TYPE") == "2"
                  and "12-31" in str(it.get("REPORT_DATE", ""))]
        if not annual:
            return {}

        # 按报告期分组
        from collections import defaultdict
        by_year = defaultdict(list)
        for it in annual:
            yr = str(it["REPORT_DATE"])[:4]
            by_year[yr].append({
                "name":         it.get("ITEM_NAME", ""),
                "revenue":      sf(it.get("MAIN_BUSINESS_INCOME")),
                "cost":         sf(it.get("MAIN_BUSINESS_COST")),
                "gross_margin": sf(it.get("GROSS_RPOFIT_RATIO")),
            })

        periods = sorted(by_year.keys(), reverse=True)[:5]
        return {"periods": periods, "by_year": {yr: by_year[yr] for yr in periods}}
    except Exception as e:
        print(f"[ERR] segment revenue: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════
# 第一段：业绩检验
# ═══════════════════════════════════════════════════════════════
def compute_performance(raw):
    """
    Part A：常规盈利指标（5 年时间序列）
    Part B：利润表 vs 现金流量表 三组对照
    Returns: dict
    """
    income   = raw.get("income")
    cashflow = raw.get("cashflow")
    balance  = raw.get("balance")

    if income is None:
        return {}

    periods = annual_periods(income, 6)
    if not periods:
        return {}

    # ── Part A ──────────────────────────────────────────
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

    # ROE = 净利润 / 平均净资产
    roe = []
    if balance is not None:
        equity = _get_equity(balance, periods)
        for i, (p, e) in enumerate(zip(net_profit, equity)):
            if i + 1 < len(equity) and equity[i + 1] is not None and e is not None:
                roe.append(sdiv(p, (e + equity[i + 1]) / 2))
            else:
                roe.append(sdiv(p, e))
    else:
        roe = [None] * len(periods)

    # ── Part B：三组现金流对照 ──────────────────────────
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
            "cost":          cost_c,
            "purchase_cash": purchase_cash,
            "net_profit":    profit_c,
            "ocf":           ocf,
        }

    # ── Part D / E：资产 & 负债堆积图 ──────────────────────
    asset_stack = {}
    liability_stack = {}
    if balance is not None:
        bs_periods = annual_periods(balance, 6)
        # 资产项
        asset_items = [
            ("货币资金",         "货币资金"),
            ("存货",             "存货"),
            ("应收票据及应收账款", "应收票据及应收账款", "应收账款"),
            ("合同资产",         "合同资产"),
            ("预付款项",         "预付款项"),
        ]
        asset_data = {}
        for item_def in asset_items:
            label = item_def[0]
            col = fcol(balance, *item_def[1:])
            asset_data[label] = col_vals(balance, col, bs_periods)
        asset_stack = {"periods": bs_periods, "items": asset_data}

        # 负债项
        liab_items = [
            ("合同负债/预收款项",  "合同负债", "预收款项"),
            ("应付票据及应付账款", "应付票据及应付账款", "应付账款"),
            ("应付职工薪酬",      "应付职工薪酬"),
            ("短期借款",          "短期借款"),
            ("长期借款",          "长期借款"),
        ]
        liab_data = {}
        for item_def in liab_items:
            label = item_def[0]
            col = fcol(balance, *item_def[1:])
            liab_data[label] = col_vals(balance, col, bs_periods)
        liability_stack = {"periods": bs_periods, "items": liab_data}

    return {
        "periods":          periods,
        "revenue":          revenue,
        "net_profit":       net_profit,
        "gross_margin":     gross_margin,
        "net_margin":       net_margin,
        "roe":              roe,
        "revenue_yoy":      revenue_yoy,
        "profit_yoy":       profit_yoy,
        "cash_match":       cash_match,
        "asset_stack":      asset_stack,
        "liability_stack":  liability_stack,
    }


# ═══════════════════════════════════════════════════════════════
# 第二段：业绩归因
# ═══════════════════════════════════════════════════════════════
def compute_attribution(raw):
    """
    杜邦三因子分解 + 四项费用率趋势
    ROE = 净利率 × 总资产周转率 × 权益乘数
    Returns: dict
    """
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

    revenue    = col_vals(income,  rev_col,    periods)
    net_profit = col_vals(income,  profit_col, periods)
    assets     = col_vals(balance, assets_col, periods)
    equity     = _get_equity(balance, periods)

    # 杜邦三因子
    net_margin     = [sdiv(p, r) for p, r in zip(net_profit, revenue)]
    asset_turnover = [sdiv(r, a) for r, a in zip(revenue, assets)]
    equity_mult    = [sdiv(a, e) for a, e in zip(assets, equity)]
    roe_dupont = [
        m * t * eq if (m and t and eq) else None
        for m, t, eq in zip(net_margin, asset_turnover, equity_mult)
    ]

    # 四项费用率
    def expense_rate(col_key):
        col = fcol(income, col_key)
        vals = col_vals(income, col, periods)
        return [sdiv(v, r) for v, r in zip(vals, revenue)]

    return {
        "periods":       periods,
        "net_margin":    net_margin,
        "asset_turnover": asset_turnover,
        "equity_mult":   equity_mult,
        "roe":           roe_dupont,
        "selling_rate":  expense_rate("销售费用"),
        "admin_rate":    expense_rate("管理费用"),
        "rd_rate":       expense_rate("研发费用"),
        "finance_rate":  expense_rate("财务费用"),
    }


# ═══════════════════════════════════════════════════════════════
# 第三段：验证排雷
# ═══════════════════════════════════════════════════════════════
def _traffic(value, green_thr, yellow_thr, higher_is_better=True):
    """红绿灯判断"""
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
    """
    五项排雷指标 + 应收/存货增速趋势辅助图
    Returns: dict {dashboard, trend_periods, ar_yoy, inv_yoy, rev_yoy}
    """
    income   = raw.get("income")
    balance  = raw.get("balance")
    cashflow = raw.get("cashflow")

    if income is None or balance is None:
        return {"dashboard": []}

    periods = annual_periods(income, 6)
    if not periods:
        return {"dashboard": []}

    # 提取各列
    rev_col    = fcol(income,  "营业收入")
    profit_col = fcol(income,  "归属于母公司所有者的净利润", "净利润")
    ar_col     = fcol(balance, "应收账款")
    inv_col    = fcol(balance, "存货")
    gw_col     = fcol(balance, "商誉")
    assets_col = fcol(balance, "资产总计")
    liab_col   = fcol(balance, "负债合计")

    revenue    = col_vals(income,  rev_col,    periods)
    net_profit = col_vals(income,  profit_col, periods)
    ar         = col_vals(balance, ar_col,     periods)
    inventory  = col_vals(balance, inv_col,    periods)
    goodwill   = col_vals(balance, gw_col,     periods)
    assets     = col_vals(balance, assets_col, periods)
    liabilities= col_vals(balance, liab_col,   periods)
    equity     = _get_equity(balance, periods)

    # OCF（对齐年报期）
    ocf = [None] * len(periods)
    if cashflow is not None:
        ocf_col = fcol(cashflow, "经营活动产生的现金流量净额", "经营活动产生")
        if ocf_col:
            cf_map = dict(zip(cashflow["_date"].astype(str).tolist(), cashflow[ocf_col].tolist()))
            ocf = [sf(cf_map.get(p)) for p in periods]

    dashboard = []

    # ① 利润含金量
    pcq = sdiv(ocf[0], net_profit[0]) if ocf and net_profit else None
    dashboard.append({
        "name":   "利润含金量",
        "value":  round(pcq, 2) if pcq is not None else None,
        "unit":   "×",
        "status": _traffic(pcq, 0.8, 0.5, higher_is_better=True),
        "desc":   "经营现金流 / 净利润",
        "signal": "≥0.8 健康 ｜ 0.5–0.8 关注 ｜ <0.5 警示",
    })

    # ② 收入质量：应收增速 − 营收增速
    rev_yoy0 = sdiv(revenue[0] - revenue[1], abs(revenue[1])) if len(revenue) > 1 and revenue[0] and revenue[1] else None
    ar_yoy0  = sdiv(ar[0] - ar[1],           abs(ar[1]))       if len(ar) > 1 and ar[0] and ar[1] else None
    ar_gap   = (ar_yoy0 - rev_yoy0) if (ar_yoy0 is not None and rev_yoy0 is not None) else None
    dashboard.append({
        "name":   "收入质量",
        "value":  round(ar_gap * 100, 1) if ar_gap is not None else None,
        "unit":   "pp（应收增速−营收增速）",
        "status": _traffic(ar_gap, 0.0, 0.1, higher_is_better=False) if ar_gap is not None else "grey",
        "desc":   "应收账款增速 vs 营收增速差值",
        "signal": "<0 健康 ｜ 0–10pp 关注 ｜ >10pp 警示",
    })

    # ③ 库存压力：存货增速 − 营收增速
    inv_yoy0 = sdiv(inventory[0] - inventory[1], abs(inventory[1])) if len(inventory) > 1 and inventory[0] and inventory[1] else None
    inv_gap  = (inv_yoy0 - rev_yoy0) if (inv_yoy0 is not None and rev_yoy0 is not None) else None
    dashboard.append({
        "name":   "库存压力",
        "value":  round(inv_gap * 100, 1) if inv_gap is not None else None,
        "unit":   "pp（存货增速−营收增速）",
        "status": _traffic(inv_gap, 0.05, 0.15, higher_is_better=False) if inv_gap is not None else "grey",
        "desc":   "存货增速 vs 营收增速差值",
        "signal": "<5pp 健康 ｜ 5–15pp 关注 ｜ >15pp 警示",
    })

    # ④ 商誉风险：商誉 / 净资产
    gw_ratio = sdiv(goodwill[0], equity[0]) if goodwill[0] is not None and equity and equity[0] else None
    dashboard.append({
        "name":   "商誉风险",
        "value":  round(gw_ratio * 100, 1) if gw_ratio is not None else None,
        "unit":   "%（商誉 / 净资产）",
        "status": _traffic(gw_ratio, 0.1, 0.3, higher_is_better=False) if gw_ratio is not None else "grey",
        "desc":   "商誉 / 净资产",
        "signal": "<10% 健康 ｜ 10–30% 关注 ｜ >30% 警示",
    })

    # ⑤ 偿债风险：资产负债率
    dr = sdiv(liabilities[0], assets[0]) if liabilities and assets and liabilities[0] and assets[0] else None
    dashboard.append({
        "name":   "偿债风险",
        "value":  round(dr * 100, 1) if dr is not None else None,
        "unit":   "%（资产负债率）",
        "status": _traffic(dr, 0.5, 0.7, higher_is_better=False),
        "desc":   "负债合计 / 资产总计（制造/消费行业参考）",
        "signal": "<50% 健康 ｜ 50–70% 关注 ｜ >70% 警示",
    })

    # 辅助趋势数据（应收 & 存货 & 营收增速，用于图表）
    trend_periods = periods[:-1]
    rev_yoys = yoy_list(revenue)[:-1]
    ar_yoys  = yoy_list(ar)[:-1]
    inv_yoys = yoy_list(inventory)[:-1]

    return {
        "dashboard":      dashboard,
        "trend_periods":  trend_periods,
        "rev_yoy":        rev_yoys,
        "ar_yoy":         ar_yoys,
        "inv_yoy":        inv_yoys,
    }


# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# 第四段：PE / PB 估值
# ═══════════════════════════════════════════════════════════════
def _get_pe_history_ak(code):
    """
    用 akshare 日线（新浪源）重采样为月线 + 财务指标计算历史 PE/PB。
    Returns: DataFrame [date, pe, pb]
    """
    try:
        daily = ak.stock_zh_a_daily(symbol=to_sina_code(code), adjust="")
        if daily is None or daily.empty:
            return None
        daily["dt"] = pd.to_datetime(daily["date"], errors="coerce")
        daily = daily.dropna(subset=["dt", "close"])
        daily = daily[daily["dt"] >= pd.Timestamp("2019-01-01")]
        freq = "ME" if hasattr(pd.tseries.offsets, "MonthEnd") else "M"
        try:
            monthly = daily.set_index("dt").resample(freq)["close"].last().reset_index()
        except ValueError:
            monthly = daily.set_index("dt").resample("M")["close"].last().reset_index()
        monthly["date"] = monthly["dt"].dt.strftime("%Y%m%d")
        price_df = monthly[["date", "close"]].dropna()
        if price_df.empty:
            return None
    except Exception as e:
        print(f"[ERR] 月线价格: {e}")
        return None

    try:
        ind_df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2019")
        if ind_df is None or ind_df.empty:
            return None
        ind_df["_d"] = ind_df["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        ann = ind_df[ind_df["_d"].str.endswith("1231")].copy()
        eps_col  = "摊薄每股收益(元)"
        bvps_col = "每股净资产_调整前(元)"
        if eps_col not in ann.columns:
            return None
        ann = ann[["_d", eps_col, bvps_col]].copy()
        ann[eps_col]  = pd.to_numeric(ann[eps_col],  errors="coerce")
        ann[bvps_col] = pd.to_numeric(ann[bvps_col], errors="coerce")
        ann = ann.sort_values("_d")
    except Exception as e:
        print(f"[ERR] 财务指标: {e}")
        return None

    rows = []
    for _, row in price_df.iterrows():
        d     = row["date"]
        close = sf(row["close"])
        if close is None or close <= 0:
            continue
        past = ann[ann["_d"] <= d]
        if past.empty:
            continue
        last     = past.iloc[-1]
        eps_val  = sf(last[eps_col])
        bvps_val = sf(last[bvps_col])
        pe = sdiv(close, eps_val)  if (eps_val  and eps_val  > 0) else None
        pb = sdiv(close, bvps_val) if (bvps_val and bvps_val > 0) else None
        rows.append({"date": d, "pe": pe, "pb": pb})

    if not rows:
        return None
    return pd.DataFrame(rows)


def _get_pe_history(code):
    """akshare 月线 + 财务指标计算历史 PE/PB"""
    return _get_pe_history_ak(code)


def compute_valuation(code, raw, price):
    """
    PE/PB 估值模块：
      - 历史 PE/PB 序列 + 当前分位数
      - PE 六维矩阵信号
      - 三角验证（历史分位法 / PEG 法 / 历史均值法）
      - 三档情景（悲观 / 中性 / 乐观）
    Returns: dict
    """
    income = raw.get("income")
    if income is None or price is None:
        return {}

    # ── 历史 PE/PB 序列 ────────────────────────────────────
    pe_hist_df = _get_pe_history(code)

    dates, pe_series, pb_series = [], [], []
    pe_current = pb_current = pe_median = pb_median = pe_pct = pb_pct = None

    if pe_hist_df is not None and not pe_hist_df.empty:
        tmp = pe_hist_df.sort_values("date")

        pe_vals = pd.to_numeric(tmp["pe"], errors="coerce").dropna()
        pe_vals = pe_vals[pe_vals > 0]
        if len(pe_vals) > 6:
            dates      = tmp.loc[pe_vals.index, "date"].tolist()
            pe_series  = pe_vals.tolist()
            pe_current = float(pe_vals.iloc[-1])
            pe_median  = float(pe_vals.median())
            pe_pct     = float((pe_vals < pe_current).sum() / len(pe_vals) * 100)

        pb_vals = pd.to_numeric(tmp["pb"], errors="coerce").dropna()
        pb_vals = pb_vals[pb_vals > 0]
        if len(pb_vals) > 6:
            pb_series  = pb_vals.tolist()
            pb_current = float(pb_vals.iloc[-1])
            pb_median  = float(pb_vals.median())
            pb_pct     = float((pb_vals < pb_current).sum() / len(pb_vals) * 100)

    # ── EPS / 增速（来自利润表）────────────────────────────
    periods    = annual_periods(income, 7)
    eps_col    = fcol(income, "基本每股收益", "每股收益")
    profit_col = fcol(income, "归属于母公司所有者的净利润", "净利润")

    eps_vals = col_vals(income, eps_col, periods) if eps_col else [None] * len(periods)
    eps_latest = next((e for e in eps_vals if e is not None and e > 0), None)

    # 3 年历史 EPS CAGR 作为增速代理
    eps_growth = None
    valid = [(i, e) for i, e in enumerate(eps_vals) if e is not None and e > 0]
    if len(valid) >= 4:
        e_new, e_old = valid[0][1], valid[3][1]
        if e_old > 0:
            eps_growth = (e_new / e_old) ** (1 / 3) - 1

    eps_forward = eps_latest * (1 + eps_growth) if (eps_latest and eps_growth is not None) else eps_latest

    pe_from_price = sdiv(price, eps_latest) if (eps_latest and eps_latest > 0) else None
    forward_pe    = sdiv(price, eps_forward) if (eps_forward and eps_forward > 0) else None
    peg           = sdiv(pe_from_price, (eps_growth or 0) * 100) if (pe_from_price and eps_growth and eps_growth > 0) else None

    # ── PE 六维矩阵信号 ─────────────────────────────────────
    def sig(positive_cond, neutral_cond=None):
        if positive_cond is None:
            return None
        if positive_cond:
            return "正面"
        if neutral_cond:
            return "中性"
        return "负面"

    matrix = [
        {
            "name":   "TTM PE vs 历史中位",
            "value":  f"{pe_current:.1f}× / 中位 {pe_median:.1f}×" if (pe_current and pe_median) else "N/A",
            "signal": sig(pe_current < pe_median if (pe_current and pe_median) else None),
        },
        {
            "name":   "历史 PE 分位",
            "value":  f"{pe_pct:.0f}%" if pe_pct is not None else "N/A",
            "signal": sig(pe_pct < 30 if pe_pct is not None else None,
                          pe_pct < 70 if pe_pct is not None else None),
        },
        {
            "name":   "Forward PE",
            "value":  f"{forward_pe:.1f}×" if forward_pe else "N/A",
            "signal": sig(forward_pe < pe_from_price if (forward_pe and pe_from_price) else None),
        },
        {
            "name":   "PEG",
            "value":  f"{peg:.2f}" if peg else "N/A",
            "signal": sig(peg < 1 if peg else None, peg < 1.5 if peg else None),
        },
        {
            "name":   "PB 历史分位",
            "value":  f"{pb_pct:.0f}%" if pb_pct is not None else "N/A",
            "signal": sig(pb_pct < 30 if pb_pct is not None else None,
                          pb_pct < 70 if pb_pct is not None else None),
        },
        {
            "name":   "EPS 3年增速",
            "value":  f"{eps_growth*100:.1f}%" if eps_growth else "N/A",
            "signal": sig(eps_growth > 0.1 if eps_growth else None,
                          eps_growth > 0   if eps_growth else None),
        },
    ]
    positive_count = sum(1 for m in matrix if m["signal"] == "正面")

    # ── 三角验证（三种目标价法）──────────────────────────────
    v1 = (pe_median  * eps_forward) if (pe_median and eps_forward)  else None  # 历史分位法
    v2 = ((eps_growth or 0) * 100 * eps_forward) if (eps_growth and eps_growth > 0 and eps_forward) else None  # PEG=1
    v3 = None  # 历史均值法（用 5 年内 25 分位 PE）
    if pe_series and eps_forward:
        pe25 = float(pd.Series(pe_series).quantile(0.25))
        v3 = pe25 * eps_forward if pe25 > 0 else None

    candidates = [v for v in [v1, v2, v3] if v is not None]
    v_low  = round(min(candidates), 2)  if candidates else None
    v_mid  = round(float(pd.Series(candidates).median()), 2) if candidates else None
    v_high = round(max(candidates), 2)  if candidates else None

    # ── 三档情景 ────────────────────────────────────────────
    scenarios = []
    if eps_forward and pe_median:
        for label, e_mult, pe_mult in [("悲观", 0.8, 0.8), ("中性", 1.0, 1.0), ("乐观", 1.2, 1.2)]:
            tp = eps_forward * e_mult * pe_median * pe_mult
            up = sdiv(tp - price, price)
            scenarios.append({
                "scenario":     label,
                "eps":          round(eps_forward * e_mult, 2),
                "pe":           round(pe_median * pe_mult, 1),
                "target_price": round(tp, 2),
                "upside":       round(up * 100, 1) if up is not None else None,
            })

    return {
        "pe_current":    round(pe_current, 1)  if pe_current  else None,
        "pb_current":    round(pb_current, 2)  if pb_current  else None,
        "pe_median":     round(pe_median, 1)   if pe_median   else None,
        "pb_median":     round(pb_median, 2)   if pb_median   else None,
        "pe_percentile": round(pe_pct, 0)      if pe_pct is not None else None,
        "pb_percentile": round(pb_pct, 0)      if pb_pct is not None else None,
        "peg":           round(peg, 2)          if peg         else None,
        "eps_ttm":       round(eps_latest, 2)  if eps_latest  else None,
        "eps_forward":   round(eps_forward, 2) if eps_forward else None,
        "eps_growth_3y": round(eps_growth * 100, 1) if eps_growth else None,
        "matrix":        matrix,
        "positive_count": positive_count,
        "scenarios":     scenarios,
        "target":        {"low": v_low, "mid": v_mid, "high": v_high},
        "history":       {"dates": dates, "pe": pe_series, "pb": pb_series},
    }


# 价格与行业信息（复用 V1 逻辑）
# ═══════════════════════════════════════════════════════════════
def fetch_price(code):
    """获取最新股价（akshare）"""
    try:
        df = ak.stock_zh_a_daily(symbol=to_sina_code(code), adjust="")
        if df is not None and not df.empty and "close" in df.columns:
            return sf(df.iloc[-1]["close"])
    except Exception:
        pass
    try:
        info = ak.stock_individual_info_em(symbol=code)
        d = dict(zip(info["item"].astype(str), info["value"]))
        return sf(d.get("最新"))
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# 搜索
# ═══════════════════════════════════════════════════════════════
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}

def _sina_name(code):
    """用新浪实时行情接口取股票名称（境外服务器可访问）。返回名称或 None。"""
    try:
        import requests as _req
        r = _req.get(f"https://hq.sinajs.cn/list={to_sina_code(code)}",
                     headers=_SINA_HEADERS, timeout=8)
        r.encoding = "gbk"
        body = r.text.split('"')
        if len(body) > 1 and body[1]:
            name = body[1].split(",")[0].strip()
            return name or None
    except Exception:
        pass
    return None


def _sina_suggest(keyword, limit=12):
    """
    用新浪搜索建议接口做名称/代码模糊搜索（境外服务器可访问）。
    返回 [{code, name}]，仅保留沪深 A 股（sh/sz + 6 位代码）。
    """
    try:
        import requests as _req
        r = _req.get(f"https://suggest3.sinajs.cn/suggest/type=11,12&key={keyword}",
                     headers=_SINA_HEADERS, timeout=8)
        r.encoding = "gbk"
        raw = r.text.split('"')[1] if '"' in r.text else ""
    except Exception:
        return []

    out, seen = [], set()
    for item in raw.split(";"):
        f = item.split(",")
        if len(f) < 4:
            continue
        name, code, sina_code = f[0].strip(), f[2].strip(), f[3].strip()
        if not (sina_code[:2] in ("sh", "sz") and code.isdigit() and len(code) == 6):
            continue
        # field[0] 在纯代码查询时是 sina_code，此时回查名称
        disp = name if not name.startswith(("sh", "sz")) else (_sina_name(code) or code)
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": disp})
        if len(out) >= limit:
            break
    return out


def search_stocks(query, limit=12):
    q = query.strip()
    if not q:
        return []
    # 优先走新浪 suggest（实时、境外可达）
    hits = _sina_suggest(q, limit)
    if hits:
        return hits
    # 兜底：本地代码名称表（若启动时加载成功）
    if not _CODE_NAME.empty:
        df = _CODE_NAME
        m = df[df["code"].astype(str).str.contains(q)] if q.isdigit() \
            else df[df["name"].astype(str).str.contains(q, na=False)]
        if not m.empty:
            return [{"code": str(r["code"]), "name": str(r["name"])}
                    for _, r in m.head(limit).iterrows()]
    # 最后兜底：纯数字代码直接放行
    return [{"code": q.zfill(6), "name": q}] if q.isdigit() else []


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

    # 名称：优先新浪行情接口（境外可达），失败再退东财
    if name == code:
        name = _sina_name(code) or code

    try:
        info_df = ak.stock_individual_info_em(symbol=code)
        info_d  = dict(zip(info_df["item"].astype(str), info_df["value"]))
        industry = str(info_d.get("行业", ""))
        if name == code and info_d.get("股票简称"):
            name = str(info_d["股票简称"])
    except Exception:
        pass

    price = fetch_price(code)
    raw   = fetch_raw(code)

    perf  = compute_performance(raw)
    attr  = compute_attribution(raw)
    risk  = compute_risk(raw)
    val   = compute_valuation(code, raw, price)
    seg   = _fetch_segment_revenue(code)

    return jsonify({
        "info":        {"code": code, "name": name, "industry": industry, "price": price},
        "performance": perf,
        "attribution": attr,
        "risk":        risk,
        "valuation":   val,
        "segment":     seg,
    })



@app.route("/")
def index():
    return render_template("index.html",
                           project_name=PROJECT_NAME,
                           project_desc=PROJECT_DESC)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=(port == 5000))
