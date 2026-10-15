"""
核心财务比率计算
输入：标准化后的面板 DataFrame（index=ticker, report_date）
输出：同结构 DataFrame，新增比率列
"""

import pandas as pd
import numpy as np
from typing import List, Optional


def compute_all_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """计算全套核心比率，返回新增列的 DataFrame"""
    df = df.copy()
    df = df.sort_values(["ticker", "report_date"])

    # 分组应用，避免跨公司计算
    def _calc(group: pd.DataFrame) -> pd.DataFrame:
        g = group.copy()

        # ===== 盈利能力 =====
        # ROE = 归母净利润 / 期初归母权益（近似用期末）
        if "net_profit_parent" in g and "equity_parent" in g:
            g["roe"] = g["net_profit_parent"] / g["equity_parent"].replace(0, np.nan)

        # ROIC = NOPAT / 投入资本
        # NOPAT ≈ 营业利润 * (1 - 税率)，简化用营业利润 * 0.75
        # 投入资本 = 总资产 - 无息流动负债（应付账款等） - 现金
        # 简化：总资产 - 货币资金 - 应付账款 - 其他流动负债(近似)
        if all(c in g for c in ["operating_profit", "total_assets", "cash_equiv", "accounts_payable"]):
            tax_rate = 0.25
            nopat = g["operating_profit"] * (1 - tax_rate)
            invested_capital = g["total_assets"] - g["cash_equiv"] - g["accounts_payable"]
            g["roic"] = nopat / invested_capital.replace(0, np.nan)

        # 毛利率 / 营业利润率 / 净利率
        if "revenue" in g:
            if "gross_profit" in g:
                g["gross_margin"] = g["gross_profit"] / g["revenue"].replace(0, np.nan)
            if "operating_profit" in g:
                g["operating_margin"] = g["operating_profit"] / g["revenue"].replace(0, np.nan)
            if "net_profit_parent" in g:
                g["net_margin"] = g["net_profit_parent"] / g["revenue"].replace(0, np.nan)

        # ===== 现金流质量 =====
        # FCF 转换率 = FCF / 归母净利润
        if "fcf" in g and "net_profit_parent" in g:
            g["fcf_conversion"] = g["fcf"] / g["net_profit_parent"].replace(0, np.nan)

        # 经营现金流/净利润
        if "ocf" in g and "net_profit_parent" in g:
            g["ocf_to_net_profit"] = g["ocf"] / g["net_profit_parent"].replace(0, np.nan)

        # 资本支出/折旧摊销（维持性 Capex 代理）
        # 需要折旧摊销数据，若无则用 Capex/Revenue
        if "capex" in g and "revenue" in g:
            g["capex_to_revenue"] = g["capex"].abs() / g["revenue"].replace(0, np.nan)

        # ===== 偿债能力 =====
        # 净负债 = 短期借款 + 长期借款 - 货币资金
        if all(c in g for c in ["short_term_debt", "long_term_debt", "cash_equiv"]):
            g["net_debt"] = g["short_term_debt"].fillna(0) + g["long_term_debt"].fillna(0) - g["cash_equiv"].fillna(0)

        # 净负债/EBITDA（近似：EBITDA ≈ 营业利润 + 折旧摊销，无折旧用营业利润*1.3）
        if "net_debt" in g and "operating_profit" in g:
            ebitda_approx = g["operating_profit"] * 1.3
            g["net_debt_to_ebitda"] = g["net_debt"] / ebitda_approx.replace(0, np.nan)

        # 资产负债率
        if "total_liabilities" in g and "total_assets" in g:
            g["debt_to_asset"] = g["total_liabilities"] / g["total_assets"].replace(0, np.nan)

        # 权益乘数
        if "total_assets" in g and "equity_parent" in g:
            g["equity_multiplier"] = g["total_assets"] / g["equity_parent"].replace(0, np.nan)

        # ===== 营运效率 =====
        # 应收账款周转天数 = 应收账款 / 营业收入 * 365
        if "accounts_receivable" in g and "revenue" in g:
            g["ar_days"] = g["accounts_receivable"] / g["revenue"].replace(0, np.nan) * 365

        # 存货周转天数
        if "inventory" in g and "revenue" in g:
            g["inventory_days"] = g["inventory"] / g["revenue"].replace(0, np.nan) * 365

        # 应付账款周转天数
        if "accounts_payable" in g and "revenue" in g:
            g["ap_days"] = g["accounts_payable"] / g["revenue"].replace(0, np.nan) * 365

        # 现金周期 = 应收天数 + 存货天数 - 应付天数
        if all(c in g for c in ["ar_days", "inventory_days", "ap_days"]):
            g["cash_cycle_days"] = g["ar_days"] + g["inventory_days"] - g["ap_days"]

        # 总资产周转
        if "revenue" in g and "total_assets" in g:
            g["asset_turnover"] = g["revenue"] / g["total_assets"].replace(0, np.nan)

        # ===== 成长性（YoY）=====
        for col in ["revenue", "net_profit_parent", "ocf", "fcf", "equity_parent"]:
            if col in g:
                g[f"{col}_yoy"] = g[col].pct_change()

        # ===== 分红/回购 =====
        # 股息支付率 = 分红 / 归母净利润（需分红数据，AKShare 可单独获取）
        # 这里留接口

        return g

    result = df.groupby("ticker").apply(_calc)
    if "ticker" not in result.columns:
        result = result.reset_index()
    # 丢弃 groupby.apply 产生的残留索引列（level_*）
    result = result.drop(columns=[c for c in result.columns if str(c).startswith("level_")], errors="ignore")
    return result


def compute_dupont(df: pd.DataFrame) -> pd.DataFrame:
    """杜邦分解：ROE = 净利率 * 资产周转 * 权益乘数"""
    df = df.copy()
    if all(c in df for c in ["net_margin", "asset_turnover", "equity_multiplier"]):
        df["roe_dupont"] = df["net_margin"] * df["asset_turnover"] * df["equity_multiplier"]
        # 五因子分解
        # ROE = (净利润/税前利润) * (税前利润/息税前利润) * (息税前利润/营收) * (营收/总资产) * (总资产/净资产)
        # 简化：留给需要时扩展
    return df


def add_percentile_ranks(df: pd.DataFrame, metrics: List[str], by: str = "report_date") -> pd.DataFrame:
    """按报告期横向排名，添加分位数列（用于行业对标/异常检测）"""
    df = df.copy()
    for metric in metrics:
        if metric in df.columns:
            pct_col = f"{metric}_pct"
            df[pct_col] = df.groupby(by)[metric].transform(
                lambda x: x.rank(pct=True, method="average")
            )
    return df


def detect_anomalies(df: pd.DataFrame, rules: dict) -> pd.DataFrame:
    """
    基于规则的异常检测
    rules = {
        "fcf_conversion": {"type": "threshold", "operator": "<", "value": 0.5, "window": 3},
        "ar_days_yoy": {"type": "zscore", "threshold": 2, "window": 5},
        ...
    }
    """
    df = df.copy()
    anomalies = pd.DataFrame(index=df.index)

    for metric, rule in rules.items():
        if metric not in df.columns:
            continue

        if rule["type"] == "threshold":
            op = rule["operator"]
            val = rule["value"]
            window = rule.get("window", 1)
            if window > 1:
                # 连续 N 期满足
                mask = df.groupby("ticker")[metric].transform(
                    lambda x: x.rolling(window).apply(
                        lambda s: all(eval(f"v {op} {val}") for v in s if not np.isnan(v))
                    )
                ).fillna(0).astype(bool)
            else:
                mask = eval(f"df[metric] {op} {val}")
            anomalies[f"anomaly_{metric}"] = mask

        elif rule["type"] == "zscore":
            threshold = rule["threshold"]
            window = rule.get("window", 8)
            # 滚动 Z-score
            z = df.groupby("ticker")[metric].transform(
                lambda x: (x - x.rolling(window).mean()) / x.rolling(window).std().replace(0, np.nan)
            )
            anomalies[f"anomaly_{metric}"] = z.abs() > threshold

    df = pd.concat([df, anomalies], axis=1)
    return df


if __name__ == "__main__":
    # 简单测试
    import duckdb
    conn = duckdb.connect("data/financial.duckdb")
    df = conn.execute("SELECT * FROM financials").df()
    if not df.empty:
        df = compute_all_ratios(df)
        print(df[["ticker", "report_date", "roe", "roic", "fcf_conversion", "net_debt_to_ebitda", "cash_cycle_days"]].tail(20))
    conn.close()