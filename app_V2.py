# -*- coding: utf-8 -*-
"""
app_V2.py — A 股三段式财务分析
三段式：业绩检验 → 业绩归因 → 验证排雷
数据源：AKShare（免费，无需注册）
启动后访问：http://127.0.0.1:5000
"""

import os
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import math
import akshare as ak
import pandas as pd
from flask import Flask, request, jsonify, Response

PROJECT_NAME = "三段式财报透视"
PROJECT_DESC = "业绩检验 · 业绩归因 · 验证排雷"

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
    """在 DataFrame 中找第一个包含候选关键字的列名"""
    for c in candidates:
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
    """提取日期列，写入 _date（8位字符串），并按降序排列"""
    if df is None or df.empty:
        return df
    df = df.copy()
    date_col = fcol(df, "报表日期", "报告日")
    if date_col:
        df["_date"] = df[date_col].astype(str).str[:8]
        df = df.sort_values("_date", ascending=False).reset_index(drop=True)
    return df

def annual_periods(df, n=6):
    """从 df 中取最近 n 个年报期（以 1231 结尾）"""
    if df is None or "_date" not in df.columns:
        return []
    return df[df["_date"].str.endswith("1231")]["_date"].tolist()[:n]


# ═══════════════════════════════════════════════════════════════
# 数据抓取层
# ═══════════════════════════════════════════════════════════════
def fetch_raw(code):
    """
    抓取三大报表原始 DataFrame。
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
        eq_col = fcol(balance, "所有者权益", "股东权益")
        equity = col_vals(balance, eq_col, periods)
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
            # 对照组 1：收入含金量
            "revenue":       rev_c,
            "sales_cash":    sales_cash,
            "rev_ratio":     [sdiv(s, r) for s, r in zip(sales_cash, rev_c)],
            # 对照组 2：成本含金量
            "cost":          cost_c,
            "purchase_cash": purchase_cash,
            "cost_ratio":    [sdiv(p, c) for p, c in zip(purchase_cash, cost_c)],
            # 对照组 3：利润含金量
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
    equity_col = fcol(balance, "所有者权益", "股东权益")

    revenue    = col_vals(income,  rev_col,    periods)
    net_profit = col_vals(income,  profit_col, periods)
    assets     = col_vals(balance, assets_col, periods)
    equity     = col_vals(balance, equity_col, periods)

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
    equity_col = fcol(balance, "所有者权益", "股东权益")

    revenue    = col_vals(income,  rev_col,    periods)
    net_profit = col_vals(income,  profit_col, periods)
    ar         = col_vals(balance, ar_col,     periods)
    inventory  = col_vals(balance, inv_col,    periods)
    goodwill   = col_vals(balance, gw_col,     periods)
    assets     = col_vals(balance, assets_col, periods)
    liabilities= col_vals(balance, liab_col,   periods)
    equity     = col_vals(balance, equity_col, periods)

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
# 价格与行业信息（复用 V1 逻辑）
# ═══════════════════════════════════════════════════════════════
def fetch_price(code):
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

    try:
        info_df = ak.stock_individual_info_em(symbol=code)
        info_d  = dict(zip(info_df["item"].astype(str), info_df["value"]))
        industry = str(info_d.get("行业", ""))
    except Exception:
        pass

    price = fetch_price(code)
    raw   = fetch_raw(code)

    return jsonify({
        "info": {"code": code, "name": name, "industry": industry, "price": price},
        "performance": compute_performance(raw),
        "attribution": compute_attribution(raw),
        "risk":        compute_risk(raw),
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

/* ── 主页：垂直居中；分析后：顶部对齐 ── */
.app{min-height:100vh;display:flex;flex-direction:column;justify-content:center;
  align-items:center;padding:32px 20px;transition:justify-content .3s}
.app.searched{justify-content:flex-start;padding-top:40px}

/* ── Hero ── */
.hero{width:100%;max-width:600px;text-align:center}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--accent);margin-bottom:14px}
h1{font-family:var(--display);font-weight:700;font-size:clamp(36px,7vw,56px);
  letter-spacing:-.02em;margin:0 0 8px;line-height:1.05}
.tag{color:var(--muted);font-size:15px;margin:0 auto 28px;max-width:28em}

/* ── 搜索框 ── */
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

/* ── 结果区 ── */
#out{display:none;width:100%;max-width:980px;margin:40px auto 64px}

.stockhead{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;
  padding-bottom:18px;border-bottom:2px solid var(--ink);margin-bottom:28px}
.stockhead .sname{font-family:var(--display);font-weight:700;font-size:28px}
.stockhead .scode{font-family:var(--mono);color:var(--muted);font-size:14px}
.stockhead .smeta{margin-left:auto;font-family:var(--mono);font-size:13px;color:var(--muted)}
.stockhead .smeta b{color:var(--ink)}

/* ── 三段 Tab ── */
.tabs{display:flex;gap:0;border-bottom:2px solid var(--line);margin-bottom:32px}
.tab-btn{font-family:var(--display);font-weight:600;font-size:14px;
  padding:10px 22px;border:0;background:transparent;cursor:pointer;
  color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-2px}
.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ── 区块标题 ── */
.sec-label{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.sec-title{font-family:var(--display);font-weight:600;font-size:18px;
  margin:32px 0 6px}
.sec-sub{color:var(--muted);font-size:14px;margin:0 0 18px}

/* ── KPI 卡片行 ── */
.kpi-row{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:28px}
.kpi-card{background:var(--surface);border:1px solid var(--line);
  border-radius:3px;padding:16px}
.kpi-card .ktag{font-family:var(--mono);font-size:11px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
.kpi-card .kval{font-family:var(--display);font-weight:700;font-size:22px;
  letter-spacing:-.01em}
.kpi-card .kunit{font-family:var(--mono);font-size:12px;color:var(--muted);margin-left:3px}

/* ── 图表容器 ── */
.chart-wrap{background:var(--surface);border:1px solid var(--line);
  border-radius:3px;padding:20px;margin-bottom:20px}
.chart-title{font-family:var(--display);font-weight:600;font-size:15px;margin-bottom:14px}
.chart-note{font-size:12.5px;color:var(--muted);margin-top:8px}
.charts-3col{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px}

/* ── 排雷仪表盘 ── */
.dashboard-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:28px}
.dash-card{background:var(--surface);border:1px solid var(--line);
  border-radius:3px;padding:16px 14px}
.dash-card .d-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.dash-card .d-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.d-dot.green{background:#1DBF73}
.d-dot.yellow{background:#E8A800}
.d-dot.red{background:var(--red)}
.d-dot.grey{background:#CCC}
.dash-card .d-name{font-family:var(--display);font-weight:600;font-size:14px}
.dash-card .d-val{font-family:var(--mono);font-weight:600;font-size:22px;margin-bottom:4px}
.dash-card.green{background:var(--green-soft);border-color:#A8D9CE}
.dash-card.yellow{background:var(--yellow-soft);border-color:#E8D59A}
.dash-card.red{background:var(--red-soft);border-color:#E8BDBA}
.dash-card .d-unit{font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:8px}
.dash-card .d-desc{font-size:12.5px;color:var(--muted);line-height:1.4}
.dash-card .d-signal{font-family:var(--mono);font-size:11px;color:var(--muted);
  margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}

/* ── 杜邦表格 ── */
.dupont-table{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px}
.dupont-table th{background:#F4F6F8;font-family:var(--mono);font-size:12px;
  font-weight:500;color:var(--muted);padding:8px 12px;text-align:right;
  border-bottom:1px solid var(--line)}
.dupont-table th:first-child{text-align:left}
.dupont-table td{padding:8px 12px;text-align:right;font-family:var(--mono);
  border-bottom:1px solid var(--line)}
.dupont-table td:first-child{text-align:left;font-weight:600;font-family:var(--body)}
.dupont-table tr:last-child td{border-bottom:0}

footer{width:100%;max-width:980px;margin:0 auto;font-family:var(--mono);font-size:12px;
  color:var(--muted);text-align:center;padding:16px 0 32px;border-top:1px solid var(--line)}

@media(max-width:720px){
  .kpi-row{grid-template-columns:repeat(3,1fr)}
  .dashboard-grid{grid-template-columns:repeat(2,1fr)}
  .charts-3col{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="app" id="app">

  <!-- ── 搜索主页 ── -->
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

  <!-- ── 分析结果 ── -->
  <main id="out">
    <!-- 股票头 -->
    <div class="stockhead">
      <span class="sname" id="o-name"></span>
      <span class="scode" id="o-code"></span>
      <span class="smeta" id="o-meta"></span>
    </div>

    <!-- Tab 导航 -->
    <nav class="tabs">
      <button class="tab-btn active" data-tab="t1">① 业绩检验</button>
      <button class="tab-btn"        data-tab="t2">② 业绩归因</button>
      <button class="tab-btn"        data-tab="t3">③ 验证排雷</button>
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
          <div class="chart-note">健康：销售收到现金 / 营收 ≈ 1.0–1.17（含增值税）</div>
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
  </main>

  <footer id="footer" style="display:none">
    数据来自 AKShare 公开接口，可能存在延迟或缺失，仅供学习研究，不构成投资建议。
  </footer>
</div>

<script>
// ── 工具 ────────────────────────────────────────────────
const $ = s => document.querySelector(s);
const appEl = $('#app'), stateEl = $('#state'), candsEl = $('#cands');
const outEl = $('#out'), footEl = $('#footer');

function setState(msg, isErr) {
  stateEl.className = 'state-msg' + (isErr ? ' err' : '');
  stateEl.innerHTML = msg ? (isErr ? msg : '<span class="dot"></span>' + msg) : '';
}

function fmtPeriod(d) {
  // "20231231" → "2023"  /  "20230630" → "2023H1"
  const y = d.slice(0,4), m = d.slice(4,6);
  if (m === '12') return y;
  const labels = {'03':'Q1','06':'H1','09':'Q3'};
  return y + (labels[m] || '');
}

const yi  = v => v == null ? null : v / 1e8;           // 元 → 亿元
const pct = v => v == null ? null : v * 100;           // 小数 → %
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

// 颜色组
const C = {
  teal:'#0B6E5D', amber:'#C07B12', blue:'#1A5FAD', purple:'#6D3DB2',
  red:'#B23A2E', grey:'#A0AAB4',
  teal_a:'rgba(11,110,93,.18)', amber_a:'rgba(192,123,18,.18)',
};

// ── 搜索 ────────────────────────────────────────────────
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
  setState('正在抓取财报数据，约需 15–25 秒…');
  try {
    const data = await (await fetch('/api/analyze?code=' + code)).json();
    if (data.error) { setState(data.error, true); return; }
    render(data);
    setState('');
  } catch(e) { setState('抓取失败，数据源可能临时不可用，请重试。', true); }
}

// ── 渲染主入口 ───────────────────────────────────────────
function render(data) {
  renderHeader(data.info);
  renderPerformance(data.performance);
  renderAttribution(data.attribution);
  renderRisk(data.risk);
  outEl.style.display = 'block';
  footEl.style.display = 'block';
}

// ── 股票头 ──────────────────────────────────────────────
function renderHeader(info) {
  $('#o-name').textContent = info.name;
  $('#o-code').textContent = info.code;
  const price = info.price == null ? '—' : '¥' + Number(info.price).toFixed(2);
  $('#o-meta').innerHTML = `现价 <b>${price}</b>　·　${info.industry || '—'}`;
}

// ── Tab 切换 ─────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('#' + btn.dataset.tab).classList.add('active');
    // Plotly 在隐藏时不会自动调整大小，切换后 relayout
    window.dispatchEvent(new Event('resize'));
  });
});

// ── 第一段：业绩检验 ─────────────────────────────────────
function renderPerformance(perf) {
  if (!perf || !perf.periods || !perf.periods.length) return;

  const xLabels = perf.periods.map(fmtPeriod);
  const rev     = perf.revenue.map(yi);
  const prof    = perf.net_profit.map(yi);
  const gm      = perf.gross_margin.map(pct);
  const nm      = perf.net_margin.map(pct);
  const roe     = perf.roe.map(pct);
  const ryoy    = perf.revenue_yoy.map(pct);
  const pyoy    = perf.profit_yoy.map(pct);

  // KPI 卡片（最新一期）
  const latest = xLabels[0];
  const kpis = [
    {tag:'营业收入', val: rev[0]   == null ? '—' : rev[0].toFixed(1),   unit:'亿元'},
    {tag:'归母净利润', val: prof[0] == null ? '—' : prof[0].toFixed(1), unit:'亿元'},
    {tag:'毛利率',  val: gm[0]     == null ? '—' : gm[0].toFixed(1),   unit:'%'},
    {tag:'净利率',  val: nm[0]     == null ? '—' : nm[0].toFixed(1),   unit:'%'},
    {tag:'ROE',     val: roe[0]    == null ? '—' : roe[0].toFixed(1),  unit:'%'},
  ];
  $('#kpi-row').innerHTML = kpis.map(k =>
    `<div class="kpi-card">
       <div class="ktag">${k.tag}</div>
       <div class="kval">${k.val}<span class="kunit">${k.unit}</span></div>
     </div>`).join('');

  // 图1：营收 & 净利润柱状 + YoY 线
  Plotly.newPlot('ch-rev-profit', [
    {name:'营业收入', type:'bar', x:xLabels, y:rev,  marker:{color:C.teal_a, line:{color:C.teal,width:1.5}}, yaxis:'y'},
    {name:'净利润',   type:'bar', x:xLabels, y:prof, marker:{color:C.amber_a,line:{color:C.amber,width:1.5}}, yaxis:'y'},
    {name:'营收 YoY', type:'scatter', mode:'lines+markers', x:xLabels, y:ryoy,
      line:{color:C.teal,width:2}, marker:{size:5}, yaxis:'y2'},
    {name:'利润 YoY', type:'scatter', mode:'lines+markers', x:xLabels, y:pyoy,
      line:{color:C.amber,width:2,dash:'dot'}, marker:{size:5}, yaxis:'y2'},
  ], {
    ...LAYOUT_BASE,
    barmode:'group',
    yaxis: {title:'亿元', gridcolor:'#F0F2F4'},
    yaxis2:{title:'增速 %', overlaying:'y', side:'right', showgrid:false, ticksuffix:'%'},
  }, PLOTLY_CFG);

  // 图2：毛利率 / 净利率 / ROE
  Plotly.newPlot('ch-margins', [
    {name:'毛利率', type:'scatter', mode:'lines+markers', x:xLabels, y:gm,
      line:{color:C.teal,  width:2.5}, marker:{size:6}},
    {name:'净利率', type:'scatter', mode:'lines+markers', x:xLabels, y:nm,
      line:{color:C.amber, width:2.5}, marker:{size:6}},
    {name:'ROE',    type:'scatter', mode:'lines+markers', x:xLabels, y:roe,
      line:{color:C.blue,  width:2.5}, marker:{size:6}},
  ], {
    ...LAYOUT_BASE,
    yaxis:{title:'%', gridcolor:'#F0F2F4', ticksuffix:'%'},
  }, PLOTLY_CFG);

  // 图3a/b/c：三组现金流对照
  const cm = perf.cash_match;
  if (cm && cm.periods && cm.periods.length) {
    const xl = cm.periods.map(fmtPeriod);
    renderCashMatch('ch-cm1', xl,
      cm.revenue.map(yi),    cm.sales_cash.map(yi),    cm.rev_ratio,
      '营业收入', '销售收到现金');
    renderCashMatch('ch-cm2', xl,
      cm.cost.map(yi),       cm.purchase_cash.map(yi), cm.cost_ratio,
      '营业成本', '购买支付现金');
    renderCashMatch('ch-cm3', xl,
      cm.net_profit.map(yi), cm.ocf.map(yi),           cm.profit_ratio,
      '净利润', '经营现金流');
  }
}

function renderCashMatch(divId, xl, a, b, ratio, nameA, nameB) {
  Plotly.newPlot(divId, [
    {name:nameA, type:'bar', x:xl, y:a, marker:{color:C.teal_a, line:{color:C.teal,width:1.5}}, yaxis:'y'},
    {name:nameB, type:'bar', x:xl, y:b, marker:{color:C.amber_a,line:{color:C.amber,width:1.5}}, yaxis:'y'},
    {name:'比值', type:'scatter', mode:'lines+markers', x:xl, y:ratio,
      line:{color:C.red,width:2}, marker:{size:5}, yaxis:'y2'},
  ], {
    ...LAYOUT_BASE,
    margin:{t:10,r:40,b:36,l:44},
    barmode:'group',
    yaxis:{title:'亿元', gridcolor:'#F0F2F4'},
    yaxis2:{title:'比值', overlaying:'y', side:'right', showgrid:false},
    legend:{orientation:'h', y:-0.22, font:{size:10}},
  }, PLOTLY_CFG);
}

// ── 第二段：业绩归因 ──────────────────────────────────────
function renderAttribution(attr) {
  if (!attr || !attr.periods || !attr.periods.length) return;

  const xl  = attr.periods.map(fmtPeriod);
  const nm  = attr.net_margin.map(pct);
  const at  = attr.asset_turnover;
  const em  = attr.equity_mult;
  const roe = attr.roe.map(pct);
  const rev = xl; // same x

  // 杜邦图（净利率 & 权益乘数左轴，总资产周转右轴）
  Plotly.newPlot('ch-dupont', [
    {name:'净利率 %',   type:'scatter', mode:'lines+markers', x:xl, y:nm,
      line:{color:C.teal, width:2.5}, marker:{size:6}, yaxis:'y'},
    {name:'权益乘数 ×', type:'scatter', mode:'lines+markers', x:xl, y:em,
      line:{color:C.purple,width:2.5}, marker:{size:6}, yaxis:'y'},
    {name:'资产周转 ×', type:'scatter', mode:'lines+markers', x:xl, y:at,
      line:{color:C.amber, width:2.5,dash:'dot'}, marker:{size:6}, yaxis:'y2'},
    {name:'ROE %',      type:'scatter', mode:'lines+markers', x:xl, y:roe,
      line:{color:C.red,   width:3}, marker:{size:7,symbol:'star'}, yaxis:'y'},
  ], {
    ...LAYOUT_BASE,
    yaxis: {title:'% 或 ×', gridcolor:'#F0F2F4'},
    yaxis2:{title:'总资产周转（×）', overlaying:'y', side:'right', showgrid:false},
  }, PLOTLY_CFG);

  // 杜邦数字表
  const rows = xl.map((yr, i) => [
    yr,
    fmtP(nm[i]),
    fmtX(at[i]),
    fmtX(em[i]),
    fmtP(roe[i]),
  ]);
  const thead = `<tr>${['期间','净利率','总资产周转','权益乘数','ROE（验证）']
    .map(h=>`<th>${h}</th>`).join('')}</tr>`;
  const tbody = rows.map(r =>
    `<tr>${r.map((v,i)=>`<td ${i===0?'style="text-align:left"':''}>${v}</td>`).join('')}</tr>`
  ).join('');
  $('#dupont-table').innerHTML = `<thead>${thead}</thead><tbody>${tbody}</tbody>`;

  // 费用率图
  const sr = attr.selling_rate.map(pct);
  const ar = attr.admin_rate.map(pct);
  const rdr= attr.rd_rate.map(pct);
  const fr = attr.finance_rate.map(pct);

  Plotly.newPlot('ch-expense', [
    {name:'销售费用率', type:'scatter', mode:'lines+markers', x:xl, y:sr,
      line:{color:C.teal,  width:2}, marker:{size:5}},
    {name:'管理费用率', type:'scatter', mode:'lines+markers', x:xl, y:ar,
      line:{color:C.amber, width:2}, marker:{size:5}},
    {name:'研发费用率', type:'scatter', mode:'lines+markers', x:xl, y:rdr,
      line:{color:C.blue,  width:2}, marker:{size:5}},
    {name:'财务费用率', type:'scatter', mode:'lines+markers', x:xl, y:fr,
      line:{color:C.grey,  width:2, dash:'dot'}, marker:{size:5}},
  ], {
    ...LAYOUT_BASE,
    yaxis:{title:'%', gridcolor:'#F0F2F4', ticksuffix:'%'},
  }, PLOTLY_CFG);
}

// ── 第三段：验证排雷 ──────────────────────────────────────
function renderRisk(risk) {
  if (!risk) return;

  // 排雷仪表盘卡片
  const dash = risk.dashboard || [];
  $('#dash-grid').innerHTML = dash.map(d => {
    const valStr = d.value == null ? '—' : String(d.value);
    return `<div class="dash-card ${d.status}">
      <div class="d-head">
        <span class="d-dot ${d.status}"></span>
        <span class="d-name">${d.name}</span>
      </div>
      <div class="d-val">${valStr}</div>
      <div class="d-unit">${d.unit}</div>
      <div class="d-desc">${d.desc}</div>
      <div class="d-signal">${d.signal}</div>
    </div>`;
  }).join('');

  // 应收 & 存货增速趋势图
  const tp = (risk.trend_periods || []).map(fmtPeriod);
  const rvy = (risk.rev_yoy  || []).map(pct);
  const ary = (risk.ar_yoy   || []).map(pct);
  const ivy = (risk.inv_yoy  || []).map(pct);

  if (tp.length) {
    Plotly.newPlot('ch-ar', [
      {name:'应收账款增速', type:'bar', x:tp, y:ary,
        marker:{color:C.amber_a, line:{color:C.amber,width:1.5}}},
      {name:'营收增速',     type:'bar', x:tp, y:rvy,
        marker:{color:C.teal_a,  line:{color:C.teal, width:1.5}}},
    ], {
      ...LAYOUT_BASE,
      barmode:'group',
      yaxis:{title:'%', gridcolor:'#F0F2F4', ticksuffix:'%'},
    }, PLOTLY_CFG);

    Plotly.newPlot('ch-inv', [
      {name:'存货增速', type:'bar', x:tp, y:ivy,
        marker:{color:C.purple + '30', line:{color:C.purple,width:1.5}}},
      {name:'营收增速', type:'bar', x:tp, y:rvy,
        marker:{color:C.teal_a,  line:{color:C.teal, width:1.5}}},
    ], {
      ...LAYOUT_BASE,
      barmode:'group',
      yaxis:{title:'%', gridcolor:'#F0F2F4', ticksuffix:'%'},
    }, PLOTLY_CFG);
  }
}

// ── 事件绑定 ─────────────────────────────────────────────
$('#go').onclick = doSearch;
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
