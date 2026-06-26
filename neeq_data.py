# -*- coding: utf-8 -*-
"""
新三板（NEEQ）数据层 —— 阶段六「年报解析」落地（文本版，非 PDF）
================================================================
通路（代理环境实测可行）：
  代码 → 东财统一搜索找「年度报告」公告 art_code（search-api-web）
       → 东财公告内容 API 取服务端抽取文本（np-cnotice，分页，绕开 PDF 反爬）
       → 解析合并三表 → 多份年报拼多年 → 组装为应用现有 `raw` 结构（新浪报表口径）

为何走这条：新三板无结构化财务接口；新浪/emweb HSF10/financial_indicator 不覆盖；
akshare 本版无新三板函数；push2 行情被代理墙；年报 PDF 在 pdf.dfcfw.com 有 JS 反爬。
而东财公告内容 API 直接给「年报全文文本」，三表以文本表格存在，可解析。

口径与局限（必显式声明）：
  - **仅年报、无季度**；一份年报含「本期+上期」两年，多年需拼多份。
  - 解析依赖年报版式，**脆弱**；缺失项返回 None，不臆造。
  - **现价 / K线 取不到**（push2 被墙）→ 估值/量价维度不可用。
  - 仅供学习研究，不构成投资建议。
"""

import os
import re
import json
import time
import requests
import pandas as pd

_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://xinsanban.eastmoney.com/"}
_NUM = re.compile(r"-?[\d,]+\.\d{2}")
_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
os.makedirs(_CACHE, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# 一、发现：按代码搜「年度报告」公告
# ═══════════════════════════════════════════════════════════════
def _gg_list(code, max_pages=15):
    """
    东财新三板公告列表 API（/api/gg/list，xinsanban）→ 该股全部公告。
    返回 [{title, art_code, date}]。按 securitycodes 过滤、page_index 翻页。
    """
    out = []
    for pi in range(1, max_pages + 1):
        try:
            r = requests.get("https://xinsanban.eastmoney.com/api/gg/list",
                             params={"page_index": pi, "type": "", "begin": "", "end": "",
                                     "securitycodes": str(code), "content": "", "sortRule": "1"},
                             headers=_H, timeout=15)
            res = (r.json() or {}).get("result") or []
        except Exception:
            res = []
        if not res:
            break
        for it in res:
            out.append({"title": it.get("title") or "", "art_code": it.get("art_code"),
                        "date": it.get("notice_date") or ""})
        time.sleep(0.05)
    return out


_BAD = ("摘要", "英文", "已取消", "取消", "更正", "补充", "披露公告", "提示性")


def stock_name(code):
    """东财搜索建议解析代码 → 名称（确认是否新三板 Classify=NEEQ）。返回 (name, is_neeq)。"""
    try:
        r = requests.get("https://searchapi.eastmoney.com/api/suggest/get",
                         params={"input": str(code), "type": "14",
                                 "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 5},
                         headers=_H, timeout=10)
        for d in ((r.json().get("QuotationCodeTable") or {}).get("Data") or []):
            if str(d.get("Code")) == str(code):
                return d.get("Name"), (d.get("Classify") == "NEEQ")
    except Exception:
        pass
    return None, False


def discover_annual_reports(code, need=4):
    """
    返回该新三板代码的年度报告 [(year:int, art_code)]，按年份降序、每年取一份。
    数据来自东财新三板公告列表 API，标题含「年度报告」者按年份提取。
    """
    found = {}
    for n in _gg_list(code):
        t = n.get("title") or ""
        if "年度报告" not in t or any(b in t for b in _BAD):
            continue
        m = re.search(r"(20\d{2})\s*年年度报告", t) or re.search(r"(20\d{2})\s*年度报告", t)
        if not m or not n.get("art_code"):
            continue
        y = int(m.group(1))
        found.setdefault(y, n["art_code"])             # 每年第一份（列表中较新者）
        if len(found) >= need:
            break
    return sorted(found.items(), key=lambda kv: -kv[0])


# ═══════════════════════════════════════════════════════════════
# 二、抓取：公告全文文本（np-cnotice 内容 API，分页）
# ═══════════════════════════════════════════════════════════════
def fetch_report_text(art_code, max_pages=60):
    base = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    parts, page_size, p = [], None, 1
    meta = {}
    while p <= max_pages:
        try:
            r = requests.get(base, params={"art_code": art_code, "client_source": "web",
                                           "page_index": p}, headers=_H, timeout=15)
            d = (r.json() or {}).get("data") or {}
        except Exception:
            break
        if p == 1:
            page_size = d.get("page_size") or 1
            meta = {"title": d.get("notice_title"), "date": d.get("notice_date"),
                    "name": d.get("short_name")}
        parts.append(d.get("notice_content") or "")
        if page_size and p >= page_size:
            break
        p += 1
        time.sleep(0.05)
    return "".join(parts), meta


# ═══════════════════════════════════════════════════════════════
# 三、解析：合并三大报表
# ═══════════════════════════════════════════════════════════════
def _nums(line):
    return [float(x.replace(",", "")) for x in _NUM.findall(line)]


def _locate(text, header, anchor, end=None, window=20000):
    """定位某张合并报表正文段（含 anchor 的那处，避开目录/附注；end 截断防串入母公司表）。"""
    start = 0
    while True:
        i = text.find(header, start)
        if i < 0:
            return ""
        seg = text[i:i + window]
        if anchor in seg:
            if end:
                j = seg.find(end, len(header))
                if j > 0:
                    seg = seg[:j]
            return seg
        start = i + len(header)


def _grab(section, match_label, exclude=None):
    """段内取「剥离前导编号/序号/其中：等后以 match_label 开头」且带数字的首行 → [本期, 上期]。"""
    for line in section.splitlines():
        if exclude and exclude in line:
            continue
        s = line.strip().lstrip("0123456789一二三四五六七八九十、（）().：，  　其中加减")
        if s.startswith(match_label):
            ns = _nums(line)
            if ns:
                return (ns + [None, None])[:2]
    return [None, None]


# (输出列名=新浪报表口径, 文本匹配标签, 排除关键词)
_INCOME = [
    ("营业收入", "营业总收入", None), ("营业成本", "营业成本", "营业总成本"),
    ("营业利润", "营业利润", None), ("利润总额", "利润总额", None),
    ("净利润", "净利润", None), ("归属于母公司所有者的净利润", "归属于母公司所有者的净利润", None),
    ("所得税费用", "所得税费用", None), ("利息费用", "利息费用", None),
    ("财务费用", "财务费用", None), ("销售费用", "销售费用", None),
    ("管理费用", "管理费用", None), ("研发费用", "研发费用", None),
]
_BALANCE = [
    ("货币资金", "货币资金", None), ("应收账款", "应收账款", None), ("存货", "存货", None),
    ("流动资产合计", "流动资产合计", None), ("固定资产", "固定资产", None),
    ("资产总计", "资产总计", None), ("流动负债合计", "流动负债合计", None),
    ("负债合计", "负债合计", None),
    ("归属于母公司所有者权益合计", "归属于母公司所有者权益", None),
    ("短期借款", "短期借款", None), ("长期借款", "长期借款", None),
    ("应付票据", "应付票据", None), ("应付账款", "应付账款", None),
    ("合同负债", "合同负债", None), ("一年内到期的非流动负债", "一年内到期的非流动负债", None),
    ("长期股权投资", "长期股权投资", None),
]
_CASHFLOW = [
    ("销售商品、提供劳务收到的现金", "销售商品、提供劳务收到的现金", None),
    ("购买商品、接受劳务支付的现金", "购买商品、接受劳务支付的现金", None),
    ("经营活动产生的现金流量净额", "经营活动产生的现金流量净额", None),
    ("投资活动产生的现金流量净额", "投资活动产生的现金流量净额", None),
    ("筹资活动产生的现金流量净额", "筹资活动产生的现金流量净额", None),
    ("购建固定资产、无形资产和其他长期资产支付的现金", "购建固定资产", None),
]


def parse_industry(text):
    """
    从年报「所属行业 / 行业分类」段抽取行业名称（CSRC/国标口径，如「电气机械和器材制造业-
    家用电力器具制造-家用空气调节器制造」），供「行业名称→申万」跨分类检索用。失败返回 ""。
    """
    i = text.find("行业分类")
    if i < 0:
        i = text.find("所属行业")
    if i < 0:
        m = re.search(r"C\d{2,4}[一-龥]{3,}制造业", text)
        i = m.start() if m else -1
    if i < 0:
        return ""
    win = text[max(0, i - 220):i + 240]
    names = []
    for mm in re.finditer(r"[一-龥]{2,12}制造业?", win):
        nm = mm.group()
        if 3 <= len(nm) <= 16 and nm not in names:
            names.append(nm)
    return "-".join(names[:4])


def parse_statements(text):
    """解析合并三表 → {periods:[本期年, 上期年(int)], income/balance/cashflow:{列名:[本期,上期]}}。"""
    bal = _locate(text, "合并资产负债表", "资产总计", end="母公司资产负债表")
    inc = _locate(text, "合并利润表", "营业利润", end="母公司利润表")
    cfs = _locate(text, "合并现金流量表", "经营活动产生的现金流量净额", end="母公司现金流量表")
    yrs = [int(y) for y in (re.findall(r"(20\d{2})\s*年", inc[:400]) or
                            re.findall(r"(20\d{2})\s*年", bal[:400]))][:2]

    def blk(section, spec):
        return {out: _grab(section, lab, exc) for out, lab, exc in spec}

    return {"periods": yrs, "income": blk(inc, _INCOME),
            "balance": blk(bal, _BALANCE), "cashflow": blk(cfs, _CASHFLOW)}


# ═══════════════════════════════════════════════════════════════
# 四、组装：多份年报 → 应用 `raw`（新浪报表口径 DataFrame）
# ═══════════════════════════════════════════════════════════════
def _build_dataframes(merged):
    """merged: {stmt: {year:int: {col: val}}} → {stmt: DataFrame(rows=期, _date=YYYY1231, 降序)}。"""
    raw = {}
    for stmt, by_year in merged.items():
        years = sorted(by_year.keys(), reverse=True)
        rows = []
        for y in years:
            row = {"_date": f"{y}1231"}
            row.update(by_year[y])
            rows.append(row)
        raw[stmt] = pd.DataFrame(rows) if rows else None
    return raw


def build_raw(code, name=None, n_reports=3, ttl_days=30):
    """
    新三板个股 → (raw, meta)。raw={income,balance,cashflow} DataFrame（兼容现有 compute_*）。
    多份年报合并：以「本期」口径优先（审计当年数），「上期」仅补未覆盖年。
    无年报可解析时返回 (None, None)。
    """
    code = str(code)
    cache = os.path.join(_CACHE, f"neeq_{code}.json")
    merged, meta = None, None
    if os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < ttl_days * 86400:
        try:
            obj = json.load(open(cache, encoding="utf-8"))
            merged = {s: {int(y): v for y, v in d.items()} for s, d in obj["merged"].items()}
            meta = obj["meta"]
        except Exception:
            merged = None
    if merged is None:
        reports = discover_annual_reports(code)
        if not reports:
            return None, None
        merged = {"income": {}, "balance": {}, "cashflow": {}}
        used = []
        industry = ""
        for idx, (year, art) in enumerate(reports[:n_reports]):
            text, m = fetch_report_text(art)
            if idx == 0:
                industry = parse_industry(text)        # 取最新一份年报的行业
            r = parse_statements(text)
            ps = r.get("periods") or [year, year - 1]
            if not name and m.get("name"):
                name = m["name"]
            used.append({"year": year, "art_code": art})
            for stmt in ("income", "balance", "cashflow"):
                for col, (cur, prev) in r[stmt].items():
                    for yr, val in ((ps[0], cur), (ps[1] if len(ps) > 1 else year - 1, prev)):
                        if val is None:
                            continue
                        merged[stmt].setdefault(yr, {})
                        # 「本期」(cur, 来自更新的年报先处理) 优先；不覆盖已有
                        if col not in merged[stmt][yr]:
                            merged[stmt][yr][col] = val
        all_years = set()
        for s in merged:
            all_years |= set(merged[s].keys())
        meta = {"name": name, "market": "NEEQ", "annual_only": True, "industry": industry,
                "reports": used, "periods": sorted(all_years, reverse=True)}
        try:
            json.dump({"merged": merged, "meta": meta}, open(cache, "w", encoding="utf-8"),
                      ensure_ascii=False)
        except Exception:
            pass
    raw = _build_dataframes(merged)
    if raw.get("income") is None or raw["income"].empty:
        return None, None
    return raw, meta


# ═══════════════════════════════════════════════════════════════
# 五、标准化指标（供行业对比：与 industry_compare 口径一致，最新年报期）
# ═══════════════════════════════════════════════════════════════
def _m_from_raw(raw):
    inc, bal, cf = raw.get("income"), raw.get("balance"), raw.get("cashflow")

    def g(df, c, i=0):
        if df is None or c not in df.columns or i >= len(df):
            return None
        v = df[c].iloc[i]
        try:
            return None if (v is None or pd.isna(v)) else float(v)
        except Exception:
            return None

    rev, rev1 = g(inc, "营业收入"), g(inc, "营业收入", 1)
    cogs = g(inc, "营业成本")
    npar = g(inc, "归属于母公司所有者的净利润") or g(inc, "净利润")
    npar1 = g(inc, "归属于母公司所有者的净利润", 1) or g(inc, "净利润", 1)
    nett = g(inc, "净利润")
    ta, tl = g(bal, "资产总计"), g(bal, "负债合计")
    eq = g(bal, "归属于母公司所有者权益合计")
    ar, invn = g(bal, "应收账款"), g(bal, "存货")
    ca, cl = g(bal, "流动资产合计"), g(bal, "流动负债合计")
    ocf = g(cf, "经营活动产生的现金流量净额")

    def pct(a, b):
        return round(a / b * 100, 2) if (a is not None and b not in (None, 0)) else None

    def rat(a, b):
        return round(a / b, 4) if (a is not None and b not in (None, 0)) else None

    gm = pct(rev - cogs, rev) if (rev is not None and cogs is not None and rev) else None
    return {
        "roe": pct(npar, eq), "roa": pct(nett, ta), "gross_margin": gm,
        "net_margin": pct(npar, rev), "debt_ratio": pct(tl, ta),
        "asset_turnover": rat(rev, ta),
        "rev_growth": pct(rev - rev1, rev1) if (rev is not None and rev1) else None,
        "profit_growth": pct(npar - npar1, abs(npar1)) if (npar is not None and npar1) else None,
        "ar_turnover": rat(rev, ar), "inv_turnover": rat(cogs, invn),
        "current_ratio": rat(ca, cl), "interest_cover": None,
        "ocf_to_ni": pct(ocf, nett), "total_assets": ta, "eps": None, "bvps": None,
    }


def neeq_metrics(code):
    """新三板个股标准化指标（最新年报期），供 industry_compare 注入目标。无则 None。"""
    raw, _ = build_raw(code)
    return _m_from_raw(raw) if raw else None


# 自测：python neeq_data.py 874628
if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "874628"
    print("discover:", discover_annual_reports(code)[:5])
    raw, meta = build_raw(code)
    print("meta:", json.dumps(meta, ensure_ascii=False))
    if raw:
        inc = raw["income"]
        print("income _date:", inc["_date"].tolist())
        for c in ("营业收入", "营业成本", "归属于母公司所有者的净利润"):
            if c in inc.columns:
                print(f"  {c}: {inc[c].tolist()}")
        bal = raw["balance"]
        for c in ("资产总计", "负债合计", "归属于母公司所有者权益合计"):
            if c in bal.columns:
                print(f"  {c}: {bal[c].tolist()}")
