# -*- coding: utf-8 -*-
"""
行业对比模块（按《行业对比框架》落地）
================================================================
为每只股票提供两类对比：
  A · 精选同行对比：8 维相似度筛选 Top 15 + 行业龙头锚定 + 双基准
  B · 行业相对位置：各指标行业分位 + 雷达 + 分布直方图 + 综合定位标签

数据可行性（代理环境实测）：
  - 东财 push2 接口（stock_individual_info_em / stock_board_industry_* /
    stock_zh_a_spot_em）全部被代理阻断 → 不可用
  - 申万（legulegu 源）可用：sw_index_second_info / index_component_sw
  - 个股指标 stock_financial_analysis_indicator 可用（~0.7s/只）

因此本模块完全走「申万二级」路径：
  1. 构建「证券代码 → 申万二级行业」反查图（遍历 131 个二级行业成分，
     实测 ~29s / 5198 只 / 0 失败），落盘缓存 90 天（框架 L1 思想）
  2. index_component_sw 的「最新权重」≈ 自由流通市值占比，用作
     「市值规模」相似维度与「行业龙头」锚定的代理，规避被阻断的行情源

会计/口径假设（CAS）：
  - 指标取 stock_financial_analysis_indicator 最新一期快照
  - 毛利率优先「销售毛利率」，缺失（部分公司为空）退「主营业务利润率」
  - 成长性以「近 4 期营业收入同比」均值近似（绝对营收/真 3 年 CAGR 无批量源）
  - 规模维度用「总资产」与「申万权重」双代理（总市值无批量源）
  - 中位数而非均值；剔除 ST/*ST 与 ROE 极端值（框架第三节清洗规则）

免责声明：本模块仅供学习研究，所有结论以区间/分位呈现，不构成投资建议。
"""

import os
import json
import time
import math
import datetime
import statistics

import akshare as ak

# ── 缓存目录（data/ 已在 .gitignore） ──────────────────────────
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

# ── 可配置阈值（框架 A-2，可在网页层暴露给用户调） ─────────────
#   ('rel', x): 相对偏离，|c-t|/|t| <= x ；('abs', x): 绝对差，|c-t| <= x
SIM_THRESHOLDS = {
    "weight":         ("rel", 0.50),   # 市值规模（申万权重代理）
    "total_assets":   ("rel", 0.50),   # 规模（总资产代理营收）
    "rev_growth":     ("abs", 15.0),   # 营收增速（pct）
    "roe":            ("abs", 5.0),     # ROE（pct）
    "gross_margin":   ("abs", 5.0),     # 毛利率（pct）
    "net_margin":     ("abs", 5.0),     # 净利率（pct）
    "debt_ratio":     ("abs", 10.0),    # 资产负债率（pct）
    "asset_turnover": ("abs", 0.30),    # 总资产周转率（次）
}

# 相似维度展示名
SIM_LABELS = {
    "weight": "市值(权重)", "total_assets": "规模(总资产)", "rev_growth": "营收增速",
    "roe": "ROE", "gross_margin": "毛利率", "net_margin": "净利率",
    "debt_ratio": "资产负债率", "asset_turnover": "总资产周转",
}

# B 路径分位展示的指标（key, 中文名, 是否反向[低更好]）
PCT_INDICATORS = [
    ("roe",            "ROE",          False),
    ("roa",            "ROA",          False),
    ("gross_margin",   "毛利率",        False),
    ("net_margin",     "净利率",        False),
    ("rev_growth",     "营收增速",      False),
    ("profit_growth",  "净利润增速",    False),
    ("asset_turnover", "总资产周转",    False),
    ("ar_turnover",    "应收周转",      False),
    ("inv_turnover",   "存货周转",      False),
    ("current_ratio",  "流动比率",      False),
    ("ocf_to_ni",      "现金流质量",    False),
    ("debt_ratio",     "资产负债率",    True),   # 反向：低更好
]

# 雷达 6 轴（key, 名, 反向）
RADAR_AXES = [
    ("roe",            "ROE",        False),
    ("net_margin",     "净利率",      False),
    ("rev_growth",     "营收增速",    False),
    ("asset_turnover", "总资产周转",  False),
    ("debt_ratio",     "资产负债率",  True),
    ("ocf_to_ni",      "现金流质量",  False),
]

POOL_CAP = 40          # 单行业最多分析的成分股数（按权重取前 N），控制耗时
_REQ_SLEEP = 0.30      # 个股指标抓取间隔


# ═══════════════════════════════════════════════════════════════
# 小工具
# ═══════════════════════════════════════════════════════════════
def sf(x):
    """safe float：失败 / NaN → None"""
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None


def _median(vals):
    v = [x for x in vals if x is not None]
    return statistics.median(v) if v else None


def _is_st(name):
    return "ST" in str(name or "").upper()


def _cache_path(key):
    return os.path.join(_CACHE_DIR, key + ".json")


def _cache_get(key, ttl_days):
    """读缓存；超过 ttl_days 视为失效返回 None。"""
    p = _cache_path(key)
    try:
        if not os.path.exists(p):
            return None
        if (time.time() - os.path.getmtime(p)) > ttl_days * 86400:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _cache_set(key, data):
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[行业对比] 写缓存失败 {key}: {e}")


# ═══════════════════════════════════════════════════════════════
# L1：申万二级反查图（证券代码 → 行业）
# ═══════════════════════════════════════════════════════════════
def _build_reverse_map():
    """
    遍历 131 个申万二级行业的成分，构建：
      code2ind: {证券代码: {ind_code, ind_name, parent, weight}}
      industries: {ind_code: {name, parent, members:[{code,name,weight}], pe_ttm, pb}}
    实测 ~29s / 5198 只 / 0 失败。落盘缓存 90 天。
    """
    t0 = time.time()
    l2 = ak.sw_index_second_info()
    names = dict(zip(l2["行业代码"], l2["行业名称"]))
    parents = dict(zip(l2["行业代码"], l2["上级行业"]))
    pe_ttm = dict(zip(l2["行业代码"], l2["TTM(滚动)市盈率"]))
    pb = dict(zip(l2["行业代码"], l2["市净率"]))

    code2ind, industries, fails = {}, {}, 0
    for full in l2["行业代码"].tolist():
        sw = str(full).split(".")[0]
        df = None
        for attempt in range(2):
            try:
                df = ak.index_component_sw(symbol=sw)
                break
            except Exception:
                if attempt == 0:
                    time.sleep(0.4)
                else:
                    fails += 1
        members = []
        if df is not None:
            for _, r in df.iterrows():
                code = str(r["证券代码"])
                w = sf(r["最新权重"])
                nm = str(r["证券名称"])
                code2ind[code] = {"ind_code": sw, "ind_name": names.get(full, ""),
                                  "parent": parents.get(full, ""), "weight": w}
                members.append({"code": code, "name": nm, "weight": w})
        industries[sw] = {"name": names.get(full, ""), "parent": parents.get(full, ""),
                          "members": members, "pe_ttm": sf(pe_ttm.get(full)),
                          "pb": sf(pb.get(full))}
        time.sleep(0.10)

    data = {"code2ind": code2ind, "industries": industries,
            "n_stocks": len(code2ind), "n_ind": len(industries),
            "fails": fails, "built_sec": round(time.time() - t0, 1)}
    print(f"[行业对比] 反查图构建：{data['n_stocks']} 只 / {data['n_ind']} 行业 / "
          f"失败 {fails} / {data['built_sec']}s")
    return data


def get_reverse_map(force=False):
    """取反查图，缓存 90 天（框架 L1）。"""
    if not force:
        c = _cache_get("sw_reverse_map", ttl_days=90)
        if c:
            return c
    data = _build_reverse_map()
    _cache_set("sw_reverse_map", data)
    return data


# ═══════════════════════════════════════════════════════════════
# 个股指标（stock_financial_analysis_indicator）
# ═══════════════════════════════════════════════════════════════
def _extract_metrics(df):
    """从财务指标 DataFrame 提炼标准化指标字典（取最新一期快照）。"""
    if df is None or df.empty:
        return None
    df = df.copy()
    # 日期列转可排序
    try:
        df["_d"] = df["日期"]
        df = df.sort_values("_d")
    except Exception:
        pass
    last = df.iloc[-1]

    def g(col):
        return sf(last.get(col)) if col in df.columns else None

    gross = g("销售毛利率(%)")
    if gross is None:                      # 部分公司销售毛利率为空 → 退主营业务利润率
        gross = g("主营业务利润率(%)")

    # 成长性：近 4 期营业收入同比均值（近似，绝对营收无批量源）
    rev_growth = g("主营业务收入增长率(%)")
    if "主营业务收入增长率(%)" in df.columns:
        recent = [sf(v) for v in df["主营业务收入增长率(%)"].tolist()[-4:]]
        recent = [v for v in recent if v is not None]
        if recent:
            rev_growth = round(sum(recent) / len(recent), 2)

    # 现金流质量：经营现金净流量与净利润比率（原始为倍数，转 %）
    ocf = g("经营现金净流量与净利润的比率(%)")
    if ocf is not None and abs(ocf) <= 5:   # 源数据多为倍数(0.95) → 折算百分比
        ocf = round(ocf * 100, 2)

    return {
        "roe":            g("净资产收益率(%)"),
        "roa":            g("总资产净利润率(%)"),
        "gross_margin":   gross,
        "net_margin":     g("销售净利率(%)"),
        "debt_ratio":     g("资产负债率(%)"),
        "asset_turnover": g("总资产周转率(次)"),
        "rev_growth":     rev_growth,
        "profit_growth":  g("净利润增长率(%)"),
        "ar_turnover":    g("应收账款周转率(次)"),
        "inv_turnover":   g("存货周转率(次)"),
        "current_ratio":  g("流动比率"),
        "interest_cover": g("利息支付倍数"),
        "ocf_to_ni":      ocf,
        "total_assets":   g("总资产(元)"),
        "eps":            g("摊薄每股收益(元)"),
        "bvps":           g("每股净资产_调整前(元)"),
    }


def get_stock_metrics(code, ttl_days=7):
    """单只股票标准化指标，缓存 7 天（跨行业共享，框架 L2）。"""
    key = f"metrics_{code}"
    c = _cache_get(key, ttl_days=ttl_days)
    if c is not None:
        return c.get("m")
    m = None
    try:
        y = datetime.date.today().year - 4
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(y))
        m = _extract_metrics(df)
    except Exception as e:
        print(f"[行业对比] 指标抓取失败 {code}: {str(e)[:80]}")
    _cache_set(key, {"m": m})
    return m


# ═══════════════════════════════════════════════════════════════
# 行业成分指标池（L1：按行业缓存 7 天）
# ═══════════════════════════════════════════════════════════════
def get_industry_pool(ind_code, members, cap=POOL_CAP):
    """
    抓取行业内（按权重取前 cap 只）成分股指标，组成对比池。
    返回 list[{code, name, weight, metrics}]，按权重降序。
    """
    key = f"pool_{ind_code}"
    c = _cache_get(key, ttl_days=7)
    if c is not None:
        return c.get("pool"), c.get("capped", False)

    ms = sorted(members, key=lambda x: (x.get("weight") or 0), reverse=True)
    capped = len(ms) > cap
    ms = ms[:cap]

    pool = []
    for it in ms:
        m = get_stock_metrics(it["code"])
        time.sleep(_REQ_SLEEP)
        if m is None:
            continue
        pool.append({"code": it["code"], "name": it["name"],
                     "weight": it.get("weight"), "metrics": m})
    _cache_set(key, {"pool": pool, "capped": capped})
    return pool, capped


# ═══════════════════════════════════════════════════════════════
# A · 相似度筛选
# ═══════════════════════════════════════════════════════════════
def _is_similar(c, t, rule):
    if c is None or t is None:
        return False
    kind, thr = rule
    if kind == "rel":
        if t == 0:
            return False
        return abs(c - t) / abs(t) <= thr
    return abs(c - t) <= thr


def _similarity(target, cand):
    """返回 (满足阈值数, 相似维度列表)。"""
    dims = []
    for k, rule in SIM_THRESHOLDS.items():
        tv = target.get(k) if k != "weight" else target.get("weight")
        cv = cand.get(k) if k != "weight" else cand.get("weight")
        if _is_similar(cv, tv, rule):
            dims.append(SIM_LABELS[k])
    return len(dims), dims


def select_peers(target_row, pool, top_n=15, anchor_k=3):
    """
    框架 A-4：Top 15 相似 + 行业前 anchor_k 龙头锚定 + 双基准。
    target_row / pool 元素均含 {code,name,weight,metrics}。
    """
    tm = dict(target_row["metrics"])
    tm["weight"] = target_row.get("weight")

    scored = []
    for p in pool:
        if p["code"] == target_row["code"]:
            continue
        cm = dict(p["metrics"])
        cm["weight"] = p.get("weight")
        cnt, dims = _similarity(tm, cm)
        scored.append({"code": p["code"], "name": p["name"], "weight": p.get("weight"),
                       "count": cnt, "similar_dims": dims, "metrics": p["metrics"]})

    scored.sort(key=lambda x: (x["count"], x["weight"] or 0), reverse=True)
    top_similar = scored[:top_n]
    max_count = top_similar[0]["count"] if top_similar else 0

    # 龙头锚定：行业内权重前 anchor_k（排除目标）
    anchors = sorted([p for p in pool if p["code"] != target_row["code"]],
                     key=lambda x: (x.get("weight") or 0), reverse=True)[:anchor_k]
    anchor_codes = {a["code"] for a in anchors}
    top_codes = {p["code"] for p in top_similar}
    for p in top_similar:
        p["is_anchor"] = p["code"] in anchor_codes
    # 锚定龙头若不在 Top15，补入
    extra_anchors = []
    for a in anchors:
        if a["code"] not in top_codes:
            cm = dict(a["metrics"]); cm["weight"] = a.get("weight")
            cnt, dims = _similarity(tm, cm)
            extra_anchors.append({"code": a["code"], "name": a["name"], "weight": a.get("weight"),
                                  "count": cnt, "similar_dims": dims, "metrics": a["metrics"],
                                  "is_anchor": True})

    merged = top_similar + extra_anchors

    # 双基准：精选同行中位数 / 全行业中位数
    sel_keys = [k for k, _, _ in PCT_INDICATORS]
    peer_median = {k: _median([p["metrics"].get(k) for p in top_similar]) for k in sel_keys}
    industry_median = {k: _median([p["metrics"].get(k) for p in pool]) for k in sel_keys}

    # 降级判定（框架 A-5）
    if max_count >= 5:
        degrade = {"level": "ok", "msg": "同行业可比公司充分"}
    elif max_count >= 3:
        degrade = {"level": "warn", "msg": "行业内业务结构存在差异，对比需谨慎"}
    else:
        degrade = {"level": "alert", "msg": "本公司业务结构相对独特，几乎无高度可比公司"}

    return {
        "peers": merged,
        "anchors": [a["code"] for a in anchors],
        "max_count": max_count,
        "degrade": degrade,
        "peer_median": peer_median,
        "industry_median": industry_median,
        "n_top_similar": len(top_similar),
    }


# ═══════════════════════════════════════════════════════════════
# B · 行业相对位置
# ═══════════════════════════════════════════════════════════════
def percentile(value, values, reverse=False):
    """框架 B-2 标准分位；reverse=True 时低值更好（分位反转）。"""
    vals = [v for v in values if v is not None]
    if value is None or not vals:
        return None
    below = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    p = (below + 0.5 * equal) / len(vals) * 100.0
    return round(100.0 - p, 1) if reverse else round(p, 1)


def _pct_label(p):
    if p is None:
        return ("—", "grey")
    if p >= 80:  return ("优秀", "green")
    if p >= 60:  return ("良好", "green")
    if p >= 40:  return ("中等", "yellow")
    if p >= 20:  return ("偏弱", "yellow")
    return ("落后", "red")


def _histogram(values, target, bins=10):
    vals = [v for v in values if v is not None]
    if len(vals) < 4 or target is None:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return None
    width = (hi - lo) / bins
    edges = [lo + i * width for i in range(bins + 1)]
    counts = [0] * bins
    for v in vals:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    centers = [round((edges[i] + edges[i + 1]) / 2, 2) for i in range(bins)]
    return {"centers": centers, "counts": counts, "target": round(target, 2),
            "lo": round(lo, 2), "hi": round(hi, 2)}


def industry_position(target_row, pool):
    """框架 B：分位排名 + 雷达 + 分布 + 综合定位标签。"""
    tm = target_row["metrics"]

    percentiles = {}
    for k, name, rev in PCT_INDICATORS:
        vals = [p["metrics"].get(k) for p in pool]
        pc = percentile(tm.get(k), vals, reverse=rev)
        lab, color = _pct_label(pc)
        percentiles[k] = {"name": name, "value": tm.get(k), "percentile": pc,
                          "label": lab, "color": color, "reverse": rev}

    # 雷达（分位）：目标 vs 行业中位（中位分位≈50）
    radar = {"labels": [], "target": [], "median": []}
    for k, name, rev in RADAR_AXES:
        radar["labels"].append(name)
        radar["target"].append(percentiles[k]["percentile"] if k in percentiles
                               else percentile(tm.get(k), [p["metrics"].get(k) for p in pool], rev))
        radar["median"].append(50)

    # 分布直方图：ROE 与 净利率
    distribution = {
        "roe": _histogram([p["metrics"].get("roe") for p in pool], tm.get("roe")),
        "net_margin": _histogram([p["metrics"].get("net_margin") for p in pool], tm.get("net_margin")),
    }

    # 综合定位（框架 B-3）
    def pillar(keys):
        ps = [percentiles[k]["percentile"] for k in keys if percentiles.get(k) and percentiles[k]["percentile"] is not None]
        return round(sum(ps) / len(ps), 1) if ps else None

    profit_p = pillar(["roe", "roa", "gross_margin", "net_margin"])
    growth_p = pillar(["rev_growth", "profit_growth"])
    oper_p = pillar(["asset_turnover", "ar_turnover", "inv_turnover"])
    health_p = pillar(["debt_ratio", "current_ratio", "ocf_to_ni"])
    pillars = {"盈利能力": profit_p, "成长性": growth_p, "运营效率": oper_p, "财务健康": health_p}

    label = "—"
    if profit_p is not None and growth_p is not None:
        if profit_p > 70 and growth_p > 70:    label = "绩优成长"
        elif profit_p > 70 and growth_p < 50:  label = "稳健盈利"
        elif profit_p < 50 and growth_p > 70:  label = "激进成长"
        elif profit_p < 50 and growth_p < 50:  label = "行业落后"
        else:                                  label = "均衡中游"

    return {"percentiles": percentiles, "radar": radar, "distribution": distribution,
            "pillars": pillars, "composite_label": label}


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
def industry_comparison(code, name=None):
    """
    完整行业对比。返回：
    {
      available: bool, reason?: str,
      target: {code, name, industry, ind_code, weight, metrics},
      industry: {pe_ttm, pb, n_total, n_pool, capped},
      section_a: {...},   # 精选同行
      section_b: {...},   # 行业分位
      notes: [...]        # 口径 / 局限
    }
    缓存：最终结果按个股 7 天（框架 L2）。
    """
    cached = _cache_get(f"result_{code}", ttl_days=7)
    if cached is not None:
        return cached

    rev = get_reverse_map()
    info = rev["code2ind"].get(str(code))
    if not info:
        res = {"available": False,
               "reason": "未在申万二级行业成分中找到该股票（可能为次新股 / 北交所 / 申万样本未覆盖）。"}
        _cache_set(f"result_{code}", res)
        return res

    ind_code = info["ind_code"]
    ind = rev["industries"].get(ind_code, {})
    members = ind.get("members", [])

    # 剔除 ST/*ST（框架第三节清洗），但保留目标自身
    members_clean = [m for m in members if not _is_st(m["name"]) or m["code"] == str(code)]

    pool, capped = get_industry_pool(ind_code, members_clean)
    if not pool:
        res = {"available": False, "reason": "行业成分指标抓取为空，稍后重试。"}
        return res

    # 确保目标在池内（不在则单独抓取补入）
    target_row = next((p for p in pool if p["code"] == str(code)), None)
    if target_row is None:
        tm = get_stock_metrics(str(code))
        if tm is None:
            res = {"available": False, "reason": "目标股财务指标抓取失败。"}
            return res
        target_row = {"code": str(code), "name": name or info["ind_name"],
                      "weight": info.get("weight"), "metrics": tm}
        pool = [target_row] + pool

    # 清洗：剔除 ROE 极端值的样本用于中位/分位（保留目标）
    def _valid(p):
        roe = p["metrics"].get("roe")
        if p["code"] == str(code):
            return True
        return roe is None or (-50 <= roe <= 100)
    pool_clean = [p for p in pool if _valid(p)]

    section_a = select_peers(target_row, pool_clean)
    section_b = industry_position(target_row, pool_clean)

    notes = [
        f"对比基础：申万二级「{ind.get('name','')}」行业，共 {len(members)} 只成分"
        + (f"，按权重取前 {POOL_CAP} 只分析" if capped else f"，全部 {len(pool_clean)} 只纳入分析") + "。",
        "数据口径：个股指标取 stock_financial_analysis_indicator 最新一期；中位数而非均值；已剔除 ST/*ST 与 ROE 极端值。",
        "维度代理（受数据源限制）：市值用申万权重代理、规模用总资产代理、成长性以近 4 期营收同比均值近似；毛利率缺失时退主营业务利润率。",
        "申万成分与权重来自 legulegu 快照（计入日期较早），用于行业归属/龙头锚定/规模代理方向上可靠，绝对权重可能滞后。",
        "暂未实现：5 年行业分位趋势（需逐年重抓全行业历史，开销大）；东财行情/北向/龙虎榜等 push2 接口在本地代理环境不可用。",
        "本结果仅供学习研究，所有定位以分位/区间呈现，不构成投资建议。",
    ]

    target_out = {"code": str(code), "name": name or target_row["name"],
                  "industry": ind.get("name", ""), "ind_code": ind_code,
                  "parent": ind.get("parent", ""), "weight": target_row.get("weight"),
                  "metrics": target_row["metrics"]}

    res = {
        "available": True,
        "target": target_out,
        "industry": {"pe_ttm": ind.get("pe_ttm"), "pb": ind.get("pb"),
                     "n_total": len(members), "n_pool": len(pool_clean), "capped": capped},
        "section_a": section_a,
        "section_b": section_b,
        "notes": notes,
    }
    _cache_set(f"result_{code}", res)
    return res


# 自测：python industry_compare.py 600519
if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "600519"
    t0 = time.time()
    r = industry_comparison(c)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:3000])
    print(f"\n用时 {time.time()-t0:.1f}s  available={r.get('available')}")
