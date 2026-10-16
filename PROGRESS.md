# 开发进度记录

> 按金融分析师视角的 P0/P1/P2/P3 优先级推进，本文档记录每轮改动的范围、验证结果与遗留问题。

---

## 2026-10-16 · P0 全部完成

### 目标
打通「实时行情 → 估值 → 看板」数据闭环，启用行业分位数对标。

### 改动清单

| # | 任务 | 涉及文件 | 说明 |
|---|------|----------|------|
| P0-1 | 实时行情接入 | `data/provider.py` | 新增 `fetch_realtime_quotes_a/hk/quotes`。A股：分钟线取最新收盘价 + `stock_zh_a_gdhs_detail_em` 列[9]取总股本（个股逐只调用，避开全量 spot 接口的 2 分钟延迟与 GBK 乱码列名问题） |
| P0-2 | 估值链路打通 | `main.py`, `analysis/valuation.py`, `config/assumptions.yaml` | `run_analyze` 集成行情获取并逐票估值，导出 `output/latest_valuation.csv`；`run_valuation_for_ticker` 新增 `sector` 参数（原来从 `assumptions["sectors"]` 取值，该键不存在，改由 tickers.yaml 提供）；市值单位修正为**亿元**（财报数据为亿元口径）；`your_growth_estimate` 写入 base 及各行业覆盖 |
| P0-3 | 行业分位数 | `main.py`, `ui/dashboard.py` | analyze 时调用 `add_percentile_ranks`（14 个关键指标）；看板总表追加 `_pct` 列并按百分比格式化；详情面板新增「📊 行业对标」标签页（盈利/现金流/偿债/营运/成长五组展示） |
| P0-4 | 估值标签页 | `ui/dashboard.py` | 详情面板新增「💰 估值分析」标签页：现价/隐含增长P50/安全边际/建议仓位卡片 + 情景树表格（熊/基/牛）+ 反向DCF分位表 + 敏感性热力图 |

### 估值输出示例（output/latest_valuation.csv）

| 标的 | 现价 | 隐含增长P50 | 安全边际 | 建议仓位 |
|------|------|-------------|----------|----------|
| 000858.SZ 五粮液 | 72.02 | -0.7% | +136% | 15% |
| 300750.SZ 宁德时代 | 279.2 | 4.1% | +0.7% | 15% |
| 600519.SH 茅台 | 1318 | 5.4% | -34.8% | 15% |

### 验证
```bash
python test_regression.py    # 15/15 通过
python -m main analyze       # 生成 latest_metrics.csv + latest_valuation.csv
python -m main dashboard     # http://localhost:8501 可正常启动
```

### 遗留问题
1. **港股实时行情失败**：`stock_hk_minute` 不存在，00700.HK 等港股无现价/总股本 → 估值跳过。待接 `stock_hk_spot_em`（慢，~1分钟）或人工维护总股本映射
2. **单位口径**：全链路假定财报为亿元，需在 README 标注
3. 总股本取自股东户数详情最新一期，股本变动频繁的公司可能滞后

### 后续待办（未开始）
- **P1**：分红/股本/回购数据入库（AKShare `stock_fhps_em`/`stock_repurchase`）；ROIC/FCF 口径规范化（区分维持性Capex、标准投入资本定义、补 Beneish 缺失变量）
- **P2**：SOTP/rNPV/DDM 估值分支实现（assumptions.yaml 已定义 `valuation_method`，`valuation.py` 分支待写）；新闻/公告催化剂抓取
- **P3**：组合风控（相关性矩阵、集中度、VaR）
