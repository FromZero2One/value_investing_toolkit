"""
价值投资分析工具包 - 主入口
用法：
  python -m main fetch          # 拉取财报数据
  python -m main analyze        # 计算指标/评分/估值
  python -m main monitor        # 运行监控预警
  python -m main dashboard      # 启动 Streamlit 看板
  python -m main review         # 生成复盘报告
"""

import sys
import subprocess
from pathlib import Path
from datetime import datetime
from loguru import logger

# 确保日志目录
Path("logs").mkdir(exist_ok=True)
logger.add("logs/value_investing_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days", level="INFO")


def run_fetch():
    """拉取 A/H 股财报数据入库"""
    logger.info("=== 开始拉取财报数据 ===")
    from data.provider import AKShareProvider
    import yaml
    with open("config/tickers.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    provider = AKShareProvider()
    try:
        for item in cfg["tickers"]:
            ticker = item["ticker"]
            market = item["market"]
            logger.info(f"Fetching {ticker} ({market})...")
            try:
                provider.fetch_and_store(ticker, market)
            except Exception as e:
                logger.error(f"Failed {ticker}: {e}")
    finally:
        provider.close()
    logger.info("=== 财报拉取完成 ===")


def run_analyze():
    """计算全套指标、评分、估值"""
    logger.info("=== 开始计算指标 ===")
    import duckdb
    import pandas as pd
    from analysis.ratios import compute_all_ratios
    from analysis.quality import piotroski_f_score, beneish_m_score, moat_scorecard, quality_summary
    from analysis.valuation import load_assumptions, run_valuation_for_ticker, valuation_to_dataframe
    from data.provider import AKShareProvider

    conn = duckdb.connect("data/financial.duckdb")
    df = conn.execute("SELECT * FROM financials").df()
    conn.close()

    if df.empty:
        logger.error("无财报数据，请先运行 fetch")
        return

    # 1. 比率
    df = compute_all_ratios(df)
    # 1b. 行业分位数（用于对标）
    from analysis.ratios import add_percentile_ranks
    key_metrics = ["roe", "roic", "gross_margin", "operating_margin", "net_margin", 
                   "fcf_conversion", "net_debt_to_ebitda", "debt_to_asset",
                   "cash_cycle_days", "ar_days", "inventory_days",
                   "asset_turnover", "revenue_yoy", "net_profit_parent_yoy"]
    df = add_percentile_ranks(df, key_metrics)
    # 2. 质量评分
    df = piotroski_f_score(df)
    df = beneish_m_score(df)
    df = moat_scorecard(df)

    # 3. 获取实时行情用于估值
    logger.info("获取实时行情...")
    provider = AKShareProvider()
    try:
        # 只处理配置文件中有的有效 tickers
        import yaml
        with open("config/tickers.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        valid_tickers = [item["ticker"] for item in cfg["tickers"]]
        tickers = [t for t in df["ticker"].unique() if t in valid_tickers]
        quotes = provider.fetch_realtime_quotes(tickers)
    finally:
        provider.close()

    if quotes.empty:
        logger.warning("未获取到实时行情，跳过估值计算")
    else:
        logger.info(f"获取到 {len(quotes)} 只标的实时行情")
        # 4. 估值
        assumptions = load_assumptions()
        valuation_results = []
        
        # 加载 tickers 配置获取 sector
        import yaml
        with open("config/tickers.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        ticker_sector = {item["ticker"]: item.get("sector", "default") for item in cfg["tickers"]}
        
        for _, row in quotes.iterrows():
            ticker = row["ticker"]
            current_price = row["current_price"]
            total_shares = row.get("total_shares", 0)
            
            if pd.isna(current_price) or current_price <= 0:
                continue
            if pd.isna(total_shares) or total_shares <= 0:
                logger.warning(f"{ticker} 总股本为空，跳过估值")
                continue
            
            sector = ticker_sector.get(ticker, "default")
            # 传入 sector 让估值模块用行业假设
            try:
                res = run_valuation_for_ticker(
                    ticker=ticker,
                    current_price=current_price,
                    shares_outstanding=total_shares,
                    df=df,
                    assumptions=assumptions,
                    your_growth_estimate=assumptions.get("sector_overrides", {}).get(sector, {}).get("your_growth_estimate", 0.08)
                )
                valuation_results.append(res)
                logger.info(f"{ticker} 估值完成: 隐含增长P50={res.implied_growth_p50:.1%}, 安全边际={res.margin_of_safety_pct:.1%}, 建议仓位={res.margin_of_safety_pct:.1%}")
            except Exception as e:
                logger.error(f"{ticker} 估值失败: {e}")
        
        if valuation_results:
            val_df = valuation_to_dataframe(valuation_results)
            # 导出估值结果
            Path("output").mkdir(exist_ok=True)
            val_df.to_csv("output/latest_valuation.csv", index=False, encoding="utf-8-sig")
            logger.info(f"估值结果已导出: output/latest_valuation.csv")

    # 回写数据库（更新比率列）
    # 为简化，这里只打印摘要
    latest = df.sort_values("report_date").groupby("ticker").last().reset_index()
    summary = quality_summary(df)
    logger.info(f"\n{summary.to_string()}")

    # 导出最新指标快照
    output_path = "output/latest_metrics.csv"
    Path("output").mkdir(exist_ok=True)
    latest.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info(f"最新指标已导出: {output_path}")
    logger.info("=== 指标计算完成 ===")


def run_monitor():
    """运行监控预警"""
    logger.info("=== 运行监控预警 ===")
    import duckdb
    import pandas as pd
    from analysis.ratios import compute_all_ratios
    from analysis.quality import piotroski_f_score, beneish_m_score, moat_scorecard
    from monitoring.alerts import run_monitoring

    conn = duckdb.connect("data/financial.duckdb")
    df = conn.execute("SELECT * FROM financials").df()
    conn.close()

    if df.empty:
        logger.error("无数据")
        return

    df = compute_all_ratios(df)
    df = piotroski_f_score(df)
    df = beneish_m_score(df)
    df = moat_scorecard(df)

    alerts = run_monitoring(df)
    logger.info(f"监控完成，触发 {len(alerts)} 条预警")


def run_dashboard():
    """启动 Streamlit 看板"""
    logger.info("启动 Streamlit 看板...")
    subprocess.run([sys.executable, "-m", "streamlit", "run", "ui/dashboard.py"])


def run_review():
    """生成复盘报告"""
    from portfolio.decision_log import generate_review_report
    report = generate_review_report()
    print(report)
    Path("output").mkdir(exist_ok=True)
    with open(f"output/review_{datetime.now().strftime('%Y%m%d')}.md", "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("复盘报告已生成")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "fetch":
        run_fetch()
    elif cmd == "analyze":
        run_analyze()
    elif cmd == "monitor":
        run_monitor()
    elif cmd == "dashboard":
        run_dashboard()
    elif cmd == "review":
        run_review()
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)