"""
质量评分卡：Piotroski F-Score + Beneish M-Score + 自定义护城河评分
输入：已计算比率的面板 DataFrame
输出：新增评分列
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional


def piotroski_f_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    Piotroski F-Score (9 分制)
    1. ROA > 0
    2. CFO > 0
    3. ROA 改善 (YoY)
    4. CFO > 净利润 (现金含量)
    5. 长期债务减少
    6. 流动比率改善
    7. 无股份稀释
    8. 毛利率改善
    9. 资产周转改善
    """
    df = df.copy()
    # Drop any index level columns from previous groupby operations
    df = df.drop(columns=[c for c in df.columns if c.startswith('level_')], errors='ignore')
    df = df.sort_values(["ticker", "report_date"])

    def _score(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()
        score = pd.Series(0, index=g.index, dtype=int)

        # 1. ROA > 0
        if "net_profit_parent" in g and "total_assets" in g:
            roa = g["net_profit_parent"] / g["total_assets"].replace(0, np.nan)
            score += (roa > 0).astype(int)

        # 2. CFO > 0
        if "ocf" in g:
            score += (g["ocf"] > 0).astype(int)

        # 3. ROA 改善
        if "net_profit_parent" in g and "total_assets" in g:
            roa = g["net_profit_parent"] / g["total_assets"].replace(0, np.nan)
            score += (roa > roa.shift(1)).astype(int)

        # 4. CFO > 净利润
        if "ocf" in g and "net_profit_parent" in g:
            score += (g["ocf"] > g["net_profit_parent"]).astype(int)

        # 5. 长期债务减少
        if "long_term_debt" in g:
            score += (g["long_term_debt"] <= g["long_term_debt"].shift(1)).astype(int)

        # 6. 流动比率改善
        if "total_assets" in g and "short_term_debt" in g:
            # 简化：流动资产 ≈ 总资产 - 固定资产(无数据)，用总资产/短期借款近似
            current_ratio = g["total_assets"] / g["short_term_debt"].replace(0, np.nan)
            score += (current_ratio > current_ratio.shift(1)).astype(int)

        # 7. 无股份稀释（股本不增加）
        # 需要股本数据，AKShare 可能无直接字段，用 equity_parent 近似
        if "equity_parent" in g and "net_profit_parent" in g:
            # 归母权益增长 > 归母净利润 → 可能有增发
            equity_growth = g["equity_parent"].pct_change()
            profit_growth = g["net_profit_parent"].pct_change()
            score += (equity_growth <= profit_growth * 1.2).astype(int)  # 允许留存收益增长

        # 8. 毛利率改善
        if "gross_margin" in g:
            score += (g["gross_margin"] > g["gross_margin"].shift(1)).astype(int)

        # 9. 资产周转改善
        if "asset_turnover" in g:
            score += (g["asset_turnover"] > g["asset_turnover"].shift(1)).astype(int)

        g["piotroski_f_score"] = score
        # 细分项
        g["piotroski_detail"] = g.apply(lambda r: {
            "roa_pos": int(r.get("net_profit_parent", 0) / r.get("total_assets", 1) > 0) if r.get("total_assets") else 0,
            "cfo_pos": int(r.get("ocf", 0) > 0),
            "roa_improve": int((r.get("net_profit_parent", 0) / r.get("total_assets", 1)) > (r.get("net_profit_parent", 0) / r.get("total_assets", 1)) if r.get("total_assets") else 0),
            "cfo_gt_ni": int(r.get("ocf", 0) > r.get("net_profit_parent", 0)),
            "debt_down": int(r.get("long_term_debt", 0) <= r.get("long_term_debt", 0)),  # 占位
            "current_improve": 0,
            "no_dilution": 0,
            "margin_improve": int(r.get("gross_margin", 0) > r.get("gross_margin", 0)),
            "turnover_improve": int(r.get("asset_turnover", 0) > r.get("asset_turnover", 0)),
        }, axis=1)

        return g

    result = df.groupby("ticker").apply(_score)
    # Drop any level_* columns that may have been created by previous groupby operations
    result = result.drop(columns=[c for c in result.columns if c.startswith('level_')], errors='ignore')
    if "ticker" not in result.columns:
        result = result.reset_index()
        result = result.drop(columns=[c for c in result.columns if str(c).startswith("level_")], errors="ignore")
    return result


def beneish_m_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    Beneish M-Score (舞弊检测)
    M = -4.84 + 0.92*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI - 0.172*SGAI + 4.679*TATA - 0.327*LVGI
    M > -2.22 疑似舞弊
    八个变量：
    DSRI = (应收/营收)_t / (应收/营收)_t-1
    GMI = (毛利率)_t-1 / (毛利率)_t
    AQI = (1 - 核心资产/总资产)_t / (1 - 核心资产/总资产)_t-1  (核心资产=货币资金+应收+存货)
    SGI = 营收_t / 营收_t-1
    DEPI = (折旧/固定资产)_t-1 / (折旧/固定资产)_t  (无折旧数据用 Capex 近似)
    SGAI = (销管费/营收)_t / (销管费/营收)_t-1
    LVGI = (总负债/总资产)_t / (总负债/总资产)_t-1
    TATA = (净利润 - 经营现金流) / 总资产
    """
    df = df.copy()
    df = df.sort_values(["ticker", "report_date"])

    def _calc(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()

        # 所需字段检查
        required = ["accounts_receivable", "revenue", "gross_margin", "cash_equiv",
                    "inventory", "total_assets", "total_liabilities", "net_profit_parent",
                    "ocf", "capex"]
        if not all(c in g.columns for c in required):
            g["beneish_m_score"] = np.nan
            return g

        # DSRI
        ar_ratio = g["accounts_receivable"] / g["revenue"].replace(0, np.nan)
        g["DSRI"] = ar_ratio / ar_ratio.shift(1).replace(0, np.nan)

        # GMI
        g["GMI"] = g["gross_margin"].shift(1) / g["gross_margin"].replace(0, np.nan)

        # AQI
        core_assets = g["cash_equiv"].fillna(0) + g["accounts_receivable"].fillna(0) + g["inventory"].fillna(0)
        non_core_ratio = 1 - core_assets / g["total_assets"].replace(0, np.nan)
        g["AQI"] = non_core_ratio / non_core_ratio.shift(1).replace(0, np.nan)

        # SGI
        g["SGI"] = g["revenue"] / g["revenue"].shift(1).replace(0, np.nan)

        # DEPI (用 Capex/总资产 近似折旧率)
        dep_rate = g["capex"].abs() / g["total_assets"].replace(0, np.nan)
        g["DEPI"] = dep_rate.shift(1) / dep_rate.replace(0, np.nan)

        # SGAI (需销管费，无则跳过)
        if "selling_expense" in g and "admin_expense" in g:
            sga_ratio = (g["selling_expense"] + g["admin_expense"]) / g["revenue"].replace(0, np.nan)
            g["SGAI"] = sga_ratio / sga_ratio.shift(1).replace(0, np.nan)
        else:
            g["SGAI"] = 1.0

        # LVGI
        lev_ratio = g["total_liabilities"] / g["total_assets"].replace(0, np.nan)
        g["LVGI"] = lev_ratio / lev_ratio.shift(1).replace(0, np.nan)

        # TATA
        g["TATA"] = (g["net_profit_parent"] - g["ocf"]) / g["total_assets"].replace(0, np.nan)

        # M-Score
        g["beneish_m_score"] = (
            -4.84
            + 0.92 * g["DSRI"].fillna(1)
            + 0.528 * g["GMI"].fillna(1)
            + 0.404 * g["AQI"].fillna(1)
            + 0.892 * g["SGI"].fillna(1)
            + 0.115 * g["DEPI"].fillna(1)
            - 0.172 * g["SGAI"].fillna(1)
            + 4.679 * g["TATA"].fillna(0)
            - 0.327 * g["LVGI"].fillna(1)
        )

        # 标记
        g["beneish_flag"] = g["beneish_m_score"] > -2.22
        return g

    result = df.groupby("ticker").apply(_calc)
    # 丢弃 groupby.apply 产生的残留索引列（level_*）
    result = result.drop(columns=[c for c in result.columns if str(c).startswith("level_")], errors="ignore")
    if "ticker" not in result.columns:
        result = result.reset_index()
        result = result.drop(columns=[c for c in result.columns if str(c).startswith("level_")], errors="ignore")
    return result


def moat_scorecard(df: pd.DataFrame, config: Dict = None) -> pd.DataFrame:
    """
    自定义护城河评分卡 (0-100)
    五大维度：定价权、转换成本、网络效应、无形资产、成本优势、有效规模
    每维度 0-20 分，可配置权重
    """
    if config is None:
        config = {
            "weights": {
                "pricing_power": 0.25,
                "switching_cost": 0.20,
                "network_effect": 0.15,
                "intangible_assets": 0.15,
                "cost_advantage": 0.15,
                "efficient_scale": 0.10,
            },
            # 行业基准（用于横向对比）
            "industry_benchmarks": {}
        }

    df = df.copy()
    df = df.sort_values(["ticker", "report_date"])

    def _score(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()
        scores = pd.DataFrame(index=g.index)

        # 1. 定价权
        # 指标：毛利率水平(行业对标)、毛利率稳定性、收入增长>成本增长
        pricing = 0
        if "gross_margin" in g:
            # 毛利率 5 年中位数
            gm_median = g["gross_margin"].rolling(5, min_periods=1).median()
            # 毛利率波动率 (越低越好)
            gm_std = g["gross_margin"].rolling(5, min_periods=1).std()
            # 收入增速 vs 成本增速
            rev_growth = g["revenue"].pct_change() if "revenue" in g else 0
            cost_growth = g["operating_cost"].pct_change() if "operating_cost" in g else 0
            pricing_power = (rev_growth > cost_growth).astype(int)
            # 简单评分：毛利率中位数>0.4 + 波动率<0.05 + 定价权通过
            pricing = (
                (gm_median > 0.4).astype(int) * 8 +
                (gm_std < 0.05).astype(int) * 7 +
                pricing_power * 5
            ).clip(upper=20)
        scores["pricing_power"] = pricing

        # 2. 转换成本
        # 代理：客户留存率(无数据)、合同负债/营收、研发投入/营收
        switching = pd.Series(0, index=g.index)
        if "revenue" in g:
            # 合同负债(预收账款)占比 - 无直接字段
            # 研发强度
            if "rnd_expense" in g:
                rnd_intensity = g["rnd_expense"] / g["revenue"].replace(0, np.nan)
                switching += (rnd_intensity > 0.05).astype(int) * 10
            # 广告投入占比(品牌粘性)
            if "ad_expense" in g:
                ad_ratio = g["ad_expense"] / g["revenue"].replace(0, np.nan)
                switching += (ad_ratio > 0.03).astype(int) * 5
            # 简化给基础分
            switching += 5
        scores["switching_cost"] = switching.clip(upper=20)

        # 3. 网络效应
        # 代理：用户数增长驱动收入指数级、边际成本递减
        network = pd.Series(0, index=g.index)
        if "revenue" in g and "operating_cost" in g:
            # 边际成本递减：营收增速 > 成本增速 且持续
            rev_growth = g["revenue"].pct_change()
            cost_growth = g["operating_cost"].pct_change()
            scale_advantage = (rev_growth > cost_growth).rolling(3, min_periods=1).mean()
            network = (scale_advantage > 0.6).astype(int) * 15 + 5
        scores["network_effect"] = network.clip(upper=20)

        # 4. 无形资产
        # 商誉/总资产、品牌价值(广告累积)、专利/牌照
        intangible = pd.Series(0, index=g.index)
        if "goodwill" in g and "total_assets" in g:
            gw_ratio = g["goodwill"] / g["total_assets"].replace(0, np.nan)
            intangible += (gw_ratio > 0.05).astype(int) * 8  # 高商誉可能源于收购护城河
        if "ad_expense" in g and "revenue" in g:
            ad_cum = (g["ad_expense"] / g["revenue"].replace(0, np.nan)).rolling(5).sum()
            intangible += (ad_cum > 0.15).astype(int) * 7
        intangible += 5
        scores["intangible_assets"] = intangible.clip(upper=20)

        # 5. 成本优势
        # 单位成本行业最低、规模效应、ROIC > WACC
        cost_adv = pd.Series(0, index=g.index)
        if "roic" in g:
            cost_adv += (g["roic"] > 0.15).astype(int) * 10
        if "operating_margin" in g:
            cost_adv += (g["operating_margin"] > 0.2).astype(int) * 5
        cost_adv += 5
        scores["cost_advantage"] = cost_adv.clip(upper=20)

        # 6. 有效规模
        # 市场占有率(无数据)、行业 CR3/CR5
        efficient = pd.Series(10, index=g.index)  # 基础分
        scores["efficient_scale"] = efficient

        # 加权总分
        weights = config["weights"]
        g["moat_score"] = sum(scores[k] * weights.get(k, 0) for k in scores.columns)
        g["moat_score"] = g["moat_score"].round(1)

        # 细分项存入
        for k in scores.columns:
            g[f"moat_{k}"] = scores[k].round(1)

        return g

    result = df.groupby("ticker").apply(_score)
    # 丢弃 groupby.apply 产生的残留索引列（level_*）
    result = result.drop(columns=[c for c in result.columns if str(c).startswith("level_")], errors="ignore")
    if "ticker" not in result.columns:
        result = result.reset_index()
        result = result.drop(columns=[c for c in result.columns if str(c).startswith("level_")], errors="ignore")
    return result


def quality_summary(df: pd.DataFrame) -> pd.DataFrame:
    """汇总质量指标，生成单行摘要（最新期）"""
    latest = df.sort_values("report_date").groupby("ticker").last().reset_index()
    cols = ["ticker", "piotroski_f_score", "beneish_m_score", "beneish_flag", "moat_score"]
    avail = [c for c in cols if c in latest.columns]
    return latest[avail]


if __name__ == "__main__":
    import duckdb
    conn = duckdb.connect("data/financial.duckdb")
    df = conn.execute("SELECT * FROM financials").df()
    if not df.empty:
        from analysis.ratios import compute_all_ratios
        df = compute_all_ratios(df)
        df = piotroski_f_score(df)
        df = beneish_m_score(df)
        df = moat_scorecard(df)
        summary = quality_summary(df)
        print(summary.to_string())
    conn.close()