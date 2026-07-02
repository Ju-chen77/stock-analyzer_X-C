import os

# === 强制所有 HTTP 请求直连，不走代理 ===
# 因为本应用访问的都是国内财经 API，走代理会失败
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)
# === 结束 ===

# 你原本的 imports 从这里开始
from flask import Flask
# -*- coding: utf-8 -*-
"""
app_V5.py — A 股财报透析（科技感 UI）
六段式：业绩检验 → 业绩归因 → 验证排雷 → 估值区间 → 盈利预测 → 量价印证
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

# Wind 第一通道（估值 + 名称/行业）；导入失败即禁用，全程回退原通道，绝不影响主流程
try:
    import wind_data as wind
except Exception as _wind_err:
    wind = None
    print(f"[Wind] 模块加载失败，禁用 Wind 通道: {_wind_err}")

PROJECT_NAME = "财报透析"
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
def _exchange(code):
    """
    判定交易所前缀：SH 上证 / SZ 深证 / BJ 北证。
    北交所代码段：43 / 83 / 87 / 88 开头，及新增 920 段（920 也以 9 开头，
    故必须先于「6/9 → 上证」判断，否则会被误判为上证）；其余 6/9 归上证
    （含 900 B 股），0/3 归深证。
    """
    code = str(code).strip()
    if code.startswith(("4", "8", "920")): return "BJ"
    if code.startswith(("0", "3")):        return "SZ"
    if code.startswith(("6", "9")):        return "SH"
    return "BJ"

def to_sina_code(code):
    """6 位代码 → 新浪带市场前缀代码（sh / sz / bj）。"""
    return _exchange(code).lower() + str(code).strip()

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
    # 逐候选尝试，跳过全空的表头列（银行「股东权益」常是空表头，真值在「归属于母公司股东权益」）
    for cand in ("所有者权益(或股东权益)合计",
                 "归属于母公司所有者权益合计",
                 "所有者权益合计",
                 "股东权益合计",
                 "归属于母公司股东权益",     # 银行 / 部分金融的归母权益写法
                 "所有者权益",
                 "股东权益"):
        col = fcol(balance, cand)
        if col:
            vals = col_vals(balance, col, periods)
            if any(v is not None for v in vals):
                return vals

    # 兜底：资产 − 负债
    assets_col = fcol(balance, "资产总计")
    liab_col   = fcol(balance, "负债合计")
    if assets_col and liab_col:
        assets = col_vals(balance, assets_col, periods)
        liabs  = col_vals(balance, liab_col,   periods)
        return [a - l if (a is not None and l is not None) else None
                for a, l in zip(assets, liabs)]
    return [None] * len(periods)

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
        prefix = _exchange(code)
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
def _fetch_deduct_indicator(code):
    """
    额外抓取扣非净利润（新浪财务指标接口），三大报表不含此项。
    Returns: dict {期间(yyyymmdd): 扣非净利润(元)}；失败返回 {}（容错，不阻塞主流程）
    会计假设：扣除非经常性损益后归母净利润，CAS 口径
    """
    try:
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2018")
        if df is None or df.empty:
            return {}
        dcol = next((c for c in df.columns if "日期" in str(c)), None)
        vcol = next((c for c in df.columns if "扣除非经常性损益后的净利润" in str(c)), None)
        if not dcol or not vcol:
            return {}
        out = {}
        for _, row in df.iterrows():
            d = str(row[dcol]).replace("-", "").strip()
            if d.endswith("1231"):
                out[d] = sf(row[vcol])
        return out
    except Exception as e:
        print(f"[ERR] deduct indicator: {e}")
        return {}


def compute_performance(raw, code=None):
    """
    业绩检验三层结构：
    A · 盈利能力（增长性 + 盈利率 + 回报率 ROE/ROA/ROIC + 扣非）
    B · 业绩自洽性（营收 vs 合同负债；营收 vs 应收/存货见验证排雷）
    C · 现金流验证（三组含金量 + FCF + 三类现金流组合判读）
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

    # ── A 层补充：营业利润率 / ROA / ROIC / 扣非 ──────────
    op_profit = col_vals(income, fcol(income, "营业利润"), periods)
    op_margin = [sdiv(o, r) for o, r in zip(op_profit, revenue)]

    assets = (col_vals(balance, fcol(balance, "资产总计"), periods)
              if balance is not None else [None] * len(periods))
    roa = []
    for i, (p, a) in enumerate(zip(net_profit, assets)):
        if i + 1 < len(assets) and assets[i + 1] is not None and a is not None:
            roa.append(sdiv(p, (a + assets[i + 1]) / 2))   # 平均总资产
        else:
            roa.append(sdiv(p, a))

    # ROIC = EBIT×(1−税率) /（净资产 + 有息负债）排除杠杆看真实回报
    roic = [None] * len(periods)
    if balance is not None:
        pretax  = col_vals(income, fcol(income, "利润总额"),    periods)
        taxexp  = col_vals(income, fcol(income, "所得税费用"),  periods)
        int_exp = col_vals(income, fcol(income, "利息费用"),    periods)
        fin_exp = col_vals(income, fcol(income, "财务费用"),    periods)
        eq      = _get_equity(balance, periods)
        stl = col_vals(balance, fcol(balance, "短期借款"), periods)
        cdu = col_vals(balance, fcol(balance, "一年内到期的非流动负债"), periods)
        ltl = col_vals(balance, fcol(balance, "长期借款"), periods)
        bnd = col_vals(balance, fcol(balance, "应付债券"), periods)
        for i in range(len(periods)):
            if pretax[i] is None:
                continue
            ie   = int_exp[i] if int_exp[i] not in (None, 0) else (fin_exp[i] or 0)
            ebit = pretax[i] + ie
            tax  = sdiv(taxexp[i], pretax[i])
            tax  = min(max(tax, 0.0), 0.4) if tax is not None else 0.25
            debt = sum(v for v in [stl[i], cdu[i], ltl[i], bnd[i]] if v is not None)
            invcap = (eq[i] or 0) + debt
            if invcap:
                roic[i] = ebit * (1 - tax) / invcap

    # 扣非净利润（额外数据源，容错；失败则全 None）
    deduct_map    = _fetch_deduct_indicator(code) if code else {}
    deduct        = [deduct_map.get(p) for p in periods]
    deduct_margin = [sdiv(d, r) for d, r in zip(deduct, revenue)]
    deduct_yoy    = yoy_list(deduct)

    # ── B 层：营收 vs 合同负债（未来订单领先指标）──────────
    contract = {}
    if balance is not None:
        cl  = col_vals(balance, fcol(balance, "合同负债"),   periods)
        pre = col_vals(balance, fcol(balance, "预收款项"),   periods)
        contract_liab = [(cl[i] if cl[i] not in (None, 0) else pre[i])
                         for i in range(len(periods))]
        contract = {
            "periods":       periods,
            "revenue":       revenue,
            "contract_liab": contract_liab,
            "contract_yoy":  yoy_list(contract_liab),
            "rev_yoy":       revenue_yoy,
        }

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

        # 累计 NIO vs CFO（判断财务造假）
        cum_profit = []
        cum_ocf    = []
        sp, so = 0, 0
        for p, o in zip(reversed(profit_c), reversed(ocf)):
            sp += p if p else 0
            so += o if o else 0
            cum_profit.append(sp)
            cum_ocf.append(so)

        cash_match = {
            "periods":       common,
            "revenue":       rev_c,
            "sales_cash":    sales_cash,
            "cost":          cost_c,
            "purchase_cash": purchase_cash,
            "net_profit":    profit_c,
            "ocf":           ocf,
            "cum_profit":    list(reversed(cum_profit)),
            "cum_ocf":       list(reversed(cum_ocf)),
        }

    # ── C 层补充：自由现金流 FCF + 三类现金流组合判读 ──────
    fcf_data = {}
    cf_pattern = {}
    if cashflow is not None:
        cfp     = annual_periods(cashflow, 6)
        ocf_c   = col_vals(cashflow, fcol(cashflow, "经营活动产生的现金流量净额", "经营活动产生"), cfp)
        inv_c   = col_vals(cashflow, fcol(cashflow, "投资活动产生的现金流量净额", "投资活动产生"), cfp)
        fin_c   = col_vals(cashflow, fcol(cashflow, "筹资活动产生的现金流量净额", "筹资活动产生"), cfp)
        capex_c = col_vals(cashflow, fcol(cashflow, "购建固定资产、无形资产和其他长期资产支付的现金", "购建固定资产"), cfp)
        fcf = [(ocf_c[i] - abs(capex_c[i]))
               if (ocf_c[i] is not None and capex_c[i] is not None) else None
               for i in range(len(cfp))]
        fcf_data = {"periods": cfp, "ocf": ocf_c, "capex": capex_c, "fcf": fcf}

        # 经营/投资/筹资 正负组合 → 8 种公司阶段
        PATTERN = {
            (1, -1, -1): ("健康成熟期", "low"),
            (1, -1,  1): ("成长扩张期", "mid"),
            (1,  1, -1): ("收缩调整期", "mid"),
            (1,  1,  1): ("现金积累期", "low"),
            (-1, -1, 1): ("靠融资生存", "high"),
            (-1,  1, 1): ("变卖资产+借钱", "extreme"),
            (-1,  1, -1): ("收缩自救期", "high"),
            (-1, -1, -1): ("三流皆出", "extreme"),
        }
        labels = []
        for i in range(len(cfp)):
            o, v, f = ocf_c[i], inv_c[i], fin_c[i]
            if None in (o, v, f):
                labels.append({"period": cfp[i], "label": "数据缺失", "risk": "grey", "signs": [None, None, None]})
                continue
            sg = (1 if o >= 0 else -1, 1 if v >= 0 else -1, 1 if f >= 0 else -1)
            lab, risk = PATTERN.get(sg, ("—", "grey"))
            labels.append({"period": cfp[i], "label": lab, "risk": risk, "signs": list(sg)})
        cf_pattern = {"periods": cfp, "op_cf": ocf_c, "inv_cf": inv_c, "fin_cf": fin_c, "labels": labels}

    # ── Part D / E：资产 & 负债堆积图 ──────────────────────
    asset_stack = {}
    liability_stack = {}
    if balance is not None:
        bs_periods = annual_periods(balance, 6)
        # 资产项（参考面积堆积图分类）
        def _sum_cols(df, periods, *col_keys):
            """多列求和"""
            result = [0.0] * len(periods)
            for ck in col_keys:
                c = fcol(df, ck)
                vals = col_vals(df, c, periods)
                for i, v in enumerate(vals):
                    if v is not None:
                        result[i] += v
            return [v if v != 0 else None for v in result]

        asset_data = {
            "固定资产+在建工程": _sum_cols(balance, bs_periods,
                "固定资产", "在建工程"),
            "长期股权投资": col_vals(balance,
                fcol(balance, "长期股权投资"), bs_periods),
            "应收类资产": _sum_cols(balance, bs_periods,
                "应收票据及应收账款", "应收账款", "其他应收款"),
            "商誉+无形资产": _sum_cols(balance, bs_periods,
                "商誉", "无形资产"),
            "现金类资产": _sum_cols(balance, bs_periods,
                "货币资金", "交易性金融资产"),
            "存货": col_vals(balance,
                fcol(balance, "存货"), bs_periods),
            "预付类资产": _sum_cols(balance, bs_periods,
                "预付款项", "合同资产"),
        }
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
        "op_margin":        op_margin,
        "deduct_margin":    deduct_margin,
        "deduct_yoy":       deduct_yoy,
        "roe":              roe,
        "roa":              roa,
        "roic":             roic,
        "revenue_yoy":      revenue_yoy,
        "profit_yoy":       profit_yoy,
        "contract":         contract,
        "cash_match":       cash_match,
        "fcf":              fcf_data,
        "cf_pattern":       cf_pattern,
        "asset_stack":      asset_stack,
        "liability_stack":  liability_stack,
    }


# ═══════════════════════════════════════════════════════════════
# 第二段：业绩归因
# ═══════════════════════════════════════════════════════════════
def compute_attribution(raw):
    """
    四层递进业绩归因：
    第一层 ROE 杜邦分解（含贡献度量化）
    第三层 利润率拆解（瀑布 + 扣非净利率）
    第四层 现金流验证总结（三组通过/不通过）
    Returns: dict
    """
    income   = raw.get("income")
    balance  = raw.get("balance")
    cashflow = raw.get("cashflow")

    if income is None or balance is None:
        return {}

    periods = annual_periods(income, 6)
    if not periods:
        return {}

    rev_col    = fcol(income,  "营业收入")
    cost_col   = fcol(income,  "营业成本")
    profit_col = fcol(income,  "归属于母公司所有者的净利润", "净利润")
    assets_col = fcol(balance, "资产总计")

    revenue    = col_vals(income,  rev_col,    periods)
    cost       = col_vals(income,  cost_col,   periods)
    net_profit = col_vals(income,  profit_col, periods)
    assets     = col_vals(balance, assets_col, periods)
    equity     = _get_equity(balance, periods)

    # ── 第一层：杜邦三因子 ──
    net_margin     = [sdiv(p, r) for p, r in zip(net_profit, revenue)]
    asset_turnover = [sdiv(r, a) for r, a in zip(revenue, assets)]
    equity_mult    = [sdiv(a, e) for a, e in zip(assets, equity)]
    roe_dupont = [
        m * t * eq if (m and t and eq) else None
        for m, t, eq in zip(net_margin, asset_turnover, equity_mult)
    ]

    # 贡献度量化：最新期 vs 上一期的 pct 变化
    dupont_contrib = None
    if len(periods) >= 2:
        nm0, nm1 = net_margin[0], net_margin[1]
        at0, at1 = asset_turnover[0], asset_turnover[1]
        em0, em1 = equity_mult[0], equity_mult[1]
        roe0, roe1 = roe_dupont[0], roe_dupont[1]
        if all(v is not None for v in [nm0, nm1, at0, at1, em0, em1, roe0, roe1]):
            roe_chg = (roe0 - roe1) * 100
            nm_contrib  = (nm0 - nm1) * at1 * em1 * 100
            at_contrib  = nm1 * (at0 - at1) * em1 * 100
            em_contrib  = nm1 * at1 * (em0 - em1) * 100
            drivers = [
                ("净利率", nm_contrib),
                ("总资产周转率", at_contrib),
                ("权益乘数", em_contrib),
            ]
            main_driver = max(drivers, key=lambda x: abs(x[1]))[0]
            dupont_contrib = {
                "roe_change":    round(roe_chg, 2),
                "nm_contrib":    round(nm_contrib, 2),
                "at_contrib":    round(at_contrib, 2),
                "em_contrib":    round(em_contrib, 2),
                "main_driver":   main_driver,
                "period_curr":   periods[0],
                "period_prev":   periods[1],
            }

    # ── 第三层：利润率拆解（瀑布数据） ──
    def expense_rate(col_key):
        col = fcol(income, col_key)
        vals = col_vals(income, col, periods)
        return [sdiv(v, r) for v, r in zip(vals, revenue)]

    gross_margin = [sdiv((r - c) if (r and c) else None, r)
                    for r, c in zip(revenue, cost)]

    selling_rate = expense_rate("销售费用")
    admin_rate   = expense_rate("管理费用")
    rd_rate      = expense_rate("研发费用")
    finance_rate = expense_rate("财务费用")

    # 营业利润率
    op_col = fcol(income, "营业利润")
    op_profit = col_vals(income, op_col, periods)
    op_margin = [sdiv(o, r) for o, r in zip(op_profit, revenue)]

    # 扣非净利润率
    deduct_col = fcol(income, "扣除非经常性损益后的净利润", "扣非净利润")
    deduct_vals = col_vals(income, deduct_col, periods) if deduct_col else [None] * len(periods)
    deduct_margin = [sdiv(d, r) for d, r in zip(deduct_vals, revenue)]

    # 最新期瀑布数据（毛利率 → 各费用拖累 → 净利率）
    waterfall = None
    if gross_margin[0] is not None:
        wf_items = [
            {"name": "毛利率",   "value": round((gross_margin[0] or 0) * 100, 2)},
            {"name": "销售费用", "value": -round((selling_rate[0] or 0) * 100, 2)},
            {"name": "管理费用", "value": -round((admin_rate[0] or 0) * 100, 2)},
            {"name": "研发费用", "value": -round((rd_rate[0] or 0) * 100, 2)},
            {"name": "财务费用", "value": -round((finance_rate[0] or 0) * 100, 2)},
        ]
        # 其他损益 = 净利率 - (毛利率 - 四项费用率)
        explained = sum(it["value"] for it in wf_items)
        net_m_pct = round((net_margin[0] or 0) * 100, 2)
        other = round(net_m_pct - explained, 2)
        wf_items.append({"name": "其他损益", "value": other})
        wf_items.append({"name": "净利率", "value": net_m_pct})
        waterfall = {"period": periods[0], "items": wf_items}

    # ── 第四层：现金流验证总结 ──
    cf_validation = None
    if cashflow is not None:
        sc_col  = fcol(cashflow, "销售商品、提供劳务收到的现金", "销售商品")
        ocf_col = fcol(cashflow, "经营活动产生的现金流量净额", "经营活动产生")
        capex_col = fcol(cashflow, "购建固定资产、无形资产和其他长期资产支付的现金",
                         "购建固定资产")

        cf_periods = [p for p in periods if p in (cashflow.index if hasattr(cashflow, 'index') else [])]
        if not cf_periods and len(periods) > 0:
            cf_periods = periods

        sc  = col_vals(cashflow, sc_col,  cf_periods[:1])
        ocf = col_vals(cashflow, ocf_col, cf_periods[:1])
        capex = col_vals(cashflow, capex_col, cf_periods[:1])
        rev0 = col_vals(income, rev_col, cf_periods[:1])
        np0  = col_vals(income, profit_col, cf_periods[:1])

        rev_quality   = sdiv(sc[0], rev0[0]) if sc and rev0 else None
        profit_quality = sdiv(ocf[0], np0[0]) if ocf and np0 else None
        fcf = (ocf[0] or 0) - abs(capex[0] or 0) if ocf and capex else None

        def _verdict(rq, pq, fcf_val):
            fails = 0
            if rq is not None and rq < 1.0:   fails += 1
            if pq is not None and pq < 0.8:   fails += 1
            if fcf_val is not None and fcf_val < 0: fails += 1
            if fails == 0: return "PASS"
            if fails >= 2: return "FAIL"
            return "WARNING"

        cf_validation = {
            "period":          cf_periods[0] if cf_periods else None,
            "rev_quality":     round(rev_quality, 2) if rev_quality else None,
            "profit_quality":  round(profit_quality, 2) if profit_quality else None,
            "fcf":             round(fcf / 1e8, 2) if fcf else None,
            "verdict":         _verdict(rev_quality, profit_quality, fcf),
        }

    return {
        "periods":       periods,
        "net_margin":    net_margin,
        "asset_turnover": asset_turnover,
        "equity_mult":   equity_mult,
        "roe":           roe_dupont,
        "dupont_contrib": dupont_contrib,
        "selling_rate":  selling_rate,
        "admin_rate":    admin_rate,
        "rd_rate":       rd_rate,
        "finance_rate":  finance_rate,
        "gross_margin":  gross_margin,
        "op_margin":     op_margin,
        "deduct_margin": deduct_margin,
        "waterfall":     waterfall,
        "cf_validation": cf_validation,
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
    验证排雷：两大维度（A 资产端 / B 偿债杠杆）× 红黄绿历史热力图 + 一票否决判定。
    依据「验证排雷框架」，全部基于已抓取的三大报表结构化数据。
    治理维度 C（质押/审计/监管）需额外数据源，留待后续接入。

    Returns: dict {
        verdict:    {level, label, red, yellow, green, summary},
        dimensions: [{group, items:[{name, value, unit, status, desc, signal}]}],
        heatmap:    {periods, rows:[{name, group, statuses, values}]},
        trend_periods, rev_yoy, ar_yoy, inv_yoy
    }
    """
    income   = raw.get("income")
    balance  = raw.get("balance")
    cashflow = raw.get("cashflow")

    if income is None or balance is None:
        return {"verdict": None, "dimensions": [], "heatmap": None}

    periods = annual_periods(income, 6)
    if not periods:
        return {"verdict": None, "dimensions": [], "heatmap": None}

    n = len(periods)

    def bcol(*keys):
        return col_vals(balance, fcol(balance, *keys), periods)
    def icol(*keys):
        return col_vals(income,  fcol(income,  *keys), periods)

    # ── 原始科目（按 periods 降序，[0] 为最新） ──
    revenue   = icol("营业收入")
    fin_exp   = icol("财务费用")
    int_exp   = icol("利息费用")
    pretax    = icol("利润总额")
    rd_exp    = icol("研发费用")

    ar        = bcol("应收账款")
    inventory = bcol("存货")
    oth_recv  = bcol("其他应收款(合计)", "其他应收款")
    dev_exp   = bcol("开发支出")
    st_loan   = bcol("短期借款")
    lt_loan   = bcol("长期借款")
    bond      = bcol("应付债券")
    cur_due   = bcol("一年内到期的非流动负债")
    cur_asset = bcol("流动资产合计")
    cur_liab  = bcol("流动负债合计")
    assets    = bcol("资产总计")
    liab      = bcol("负债合计")
    equity    = _get_equity(balance, periods)

    # OCF（对齐年报期）
    ocf = [None] * n
    if cashflow is not None:
        oc = fcol(cashflow, "经营活动产生的现金流量净额", "经营活动产生")
        if oc:
            cf_map = dict(zip(cashflow["_date"].astype(str).tolist(), cashflow[oc].tolist()))
            ocf = [sf(cf_map.get(p)) for p in periods]

    # 有息负债 = 短期借款 + 一年内到期非流动负债 + 长期借款 + 应付债券
    def _isum(i, *lists):
        vals = [l[i] for l in lists]
        nn = [v for v in vals if v is not None]
        return sum(nn) if nn else None
    int_debt = [_isum(i, st_loan, cur_due, lt_loan, bond) for i in range(n)]

    # 利息费用（用于覆盖率）：优先 利息费用，缺失用 财务费用
    def _int(i):
        if int_exp[i] not in (None, 0):
            return int_exp[i]
        return fin_exp[i]

    # ── 趋势型指标：增速差（应收/存货 增速 − 营收增速），按年逐期 ──
    def _yoy_gap(num, den):
        g = [None] * n
        for i in range(n - 1):
            nu = sdiv(num[i] - num[i + 1], abs(num[i + 1])) if (num[i] is not None and num[i + 1] not in (None, 0)) else None
            de = sdiv(den[i] - den[i + 1], abs(den[i + 1])) if (den[i] is not None and den[i + 1] not in (None, 0)) else None
            if nu is not None and de is not None:
                g[i] = nu - de
        return g

    rev_gap = _yoy_gap(ar, revenue)         # 收入质量
    inv_gap = _yoy_gap(inventory, revenue)  # 库存压力

    # ── 各指标按年序列 ──
    oth_ratio = [sdiv(oth_recv[i], assets[i]) for i in range(n)]
    # 研发资本化比例 = 本期资本化(Δ开发支出) /（研发费用 + 本期资本化）
    # 开发支出是资产负债表余额（存量），需取同比增量近似当期资本化（流量）
    cap_ratio = [None] * n
    for i in range(n):
        de_now  = dev_exp[i]
        de_prev = dev_exp[i + 1] if i + 1 < n else None
        if de_now is None:
            cap_ratio[i] = 0.0 if rd_exp[i] is not None else None   # 无开发支出 → 全部费用化
        elif de_prev is None:
            cap_ratio[i] = None                                     # 最早一年无增量，无法计算
        else:
            delta = max(de_now - de_prev, 0.0)
            denom = (rd_exp[i] or 0) + delta
            cap_ratio[i] = (delta / denom) if denom else 0.0
    dr        = [sdiv(liab[i], assets[i]) for i in range(n)]
    quick     = [
        sdiv((cur_asset[i] - inventory[i]) if (cur_asset[i] is not None and inventory[i] is not None) else None,
             cur_liab[i])
        for i in range(n)
    ]

    # 利息保障倍数 = EBIT / 利息费用；无有息负债视为绿灯
    icr, icr_status = [], []
    for i in range(n):
        ie = _int(i)
        if not int_debt[i]:                      # 无有息负债 → 偿债无压力
            icr.append(None); icr_status.append("green")
        elif ie in (None, 0):
            icr.append(None); icr_status.append("grey")
        else:
            ebit = (pretax[i] or 0) + ie
            v = ebit / ie
            icr.append(v)
            icr_status.append(_traffic(v, 3, 1, higher_is_better=True))

    # 经营现金流 / 有息负债；无有息负债视为绿灯
    ocf_debt, ocf_debt_status = [], []
    for i in range(n):
        if not int_debt[i]:
            ocf_debt.append(None); ocf_debt_status.append("green")
        else:
            v = sdiv(ocf[i], int_debt[i])
            ocf_debt.append(v)
            ocf_debt_status.append(_traffic(v, 0.2, 0.1, higher_is_better=True)
                                   if v is not None else "grey")

    # ── 指标规格表（含每年状态序列，[0] 最新） ──
    def _ss(vals, g, y, higher):
        return [_traffic(v, g, y, higher) for v in vals]

    SPECS = [
        # A · 资产端
        {"group": "资产端", "name": "收入质量", "vals": rev_gap,
         "status_series": [(_traffic(v, 0.0, 0.10, False) if v is not None else "grey") for v in rev_gap],
         "fmt": "pp", "unit": "应收增速 − 营收增速",
         "signal": "<0 健康 ｜ 0–10pp 关注 ｜ >10pp 警示"},
        {"group": "资产端", "name": "库存压力", "vals": inv_gap,
         "status_series": [(_traffic(v, 0.05, 0.15, False) if v is not None else "grey") for v in inv_gap],
         "fmt": "pp", "unit": "存货增速 − 营收增速",
         "signal": "<5pp 健康 ｜ 5–15pp 关注 ｜ >15pp 警示"},
        {"group": "资产端", "name": "其他应收占比", "vals": oth_ratio,
         "status_series": _ss(oth_ratio, 0.05, 0.10, False),
         "fmt": "pct", "unit": "其他应收款 / 总资产",
         "signal": "<5% 健康 ｜ 5–10% 关注 ｜ >10% 警示（资金占用高发区）"},
        {"group": "资产端", "name": "研发资本化", "vals": cap_ratio,
         "status_series": [(_traffic(v, 0.10, 0.30, False) if v is not None else "grey") for v in cap_ratio],
         "fmt": "pct", "unit": "Δ开发支出 /（研发费用+Δ开发支出）",
         "signal": "<10% 健康 ｜ 10–30% 关注 ｜ >30% 警示（过度资本化虚增利润）"},
        # B · 偿债与杠杆
        {"group": "偿债与杠杆", "name": "资产负债率", "vals": dr,
         "status_series": _ss(dr, 0.50, 0.70, False),
         "fmt": "pct", "unit": "负债合计 / 总资产",
         "signal": "<50% 健康 ｜ 50–70% 关注 ｜ >70% 警示（行业差异大）"},
        {"group": "偿债与杠杆", "name": "速动比率", "vals": quick,
         "status_series": _ss(quick, 1.0, 0.5, True),
         "fmt": "x", "unit": "(流动资产−存货) / 流动负债",
         "signal": ">1.0 健康 ｜ 0.5–1.0 关注 ｜ <0.5 警示"},
        {"group": "偿债与杠杆", "name": "利息保障倍数", "vals": icr,
         "status_series": icr_status,
         "fmt": "x", "unit": "EBIT / 利息费用",
         "signal": ">3 健康 ｜ 1–3 关注 ｜ <1 危险（无有息负债视为健康）"},
        {"group": "偿债与杠杆", "name": "现金流偿债覆盖", "vals": ocf_debt,
         "status_series": ocf_debt_status,
         "fmt": "x", "unit": "经营现金流 / 有息负债",
         "signal": ">0.2 健康 ｜ 0.1–0.2 关注 ｜ <0.1 警示"},
    ]

    def _disp(v, fmt):
        if v is None:
            return None
        if fmt == "pct":
            return round(v * 100, 1)
        if fmt == "pp":
            return round(v * 100, 1)
        return round(v, 2)  # x

    # ── 维度分组（取最新期状态/值用于卡片） ──
    groups = {"资产端": [], "偿债与杠杆": []}
    for s in SPECS:
        groups[s["group"]].append({
            "name":   s["name"],
            "value":  _disp(s["vals"][0], s["fmt"]),
            "unit":   ("%（" + s["unit"] + "）") if s["fmt"] in ("pct", "pp")
                      else ("×（" + s["unit"] + "）"),
            "status": s["status_series"][0],
            "desc":   s["unit"],
            "signal": s["signal"],
        })
    dimensions = [
        {"group": "A · 资产端",     "items": groups["资产端"]},
        {"group": "B · 偿债与杠杆", "items": groups["偿债与杠杆"]},
    ]

    # ── 一票否决判定（基于最新期） ──
    latest = [s["status_series"][0] for s in SPECS]
    red    = latest.count("red")
    yellow = latest.count("yellow")
    green  = latest.count("green")
    if red >= 1:
        level, label, summary = "FAIL", "不通过", f"{red} 项红灯 → 红灯一票否决，估值结果作废，标的剔除"
    elif yellow >= 3:
        level, label, summary = "WARNING", "警示", f"{yellow} 项黄灯 → 估值结果打折，标记观察"
    elif green == len(latest):
        level, label, summary = "PREMIUM", "优质", "全部绿灯 → 优先纳入候选池"
    else:
        level, label, summary = "PASS", "通过", "无红灯、黄灯 ≤2 → 可进入估值流程"
    verdict = {"level": level, "label": label,
               "red": red, "yellow": yellow, "green": green, "summary": summary}

    # ── 历史热力图（年份升序，左→右；状态/值同序） ──
    pasc = list(reversed(periods))
    heatmap = {
        "periods": pasc,
        "rows": [{
            "name":     s["name"],
            "group":    s["group"],
            "statuses": list(reversed(s["status_series"])),
            "values":   [_disp(v, s["fmt"]) for v in reversed(s["vals"])],
        } for s in SPECS],
    }

    # ── 辅助趋势数据（应收/存货 vs 营收增速柱状图） ──
    trend_periods = periods[:-1]
    rev_yoys = yoy_list(revenue)[:-1]
    ar_yoys  = yoy_list(ar)[:-1]
    inv_yoys = yoy_list(inventory)[:-1]

    return {
        "verdict":        verdict,
        "dimensions":     dimensions,
        "heatmap":        heatmap,
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
    东财日频估值（stock_value_em）→ 历史 PE-TTM / PB 序列（近 5 年，日频）。

    替代已失效的新浪 stock_financial_analysis_indicator 通路——新浪旧财务指标页
    (vFD_FinancialGuideLine) 改版后 akshare 解析崩（soup.find(id='con02-1') 返回 None
    → 'NoneType' object has no attribute 'find'）。stock_value_em 直接给日频
    PE(TTM)/PE(静)/市净率/总市值等，2018 至今、可达，且 PE-TTM 与 Wind 口径一致
    （实测 600519：18.03 对 18.03）。

    Returns: DataFrame [date(YYYYMMDD), pe, pb]，本期在最后一行。失败 → None
    """
    try:
        df = ak.stock_value_em(symbol=code)
        if df is None or df.empty:
            return None
    except Exception as e:
        print(f"[ERR] 东财日频估值(stock_value_em): {e}")
        return None

    cols = list(df.columns)

    def _col(*keys):
        for k in keys:
            for c in cols:
                if k in str(c):
                    return c
        return None

    date_c = _col("数据日期", "日期") or cols[0]
    pe_c   = _col("PE(TTM)", "市盈率(TTM)", "市盈率")
    pb_c   = _col("市净率", "PB")
    if pe_c is None:
        return None

    out = pd.DataFrame({
        "dt": pd.to_datetime(df[date_c], errors="coerce"),
        "pe": pd.to_numeric(df[pe_c], errors="coerce"),
        "pb": (pd.to_numeric(df[pb_c], errors="coerce") if pb_c else pd.NA),
    }).dropna(subset=["dt"]).sort_values("dt")
    if out.empty:
        return None

    # 近 5 年（与图注「近 5 年」一致）；保留日频，Plotly 可平滑渲染、分位样本更充分
    cutoff = out["dt"].max() - pd.DateOffset(years=5)
    out = out[out["dt"] >= cutoff].copy()
    out["date"] = out["dt"].dt.strftime("%Y%m%d")
    res = out[["date", "pe", "pb"]].dropna(subset=["pe"])
    return res if not res.empty else None


def _get_pe_history(code):
    """历史 PE/PB 序列（东财日频估值 stock_value_em）"""
    return _get_pe_history_ak(code)


# ── 周期股 / 金融股判定（用于 PB 估值路径路由）──────────────────
_CYCLICAL_KW = [
    "石油石化", "基础化工", "钢铁", "有色金属", "煤炭", "建筑材料", "房地产",
    "化学原料", "化学纤维", "化纤", "水泥", "玻璃",
    "航运", "港口", "航空", "养殖", "生猪", "畜禽",
]
_FINANCIAL_KW = ["银行", "证券", "保险", "非银金融", "多元金融"]


def _roe_series(income, balance, periods):
    """按年 ROE(%) = 归母净利润 / 归母权益，与 periods 对齐（缺失 None）。"""
    prof_col = fcol(income, "归属于母公司所有者的净利润", "净利润")
    profits  = col_vals(income, prof_col, periods) if prof_col else [None] * len(periods)
    equity   = _get_equity(balance, periods) if balance is not None else [None] * len(periods)
    out = []
    for p, e in zip(profits, equity):
        out.append(round(p / e * 100, 2) if (p is not None and e not in (None, 0)) else None)
    return out


def _detect_cyclical(industry, eps_vals, roe_vals=None, eps_growth=None):
    """
    周期股 / 金融股判定（简明三步）。返回 {cyclical, financial, reason}。

      Step1 金融：银行/券商/保险 → financial（PB 惯例，另立口径）
      Step2 行业白名单（主）：申万强周期行业关键词命中 → cyclical
      Step3 盈利波动（辅，抓白名单外隐性周期，任一命中）：
            近年亏损 / EPS峰谷比≥3 / |3年EPS增速|>40% / ROE极差≥20pct
    """
    ind = str(industry or "")
    if any(k in ind for k in _FINANCIAL_KW):
        return {"cyclical": False, "financial": True, "reason": "金融业（PB 惯例）"}

    reasons = []
    if any(k in ind for k in _CYCLICAL_KW):
        reasons.append("周期行业")
    eps = [e for e in (eps_vals or []) if e is not None]
    pos = [e for e in eps if e > 0]
    if eps and min(eps) < 0:
        reasons.append("近年亏损")
    if len(pos) >= 2 and min(pos) > 0 and max(pos) / min(pos) >= 3:
        reasons.append("EPS峰谷比≥3")
    if eps_growth is not None and abs(eps_growth) > 0.40:
        reasons.append("3年EPS增速极端")
    rr = [r for r in (roe_vals or []) if r is not None]
    if len(rr) >= 3 and (max(rr) - min(rr)) >= 20:
        reasons.append("ROE极差≥20pct")

    return {"cyclical": bool(reasons), "financial": False, "reason": "、".join(reasons)}


def compute_valuation(code, raw, price, industry=""):
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
    balance = raw.get("balance")

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
    eps_annual = next((e for e in eps_vals if e is not None and e > 0), None)   # 最新正年报 EPS

    # 3 年历史 EPS CAGR 作为增速代理（周期峰谷会算出极端值，外推时加护栏）
    eps_growth = None
    valid = [(i, e) for i, e in enumerate(eps_vals) if e is not None and e > 0]
    if len(valid) >= 4:
        e_new, e_old = valid[0][1], valid[3][1]
        if e_old > 0:
            eps_growth = (e_new / e_old) ** (1 / 3) - 1

    # 估值基准 EPS：优先 TTM（现价 / TTM-PE，与模型 PE 中位口径一致），退最新年报 EPS。
    # 避免「TTM 口径的 PE」乘到「滞后 / 低谷年报 EPS」上——周期股低谷年报会严重低估。
    eps_ttm = sdiv(price, pe_current) if (pe_current and pe_current > 0) else eps_annual

    # 外推增速护栏：不把周期峰谷算出的极端 CAGR（如 -56%）直接外推
    g_fwd       = max(-0.10, min(0.30, eps_growth)) if eps_growth is not None else None
    eps_forward = eps_ttm * (1 + g_fwd) if (eps_ttm and g_fwd is not None) else eps_ttm

    peg = sdiv(pe_current, (eps_growth or 0) * 100) if (pe_current and eps_growth and eps_growth > 0) else None

    # 周期股 / 金融股判定（行业白名单 + 盈利波动）→ 决定是否走 PB 路径
    roe_vals = _roe_series(income, balance, periods)
    _cyc = _detect_cyclical(industry, eps_vals, roe_vals, eps_growth)
    cyclical        = _cyc["cyclical"]
    financial       = _cyc["financial"]
    cyclical_reason = _cyc["reason"]

    # ── 动态 PE：现价 / 最新报告期 EPS 年化（累计EPS × 4/季度数）──────
    #    纯已披露数据、不含预测；季报年化对季节性强的标的会有偏差（动态市盈率固有口径）
    pe_dynamic = None
    if eps_col and price and "_date" in income.columns and len(income):
        _idx = income["_date"].astype(str).idxmax()          # 最新报告期
        _ld  = str(income.loc[_idx, "_date"])
        _le  = sf(income.loc[_idx, eps_col])                 # 该期累计 EPS
        if _le and _le > 0 and len(_ld) == 8:
            _q = int(_ld[4:6]) // 3 or 4                      # 03→1 / 06→2 / 09→3 / 12→4
            _dyn_eps = _le * 4.0 / _q
            pe_dynamic = round(price / _dyn_eps, 1) if _dyn_eps > 0 else None

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
            # 原「Forward PE」依赖一致预期/预测 EPS（权威数据获取壁垒）→ 改用动态 PE：
            # 现价/最新报告期年化 EPS，纯已披露；动态 < TTM 表示本年盈利较滚动 12 月提速
            "name":   "动态 PE vs TTM",
            "value":  (f"{pe_dynamic:.1f}× / TTM {pe_current:.1f}×" if (pe_dynamic and pe_current)
                       else (f"{pe_dynamic:.1f}×" if pe_dynamic else "N/A")),
            "signal": sig(pe_dynamic < pe_current if (pe_dynamic and pe_current) else None,
                          pe_dynamic < pe_current * 1.15 if (pe_dynamic and pe_current) else None),
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

    # ── 周期 / 金融股 PB 估值路径（穿越周期）────────────────────
    #    以 历史 PB 分位 × BVPS 定目标价（PB 比低谷 EPS 稳）；正常化 ROE = 周期 ROE 均值
    pb_path = None
    if (cyclical or financial) and pb_current and pb_current > 0 and pb_series:
        bvps = price / pb_current                       # 每股净资产 = 现价 / 当前PB
        s = pd.Series([x for x in pb_series if x and x > 0])
        if len(s) >= 6 and bvps > 0:
            pb_lo, pb_mid, pb_hi = (float(s.quantile(0.25)), float(s.quantile(0.50)), float(s.quantile(0.75)))
            rr = [r for r in roe_vals if r is not None]
            norm_roe = round(sum(rr) / len(rr), 1) if rr else None
            norm_eps = round(norm_roe / 100 * bvps, 2) if norm_roe else None
            pb_scen = []
            for label, pbx in [("悲观", pb_lo), ("中性", pb_mid), ("乐观", pb_hi)]:
                tp = pbx * bvps
                up = sdiv(tp - price, price)
                pb_scen.append({"scenario": label, "pb": round(pbx, 2),
                                "target_price": round(tp, 2),
                                "upside": round(up * 100, 1) if up is not None else None})
            pb_path = {
                "financial":     financial,
                "pb_current":    round(pb_current, 2),
                "pb_median":     round(pb_mid, 2),
                "pb_percentile": round(pb_pct, 0) if pb_pct is not None else None,
                "bvps":          round(bvps, 2),
                "norm_roe":      norm_roe,
                "norm_eps":      norm_eps,
                "scenarios":     pb_scen,
                "target":        {"low": round(pb_lo * bvps, 2), "mid": round(pb_mid * bvps, 2),
                                  "high": round(pb_hi * bvps, 2)},
            }

    return {
        "pe_current":    round(pe_current, 1)  if pe_current  else None,
        "pb_current":    round(pb_current, 2)  if pb_current  else None,
        "pe_dynamic":    pe_dynamic,
        "pe_median":     round(pe_median, 1)   if pe_median   else None,
        "pb_median":     round(pb_median, 2)   if pb_median   else None,
        "pe_percentile": round(pe_pct, 0)      if pe_pct is not None else None,
        "pb_percentile": round(pb_pct, 0)      if pb_pct is not None else None,
        "peg":           round(peg, 2)          if peg         else None,
        "eps_ttm":       round(eps_ttm, 2)     if eps_ttm     else None,
        "eps_annual":    round(eps_annual, 2)  if eps_annual  else None,
        "eps_forward":   round(eps_forward, 2) if eps_forward else None,
        "eps_growth_3y": round(eps_growth * 100, 1) if eps_growth else None,
        "cyclical":       cyclical,
        "financial":      financial,
        "cyclical_reason": cyclical_reason,
        "pb_path":        pb_path,
        "matrix":        matrix,
        "positive_count": positive_count,
        "scenarios":     scenarios,
        "target":        {"low": v_low, "mid": v_mid, "high": v_high},
        "history":       {"dates": dates, "pe": pe_series, "pb": pb_series},
    }


def _inject_wind_multiples(val, wmkt):
    """
    用 Wind 权威 PE(TTM) / PB(LF) / 总市值 覆盖估值当前倍数。

    compute_valuation 的当前 PE/PB 来自 _get_pe_history 历史序列末值——该序列在部分标的
    （新股 / 被墙接口）缺失或滞后。Wind 提供的是权威实时口径，故一旦可得就覆盖显示值。
    仅当 compute_valuation 已产出**非空**估值时注入，避免拼出半成品估值。就地修改 val。

    Args:
        val:  compute_valuation 的返回（dict，可能为空 {}）
        wmkt: wind_data.market_indicators 的返回（dict 或 None）
    """
    if not isinstance(val, dict) or not val or not wmkt:
        return
    # 仅在历史序列(stock_value_em)未给出当前倍数时用 Wind 兜底，
    # 否则以序列末值为准，保证「历史趋势图」的当前参考线与序列自洽
    if wmkt.get("pe_ttm") is not None and not val.get("pe_current"):
        val["pe_current"] = round(wmkt["pe_ttm"], 1)
    if wmkt.get("pb") is not None and not val.get("pb_current"):
        val["pb_current"] = round(wmkt["pb"], 2)
    if wmkt.get("mktcap") is not None:
        val["mktcap"] = round(wmkt["mktcap"] / 1e8, 1)   # 元 → 亿元
    val["wind"] = True                                   # 标记：估值倍数已叠加 Wind


def _build_pe_panel(val, price, wmkt):
    """
    三口径 PE 面板：静态 / TTM / 动态，就地写入 val['pe_panel']。

    - 静态 PE：Wind 市盈率(LYR)（去年基准，上年报 EPS）；缺失退 price / 最新年度 EPS
    - TTM PE：Wind 市盈率(TTM)（当前实况，滚动 12 月）+ 历史分位（来自 compute_valuation）
    - 动态 PE：现价 / 最新报告期 EPS 年化（compute_valuation 已算 pe_dynamic，纯已披露、无预测）

    三口径按盈利基期由旧到新排列（去年 → 滚动12月 → 本年年化），并列看盈利与估值趋势。

    Args:
        val:   compute_valuation 返回（dict；空 {} 时直接跳过）
        price: 现价（Wind 或新浪）
        wmkt:  wind_data.market_indicators 返回（dict 或 None）
    """
    if not isinstance(val, dict) or not val:
        return
    wmkt = wmkt or {}

    ttm = wmkt.get("pe_ttm") or val.get("pe_current")
    static = wmkt.get("pe_lyr")
    if static is None and price and val.get("eps_annual") and val["eps_annual"] > 0:
        static = price / val["eps_annual"]     # 静态用最新年报 EPS（非 TTM）

    val["pe_panel"] = {
        "static":         round(static, 1) if static else None,
        "ttm":            round(ttm, 1) if ttm else None,
        "ttm_percentile": val.get("pe_percentile"),
        "dynamic":        val.get("pe_dynamic"),
    }


# ═══════════════════════════════════════════════════════════════
# 盈利预测（业绩检验/归因之后、PE-PB 估值之前的前置环节）
# ═══════════════════════════════════════════════════════════════
def compute_forecast(raw, val=None, price=None):
    """
    自建盈利预测：历史 CAGR 外推（方法 A）+ 毛利率/费用率/税率/归母比例历史假设，
    生成三档情景（悲观/基准/乐观）的营收→毛利→费用→税→归母净利润→EPS，
    并做营收增速 × 毛利率 敏感性矩阵与单变量冲击。永远输出区间，假设全部显式化。

    可行性调整：一致预期（方法 B，stock_profit_forecast_em）所需数据源在本地代理环境不可用，
    本模块以方法 A（历史 CAGR）+ 历史假设为基础，预期差对比暂缺。
    （方法 C 量价模型因销量/ASP/行业规模等数据三种数据源均无法提供，已从框架删除。）

    净利润推导：以「当前实际归母净利率」为锚，按毛利率/费用率相对基准的偏离（税后）调整，
    避免自下而上 营收×毛利率−费用−税 对投资收益/少数股东损益占比大的控股型公司失真。
    """
    income = raw.get("income")
    if income is None:
        return {}
    periods = annual_periods(income, 6)
    if len(periods) < 2:
        return {}

    rev    = col_vals(income, fcol(income, "营业收入"), periods)
    cost   = col_vals(income, fcol(income, "营业成本"), periods)
    taxsur = col_vals(income, fcol(income, "税金及附加", "营业税金及附加"), periods)
    pretax = col_vals(income, fcol(income, "利润总额"), periods)
    taxexp = col_vals(income, fcol(income, "所得税费用"), periods)
    np_tot = col_vals(income, fcol(income, "净利润"), periods)
    np_par = col_vals(income, fcol(income, "归属于母公司所有者的净利润", "净利润"), periods)
    eps    = col_vals(income, fcol(income, "基本每股收益", "每股收益"), periods)
    sell   = col_vals(income, fcol(income, "销售费用"), periods)
    admin  = col_vals(income, fcol(income, "管理费用"), periods)
    rd     = col_vals(income, fcol(income, "研发费用"), periods)
    fin    = col_vals(income, fcol(income, "财务费用"), periods)

    if not rev or rev[0] in (None, 0):
        return {}

    n = len(periods)

    def _gm(i):
        return sdiv((rev[i] - cost[i]) if (rev[i] is not None and cost[i] is not None) else None, rev[i])
    def _exp(i):
        parts = [sell[i], admin[i], rd[i], fin[i]]
        if all(p is None for p in parts):
            return None
        return sdiv(sum(p for p in parts if p is not None), rev[i])
    def _mean(fn, k=3):
        vals = [fn(i) for i in range(min(k, n))]
        vals = [v for v in vals if v is not None]
        return (sum(vals) / len(vals)) if vals else None

    gm_hist  = [_gm(i) for i in range(n)]
    exp_hist = [_exp(i) for i in range(n)]

    # ── 基准假设 ──
    def _cagr(k):
        if n > k and rev[k] not in (None, 0) and rev[0] is not None and rev[0] > 0 and rev[k] > 0:
            return (rev[0] / rev[k]) ** (1 / k) - 1
        return None
    cagr3 = _cagr(3)
    cagr5 = _cagr(5)
    cands = [c for c in (cagr3, cagr5) if c is not None]
    rev_growth_base = (sum(cands) / len(cands)) if cands else 0.0
    rev_growth_base = max(min(rev_growth_base, 1.0), -0.5)   # 防极端值

    gross_margin_base = gm_hist[0] if gm_hist[0] is not None else _mean(_gm, n)
    exp_hist3 = _mean(_exp, 3)

    # 规模效应摊薄（营收增速越高，期间费用率摊薄越多，单位 pct）
    g = rev_growth_base
    if   g < 0.10: dampen = 0.0
    elif g < 0.30: dampen = 0.0035
    elif g < 0.60: dampen = 0.0075
    else:          dampen = 0.015
    expense_ratio_base = max((exp_hist3 - dampen), 0.0) if exp_hist3 is not None else None

    taxsur_rate = _mean(lambda i: sdiv(taxsur[i], rev[i]), 3) or 0.0
    eff_tax = _mean(lambda i: sdiv(taxexp[i], pretax[i]), 3)
    eff_tax = min(max(eff_tax, 0.0), 0.40) if eff_tax is not None else 0.25
    parent_ratio = _mean(lambda i: sdiv(np_par[i], np_tot[i]), 3)
    parent_ratio = min(max(parent_ratio, 0.0), 1.2) if parent_ratio is not None else 1.0

    rev0, eps0, np_par0 = rev[0], eps[0], np_par[0]

    # 归母净利率锚：直接用当前实际归母净利率作基准（缺失退 3 年均值）。
    # 比自下而上 营收×毛利率−费用−税 更稳健——后者对投资收益/减值/少数股东
    # 损益占比大的控股型公司（如 TCL）会系统性失真。情景在此锚上按
    # 毛利率/费用率偏离（税后）调整净利率。
    base_net_margin = sdiv(np_par0, rev0)
    if base_net_margin is None:
        base_net_margin = _mean(lambda i: sdiv(np_par[i], rev[i]), 3)

    def _derive(growth, gmargin, expense):
        """给定营收增速/毛利率/期间费用率 → 归母净利润(元) 与 EPS(元)"""
        if base_net_margin is None or gmargin is None or expense is None \
           or gross_margin_base is None or expense_ratio_base is None:
            return rev0 * (1 + growth) if rev0 else None, None, None
        revenue1 = rev0 * (1 + growth)
        # 相对基准的毛利率↑/费用率↓ → 税前利润率改善，乘 (1−有效税率) 落到净利率
        d_pretax = (gmargin - gross_margin_base) - (expense - expense_ratio_base)
        net_margin = base_net_margin + d_pretax * (1 - eff_tax)
        parent = revenue1 * net_margin
        eps1 = (eps0 * parent / np_par0) if (eps0 not in (None, 0) and np_par0 not in (None, 0)) else None
        return revenue1, parent, eps1

    pe_median = (val or {}).get("pe_median")

    # ── 三档情景 ──
    SCEN = [
        ("悲观", rev_growth_base - 0.10, (gross_margin_base - 0.02) if gross_margin_base is not None else None,
         (expense_ratio_base + 0.005) if expense_ratio_base is not None else None),
        ("基准", rev_growth_base, gross_margin_base, expense_ratio_base),
        ("乐观", rev_growth_base + 0.10, (gross_margin_base + 0.015) if gross_margin_base is not None else None,
         (expense_ratio_base - 0.01) if expense_ratio_base is not None else None),
    ]
    scenarios = []
    for name, gth, gm, ex in SCEN:
        revenue1, parent, eps1 = _derive(gth, gm, ex)
        fwd_pe = sdiv(price, eps1) if (price and eps1 and eps1 > 0) else None
        target = (eps1 * pe_median) if (eps1 and pe_median) else None
        scenarios.append({
            "name": name,
            "rev_growth":   round(gth * 100, 1),
            "revenue":      round(revenue1 / 1e8, 1) if revenue1 is not None else None,
            "gross_margin": round(gm * 100, 1) if gm is not None else None,
            "expense_ratio":round(ex * 100, 1) if ex is not None else None,
            "net_profit":   round(parent / 1e8, 1) if parent is not None else None,
            "eps":          round(eps1, 2) if eps1 is not None else None,
            "fwd_pe":       round(fwd_pe, 1) if fwd_pe else None,
            "target":       round(target, 2) if target else None,
        })

    # ── 基准情景 3 年轨迹（复利）──
    traj = {"years": [], "revenue": [], "net_profit": [], "eps": []}
    for t in (1, 2, 3):
        gth = (1 + rev_growth_base) ** t - 1
        revenue_t, parent_t, eps_t = _derive(gth, gross_margin_base, expense_ratio_base)
        traj["years"].append(f"T+{t}")
        traj["revenue"].append(round(revenue_t / 1e8, 1) if revenue_t is not None else None)
        traj["net_profit"].append(round(parent_t / 1e8, 1) if parent_t is not None else None)
        traj["eps"].append(round(eps_t, 2) if eps_t is not None else None)

    # ── 敏感性矩阵：营收增速 × 毛利率 → 归母净利润(亿) ──
    sensitivity = {}
    if gross_margin_base is not None and expense_ratio_base is not None:
        rg = [rev_growth_base + d for d in (-0.20, -0.10, 0.0, 0.10, 0.20)]
        gms = [gross_margin_base + d for d in (-0.02, -0.01, 0.0, 0.01, 0.02)]
        matrix = []
        for gth in rg:
            row = []
            for gm in gms:
                _, parent, _ = _derive(gth, gm, expense_ratio_base)
                row.append(round(parent / 1e8, 1) if parent is not None else None)
            matrix.append(row)
        sensitivity = {
            "rev_growths":   [round(x * 100, 1) for x in rg],
            "gross_margins": [round(x * 100, 1) for x in gms],
            "matrix":        matrix, "base_i": 2, "base_j": 2,
        }

    # ── 单变量冲击（相对基准的归母净利润变化）──
    shocks = []
    _, base_np, _ = _derive(rev_growth_base, gross_margin_base, expense_ratio_base)
    if base_np:
        def _shock(label, gth, gm, ex):
            _, np_s, _ = _derive(gth, gm, ex)
            if np_s is None:
                return
            shocks.append({"var": label,
                           "net_profit": round(np_s / 1e8, 1),
                           "delta": round((np_s - base_np) / abs(base_np) * 100, 1)})
        _shock("营收增速 +10pct", rev_growth_base + 0.10, gross_margin_base, expense_ratio_base)
        _shock("毛利率 +2pct",    rev_growth_base, (gross_margin_base + 0.02) if gross_margin_base is not None else None, expense_ratio_base)
        _shock("期间费用率 +1pct", rev_growth_base, gross_margin_base, (expense_ratio_base + 0.01) if expense_ratio_base is not None else None)
        _shock("有效税率 +2pct",  rev_growth_base, gross_margin_base, expense_ratio_base)  # 见下：税率单独处理

    # 税率冲击单独算（_derive 用固定 eff_tax，这里手动按净利率缩放）
    if base_np and base_net_margin not in (None, 0):
        revenue1 = rev0 * (1 + rev_growth_base)
        nm_tax = base_net_margin * (1 - min(eff_tax + 0.02, 0.40)) / (1 - eff_tax)
        np_tax = revenue1 * nm_tax
        for s in shocks:
            if s["var"] == "有效税率 +2pct":
                s["net_profit"] = round(np_tax / 1e8, 1)
                s["delta"] = round((np_tax - base_np) / abs(base_np) * 100, 1)

    asc = list(range(n - 1, -1, -1))   # 升序索引
    history = {
        "periods":       [periods[i] for i in asc],
        "revenue":       [round(rev[i] / 1e8, 1) if rev[i] is not None else None for i in asc],
        "gross_margin":  [round(gm_hist[i] * 100, 1) if gm_hist[i] is not None else None for i in asc],
        "expense_ratio": [round(exp_hist[i] * 100, 1) if exp_hist[i] is not None else None for i in asc],
        "net_profit":    [round(np_par[i] / 1e8, 1) if np_par[i] is not None else None for i in asc],
        "eps":           [round(eps[i], 2) if eps[i] is not None else None for i in asc],
    }

    return {
        "base_period": periods[0],
        "history":     history,
        "assumptions": {
            "rev_cagr_3y":        round(cagr3 * 100, 1) if cagr3 is not None else None,
            "rev_cagr_5y":        round(cagr5 * 100, 1) if cagr5 is not None else None,
            "rev_growth_base":    round(rev_growth_base * 100, 1),
            "gross_margin_base":  round(gross_margin_base * 100, 1) if gross_margin_base is not None else None,
            "expense_ratio_hist3":round(exp_hist3 * 100, 1) if exp_hist3 is not None else None,
            "scale_dampen":       round(dampen * 100, 2),
            "expense_ratio_base": round(expense_ratio_base * 100, 1) if expense_ratio_base is not None else None,
            "taxsur_rate":        round(taxsur_rate * 100, 2),
            "base_net_margin":    round(base_net_margin * 100, 2) if base_net_margin is not None else None,
            "effective_tax_rate": round(eff_tax * 100, 1),
            "parent_ratio":       round(parent_ratio * 100, 1),
        },
        "current": {
            "period": periods[0],
            "revenue":    round(rev0 / 1e8, 1),
            "net_profit": round(np_par0 / 1e8, 1) if np_par0 is not None else None,
            "eps":        round(eps0, 2) if eps0 is not None else None,
        },
        "scenarios":   scenarios,
        "trajectory":  traj,
        "sensitivity": sensitivity,
        "shocks":      shocks,
        "valuation_link": {"pe_median": round(pe_median, 1) if pe_median else None, "price": price},
        "note": "方法 B（一致预期，stock_profit_forecast_em）在本地代理环境不可用，预期差对比暂缺；"
                "方法 C（量价模型）因销量/ASP/行业规模数据无法获取已删除。"
                "本预测基于方法 A（历史 CAGR）+ 历史假设、以归母净利率为锚外推。",
    }


# ═══════════════════════════════════════════════════════════════
# 量价印证层（市场印证）— 横切基本面，非独立体系
# ═══════════════════════════════════════════════════════════════
def compute_market(code, pe_pct=None, pb_pct=None, risk_level=None):
    """
    用市场量价行为印证/反驳基本面结论（不预测股价）。
    数据源：新浪日线（前复权，含换手率）+ 新浪上证指数。
    资金流向（北向/主力/龙虎榜/融资/大宗）依赖东财 push2 接口，本地代理阻断，暂不接入。

    Args:
        code: 6 位代码
        pe_pct / pb_pct: PE/PB 历史分位（来自估值模块），用于「估值低位」判定
        risk_level: 排雷一票否决结论（PASS/WARNING/FAIL/PREMIUM），用于四象限「基本面」轴
    Returns: dict {kline, recovery, rs, quadrant, fund_flow_available}
    """
    try:
        df = ak.stock_zh_a_daily(symbol=to_sina_code(code), adjust="qfq")
    except Exception as e:
        print(f"[ERR] market daily: {e}")
        return {}
    if df is None or df.empty or "close" not in df.columns:
        return {}

    df = df.copy()
    df["dt"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["dt", "close"]).sort_values("dt").reset_index(drop=True)
    if len(df) < 70:
        return {}

    close = df["close"].astype(float)
    df["ma5"]  = close.rolling(5).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()

    def _f(v, nd=2):
        return None if (v is None or pd.isna(v)) else round(float(v), nd)

    # ── 基础行情：近 250 交易日 K 线 + 量 + 均线 + 换手率 ──
    rec = df.tail(250)
    has_to = "turnover" in rec.columns
    kline = {
        "dates":   rec["dt"].dt.strftime("%Y-%m-%d").tolist(),
        "open":    [_f(v) for v in rec["open"]],
        "high":    [_f(v) for v in rec["high"]],
        "low":     [_f(v) for v in rec["low"]],
        "close":   [_f(v) for v in rec["close"]],
        "volume":  [None if pd.isna(v) else float(v) for v in rec["volume"]],
        "ma5":     [_f(v) for v in rec["ma5"]],
        "ma20":    [_f(v) for v in rec["ma20"]],
        "ma60":    [_f(v) for v in rec["ma60"]],
        "turnover":[_f(v) for v in rec["turnover"]] if has_to else [None] * len(rec),
    }

    # ── 场景 C：估值修复确认（三重共振）──
    vol   = df["volume"].astype(float)
    vol5  = vol.tail(5).mean()
    vol20 = vol.tail(25).head(20).mean()      # 前期 20 日（剔除最近 5 日）
    volume_expand = bool(vol20 and vol5 > vol20 * 1.3)

    last = df.iloc[-1]
    above_ma60 = bool(not pd.isna(last["ma60"]) and last["close"] > last["ma60"])
    ma_bullish = bool(not pd.isna(last["ma5"]) and not pd.isna(last["ma20"]) and not pd.isna(last["ma60"])
                      and last["ma5"] > last["ma20"] > last["ma60"])
    valuation_low = bool((pe_pct is not None and pe_pct <= 30) or
                         (pb_pct is not None and pb_pct <= 30))

    triggers = []
    if valuation_low: triggers.append("估值历史低位（PE/PB ≤30 分位）")
    if volume_expand: triggers.append("近 5 日均量较前期放大 30%+")
    if above_ma60:    triggers.append("收盘价站上 60 日均线")
    if ma_bullish:    triggers.append("均线多头排列（MA5>MA20>MA60）")

    if valuation_low and volume_expand and above_ma60:
        rec_verdict = "RESONANCE_BUY"   # 估值修复启动
    elif valuation_low and not above_ma60 and not volume_expand:
        rec_verdict = "VALUE_TRAP"      # 低估但市场不认
    else:
        rec_verdict = "NEUTRAL"
    recovery = {
        "valuation_low": valuation_low, "volume_expand": volume_expand,
        "above_ma60": above_ma60, "ma_bullish": ma_bullish,
        "triggers": triggers, "verdict": rec_verdict,
        "pe_pct": pe_pct, "pb_pct": pb_pct,
    }

    # ── 场景 D（部分）：个股相对强度 vs 大盘（上证综指）──
    def _ret_n(series, n=60):
        s = series.astype(float).reset_index(drop=True)
        if len(s) > n and s.iloc[-n - 1] not in (0, None):
            return (s.iloc[-1] - s.iloc[-n - 1]) / abs(s.iloc[-n - 1])
        return None

    rs = {}
    stock_ret = _ret_n(df["close"], 60)
    try:
        idx = ak.stock_zh_index_daily(symbol="sh000001")
        idx = idx.copy()
        idx["dt"] = pd.to_datetime(idx["date"], errors="coerce")
        idx = idx.dropna(subset=["dt", "close"]).sort_values("dt")
        index_ret = _ret_n(idx["close"], 60)
    except Exception as e:
        print(f"[ERR] market index: {e}")
        index_ret = None
    if stock_ret is not None and index_ret is not None:
        rs_val = (stock_ret - index_ret) * 100
        rs = {
            "window": 60,
            "stock_ret": round(stock_ret * 100, 1),
            "index_ret": round(index_ret * 100, 1),
            "rs": round(rs_val, 1),
            "label": "跑赢大盘" if rs_val >= 0 else "跑输大盘",
        }

    # ── 基本面 × 量价 四象限 ──
    quadrant = {}
    if risk_level:
        fund_good = risk_level != "FAIL"
        mkt_positive = bool(above_ma60 and not pd.isna(last["ma5"]) and not pd.isna(last["ma20"])
                            and last["ma5"] >= last["ma20"])
        if fund_good and mkt_positive:
            q = ("RESONANCE_BUY", "共振做多", "基本面通过 + 市场量价正向 → 最强信号")
        elif (not fund_good) and mkt_positive:
            q = ("BUBBLE_WARN", "共振警示", "基本面存疑 + 市场上涨 → 警惕泡沫/炒作")
        elif fund_good and (not mkt_positive):
            q = ("VALUE_TRAP", "价值陷阱", "基本面通过 + 市场冷淡 → 待市场确认，或复核基本面")
        else:
            q = ("AVOID", "静默回避", "基本面差 + 市场负向 → 避开")
        quadrant = {"fund_good": fund_good, "market_positive": mkt_positive,
                    "code": q[0], "label": q[1], "desc": q[2]}

    return {
        "kline":    kline,
        "recovery": recovery,
        "rs":       rs,
        "quadrant": quadrant,
        "fund_flow_available": False,   # 北向/主力/龙虎榜/融资：东财 push2 被代理阻断
    }


# 价格与行业信息（复用 V1 逻辑）
# ═══════════════════════════════════════════════════════════════
def _get_em_info(code):
    """
    东财 F10 接口（emweb，与主营构成同源，代理环境可达）获取「行业 / 名称」。
    替代有 pandas bug（Length mismatch）的 ak.stock_individual_info_em，
    以及被代理阻断的 push2.eastmoney.com 行情接口。
    Returns: dict {name, industry}；失败返回 {}
    """
    try:
        prefix = _exchange(code)
        url = (f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
               f"?code={prefix}{code}")
        r = _req.get(url, timeout=12,
                     headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://emweb.securities.eastmoney.com"})
        jbzl = (r.json() or {}).get("jbzl") or []
        row = (jbzl[0] if isinstance(jbzl, list) and jbzl
               else (jbzl if isinstance(jbzl, dict) else {}))

        # 行业：优先东财 EM2016 三级分类（去重连级），退 CSRC 大类
        industry = ""
        em = str(row.get("EM2016", "") or "").strip()
        if em:
            dedup = []
            for p in em.split("-"):
                p = p.strip()
                if p and p not in dedup:
                    dedup.append(p)
            industry = "-".join(dedup)
        if not industry:
            industry = str(row.get("INDUSTRYCSRC1", "") or "").strip()

        return {"name":     str(row.get("SECURITY_NAME_ABBR", "") or "").strip(),
                "industry": industry}
    except Exception as e:
        print(f"[ERR] em info: {e}")
        return {}


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
    返回 [{code, name}]，保留沪深京 A 股（sh/sz/bj + 6 位代码）。
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
        if not (sina_code[:2] in ("sh", "sz", "bj") and code.isdigit() and len(code) == 6):
            continue
        # field[0] 在纯代码查询时是 sina_code，此时回查名称
        disp = name if not name.startswith(("sh", "sz", "bj")) else (_sina_name(code) or code)
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

    # ── Wind 第一通道：名称 / 申万行业 / 现价 / 估值倍数；失败或额度用尽自动回退原通道 ──
    #    Wind 覆盖沪深京(.SH/.SZ/.BJ)：证券简称 + 申万行业全链 + 现价/总市值/PE(TTM)/PB(LF)。
    #    新三板(.NQ) Wind 不返回行情/申万 → 名称仍可得，其余走下方 NEEQ 年报兜底。
    wmkt = wbasic = None
    if wind is not None:
        try:
            wmkt = wind.market_indicators(code)
        except Exception:
            wmkt = None
        try:
            wbasic = wind.basic_info(code)
        except Exception:
            wbasic = None

    name, industry = code, ""
    if wbasic and wbasic.get("name"):
        name = wbasic["name"]
    elif wmkt and wmkt.get("name"):
        name = wmkt["name"]
    if wbasic and wbasic.get("sw_full"):
        industry = wbasic["sw_full"].replace("--", " · ")     # 申万全链（权威），显示用

    # 名称/行业兜底：本地代码表 → 新浪行情 → 东财 F10
    if name == code:
        hit = _CODE_NAME[_CODE_NAME["code"].astype(str) == code]
        if not hit.empty:
            name = str(hit.iloc[0]["name"])
    if name == code:
        name = _sina_name(code) or code
    if not industry or name == code:
        em = _get_em_info(code)
        if not industry and em.get("industry"):
            industry = em["industry"]
        if name == code and em.get("name"):
            name = em["name"]

    # 现价：Wind → 新浪行情
    price = (wmkt.get("price") if wmkt else None) or fetch_price(code)
    raw   = fetch_raw(code)

    perf  = compute_performance(raw, code)
    attr  = compute_attribution(raw)
    risk  = compute_risk(raw)
    val   = compute_valuation(code, raw, price, industry=industry)
    _inject_wind_multiples(val, wmkt)         # Wind 权威 PE(TTM)/PB/市值 覆盖当前倍数
    _build_pe_panel(val, price, wmkt)         # 三口径 PE：静态 / TTM / 动态
    fcst  = compute_forecast(raw, val, price)
    seg   = _fetch_segment_revenue(code)

    # 新三板（NEEQ）兜底：沪深京数据源取不到（perf 为空）且为北证段代码（4/8/920）
    # → 走「年报公告文本解析」流水线（neeq_data），重算基本面。仅年报、无季度、无行情。
    market = ""
    if _exchange(code) == "BJ" and not (perf and perf.get("periods")):
        try:
            import neeq_data as nq
            nraw, nmeta = nq.build_raw(code)
        except Exception as e:
            import traceback; traceback.print_exc()
            nraw, nmeta = None, None
        if nraw:
            market = "NEEQ"
            raw, price = nraw, None            # 新三板无 push2 行情 → 现价/估值/量价不可用
            if nmeta and nmeta.get("name"):
                name = nmeta["name"]
            if nmeta and nmeta.get("industry"):
                industry = nmeta["industry"]   # 年报抽取的行业（CSRC 口径）→ 行业对比跨分类检索
            perf = compute_performance(raw, code)
            attr = compute_attribution(raw)
            risk = compute_risk(raw)
            val  = compute_valuation(code, raw, price, industry=industry)
            fcst = compute_forecast(raw, val, price)
            seg  = (nmeta.get("segment") or {}) if nmeta else {}

    # 总市值（Wind 提供，元 → 亿元）；NEEQ 无 Wind 行情 → 不展示
    mktcap_yi = round(wmkt["mktcap"] / 1e8, 1) if (wmkt and wmkt.get("mktcap")) else None
    return jsonify({
        "info":        {"code": code, "name": name, "industry": industry, "price": price,
                        "market": market, "annual_only": market == "NEEQ",
                        "mktcap": mktcap_yi, "wind": bool(wmkt)},
        "performance": perf,
        "attribution": attr,
        "risk":        risk,
        "valuation":   val,
        "forecast":    fcst,
        "segment":     seg,
    })


@app.route("/api/market")
def api_market():
    """量价印证层（懒加载，避免拖慢主分析）。"""
    code = request.args.get("code", "").strip()
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"error": "请提供 6 位股票代码"}), 400

    def _fpct(key):
        v = request.args.get(key)
        try:
            return float(v) if v not in (None, "", "null") else None
        except ValueError:
            return None

    risk_level = request.args.get("risk") or None
    market = compute_market(code, _fpct("pe_pct"), _fpct("pb_pct"), risk_level)
    return jsonify({"market": market})


@app.route("/api/industry")
def api_industry():
    """行业对比层（懒加载；首次需构建申万二级反查图，约 30-60s，之后命中缓存 <2s）。"""
    code = request.args.get("code", "").strip()
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"error": "请提供 6 位股票代码"}), 400
    name = code
    hit = _CODE_NAME[_CODE_NAME["code"].astype(str) == code]
    if not hit.empty:
        name = str(hit.iloc[0]["name"])
    # Wind 申万二级名（沪深京可得）→ 直连反查图，优先于东财行业关键词跨分类
    sw_l2 = None
    if wind is not None:
        try:
            wb = wind.basic_info(code)   # 命中 analyze 阶段缓存，不额外耗额度
            if wb:
                sw_l2 = wb.get("sw_l2")
                if name == code and wb.get("name"):
                    name = wb["name"]
        except Exception:
            sw_l2 = None
    # 东财行业（北交所 / 申万未覆盖股用于映射到申万二级）：优先前端透传，缺失再查 F10
    ind_str = (request.args.get("ind") or "").strip()
    if not ind_str:
        em = _get_em_info(code)
        ind_str = em.get("industry") or ""
        if name == code and em.get("name"):
            name = em["name"]
    try:
        import industry_compare as ic
        data = ic.industry_comparison(code, name=name, em_industry=ind_str, sw_l2=sw_l2)
        return jsonify({"industry_compare": data})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": f"行业对比失败: {e}"}), 500


@app.route("/")
def index():
    return render_template("index_v5.html",
                           project_name=PROJECT_NAME,
                           project_desc=PROJECT_DESC)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
