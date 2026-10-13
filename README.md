# 美股低位高流动性股票观察工具

> 每天从美国上市股票中筛出**成交活跃、价格处于相对低位、且有可观察企稳迹象**的少量候选股，
> 输出一份**理由透明、风险可见、可复现**的观察名单 —— 外加一个网页仪表板、通知推送、
> 历史检索和参数实验台。
>
> **这不是投资建议，也不承诺抄到谷底。规则宁可不给名单，也不为了凑数强行推荐。**

对应产品构想：`美股低位高流动性股票观察工具_产品构想.md`（第 1–11 节全部落地，见
[`docs/产品构想对照表.md`](docs/产品构想对照表.md)）。

---

## 1. 快速开始（30 秒，无需联网）

```bash
cd us-low-scan

# ① 先用演示数据跑通全流程（离线合成数据，股票代码是假的，只验证工具）
python3 -m us_low_scan.cli demo --with-outcomes

# ② 看真实股票（免费、不用注册、不用 API key）
python3 -m us_low_scan.cli --provider nasdaq --top 150 scan --catch-up
open reports/site/index.html
```

> ⚠️ **演示模式（`demo`）用的是合成数据，代码和公司名在现实中不存在**，只能用来验证工具本身。
> 要看真实股票，第一条命令就是：`--provider nasdaq --top 150 scan`（见第 5 节数据源）。
>
> 每次 `scan` / `demo` 都会**自动导出一个静态网站**到 `reports/site/`，
> 双击 `index.html` 就能看，不需要跑任何命令、不需要联网、不需要开着终端。

**静态网站**：
```
reports/site/index.html                       ← 真实数据：最新一期名单
reports/demo/site/index.html                  ← 演示数据：合成股票
```

> 想要在线筛选 / 参数滑块 / 一键补跑这类交互，再用
> `python3 -m us_low_scan.cli serve`（见第 4 节）。

跑完 `reports/` 下会有：

```
reports/
├── latest.md                          # 最新一份日报（固定路径，方便通知脚本引用）
├── latest_brief.txt                   # 纯文本摘要（终端 / IM / 邮件正文用）
├── 2026-10-09/
│   ├── low_scan.md                    # 人读的日报
│   ├── low_scan.csv                   # 表格版（每行一只股票，含全部分项指标）
│   ├── low_scan_rejected.csv          # 淘汰明细（哪一步、为什么被淘汰）
│   └── low_scan.json                  # 程序读的完整快照
├── backtest_2026-08-14_2026-10-09.md  # 历史回放报告
├── outcomes.md                        # 候选股入选后 5/10/20 个交易日的表现
└── site/                              # ★ 静态网站：双击 index.html 就能看
    ├── index.html                     # 最新一期名单
    ├── session-2026-10-09.html        # 逐日名单（切换交易日的链接指到这里）
    ├── history.html                   # 历史名单 + 反复入选的代码
    ├── lab.html                       # 参数方案对比（静态版）
    ├── outcomes.html                  # 表现统计
    ├── symbol-ZNBT.html               # 个股时间线
    └── 2026-10-09_low_scan.csv        # 报告文件副本，直接下载
```

全部命令：

```bash
python3 -m us_low_scan.cli doctor      # 自检：配置 / 交易日历 / 数据源
python3 -m us_low_scan.cli scan        # 扫描最近一个已收盘交易日
python3 -m us_low_scan.cli scan --catch-up         # 先把错过的交易日补上（关机之后用它）
python3 -m us_low_scan.cli scan --preset conservative      # 用更严格的参数方案
python3 -m us_low_scan.cli demo        # 离线合成数据跑通全流程
python3 -m us_low_scan.cli site        # 重新导出静态网站（默认每次扫描自动导出）
python3 -m us_low_scan.cli serve       # 网页仪表板（可交互，需要本机跑着）
python3 -m us_low_scan.cli history --ticker AAPL --group stabilized --from 2026-08-01
python3 -m us_low_scan.cli outcomes    # 跟踪已入选股票的真实表现
python3 -m us_low_scan.cli backtest --step 3   # 历史回放验证规则
python3 -m us_low_scan.cli lab --set near_low_pct=20 --set min_stabilization_score=8
python3 -m us_low_scan.cli notify --dry-run    # 先看看通知长什么样，不真的发
python3 -m us_low_scan.cli report --session 2026-10-09     # 用旧数据重渲染报告
```

**运行环境**：Python 3.9+，**零第三方依赖**（只用标准库，含网页服务器）。
产品构想里建议的 pandas / NumPy 属于可选加速项，本实现在几千只股票的日常扫描上完全够用，
而且免去了装环境、锁版本、跨机器复现的麻烦 —— 见 [`docs/设计取舍.md`](docs/设计取舍.md)。

---

## 2. 它每天做什么

```
                    ┌──────────────────────────────────────────────┐
 数据源 ──► 阶段 A  │ 交易所 / 证券类型 / OTC / 名称 / 市值 / 股价  │ 硬性过滤
                    │ 20 日平均美元成交额（不是成交股数！）          │
                    └──────────────────┬───────────────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────────┐
            阶段 B  │ 距 52 周低点 ≤ 15%  且  较 52 周高点回撤 ≥ 20% │ 是否够低
                    └──────────────────┬───────────────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────────┐
            阶段 C  │ RSI 回升 / 低点抬高 / 站上 20-50 日均线 /      │ 企稳证据
                    │ 上涨日成交额改善                               │
                    └──────────────────┬───────────────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────────┐
            阶段 D  │ 财报临近 / SEC 融资稀释 / 退市文件 / 异常波动 / │ 风险标签
                    │ 持续创新低 / 低于 200 日均线 / 市场环境        │（独立标签，不用总分抵消）
                    └──────────────────┬───────────────────────────┘
                                       ▼
              100 分制评分（流动性 25 + 低位 30 + 企稳 25 + 质量 20）
                                       ▼
        ┌──────────────────────────┬────────────────────────────┐
        │ A. 低位企稳组（主要关注）  │ B. 低位但尚未企稳（次级观察） │
        └──────────────────────────┴────────────────────────────┘
```

### 报告长什么样

完全照搬产品构想第 6 节的「报告示例」：**整份报告放在一个引用块里，一行一个字段，
每只股票是一行标题加若干缩进子项**。

```
> **美股低位观察名单｜交易日：2026-10-09**
> 数据更新：2026-10-10 06:30 America/New_York（本地 2026-10-10 18:30 Asia/Shanghai）
> 市场环境：SPY 收盘 249.84，低于 200 日均线 283.97，**整体下行环境**（近 1 个月 -9.6%）
> 股票池：扫描 122 只（已通过阶段 A 硬性过滤）
> 买入偏好：买跌（dip_preference = dip）—— 主名单是位置够低、且还没涨起来的股票
>
> **A. 低位观察组（主要关注：还没涨起来）**
> - `ZNBT`｜Sample Falling Low ZNBT｜NASDAQ｜总分 62.3/80
>   - 最新行情：收盘 US$79.70（2026-10-09）；数据源 sample；调整口径：拆股调整
>   - 低位：距 52 周低点 +2.9%，较 52 周高点回撤 -59.8%（52 周区间 US$77.46 – US$198.45）。
>   - 流动性：20 日平均美元成交额 US$107.2M，当日 US$129.5M（为 20 日均值的 1.21 倍）。
>   - 技术信号：RSI(14) 41.4；相对 20 日均线 -1.0%；相对 50 日均线 -7.4%；近期未再创新低；近 1 个月 -7.4%、近 3 个月 -20.7%。
>   - 市场对比：近 1 个月跑赢 SPY 2.2 个百分点；近 3 个月跑输 SPY 0.8 个百分点；相对行业基准 -10.0 个百分点。
>   - 企稳证据：RSI(14) 41.4 仍未见回升；短期低点抬高 +2.2%；未站上 20 日均线（-1.0%）；未站上 50 日均线（-7.4%）；上涨日成交额为下跌日的 0.99 倍（未改善）。
>   - 综合分数：62.3/80（流动性 24.3/25、低位程度 27.4/30、企稳证据 10.6/25、公司质量 未核实（0/20，不补分））。**不代表上涨概率**。
>   - 入选理由：
>     - 流动性充足：20 日平均美元成交额 US$107.2M，是门槛（US$10.0M）的 10.7 倍
>     - 位置偏低：距 52 周低点仅 2.9%，较 52 周高点回撤 59.8%
>     - 动能：RSI(14) = 41.4，近 5 日变化 -4.9
>   - 风险：🟡 注意 · 财报日期未核实；🟡 注意 · 重大事件未核实；🟡 注意 · 基本面数据不足；🟡 注意 · 市场环境风险较高；低于 200 日均线。
>   - 退出观察条件：
>     - 收盘价跌破 77.07（52 周低点 77.46 附近）并创出新低
>     - 较报告日收盘价 79.70 继续下跌超过 8%（约 73.32）
>     - 放量跌破 20 日均线 80.54，且 3 个交易日内未能收回
>   - 资料来源：行情：sample；基本面：未核实（未启用）；事件（财报/申报）：未核实（未启用 SEC 检查）。
>
> **B. 已出现企稳迹象（次级观察：价格已经在回升）**
> - `ZHHLY`｜Sample Stabilized Low ZHHLY｜总分 70.1/80｜距 52 周低点 +5.2%｜已出现止跌信号，从「买跌」角度看晚了一步（相对 20 日均线 +1.0%，RSI 52.9），单独观察，不视为主要候选。
>
> **今日结论：** 主要候选 `ZNBT`、…，共 5 只。……请按每只股票的「退出观察条件」设好离场线。
>
> **数据与覆盖说明**
> - 行情数据源：`sample`
> - 基本面：未启用，公司质量维度不计入总分，标注为「未核实」
> - 事件与财报：未启用，融资/退市等重大披露与财报日期标注为「未核实」
> - 已排除（5 只）：`ZLEM`、… —— 已反弹 / 持续创新低 / 异常波动 / 退市融资类披露
```

字段与构想第 6 节那张表一一对应：
股票 / 最新行情 / 流动性 / 低位情况 / 技术信号 / 市场对比 / 综合分数 / 入选理由 /
风险标签 / 失效条件 / 资料来源。

排版约束（有回归测试守着）：

* **一行一个字段**，最长的一行不超过 200 字符（原来是 827 字的段落）；
* 引用块之外只有最后一行免责声明；
* 每个字段都齐全，缺数据写「未核实」，不用空白或 0 冒充；
* 网页仪表板仍然是表格（表格在浏览器里更好读），Markdown 报告是这种列表形式。

### 评分怎么算（每一分都能追溯）

| 维度 | 满分 | 具体刻度 |
|---|---:|---|
| 流动性 | 25 | 20 日均额相对门槛的对数刻度（达到门槛的 10 倍拿满 20 分）+ 成交额稳定性（变异系数 0.2 拿满、1.0 归零，5 分） |
| 低位程度 | 30 | 距 52 周低点越近越高（13 分）；较 52 周高点回撤 20%→60% 线性给分（12 分）；6 个月跌幅提供加分，**但半年腰斩以上反而减半**（5 分） |
| 企稳证据 | 25 | RSI 水平×斜率（8 分）；短期低点抬高（6 分，仍在创新低则归零）；站上 20/50 日均线（7 分）；上涨日/下跌日成交额比（4 分） |
| 公司质量 | 20 | 营收增速、EPS 增速、利润率、流动比率 |

> **基本面数据缺失时的处理**：不补满分，也不补平均分。该维度记为 `None`，总分按
> **80 分制**展示，并明确标注「基本面覆盖不足、置信度较低」。

### 分组规则

| 分组 | 条件 | 报告位置 |
|---|---|---|
| **低位企稳组** | 企稳分 ≥ 阈值（默认 12/25）且没有高风险标签 | 报告 A 区，主要候选 |
| **低位但尚未企稳组** | 企稳分不足，或带 `持续创新低` / `异常波动` / `融资稀释` / `退市` 等高风险标签 | 报告 B 区，仅作观察 |

---

## 3. 买跌还是买涨（`dip_preference`）

**这一节是默认配置的核心，直接决定主名单给你哪些股票。**

先说清楚一个反直觉的事实：**"企稳证据"和"买跌"是矛盾的。**
企稳证据里的"站上 20/50 日均线""RSI 从低位回升"，本身就意味着**价格已经在涨了**。
按产品构想的默认策略（企稳组当主名单）跑出来，A 组会是这样的股票：

| 代码 | 距低点 | 回撤 | 近 1 月 | RSI | vs 50 日线 |
|---|---|---|---|---|---|
| ZHHLY | +5.2% | -72.1% | +1.4% | 52.9 | **+0.3%** |
| ZXZDB | +4.9% | -52.9% | +0.8% | 52.0 | **+1.0%** |

回撤很深没错，但它们**已经站上均线、RSI 回到 52** —— 对"买跌不买涨"的人来说，这就是
"涨起来了"。所以默认配置改成了：

```ini
[general] ... （无关）
[filters]
dip_preference = dip        # 主名单 = 位置够低、且还没涨起来的那一组
max_gain_1m_pct = 10.0      # 近 1 个月涨幅超过 10% -> 不算"跌"
max_gain_3m_pct = 20.0      # 近 3 个月涨幅超过 20% -> 不算"跌"
max_rsi_for_dip = 60.0      # RSI 高于 60 -> 反弹已经走了一段
require_below_ma50 = false  # 改成 true = 必须仍在 50 日均线下方（最纯粹的买跌）
rebound_action = exclude    # exclude 直接排除 / demote 单独列"已反弹" / off 不过滤
```

改完之后，主名单变成**真正还在跌的**那批：

| 代码 | 距低点 | 回撤 | 近 1 月 | 近 3 月 | RSI | vs 50 日线 |
|---|---|---|---|---|---|---|
| ZNBT | +2.9% | -59.8% | **-7.4%** | -20.7% | 41.4 | -7.4% |
| ZUINY | +2.4% | -69.0% | **-1.3%** | -26.8% | 42.9 | -3.3% |
| ZWKRQ | +2.7% | -63.7% | **-6.9%** | -32.1% | 39.9 | -9.7% |

想要回到"优先给已经止跌的"，把 `dip_preference` 改成 `stabilized` 即可，报告里的
A/B 组标题、排序依据、数量上限分配都会跟着换。

**代价必须说清楚**：还在下跌的股票**没有任何止跌证据**，抄在半山腰的概率明显更高。
这类名单必须配合「退出观察条件」用 —— 每只股票都给了具体的离场线
（跌破 52 周低点、较报告日再跌 8%、放量跌破 20 日均线且 3 日未收回…）。

两条护栏（在买跌模式下尤其重要）：

* **连续创新低的不进主名单** —— 天天刷新新低的属于自由落体，不是"低位"。
  想关掉这个护栏：`demote_on_persistent_new_low = false`。
* **异常波动的不进主名单** —— 单日 20% 以上的波动、波动率是平时 2.5 倍以上。
  想关掉：`demote_on_extreme_volatility = false`。

## 4. 网站

工具会输出**两种**网站，各有用处：

| | 静态网站（默认） | 本地服务 |
|---|---|---|
| 怎么打开 | 双击 `reports/site/index.html` | `cli serve` 后访问 http://127.0.0.1:8765 |
| 要不要跑命令 | 不用，文件已经在磁盘上 | 要，关终端/重启就没了 |
| 能不能分享 | 整个 `site/` 目录打包发给别人就能看 | 只能本机看 |
| 交互能力 | 只能浏览 | 在线筛选、参数滑块实时试算、一键补跑 |
| 生成时机 | 每次 `scan` / `demo` 自动生成，也可 `cli site` 单独导出 | 随时 |

两者用的是**同一套渲染代码**（`webapp.py`），只是链接方式不同：
静态模式把所有页面平铺在一个目录里、链接全部用相对文件名，所以在 `file://`
下也不会出现死链（有测试专门检查这一点）。

### 静态网站

```
reports/site/
├── index.html                  ← 打开这个
├── session-2026-10-09.html     ← 每个交易日一个页面
├── history.html
├── lab.html
├── outcomes.html
├── symbol-<代码>.html           ← 每只入选过的股票一个页面
└── 2026-10-09_low_scan.csv     ← 报告文件副本，首页有下载入口
```

`[output] write_html = false` 可以关掉自动导出；`html_dir` 改输出位置。

### 本地服务

| 页面 | 用途 |
|---|---|
| `/` | 主名单：数据新鲜度横幅 + A/B 两组表格 + 每只股票展开细节（分项得分、风险标签、退出条件） |
| `/history` | 历史名单检索：代码 / 分组 / 日期区间 / 最低总分，另附「反复入选的代码」 |
| `/symbol/<TICKER>` | 单只股票：什么时候被选过、当时什么位置、之后 5/10/20 个交易日实际表现 |
| `/lab` | **参数实验台**：拖几个数字，立刻看到「多出哪几只、少掉哪几只」 |
| `/outcomes` | *（进阶，默认不在导航里）* 入选之后实际发生了什么：胜率、相对 SPY 超额、最大不利波动 |
| `/api/*` | JSON 接口：`/api/sessions`、`/api/latest`、`/api/session/<日期>`、`/api/history`、`/api/symbol/<代码>`、`/api/outcomes`、`/api/lab` |
| `/file/<日期>/low_scan.csv` | 直接下载当天的 Markdown / CSV / JSON 原始报告 |

界面是纯标准库 `http.server` 渲染的，没有构建步骤、没有 CDN、没有前端依赖，
浅色/深色跟随系统。默认只监听 `127.0.0.1`，不会对外暴露。

### 参数实验台

`daily_features` 表里存了每只股票当天的**全部特征**，所以改门槛只需要对特征重新筛选、
重新打分，**不需要重新抓行情**：

```bash
python3 -m us_low_scan.cli lab --set near_low_pct=20 --set min_stabilization_score=8
```

输出会直接告诉你「相比正式名单：新增 X 只、消失 Y 只、保持 Z 只，分别是哪些」。
口径提醒：实验台不会重算来自外部事件的风险（财报、SEC 申报），这类标签统一显示为
「未核实」，正式名单仍以 `scan` 的结果为准。

---

## 5. 数据源

工具把「数据源」抽象成一个统一接口，切换只需要改一行配置。

| provider | 是否联网 | 用途 | 关键限制 |
|---|---|---|---|
| `nasdaq` | 是 | **看真实股票就用它**：Nasdaq 官方公开接口，免费、**不需要注册或 API key**。清单约 7000 只交易所上市股票，自带**市值和行业**（Alpaca / Alpha Vantage 都没有市值） | 非官方接口，字段/限流可能变；不返回交易所；价格只做拆股调整不含分红；逐只请求，150 只约 1-3 分钟 |
| `sample` | 否 | **默认**。离线合成市场，含 8 种形态原型，用来验证规则与报告格式、跑测试 | **代码与公司名是虚构的**，不是真实行情，不可用于投资判断 |
| `csv` | 否 | 读取任何供应商导出的 `<TICKER>.csv` | 覆盖范围取决于你的数据 |
| `alpaca` | 是 | Alpaca Market Data v2，可批量拉日线 | 资产接口**不含市值**；`feed=iex` 只有 IEX 一个交易所的成交，**不能代表全市场成交量**，全市场口径需要 `sip` 付费订阅 |
| `alphavantage` | 是 | 日线（adjusted close + 拆股/分红调整） | 免费额度极低（25 次/天量级），**不适合收盘后扫全市场** |

### 看真实股票（推荐路径，不用注册）

```bash
python3 -m us_low_scan.cli --provider nasdaq --top 150 scan --catch-up
```

* `--top 150`：抓行情之前先按**市值取前 150 只**，否则全美 7000 只逐只请求要跑几个小时
  （不加 `--top` 时程序会自动兜底成 250 并提示你）；
* `--catch-up`：把错过的交易日按顺序补齐（只抓一次行情，逐日回放）；
* 数据落在 `data/market.db`，报告在 `reports/`，网站自动导出到 `reports/site/`。

想指定自己的清单：`--watchlist my_tickers.txt`（每行一个代码）。
想换回离线演示：`python3 -m us_low_scan.cli demo`（写到 `data/demo.db` + `reports/demo/`，
**不会和真实数据混在一起**）。

### 其他数据源

```ini
[data_source]
provider = nasdaq       ; nasdaq / csv / alpaca / alphavantage / sample
csv_dir  = /path/to/csv
```

用 Alpaca 时通过环境变量提供密钥（不要写进配置文件）：

```bash
export ALPACA_API_KEY=xxx
export ALPACA_SECRET_KEY=yyy
python3 -m us_low_scan.cli doctor    # 验证连通性并打印覆盖说明
```

### CSV 格式

```
<csv_dir>/AAA.csv           # 列名大小写不敏感，支持 date/open/high/low/close/adj_close/volume
<csv_dir>/symbols.csv       # 可选：ticker,name,exchange,security_type,market_cap,sector,sector_etf,is_otc
<csv_dir>/fundamentals.csv  # 可选：ticker,revenue_growth_yoy,eps_growth_yoy,profit_margin,current_ratio
```

没有账号也可以先试：`python3 tools/make_sample_csv.py --out data/sample_csv` 会导出一套
标准 CSV，把 `provider = csv`、`csv_dir = data/sample_csv` 填上即可。

### 市值补齐与风险事件

市值：Alpaca / Alpha Vantage 的资产接口都不提供市值。用 `universe.market_cap_file`
指定一个 `ticker,market_cap` 的 CSV 补齐；没补齐时按
`universe.missing_market_cap_policy` 处理（默认 `flag`：放行但打「市值未覆盖」标签）。

风险事件：`data_source.sec_enabled = true` + `sec_user_agent` 打开 SEC EDGAR 检查，
可识别 `重大融资稀释`（S-1/S-3/424B）、`退市风险文件`（Form 25/15）等。
EDGAR 不提供未来财报日历，工具从历史 8-K item 2.02 按季度节奏**推算**下次财报日并注明是推算值。

---

## 6. 通知

```bash
python3 -m us_low_scan.cli notify --dry-run              # 只打印，不发送
python3 -m us_low_scan.cli notify --channel email,webhook
```

`config/config.ini`：

```ini
[notify]
enabled = true
channels = email, webhook      # email / webhook / macos
min_main_candidates = 1        # 主要候选少于该数量就不打扰你（0 = 每天都发）
include_reasons = false        # 是否把入选理由也放进正文
```

* **邮件**：SMTP + STARTTLS，密码走环境变量 `SMTP_PASSWORD`（`smtp_host` / `mail_to` 等见配置）。
* **Webhook**：`webhook_kind` 支持 `generic` / `slack` / `wecom`（企业微信机器人）/
  `dingtalk`（钉钉机器人）/ `telegram`，各自的 JSON 载荷格式已经内置。
* **macOS 通知**：`macos_notify = true` 走 `osascript`，只发一行摘要。

`scan` 结束时若 `[notify] enabled = true` 会自动发送；任何一个渠道失败都不会让扫描失败
（通知挂了不该让数据流程挂掉），失败原因会打印在终端。

---

## 7. 会自动更新吗？（必须开着的那个问题）

**不会自己更新 —— 得有东西叫醒它。** 说清楚三种情况：

| 情况 | 会不会更新 |
|---|---|
| Mac 开着，定时任务触发 | ✅ 自动跑（launchd / cron） |
| 到点时 Mac 在**睡眠** | ⚠️ launchd 会在**唤醒后补跑**这次任务；**cron 不会**（所以推荐 launchd） |
| 到点时 Mac 是**关机**状态 | ❌ 不跑。launchd 和 cron 都不会在下次开机时补跑错过的时间点 |

针对第三种情况，工具做了**补跑（catch-up）**：

```bash
python3 -m us_low_scan.cli scan --catch-up     # 把缺的交易日按顺序全补上
python3 -m us_low_scan.cli doctor              # 直接告诉你还差几天没跑
```

* `tools/run_daily.sh` 用的就是 `scan --catch-up`，所以**定时任务每次触发时都会顺手把缺的补齐**；
* 网页仪表板打开时，如果发现数据落后，会在顶部显示一条黄色横幅
  「数据最新到 X，还有 N 个交易日没出名单」+ 一个**立即补跑**按钮，点一下就跑；
* 补跑只取一次行情，然后逐日回放，不会为每天重复请求数据源；
* 单次最多补 `[general] catch_up_max_days`（默认 10）个交易日，避免关机一周后一次性打爆 API 额度。

所以实际使用时：**不用一直开机**。你可以一周开一次电脑，打开仪表板点「立即补跑」，
或者什么都不点，等下一次定时任务触发时它自己补齐。

## 8. 什么时候跑（时区）

* 交易日历按 **NYSE 假日规则**实现（含元旦顺延、Juneteenth、耶稣受难日、感恩节等），
  并处理**提前收市日**（黑五、7/3、12/24 → 13:00 ET）。临时休市可用
  `calendar_us.EXTRA_HOLIDAYS` 补充。
* 全部时间判断使用 `America/New_York` 时区，自动处理夏令时，不写死 UTC 偏移。
* 报告里展示时间使用 `display_timezone`（默认 `Asia/Shanghai`，新加坡用户改成 `Asia/Singapore`）。
* 程序只在**最近一个已收盘交易日**上运行；如果行情数据还没更新到那天，会**跳过并说明原因**，
  而不是拿陈旧数据出一份名单。

定时任务：

```bash
./tools/install_launchd.sh          # macOS 推荐：按当前路径生成并注册 launchd 任务
```

> **⚠️ 坑：项目不能放在 `~/Documents` / `~/Desktop` / `~/Downloads` 里。**
> macOS 的隐私保护（TCC）不允许 launchd 启动的后台任务读取这几个目录，
> 任务会失败并报 `Operation not permitted`（Terminal 里手动跑却完全正常，很难查）。
> 把项目放到 `~/us-low-scan` 这类位置再装定时任务：
>
> ```bash
> cp -R /path/to/us-low-scan ~/us-low-scan && cd ~/us-low-scan && ./tools/install_launchd.sh
> ```
>
> `install_launchd.sh` 会检测到这种情况并直接给出提示。

装好之后：

```bash
launchctl list | grep uslowscan                    # 确认已注册
launchctl start com.uslowscan.daily                # 立即手动跑一次
tail -f logs/launchd.out.log                       # 看运行日志
launchctl unload -w ~/Library/LaunchAgents/com.uslowscan.daily.plist   # 卸载
```

默认时间表：**上海/新加坡时间周二~周六 05:30**（= 美东周一~周五收盘后约 1.5 小时）。
改时间：`HOUR=6 MINUTE=0 ./tools/install_launchd.sh`。

`tools/run_daily.sh` 会依次执行 `scan`（含自动通知）→ `outcomes`，并把日志写到 `logs/`。
想固定用某个参数方案，把脚本里的命令改成
`python3 -m us_low_scan.cli --preset conservative scan` 即可。

---

## 9. 回测与验收

```bash
python3 -m us_low_scan.cli backtest --start 2025-06-01 --end 2026-10-09 --step 2
```

结果会**分开报告**「低位企稳组」和「低位但尚未企稳组」，用来验证企稳过滤是否真的改善结果，
同时给出相对 SPY 的超额、胜率、最大不利波动、10 日内跌破前低的比例。

无未来数据泄漏是硬要求，做法：

1. 每个交易日只把**该日及之前**的 K 线交给筛选逻辑（`backtest.HistoricalView`）；
2. 前瞻收益用完整数据**单独**计算，与筛选过程隔离；
3. **实盘扫描与回放共用同一份 `pipeline.run_scan`**，不存在两套逻辑；
4. 有专门的测试把「能看到未来数据」与「看不到未来数据」两种情形逐一比对，必须完全一致。

仍需注意（报告里也会写明）：幸存者偏差、数据源历史口径差异、未扣除交易成本与滑点。

---

## 10. 目录结构

```
us-low-scan/
├── config/config.ini          # 全部门槛、权重、数据源、通知、参数方案
├── us_low_scan/
│   ├── calendar_us.py         # NYSE 交易日历（假日、提前收市、时区）
│   ├── config.py              # 配置解析、校验、参数方案（presets）
│   ├── db.py                  # SQLite 存储层（6 张业务表 + run_log）
│   ├── indicators.py          # RSI / 均线 / 回撤 / 波动率 / 低点形态
│   ├── features.py            # 阶段 B/C/D 的特征计算
│   ├── universe.py            # 阶段 A：股票池与硬性过滤
│   ├── scoring.py             # 100 分制评分、分组、入选理由
│   ├── risk.py                # 风险标签、市场环境、失效条件
│   ├── report.py              # Markdown / CSV / JSON / 纯文本摘要
│   ├── outcomes.py            # 入选后 5/10/20 个交易日表现跟踪
│   ├── backtest.py            # 无未来数据泄漏的历史回放
│   ├── history.py             # 历史名单检索（CLI 与网页共用）
│   ├── lab.py                 # 参数实验台（基于特征快照重算）
│   ├── notify.py              # 邮件 / Webhook / macOS 通知
│   ├── webapp.py              # 页面渲染（静态站点与本地服务共用）
│   ├── site.py                # 静态网站导出（双击即可打开）
│   ├── pipeline.py            # 每日流程编排
│   ├── cli.py                 # 命令行入口
│   └── providers/             # sample / csv / alpaca / alphavantage / SEC EDGAR
├── tests/                     # 182 个单元测试，全部离线
├── tools/                     # 定时任务、launchd 安装、示例 CSV 导出
├── docs/                      # 设计取舍、与产品构想的对照表
└── reports/                   # 输出（自动创建）
```

### 数据表

| 表 | 内容 |
|---|---|
| `symbols` | ticker、名称、交易所、证券类型、市值、行业、上市状态 |
| `daily_bars` | 日期、OHLCV、美元成交额、数据源、调整口径 |
| `daily_features` | 全部股票（不只是入选的）的 52 周高低点距离、回撤、RSI、均线、相对强弱 —— 参数实验台的数据来源 |
| `daily_candidates` | 评分、分组、入选理由、风险标签、失效条件 |
| `corporate_events` | 财报日期、SEC 申报、公司行动（带来源与抓取时间） |
| `candidate_outcomes` | 5/10/20 日收益、SPY 对照、最大不利波动、是否创新低 |
| `run_log` | 每次运行的元信息（「今天为什么没出名单」可直接查） |

---

## 11. 配置速查

### `[general]` / `[data_source]` / `[output]`

| 键 | 默认 | 说明 |
|---|---|---|
| `general.database` | `data/market.db` | 数据库路径 |
| `general.output_dir` | `reports` | 报告输出目录 |
| `general.market_timezone` | `America/New_York` | 市场时区（不要改成固定偏移） |
| `general.display_timezone` | `Asia/Shanghai` | 报告里展示时间的时区 |
| `general.benchmark` | `SPY` | 相对强弱与市场环境的基准 |
| `general.catch_up_max_days` | `10` | 关机错过后，单次最多补跑几个交易日 |
| `data_source.provider` | `sample` | `nasdaq`（真实，免注册）/ `csv` / `alpaca` / `alphavantage` / `sample`（演示） |
| `output.max_main_candidates` | `5` | A 组上限（不够就少报，不补位） |
| `output.max_watch_candidates` | `5` | B 组上限 |
| `output.save_all_features` | `true` | 是否保存全部特征（关掉实验台就失效） |
| `output.write_html` | `true` | 每次扫描后自动导出静态网站 |
| `output.html_dir` | `reports/site` | 静态网站输出目录 |

### `[universe]` 股票池与硬门槛

| 键 | 默认 | 说明 |
|---|---|---|
| `exchanges` | `NYSE, NASDAQ, NYSE_AMERICAN` | 允许的交易所 |
| `security_types` | `common_stock` | 允许的证券类型 |
| `include_adr` | `false` | 是否纳入 ADR |
| `exclude_otc` | `true` | 排除 OTC / 粉单 |
| `min_price` | `5.0` | 最低股价（美元） |
| `min_market_cap` | `1000000000` | 最低市值（美元） |
| `min_avg_dollar_volume_20d` | `10000000` | 20 日平均美元成交额门槛 |
| `watchlist_file` | 空 | 只扫描清单里的代码（原型阶段用） |
| `max_symbols` | `0` | 股票池上限（0=不限），按均额截断以控制 API 额度 |
| `prefilter_top_market_cap` | `0` | 抓行情前先按市值取前 N 只（真实数据源建议 150-300） |
| `missing_market_cap_policy` | `flag` | 市值缺失时 `flag`（放行+打标签）或 `exclude` |
| `market_cap_file` | 空 | 外部市值补充 CSV |
| `exclude_name_keywords` | 见文件 | 名称关键词排除（Warrant/ETF/Trust…） |

### `[filters]`

| 键 | 默认 | 说明 |
|---|---|---|
| `lookback_days` | `252` | 低位观察区间（交易日） |
| `near_low_pct` | `15.0` | 距 52 周低点不超过该百分比 |
| `min_drawdown_from_high_pct` | `20.0` | 相对 52 周高点最小回撤 |
| `min_stabilization_score` | `12.0` | 进入企稳组的最低企稳分 |
| `dip_preference` | `dip` | **主名单给谁**：`dip` 买跌（低位且还没涨起来）/ `stabilized` 买涨（已止跌回升）/ `both` |
| `max_gain_1m_pct` | `10.0` | 近 1 个月涨幅超过此值 -> 判为"已反弹"，不进主名单 |
| `max_gain_3m_pct` | `20.0` | 近 3 个月涨幅上限 |
| `max_rsi_for_dip` | `60.0` | RSI(14) 上限，超过说明反弹已经走了一段 |
| `require_below_ma50` | `false` | `true` = 主名单必须仍在 50 日均线下方（最纯粹的买跌） |
| `rebound_action` | `exclude` | 命中"已反弹"后：`exclude` 直接排除 / `demote` 单独列 / `off` 不过滤 |
| `max_stale_days` | `7` | 行情陈旧多少天判为数据缺失 |
| `earnings_alert_days` | `3` | 财报临近窗口 |
| `volatility_spike_ratio` | `2.5` | 5 日波动率 / 60 日波动率 超过该倍数 → 异常波动 |
| `single_day_move_pct` | `15.0` | 单日涨跌幅超过该值 → 异常波动 |
| `market_regime_ma` | `200` | 基准低于该均线 → 市场环境风险标签 |
| `demote_on_*` | `true` | 严重风险是否直接把股票降级到观察组 |

### `[scoring]`

四个维度权重与全部分项刻度都在这里。**改了维度权重，分项会按比例自动重算**
（`weight_stabilization = 30` 后，RSI/低点/均线/成交额四个分项之和会精确等于 30）。
`quality_enabled = false` 时总分按 80 分制展示。

### `[presets]` / `[preset.<名字>]` 参数方案

```ini
[preset.conservative]
near_low_pct = 10
min_stabilization_score = 16
weight_stabilization = 30
max_main_candidates = 3
```

```bash
python3 -m us_low_scan.cli --preset conservative scan
python3 -m us_low_scan.cli --preset aggressive demo
```

可以覆盖 `[universe]` / `[filters]` / `[scoring]` / `[output]` 里的任意键（写错键名启动时就会报错）。
在参数实验台里试出满意的组合后，抄进配置文件即成新方案。

---

## 12. 「表现跟踪」是什么（它不是持仓模块）

先把容易误会的地方说清楚：**工具里没有持仓、没有仓位、没有盈亏账本，也不会记录你买了什么。**
它不知道你买没买、买了多少、成本多少 —— 这些它一概不管。

那 `candidate_outcomes` 表在干什么？它只做一件事：

> 每一只进入名单的股票，**记下它入选之后 5 / 10 / 20 个交易日的真实走势**。

为什么非要记这个？因为**没有它就无法验证规则到底管不管用**。
比如现在默认的买跌模式，跑出来的是「连续下跌、没有止跌证据」的股票 ——
如果不跟踪，你永远不知道这类股票是"抄在半山腰"还是"快到谷底了"。
记录之后就能看到：

```bash
python3 -m us_low_scan.cli outcomes
```

| 窗口 | 样本 | 平均收益 | 胜率 | 平均超额 vs SPY | 跑赢 SPY 比例 | 平均最大不利波动 |
|---|---:|---:|---:|---:|---:|---:|
| 20 个交易日 | 1067 | +0.15% | 55.7% | +7.45% | 61% | -6.57% |

其中「平均最大不利波动」= 入选之后先跌了多少（用来判断你能不能扛住），
「10 日内再创新低比例」= 有多少只其实还在往下走。这两列是校正规则的主要依据。

已经按你说的做了精简：

* 仪表板导航里**去掉了「表现跟踪」**，现在只有 今日名单 / 历史检索 / 参数实验台 三个入口；
* 每日定时任务里这一步改成**静默执行**，不再打印表格；
* 报告正文里也不出现它。

但**数据仍然在后台自动记录**（成本是每次扫描多几十毫秒），因为一旦停掉，
前面这些统计和回测就都失去依据了。**如果你确认连数据表也不要，说一句，我把
`outcomes.py`、`candidate_outcomes` 表、`/outcomes` 页面和相关测试一起删掉**
（代价是回测报告里的胜率/超额统计会一并消失，只剩价格走势对比）。

## 13. 已知限制（请务必读）

1. **演示数据不是真实股票**：`demo` 用的 `sample` 是合成数据，代码（如 `ZNBT`）在现实中不存在。
   真实数据用 `--provider nasdaq`。
1.1 **Nasdaq 接口是非官方的**：字段和限流随时可能变；它不提供交易所字段（所以报告里写「交易所未覆盖」），
   价格只做拆股调整、不含分红。请保持克制频率，不要用于商业分发。
2. **免费数据源不完整**：Alpaca `iex` 只有一个交易所的成交；Alpha Vantage 免费额度极低。
   想做全市场成交活跃度排名，需要 consolidated（SIP）级别的付费数据。
3. **市值/基本面通常要另外补**：行情 API 很少有市值，工具会明确标注「未覆盖」而不是忽略。
4. **财报日期是推算值**（若开启 SEC 检查），不是官方日历。
5. **回测未扣除交易成本、价差与滑点**，且存在幸存者偏差。
6. **「低位」不等于会涨**：工具给的是观察对象和退出条件，不是买入信号，也不是上涨概率。
7. **仪表板默认只监听本机**：要给别人看请自行加反向代理并加访问控制，它没有登录体系。

---

## 14. 开发路线图的落地情况

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段 1：可运行原型 | 扫描有限股票池、每日名单、门槛全部可配置 | ✅ `scan` / `demo` |
| 阶段 2：全市场与风险信息 | 数据额度控制、公司事件、SEC 申报、风险标签、每日存档 | ✅ `risk` / `providers/sec_edgar` / `daily_*` 表 |
| 阶段 3：回测验证 | 历史回放、SPY 对比、5/10/20 日表现、最大不利波动、成本与幸存者偏差说明 | ✅ `backtest` / `outcomes` |
| 阶段 4：界面与通知 | 网页仪表板、邮件/IM 通知、历史名单检索、个性化参数 | ✅ `site`（静态网站，默认自动导出）/ `serve`（可交互）/ `notify` / `history` / `lab` / `--preset` |

---

## 15. 免责声明

本工具输出的是**研究用的观察名单**，不构成任何投资建议、要约或承诺。
所有指标都是对历史数据的描述，不预示未来表现。
「低位」与「企稳证据」都可能失败，价格可能继续下跌甚至退市。请自行判断并承担风险。

---

## 开源许可

MIT License，见 [LICENSE](LICENSE)。

**这个工具不提供任何投资建议**，也不保证选出来的股票会上涨。它做的事只有一件：
把「位置低、成交活跃、情况可查」的股票按透明的规则整理成一份名单，
并把风险和退出条件写清楚。是否买卖、买卖多少，都由你自己判断。

数据来源请注意：项目自带一个离线**合成**数据源（`sample`）用于演示和测试，
它的股票代码在现实中不存在；真实数据默认走 Nasdaq 的公开网页接口（免费、无需 key），
是非官方接口，请自用、别做商业分发或高频抓取。
