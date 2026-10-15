# 价值投资分析工具包

> 面向 A/H 股价值投资者的「飞行检查单」工具箱
> - 自动拉取财报 → 标准化入库
> - 核心比率 + 质量评分 + 反向 DCF 估值
> - 规则引擎预警 + Telegram/企微推送
> - 决策日志 + 复盘报告
> - Streamlit 一页式健康度看板

---

## 🚀 快速开始

```bash
# 1. 进入目录
cd value_investing_toolkit

# 2. 安装依赖
uv pip install -r requirements.txt
# 或：pip install -r requirements.txt

# 3. 配置自选池（已内置示例，按需修改）
# vim config/tickers.yaml

# 4. 拉取财报数据（首次运行较慢，AKShare 限速）
python -m main fetch

# 5. 计算指标/评分
python -m main analyze

# 6. 运行监控预警
python -m main monitor

# 7. 启动看板
python -m main dashboard
# 浏览器打开 http://localhost:8501
```

---

## 📁 目录结构

```
value_investing_toolkit/
├── config/
│   ├── tickers.yaml        # 自选池：代码、名称、行业、优先级
│   ├── assumptions.yaml    # 估值假设：WACC、终值、行业覆盖、维持性Capex系数
│   ├── alert_rules.yaml    # 预警规则：指标、阈值、严重度
│   └── notifier.yaml       # 推送渠道：Telegram/企微/钉钉/飞书
├── data/
│   └── provider.py         # AKShare 数据获取 + DuckDB 入库 + 手工补录接口
├── analysis/
│   ├── ratios.py           # 核心比率：ROE/ROIC/FCF转换/现金周期/杜邦分解
│   ├── quality.py          # Piotroski F-Score + Beneish M-Score + 护城河评分卡
│   └── valuation.py        # 反向DCF蒙特卡洛 + 情景树 + 敏感性矩阵 + 改良Kelly仓位
├── monitoring/
│   └── alerts.py           # 规则引擎 + 多渠道推送
├── portfolio/
│   └── decision_log.py     # 决策日志(买卖理由/假设/风险/止损) + 复盘报告 + 仓位建议
├── ui/
│   └── dashboard.py        # Streamlit 看板：总览表格 + 单标的深度诊断 + 预警面板
├── main.py                 # 统一入口：fetch/analyze/monitor/dashboard/review
├── requirements.txt
└── data/financial.duckdb   # 本地列存数据库（自动生成）
```

---

## ⚙️ 核心配置详解

### `config/tickers.yaml` - 自选池
```yaml
tickers:
  - ticker: "600519.SH"      # 格式：代码.后缀 (SH/SZ/HK)
    name: "贵州茅台"
    market: "SH"
    sector: "白酒"           # 用于行业假设覆盖、对标
    priority: "high"        # high: 跑文本分析/详细估值；medium/low: 仅基础指标
    notes: "龙头、定价权强"
```

### `config/assumptions.yaml` - 估值假设（核心）
- `base`：通用无风险利率、ERP、终值法、衰减期、蒙特卡洛次数
- `sector_overrides`：行业差异化覆盖（白酒长衰减/高终值，银行用DDM，互联网用SOTP，医药用rNPV）
- `valuation.maintenance_capex_multiplier`：各行业维持性 Capex 系数

### `config/alert_rules.yaml` - 预警规则
```yaml
rules:
  - id: "FCF_NEG_2Q"
    metric: "fcf"                    # 必须在 ratios.py 中已计算
    condition: "lt"                  # lt/lte/gt/gte/eq/cross_down/cross_up
    threshold: 0
    window: 2                        # 连续 N 期
    severity: "critical"             # critical/warning/info
    description: "自由现金流连续 2 期为负"
```

---

## 📊 看板功能

| 页面区域 | 功能 |
|----------|------|
| **核心指标总览** | 覆盖数、严重预警数、关注预警数、平均护城河分、平均FCF转换率 |
| **健康度总表** | 所有标的最新期关键指标、条件高亮（红=严重、黄=关注）、可按行业/优先级筛选 |
| **单标的深度诊断** | 4卡片关键指标 + 4标签页趋势图（盈利/现金流、营运/偿债、质量评分、估值参考） |
| **预警列表** | 当前触发的所有规则、严重度、当前值vs阈值 |
| **决策日志** | 当前持仓摘要、一键生成 Markdown 复盘报告 |

---

## 🔧 进阶用法

### 手工补录数据（解决数据质量焦虑）
```python
# 准备 Excel：列名匹配标准化字段
# ticker, report_date, period_type, revenue, net_profit_parent, ocf, capex, ...
from data.provider import manual_import_excel
manual_import_excel("data/manual_override.xlsx")
```

### 自定义行业估值模型
在 `analysis/valuation.py` 的 `forward_dcf_scenario` 中扩展 `valuation_method`：
```python
if sector == "银行":
    return dividend_discount_model(...)
elif sector == "互联网":
    return sotp_model(...)
```

### 接入实时行情（估值必需）
在 `main.py` `run_analyze` 或 `ui/dashboard.py` 中补充：
```python
# 从 AKShare/券商 API 获取实时价、总股本
import akshare as ak
spot = ak.stock_zh_a_spot_em()
price = spot[spot["代码"]==code]["最新价"].values[0]
shares = spot[spot["代码"]==code]["总股本"].values[0]
```

### 接入 LLM 文本分析（年报/电话会）
1. 启用 `requirements.txt` 可选依赖
2. 在 `analysis/text_analysis.py`（需自建）中实现：
   - PDF 下载 → 章节切片 → 向量化入 ChromaDB
   - RAG 检索 + 固定 Prompt（定价权/资本配置/竞争/风险）
   - 结果写入 `decision_log` 的 `quality_scores` 或单独表

---

## 📈 关键指标速查表

| 指标 | 优秀阈值 | 预警阈值 | 含义 |
|------|----------|----------|------|
| ROE | > 15% | < 8% | 权益回报 |
| ROIC | > 15% | < WACC(~8%) | 投入资本回报 |
| FCF转换率 | > 100% | < 50% | 利润现金化能力 |
| 净负债/EBITDA | < 1.5x | > 3x | 偿债压力 |
| 现金周期 | < 行业中位 | > 90天 | 营运效率 |
| Piotroski | ≥ 7 | ≤ 4 | 基本面质量 |
| Beneish M-Score | < -2.5 | > -2.22 | 舞弊嫌疑 |
| 护城河总分 | > 80 | < 60 | 综合竞争优势 |

---

## ⚠️ 重要提醒

1. **数据质量**：AKShare 免费源可能有延迟/缺失/错误，**重大决策前务必核对公司公告/年报原文**，或配合手工补录 Excel
2. **估值假设**：`assumptions.yaml` 中的参数直接决定估值结果，**必须按你的圈子能力圈调整**，不要盲目使用默认值
3. **预警仅作提醒**：规则引擎只报事实，**买卖决策必须人工结合业务判断**
3. **决策日志是核心**：**每次买卖必填**，季度复盘看假设验证情况，这是对抗人性弱点的唯一工具
4. **港股数据**：AKShare 港股财报接口稳定性一般，建议核心港股标的手工补录或接入付费源

---

## 🛠️ 扩展建议

| 需求 | 建议方案 |
|------|----------|
| 更多数据源 | 接入 Tushare/万得/同花顺 iFinD（需积分/付费） |
| 实时行情/图表 | 接入 `akshare` 实时行情或券商 QMT/极速 API |
| 组合风控 | 在 `portfolio/` 增加相关性矩阵、VaR、集中度限制 |
| 另类数据 | 接入专利/招聘/信用卡/卫星数据 API |
| 团队协作 | 迁移 DuckDB → PostgreSQL，加认证/权限/审计日志 |

---

## 📄 License

MIT License - 仅供个人学习研究使用，不构成投资建议。