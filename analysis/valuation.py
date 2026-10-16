"""
估值模块：反向 DCF 蒙特卡洛 + 情景树 + 敏感性分析
仅依赖 numpy、pandas、yaml、scipy
"""

import numpy as np
import pandas as pd
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from scipy import stats


@dataclass
class ValuationResult:
    ticker: str
    current_price: float
    fcf_ttm: float
    shares_outstanding: float

    # 反向 DCF 结果
    implied_growth_p10: float
    implied_growth_p50: float
    implied_growth_p90: float
    margin_of_safety_prob: float  # P(隐含增长 > 你的估计)

    # 正向 DCF 情景树
    bear_value: float
    base_value: float
    bull_value: float
    expected_value: float

    # 安全边际
    margin_of_safety_pct: float  # (期望值 - 现价) / 现价

    # 敏感性
    sensitivity: Dict  # (wacc, growth) -> value 矩阵

    # 假设记录
    assumptions: Dict


def load_assumptions(path: str = "config/assumptions.yaml") -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_sector_assumptions(sector: str, base: Dict) -> Dict:
    """合并基础假设与行业覆盖"""
    sector_cfg = base.get("sector_overrides", {}).get(sector, {})
    merged = {**base, **sector_cfg}
    # 深度合并 terminal
    if "terminal" in sector_cfg:
        merged["terminal"] = {**base.get("terminal", {}), **sector_cfg["terminal"]}
    if "maintenance_capex_multiplier" in base.get("valuation", {}):
        mult = base["valuation"]["maintenance_capex_multiplier"]
        merged["maintenance_capex_mult"] = mult.get(sector, mult.get("default", 1.0))
    else:
        merged["maintenance_capex_mult"] = 1.0
    return merged


def estimate_fcf_ttm(df: pd.DataFrame, ticker: str, assumptions: Dict) -> Tuple[float, float]:
    """
    估算 TTM FCF 与维持性 Capex
    返回：(fcf_ttm, maintenance_capex)
    """
    ticker_df = df[df["ticker"] == ticker].sort_values("report_date")
    if ticker_df.empty:
        return 0.0, 0.0

    latest = ticker_df.iloc[-1]
    fcf = latest.get("fcf", 0) or 0
    capex = latest.get("capex", 0) or 0
    revenue = latest.get("revenue", 0) or 1
    depreciation = latest.get("depreciation", 0)  # 可能无

    # 维持性 Capex 估算
    mult = assumptions.get("maintenance_capex_mult", 1.0)
    if depreciation and depreciation > 0:
        maint_capex = depreciation * mult
    else:
        # 无折旧数据：用 Capex * 比例 或 收入 * 行业比例
        maint_capex = abs(capex) * 0.6 * mult

    # 归一化 FCF = OCF - 维持性 Capex
    ocf = latest.get("ocf", 0) or 0
    norm_fcf = ocf - maint_capex

    return norm_fcf, maint_capex


def reverse_dcf_monte_carlo(
    market_cap: float,
    fcf_ttm: float,
    assumptions: Dict,
    n_sim: int = None
) -> Tuple[float, float, float, float]:
    """
    反向 DCF：求解市场隐含增长率分布
    简化模型：Gordon Growth
    Market Cap = FCF * (1+g) / (WACC - g)  =>  g = (Market Cap * WACC / FCF - 1) / (Market Cap / FCF + 1)
    更稳健：数值求解
    """
    if n_sim is None:
        n_sim = assumptions.get("monte_carlo_sims", 10000)

    base = assumptions.get("base", {})
    rf = base.get("risk_free_rate", 0.025)
    erp = base.get("equity_risk_premium", 0.055)
    # 简化：WACC ≈ Cost of Equity (假设无/低杠杆)
    beta_mean = 1.0  # 可按行业调整
    wacc_mean = rf + beta_mean * erp
    wacc_std = 0.015

    terminal = base.get("terminal", {})
    g_term_mean = terminal.get("gordon_growth", 0.03)
    g_term_std = 0.01

    implied_growth = []
    for _ in range(n_sim):
        wacc = max(np.random.normal(wacc_mean, wacc_std), 0.03)
        g_term = np.random.normal(g_term_mean, g_term_std)

        # Gordon 模型反解 g
        # V = FCF * (1+g) / (wacc - g)
        # => V*(wacc-g) = FCF*(1+g)
        # => V*wacc - V*g = FCF + FCF*g
        # => g*(FCF + V) = V*wacc - FCF
        # => g = (V*wacc - FCF) / (V + FCF)
        if market_cap > 0 and fcf_ttm > 0:
            g_implied = (market_cap * wacc - fcf_ttm) / (market_cap + fcf_ttm)
            # 限制合理范围
            g_implied = np.clip(g_implied, -0.1, 0.5)
            implied_growth.append(g_implied)

    if not implied_growth:
        return 0, 0, 0, 0

    p10, p50, p90 = np.percentile(implied_growth, [10, 50, 90])
    # 安全边际概率：市场隐含增长 < 你的估计增长
    your_growth = assumptions.get("your_growth_estimate", 0.08)
    mos_prob = (np.array(implied_growth) < your_growth).mean()

    return p10, p50, p90, mos_prob


def forward_dcf_scenario(
    fcf_ttm: float,
    assumptions: Dict,
    scenarios: Dict = None
) -> Tuple[float, float, float, float]:
    """
    正向 DCF 情景树估值
    """
    if scenarios is None:
        scenarios = {
            "bear": {"prob": 0.25, "growth": 0.03, "wacc_adj": 0.02, "terminal_mult": 8},
            "base": {"prob": 0.50, "growth": 0.08, "wacc_adj": 0.00, "terminal_mult": 12},
            "bull": {"prob": 0.25, "growth": 0.15, "wacc_adj": -0.01, "terminal_mult": 18},
        }

    base = assumptions.get("base", {})
    rf = base.get("risk_free_rate", 0.025)
    erp = base.get("equity_risk_premium", 0.055)
    beta = 1.0
    base_wacc = rf + beta * erp

    fade_years = assumptions.get("fade_years", 10)

    values = {}
    for name, sc in scenarios.items():
        wacc = base_wacc + sc["wacc_adj"]
        g = sc["growth"]
        terminal_mult = sc["terminal_mult"]

        # 显性预测期 FCF
        pv = 0
        fcf = fcf_ttm
        for year in range(1, fade_years + 1):
            fcf *= (1 + g)
            pv += fcf / (1 + wacc) ** year

        # 终值：用 FCF 倍数法
        terminal_value = fcf * terminal_mult
        pv += terminal_value / (1 + wacc) ** fade_years

        values[name] = pv * sc["prob"]

    expected = sum(values.values())
    return values.get("bear", 0), values.get("base", 0), values.get("bull", 0), expected


def sensitivity_matrix(
    fcf_ttm: float,
    wacc_range: np.ndarray = None,
    growth_range: np.ndarray = None,
    fade_years: int = 10,
    terminal_mult: float = 12
) -> pd.DataFrame:
    """生成敏感性热力图矩阵 (WACC x Growth -> 估值)"""
    if wacc_range is None:
        wacc_range = np.arange(0.06, 0.14, 0.01)
    if growth_range is None:
        growth_range = np.arange(0.0, 0.15, 0.01)

    matrix = pd.DataFrame(index=wacc_range, columns=growth_range, dtype=float)

    for w in wacc_range:
        for g in growth_range:
            if w <= g:
                matrix.loc[w, g] = np.nan
                continue
            pv = 0
            fcf = fcf_ttm
            for year in range(1, fade_years + 1):
                fcf *= (1 + g)
                pv += fcf / (1 + w) ** year
            tv = fcf * terminal_mult
            pv += tv / (1 + w) ** fade_years
            matrix.loc[w, g] = pv

    return matrix


def value_investor_position_size(
    valuation_result: ValuationResult,
    max_position: float = 0.15,
    conviction: float = 0.7
) -> float:
    """
    改良 Kelly 仓位建议
    基于估值分布的下行保护
    """
    upside = (valuation_result.expected_value - valuation_result.current_price) / valuation_result.current_price
    # 下行：熊市情景价值
    downside = (valuation_result.current_price - valuation_result.bear_value) / valuation_result.current_price
    if downside <= 0:
        downside = 0.3  # 默认允许 30% 回撤

    win_prob = valuation_result.margin_of_safety_prob  # P(内在价值 > 现价)
    kelly = (win_prob * upside - (1 - win_prob) * downside) / max(upside, 0.01)
    kelly = max(0, kelly) * conviction * 0.5  # 半凯利 + 信心调整
    return min(kelly, max_position)


def run_valuation_for_ticker(
    ticker: str,
    current_price: float,
    shares_outstanding: float,
    df: pd.DataFrame,
    assumptions: Dict,
    your_growth_estimate: float = 0.08,
    sector: str = "default"
) -> ValuationResult:
    """单只股票完整估值流程"""
    sector_assumptions = get_sector_assumptions(sector, assumptions)
    sector_assumptions["your_growth_estimate"] = your_growth_estimate

    fcf_ttm, maint_capex = estimate_fcf_ttm(df, ticker, sector_assumptions)
    # 财报单位为亿元，市值需统一单位
    market_cap = current_price * shares_outstanding / 100000000  # 转为亿元

    # 反向 DCF
    p10, p50, p90, mos_prob = reverse_dcf_monte_carlo(
        market_cap, fcf_ttm, sector_assumptions
    )

    # 正向 DCF 情景树
    bear_v, base_v, bull_v, expected_v = forward_dcf_scenario(
        fcf_ttm, sector_assumptions
    )

    # 敏感性
    sens = sensitivity_matrix(
        fcf_ttm,
        fade_years=sector_assumptions.get("fade_years", 10),
        terminal_mult=sector_assumptions.get("terminal", {}).get("exit_multiple_fcf", 15)
    )

    margin_pct = (expected_v - market_cap) / market_cap if market_cap > 0 else 0

    return ValuationResult(
        ticker=ticker,
        current_price=current_price,
        fcf_ttm=fcf_ttm,
        shares_outstanding=shares_outstanding,
        implied_growth_p10=p10,
        implied_growth_p50=p50,
        implied_growth_p90=p90,
        margin_of_safety_prob=mos_prob,
        bear_value=bear_v,
        base_value=base_v,
        bull_value=bull_v,
        expected_value=expected_v,
        margin_of_safety_pct=margin_pct,
        sensitivity=sens.to_dict(),
        assumptions={
            "sector": sector,
            "fcf_ttm": fcf_ttm,
            "maint_capex": maint_capex,
            "wacc_base": sector_assumptions.get("base", {}).get("risk_free_rate", 0.025) + 1.0 * sector_assumptions.get("base", {}).get("equity_risk_premium", 0.055),
            "fade_years": sector_assumptions.get("fade_years", 10),
            "your_growth": your_growth_estimate,
        }
    )


def valuation_to_dataframe(results: List[ValuationResult]) -> pd.DataFrame:
    """转为 DataFrame 便于展示/存储"""
    rows = []
    for r in results:
        rows.append({
            "ticker": r.ticker,
            "price": r.current_price,
            "fcf_ttm": r.fcf_ttm,
            "implied_growth_p10": r.implied_growth_p10,
            "implied_growth_p50": r.implied_growth_p50,
            "implied_growth_p90": r.implied_growth_p90,
            "mos_prob": r.margin_of_safety_prob,
            "bear_value": r.bear_value,
            "base_value": r.base_value,
            "bull_value": r.bull_value,
            "expected_value": r.expected_value,
            "margin_pct": r.margin_of_safety_pct,
            "kelly_position": value_investor_position_size(r),
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    # 测试
    import duckdb
    conn = duckdb.connect("data/financial.duckdb")
    df = conn.execute("SELECT * FROM financials").df()
    conn.close()

    if not df.empty:
        assumptions = load_assumptions()
        # 假设当前价格与股本（实际应从行情接口获取）
        test_cases = [
            ("600519.SH", 1600, 1.256e9),  # 茅台
            ("000858.SZ", 150, 3.86e9),    # 五粮液
        ]
        for ticker, price, shares in test_cases:
            if ticker in df["ticker"].values:
                res = run_valuation_for_ticker(ticker, price, shares, df, assumptions)
                print(f"\n=== {ticker} ===")
                print(f"FCF TTM: {res.fcf_ttm/1e8:.2f} 亿")
                print(f"隐含增长 P50: {res.implied_growth_p50:.1%}")
                print(f"安全边际概率: {res.margin_of_safety_prob:.1%}")
                print(f"熊/基/牛 估值: {res.bear_value/1e8:.1f} / {res.base_value/1e8:.1f} / {res.bull_value/1e8:.1f} 亿")
                print(f"期望值: {res.expected_value/1e8:.1f} 亿, 市值: {res.current_price*res.shares_outstanding/1e8:.1f} 亿")
                print(f"安全边际: {res.margin_of_safety_pct:.1%}")
                print(f"建议仓位: {value_investor_position_size(res):.1%}")