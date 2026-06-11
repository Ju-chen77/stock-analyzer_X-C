# Stock Analyzer X-C — Project Framework

## 项目简介
基于 Python 的智能股票分析平台，专为财报季设计。自动抓取并解析公开财务报表、年报及招股说明书，提取关键商业信息，计算核心财务指标，支持同行业横向对比及历史趋势追踪。初期以美股为主，后续扩展至 A 股。

---

## 项目结构

```
stock-analyzer_X-C/
│
├── src/
│   ├── scraper.py        # 抓取财务数据（负责人：Ju-chen）
│   ├── financials.py     # 计算财务指标（负责人：朋友）
│   ├── compare.py        # 横向对比（负责人：朋友）
│   ├── keywords.py       # 关键词提取（负责人：Ju-chen）
│   └── main.py           # 主程序入口
│
├── data/                 # 存储抓取的原始数据（不上传 GitHub）
├── output/               # 存储分析结果（不上传 GitHub）
├── .env                  # API Keys（绝对不上传 GitHub）
├── CLAUDE.md             # 本文件
├── CHANGELOG.md          # 每次改动记录
├── requirements.txt      # Python 依赖库
└── README.md             # 项目说明
```

---

## 开发阶段

### 第一阶段：美股基础功能（当前）
- [ ] scraper.py：用 yfinance 抓取股票财务数据
- [ ] financials.py：计算 PE、PB、ROE、EPS、营收增速、利润率
- [ ] main.py：串联抓取和计算，输入股票代码能跑通

### 第二阶段：对比与趋势
- [ ] compare.py：同行业多公司横向对比
- [ ] 历史趋势追踪，识别业绩规律
- [ ] 输出图表（matplotlib）

### 第三阶段：文本分析
- [ ] keywords.py：从财报文本提取关键信息
  - 供需关系、市场前景、竞争格局、管理层指引
- [ ] 解析 SEC EDGAR 财报 PDF（pdfplumber）
- [ ] 用 Claude API 做智能摘要

### 第四阶段：扩展 A 股
- [ ] 接入 akshare 或 tushare
- [ ] 中文财报解析
- [ ] 中文关键词提取

---

## 主要技术栈

| 库 | 用途 |
|---|---|
| `yfinance` | 抓取美股财务数据和股价 |
| `requests` | 抓取网页数据 |
| `beautifulsoup4` | 解析网页 HTML |
| `pandas` | 数据处理和分析 |
| `matplotlib` | 画图表 |
| `pdfplumber` | 解析 PDF 财报 |
| `python-dotenv` | 读取 .env 里的 API key |
| Claude API | 智能文本分析和关键词提取 |

---

## Commit 规范

每次完成一个功能后，按以下格式 commit：

```
类型: 简短标题（一行）

- 具体做了什么
- 为什么这样做
- 还有什么没完成
```

常用类型前缀：
- `feat:` 新功能
- `fix:` 修 bug
- `refactor:` 重构
- `wip:` 还没完成，临时保存
- `data:` 数据相关

示例：
```
feat: 添加 PE/ROE 计算函数

- 新增 calculate_pe() 接受股价和 EPS 参数
- 新增 calculate_roe() 从资产负债表提取数据
- 暂未处理负值情况，待后续完善
```

---

## 编码规范

- 每个函数必须写注释，说明：输入参数、返回值、功能描述
- 不确定的地方写 `# TODO: 说明`，不要乱猜
- 数据抓取必须加错误处理（try/except），网络请求经常失败
- API key 存在 `.env` 文件，用 `python-dotenv` 读取，绝对不能硬编码在代码里

函数注释格式：
```python
def calculate_pe(price: float, eps: float) -> float:
    """
    计算市盈率（PE Ratio）
    
    Args:
        price: 当前股价
        eps: 每股收益（Earnings Per Share）
    
    Returns:
        PE 比率，若 eps 为 0 或负数返回 None
    """
```

---

## 数据来源

| 数据源 | 内容 | 用途 |
|---|---|---|
| yfinance | 股价、财务数据 | 第一阶段主要来源 |
| SEC EDGAR | 官方财报、招股说明书 | 文本分析 |
| Financial Modeling Prep | 财务指标历史数据 | 横向对比 |
| akshare / tushare | A 股数据 | 第四阶段 |

---

## 安全注意事项

- `.env` 文件已在 `.gitignore` 里，不会上传 GitHub
- 任何 API key 不得出现在代码文件里
- `data/` 和 `output/` 目录不上传 GitHub

---

## 日常协作流程

```bash
# 每次开始前
git pull

# 写完一个功能后
git add .
git commit -m "feat: 你做了什么"
git push
```
