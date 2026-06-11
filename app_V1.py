# -*- coding: utf-8 -*-
"""
==============================================================================
A股财务速查网页应用  (单文件: 后端 Flask + 内嵌前端)
------------------------------------------------------------------------------
数据源: AKShare (免费 / 免注册, 替代 Wind / Choice 终端)
功能:
    · 简洁居中主页, 按 股票名称 或 代码 搜索 (如 "茅台" / "600519")
    · 自动抓取 三大报表 关键科目 (最近若干期)
    · 计算并对照 三种 PE 口径 (静态 / TTM滚动 / 动态), 数据源值 vs 手算值
启动后访问:  http://127.0.0.1:5000
==============================================================================
"""

import os
# === 强制直连: 绕过本机失效的代理 / VPN (解决 ProxyError) ===
# 东财/新浪/雪球等源在境外可直连, 不需要走代理。
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import math
import akshare as ak
import pandas as pd
from flask import Flask, request, jsonify, Response

# ============================ 改这里: 项目名 ============================
PROJECT_NAME = "三表透视"                       # ← 换成你的项目名
PROJECT_DESC = "输入股票名称或代码，一次看清三大报表与三种市盈率口径"   # ← 副标题
# =====================================================================

app = Flask(__name__)

# 启动时加载一次「代码-名称」对照表, 用于按名称搜索
# 带备用源: 主接口偶尔返回空数据时, 自动换东财全市场快照
def _load_code_name():
    try:
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            if len(df.columns) == 2:
                df.columns = ["code", "name"]
            print(f"[启动] 已加载 {df.shape[0]} 只 A 股代码表 (主源)")
            return df
        print("[启动] 主源返回空, 尝试备用源…")
    except Exception as e:
        print(f"[启动] 主源失败, 尝试备用源: {e}")
    try:
        spot = ak.stock_zh_a_spot_em()          # 东财全市场快照, 较稳但启动稍慢
        df = spot[["代码", "名称"]].copy()
        df.columns = ["code", "name"]
        print(f"[启动] 已加载 {df.shape[0]} 只 A 股代码表 (备用源)")
        return df
    except Exception as e:
        print(f"[启动] 备用源也失败 (仍可用纯代码搜索): {e}")
    return pd.DataFrame(columns=["code", "name"])


_CODE_NAME = _load_code_name()


# --------------------------- 通用工具 ---------------------------
def to_sina_code(code):
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


def to_xq_code(code):
    return to_sina_code(code).upper()


def safe_float(x):
    """转 float, 失败或 NaN 返回 None (便于 JSON 序列化)"""
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


# --------------------------- 搜索: 名称或代码 -> 候选 ---------------------------
def search_stocks(query, limit=12):
    q = query.strip()
    if not q:
        return []
    if _CODE_NAME.empty:
        return [{"code": q.zfill(6), "name": q}] if q.isdigit() else []

    df = _CODE_NAME
    if q.isdigit():
        hits = df[df["code"].astype(str).str.contains(q)]
    else:
        hits = df[df["name"].astype(str).str.contains(q, na=False)]
    return [{"code": str(r["code"]), "name": str(r["name"])} for _, r in hits.head(limit).iterrows()]


# --------------------------- 三大报表抓取与精简 ---------------------------
KEY_ITEMS = {
    "资产负债表": ["资产总计", "负债合计", "所有者权益(或股东权益)合计", "货币资金", "应收账款", "存货"],
    "利润表": ["营业收入", "营业成本", "营业利润", "利润总额", "净利润"],
    "现金流量表": ["经营活动产生的现金流量净额", "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额"],
}


def extract_statement(df, key_items, n_periods=5):
    if df is None or df.empty:
        return None
    date_col = next((c for c in df.columns if "报表日期" in c or "报告日" in c), df.columns[0])
    df = df.copy()
    df["_d"] = df[date_col].astype(str).str[:8]
    df = df.sort_values("_d", ascending=False).head(n_periods)
    periods = df["_d"].tolist()
    rows = []
    for item in key_items:
        col = next((c for c in df.columns if c.strip() == item), None)
        if col is None:
            col = next((c for c in df.columns if item in c), None)
        if col is not None:
            rows.append({"item": item, "values": [safe_float(v) for v in df[col].tolist()]})
    return {"periods": periods, "rows": rows}


def fetch_statements(code):
    sina = to_sina_code(code)
    out = {}
    for name, items in KEY_ITEMS.items():
        try:
            df = ak.stock_financial_report_sina(stock=sina, symbol=name)
            out[name] = extract_statement(df, items)
        except Exception as e:
            print(f"[ERR] {name}: {e}")
            out[name] = None
    return out


# --------------------------- 三种 PE ---------------------------
def _get_pe_indicator(code):
    """兼容不同 akshare 版本的函数名: stock_a_indicator_lg(新) / stock_a_lg_indicator(旧)"""
    for fname, kw in [("stock_a_indicator_lg", "symbol"), ("stock_a_lg_indicator", "stock")]:
        fn = getattr(ak, fname, None)
        if fn is None:
            continue
        try:
            df = fn(**{kw: code})
        except Exception:
            continue
        if df is not None and not df.empty:
            return df.reset_index()
    return None


def fetch_price(code):
    """现价: 优先用新浪日线收盘(境外可直连), 东财兜底"""
    try:
        df = ak.stock_zh_a_daily(symbol=to_sina_code(code), adjust="")
        if df is not None and not df.empty and "close" in df.columns:
            return safe_float(df.iloc[-1]["close"])
    except Exception as e:
        print(f"[ERR] 新浪日线价格(可忽略): {e}")
    try:
        info = ak.stock_individual_info_em(symbol=code)
        d = dict(zip(info["item"].astype(str), info["value"]))
        return safe_float(d.get("最新"))
    except Exception as e:
        print(f"[ERR] 东财价格(可忽略): {e}")
    return None


def _shares_from_income(income_df_raw):
    """用 年度净利润 / 年度基本每股收益 反推总股本(全部来自新浪利润表)"""
    df = income_df_raw
    if df is None or df.empty:
        return None
    eps_col = next((c for c in df.columns if "基本每股收益" in c), None) \
        or next((c for c in df.columns if "每股收益" in c), None)
    profit_col = next((c for c in df.columns if c.strip() == "净利润"), None) \
        or next((c for c in df.columns if "净利润" in c), None)
    date_col = next((c for c in df.columns if "报表日期" in c or "报告日" in c), df.columns[0])
    if not eps_col or not profit_col:
        return None
    t = df[[date_col, eps_col, profit_col]].copy()
    t.columns = ["d", "eps", "profit"]
    t["d"] = t["d"].astype(str).str[:8]
    t["eps"] = pd.to_numeric(t["eps"], errors="coerce")
    t["profit"] = pd.to_numeric(t["profit"], errors="coerce")
    ann = t[t["d"].str.endswith("1231")].dropna()
    ann = ann[ann["eps"] != 0]
    if ann.empty:
        return None
    row = ann.sort_values("d").iloc[-1]
    return row["profit"] / row["eps"]


def fetch_pe(code, income_df_raw):
    src = {"静态": None, "TTM": None, "动态": None}

    # 加分项: 雪球快照(动/静/TTM 数据源值) —— 连不上就跳过
    try:
        snap = ak.stock_individual_spot_xq(symbol=to_xq_code(code))
        d = dict(zip(snap["item"].astype(str), snap["value"]))
        src["动态"] = safe_float(d.get("市盈率(动)"))
        src["静态"] = safe_float(d.get("市盈率(静)"))
        src["TTM"] = safe_float(d.get("市盈率(TTM)"))
    except Exception as e:
        print(f"[ERR] 雪球快照(可忽略): {e}")

    # 加分项: 乐咕乐股历史指标(静/TTM 数据源值)
    try:
        ind = _get_pe_indicator(code)
        if ind is not None and "pe" in ind.columns:
            v = ind.dropna(subset=["pe", "pe_ttm"])
            if "trade_date" in v.columns:
                v = v.sort_values("trade_date")
            latest = v.iloc[-1]
            if src["静态"] is None:
                src["静态"] = safe_float(latest["pe"])
            if src["TTM"] is None:
                src["TTM"] = safe_float(latest["pe_ttm"])
    except Exception as e:
        print(f"[ERR] 历史指标(可忽略): {e}")

    # 主力: 现价(新浪) + 手算PE(利润表每股收益) + 总市值(反推总股本×现价)
    price = fetch_price(code)
    manual = _manual_pe(price, income_df_raw)
    total_mv = None
    shares = _shares_from_income(income_df_raw)
    if price and shares:
        total_mv = price * shares

    return {
        "price": price,
        "total_mv": total_mv,
        "rows": [
            {"口径": "静态市盈率 (LYR)", "src": src["静态"], "manual": manual["静态"],
             "公式": "现价 ÷ 上一完整年度每股收益"},
            {"口径": "滚动市盈率 (TTM)", "src": src["TTM"], "manual": manual["TTM"],
             "公式": "现价 ÷ 最近12个月每股收益 (上年报+本期累计−去年同期)"},
            {"口径": "动态市盈率", "src": src["动态"], "manual": manual["动态"],
             "公式": "现价 ÷ 本期累计每股收益年化值"},
        ],
    }


def _manual_pe(price, income_df_raw):
    """用利润表里的「每股收益」直接算 PE, 不依赖东财总股本"""
    res = {"静态": None, "TTM": None, "动态": None}
    if not price or income_df_raw is None or income_df_raw.empty:
        return res
    df = income_df_raw
    eps_col = next((c for c in df.columns if "基本每股收益" in c), None) \
        or next((c for c in df.columns if "每股收益" in c), None)
    date_col = next((c for c in df.columns if "报表日期" in c or "报告日" in c), df.columns[0])
    if eps_col is None:
        print("[ERR] 手算: 利润表中未找到每股收益列")
        return res

    t = df[[date_col, eps_col]].copy()
    t.columns = ["d", "eps"]
    t["d"] = t["d"].astype(str).str[:8]
    t["eps"] = pd.to_numeric(t["eps"], errors="coerce")
    t = t.dropna().sort_values("d", ascending=False).reset_index(drop=True)
    if t.empty:
        return res

    def get(d):
        r = t[t["d"] == d]
        return float(r["eps"].iloc[0]) if not r.empty else None

    try:
        annual = t[t["d"].str.endswith("1231")]
        if not annual.empty and float(annual["eps"].iloc[0]) != 0:
            res["静态"] = round(price / float(annual["eps"].iloc[0]), 2)

        latest_d = t["d"].iloc[0]
        latest_cum = float(t["eps"].iloc[0])
        yr, md = int(latest_d[:4]), latest_d[4:]
        if md == "1231":
            ttm_eps = latest_cum
        else:
            e_same = get(f"{yr-1}{md}")
            e_last = get(f"{yr-1}1231")
            ttm_eps = (e_last + latest_cum - e_same) if (e_same is not None and e_last is not None) else None
        if ttm_eps:
            res["TTM"] = round(price / ttm_eps, 2)

        factor = {"0331": 4, "0630": 2, "0930": 4 / 3, "1231": 1}.get(md)
        if factor and latest_cum != 0:
            res["动态"] = round(price / (latest_cum * factor), 2)
    except Exception as e:
        print(f"[ERR] 手算: {e}")
    return res


# --------------------------- 路由 ---------------------------
@app.route("/api/search")
def api_search():
    return jsonify(search_stocks(request.args.get("q", "")))


@app.route("/api/analyze")
def api_analyze():
    code = request.args.get("code", "").strip()
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"error": "请提供 6 位股票代码"}), 400

    # 名字: 直接从已加载的代码表取(不依赖东财)
    name, industry, income_raw = code, "", None
    hit = _CODE_NAME[_CODE_NAME["code"].astype(str) == code]
    if not hit.empty:
        name = str(hit.iloc[0]["name"])

    # 利润表(新浪) —— 手算PE与总市值的基础
    try:
        income_raw = ak.stock_financial_report_sina(stock=to_sina_code(code), symbol="利润表")
    except Exception as e:
        print(f"[ERR] 利润表原始: {e}")

    pe = fetch_pe(code, income_raw)

    # 行业: 东财能连上就补, 连不上无所谓
    try:
        info = ak.stock_individual_info_em(symbol=code)
        info_d = dict(zip(info["item"].astype(str), info["value"]))
        industry = str(info_d.get("行业", ""))
    except Exception as e:
        print(f"[ERR] 行业(可忽略): {e}")

    return jsonify({
        "info": {"code": code, "name": name, "industry": industry, "total_mv": pe.get("total_mv")},
        "pe": pe["rows"],
        "statements": fetch_statements(code),
        "_price": pe["price"],
    })


@app.route("/")
def index():
    html = PAGE.replace("__PROJECT_NAME__", PROJECT_NAME).replace("__PROJECT_DESC__", PROJECT_DESC)
    return Response(html, mimetype="text/html")


# ============================ 内嵌前端 ============================
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PROJECT_NAME__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#EBEEF1; --surface:#FFFFFF; --ink:#11151B; --muted:#6A7480;
    --line:#DCE0E6; --accent:#0B6E5D; --accent-soft:#E0F0EB; --amber:#9A6B12;
    --display:'Space Grotesk',sans-serif; --body:'Inter',system-ui,sans-serif;
    --mono:'IBM Plex Mono',ui-monospace,monospace;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--ink);font-family:var(--body);
       line-height:1.5;-webkit-font-smoothing:antialiased}

  /* 整体：默认垂直居中(主页态)，搜索后顶部对齐 */
  .app{min-height:100vh;display:flex;flex-direction:column;justify-content:center;
       align-items:center;transition:justify-content .35s ease;padding:32px 20px}
  .app.searched{justify-content:flex-start;padding-top:46px}

  /* hero */
  .hero{width:100%;max-width:600px;text-align:center}
  .eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.16em;
           text-transform:uppercase;color:var(--accent);margin-bottom:14px}
  h1{font-family:var(--display);font-weight:700;font-size:clamp(40px,8vw,60px);
     letter-spacing:-.02em;margin:0 0 10px;line-height:1}
  .tag{color:var(--muted);font-size:15.5px;margin:0 auto 32px;max-width:30em}

  /* 搜索框：居中 */
  .searchbar{display:flex;gap:8px;background:var(--surface);
             border:1.5px solid var(--ink);border-radius:3px;padding:7px 7px 7px 18px;
             text-align:left}
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

  .state{margin-top:22px;font-family:var(--mono);font-size:14px;color:var(--muted)}
  .state.err{color:#B23A2E}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
       background:var(--accent);margin-right:8px;animation:pulse 1s infinite}
  @keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}

  /* 结果区 */
  #out{display:none;width:100%;max-width:920px;margin:40px auto 60px;text-align:left}
  .stockhead{display:flex;flex-wrap:wrap;align-items:baseline;gap:14px;
             padding-bottom:18px;border-bottom:2px solid var(--ink);margin-bottom:28px}
  .stockhead .name{font-family:var(--display);font-weight:700;font-size:28px}
  .stockhead .code{font-family:var(--mono);color:var(--muted);font-size:15px}
  .stockhead .meta{margin-left:auto;font-family:var(--mono);font-size:14px;color:var(--muted)}
  .stockhead .meta b{color:var(--ink);font-weight:600}

  .sec-label{font-family:var(--mono);font-size:12px;letter-spacing:.12em;
             text-transform:uppercase;color:var(--muted);margin:0 0 14px}

  .pe-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:40px}
  .pe-card{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:18px}
  .pe-card .cap{font-family:var(--display);font-weight:600;font-size:15px;margin-bottom:14px}
  .pe-nums{display:flex;gap:18px;margin-bottom:12px}
  .pe-nums .blk{flex:1}
  .pe-nums .tg{font-family:var(--mono);font-size:11px;color:var(--muted);
               text-transform:uppercase;letter-spacing:.06em}
  .pe-nums .val{font-family:var(--mono);font-weight:600;font-size:26px;
                font-variant-numeric:tabular-nums;letter-spacing:-.01em}
  .pe-nums .blk.manual .val{color:var(--amber)}
  .pe-card .formula{font-size:12.5px;color:var(--muted);border-top:1px dashed var(--line);
                    padding-top:10px;line-height:1.45}

  .stmts{display:grid;gap:26px}
  table{width:100%;border-collapse:collapse;background:var(--surface);
        border:1px solid var(--line);font-variant-numeric:tabular-nums}
  caption{text-align:left;font-family:var(--display);font-weight:600;font-size:17px;padding:0 0 10px}
  th,td{padding:9px 12px;text-align:right;font-family:var(--mono);font-size:13px;
        border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left;font-family:var(--body);font-weight:500}
  thead th{background:#F4F6F8;color:var(--muted);font-weight:500;font-size:12px}
  tbody tr:last-child td{border-bottom:0}

  footer{width:100%;max-width:920px;margin:0 auto;font-family:var(--mono);font-size:12px;
         color:var(--muted);text-align:center;opacity:.8}
  .app.searched footer{text-align:left;border-top:1px solid var(--line);padding-top:16px;margin-top:50px}

  @media(max-width:680px){
    .pe-grid{grid-template-columns:1fr}
    .stockhead .meta{margin-left:0;width:100%}
    table{display:block;overflow-x:auto}
  }
</style>
</head>
<body>
<div class="app landing" id="app">
  <header class="hero">
    <div class="eyebrow">A股财务速查 · AKShare</div>
    <h1>__PROJECT_NAME__</h1>
    <p class="tag">__PROJECT_DESC__</p>
    <div class="searchbar">
      <input id="q" placeholder="例如：茅台 或 600519" autocomplete="off" autofocus>
      <button id="go">搜索</button>
    </div>
    <div class="hint">中文名称模糊搜索 / 6 位代码精确搜索</div>
    <div id="cands"></div>
    <div id="state" class="state"></div>
  </header>

  <main id="out">
    <div class="stockhead">
      <span class="name" id="o-name"></span>
      <span class="code" id="o-code"></span>
      <span class="meta" id="o-meta"></span>
    </div>
    <p class="sec-label">市盈率 · 三种口径（深色＝数据源，琥珀色＝按报表手算）</p>
    <div class="pe-grid" id="o-pe"></div>
    <p class="sec-label">三大财务报表 · 最近若干期（单位：亿元）</p>
    <div class="stmts" id="o-stmts"></div>
  </main>

  <footer>数据来自公开免费接口，可能延迟或缺失；手算值仅演示口径原理，实际以数据源为准。</footer>
</div>

<script>
const $ = s => document.querySelector(s);
const appEl = $('#app'), stateEl = $('#state'), candsEl = $('#cands'), outEl = $('#out');

function setState(msg, isErr){
  stateEl.className = 'state' + (isErr ? ' err' : '');
  stateEl.innerHTML = msg ? (isErr ? msg : '<span class="dot"></span>'+msg) : '';
}
const fmtPE = v => (v==null ? '—' : Number(v).toFixed(2));
const fmtYi = v => (v==null ? '—' : (v/1e8).toFixed(2));

async function doSearch(){
  const q = $('#q').value.trim();
  candsEl.innerHTML=''; candsEl.className=''; outEl.style.display='none';
  if(!q){ return; }
  appEl.classList.add('searched');
  if(/^\d{6}$/.test(q)){ analyze(q); return; }
  setState('正在搜索…');
  try{
    const list = await (await fetch('/api/search?q='+encodeURIComponent(q))).json();
    setState('');
    if(!list.length){ setState('没找到匹配的股票，换个关键词或直接输入 6 位代码。', true); return; }
    if(list.length===1){ analyze(list[0].code); return; }
    candsEl.className='candidates';
    candsEl.innerHTML = list.map(s =>
      `<div class="cand" data-code="${s.code}"><span class="nm">${s.name}</span><span class="cd">${s.code}</span></div>`).join('');
    candsEl.querySelectorAll('.cand').forEach(el =>
      el.onclick = () => { candsEl.innerHTML=''; candsEl.className=''; analyze(el.dataset.code); });
  }catch(e){ setState('搜索失败，请检查后端是否在运行。', true); }
}

async function analyze(code){
  outEl.style.display='none';
  setState('正在抓取财报与估值数据，约需 10–20 秒…');
  try{
    const data = await (await fetch('/api/analyze?code='+code)).json();
    if(data.error){ setState(data.error, true); return; }
    render(data); setState('');
  }catch(e){ setState('抓取失败，可能是数据源临时不可用，请重试。', true); }
}

function render(d){
  const i = d.info;
  $('#o-name').textContent = i.name;
  $('#o-code').textContent = i.code;
  $('#o-meta').innerHTML =
    `现价 <b>${d._price==null?'—':'¥'+Number(d._price).toFixed(2)}</b>　·　`+
    `总市值 <b>${fmtYi(i.total_mv)} 亿</b>　·　${i.industry||'—'}`;

  $('#o-pe').innerHTML = d.pe.map(r => `
    <div class="pe-card">
      <div class="cap">${r.口径}</div>
      <div class="pe-nums">
        <div class="blk"><div class="tg">数据源</div><div class="val">${fmtPE(r.src)}</div></div>
        <div class="blk manual"><div class="tg">手算</div><div class="val">${fmtPE(r.manual)}</div></div>
      </div>
      <div class="formula">${r.公式}</div>
    </div>`).join('');

  const order = ['利润表','资产负债表','现金流量表'];
  $('#o-stmts').innerHTML = order.map(name => {
    const s = d.statements[name];
    if(!s) return `<div class="sec-label">${name}：未取到数据</div>`;
    const head = s.periods.map(p => `<th>${p}</th>`).join('');
    const body = s.rows.map(row =>
      `<tr><td>${row.item}</td>${row.values.map(v=>`<td>${fmtYi(v)}</td>`).join('')}</tr>`).join('');
    return `<table><caption>${name}</caption>
      <thead><tr><th>科目</th>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }).join('');

  outEl.style.display='block';
}

$('#go').onclick = doSearch;
$('#q').addEventListener('keydown', e => { if(e.key==='Enter') doSearch(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)