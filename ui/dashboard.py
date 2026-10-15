"""
Streamlit 看板：价值投资健康度一页览
运行：streamlit run ui/dashboard.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import duckdb
import yaml
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path
from datetime import datetime
from typing import Dict, List

# 页面配置
st.set_page_config(
    page_title="价值投资健康度看板",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ===== 样式 =====
st.markdown("""
<style>
    .metric-card {background: #f8f9fa; padding: 1rem; border-radius: 0.5rem; border-left: 4px solid #2c7be5;}
    .alert-critical {background: #fff5f5; border-left-color: #e53e3e;}
    .alert-warning {background: #fffaf0; border-left-color: #dd6b20;}
    .alert-info {background: #f0f7ff; border-left-color: #3182ce;}
    .good {color: #276749; font-weight: 600;}
    .bad {color: #c53030; font-weight: 600;}
    .neutral {color: #2c7be5; font-weight: 600;}
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=300)
def load_financials() -> pd.DataFrame:
    """加载财报数据"""
    conn = duckdb.connect("data/financial.duckdb", read_only=True)
    df = conn.execute("SELECT * FROM financials").df()
    conn.close()
    return df


@st.cache_data(ttl=300)
def load_tickers() -> pd.DataFrame:
    """加载自选池配置"""
    with open("config/tickers.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return pd.DataFrame(cfg["tickers"])


@st.cache_data(ttl=300)
def load_assumptions() -> Dict:
    with open("config/assumptions.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """计算全套指标（缓存）"""
    from analysis.ratios import compute_all_ratios
    from analysis.quality import piotroski_f_score, beneish_m_score, moat_scorecard

    df = compute_all_ratios(df)
    df = piotroski_f_score(df)
    df = beneish_m_score(df)
    df = moat_scorecard(df)
    return df


def get_latest(df: pd.DataFrame) -> pd.DataFrame:
    """取每只股票最新期"""
    return df.sort_values("report_date").groupby("ticker").last().reset_index()


def format_pct(x):
    if pd.isna(x): return "—"
    return f"{x:.1%}"


def format_num(x, unit="亿"):
    if pd.isna(x): return "—"
    if unit == "亿":
        return f"{x/1e8:.1f}亿"
    return f"{x:,.0f}"


# ===== 主界面 =====
def main():
    st.title("📊 价值投资健康度看板")
    st.caption(f"数据更新: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 数据源: AKShare + 手工补录")

    # 加载数据
    with st.spinner("加载数据中..."):
        financials = load_financials()
        tickers_df = load_tickers()
        assumptions = load_assumptions()

    if financials.empty:
        st.error("❌ 财报数据为空，请先运行 `python -m data.provider` 拉取数据")
        st.stop()

    # 计算指标
    with st.spinner("计算指标..."):
        financials = compute_all(financials)
        latest = get_latest(financials)

    # 合并自选池信息
    latest = latest.merge(tickers_df[["ticker", "name", "sector", "priority"]], on="ticker", how="left")

    # ===== 侧边栏筛选 =====
    with st.sidebar:
        st.header("🔍 筛选")
        sectors = ["全部"] + sorted(latest["sector"].dropna().unique().tolist())
        selected_sector = st.selectbox("行业", sectors)
        priorities = ["全部"] + ["high", "medium", "low"]
        selected_priority = st.selectbox("优先级", priorities)

        filtered = latest.copy()
        if selected_sector != "全部":
            filtered = filtered[filtered["sector"] == selected_sector]
        if selected_priority != "全部":
            filtered = filtered[filtered["priority"] == selected_priority]

        st.divider()
        st.header("⚙️ 设置")
        show_alerts_only = st.checkbox("仅显示有预警的标的", False)
        auto_refresh = st.checkbox("自动刷新(5分钟)", True)

    # ===== 核心指标总览 =====
    def _col_or_none(frame: pd.DataFrame, col: str):
        return frame[col] if col in frame.columns else pd.Series([np.nan] * len(frame), index=frame.index)

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("覆盖标的", f"{len(filtered)} / {len(latest)}")
    with col2:
        critical_alerts = int((_col_or_none(filtered, "beneish_flag") == True).sum())
        st.metric("🔴 严重预警", critical_alerts, delta_color="inverse")
    with col3:
        piot = _col_or_none(filtered, "piotroski_f_score")
        moat = _col_or_none(filtered, "moat_score")
        warning_alerts = int(((piot < 5) | (moat < 60)).sum())
        st.metric("🟡 关注预警", warning_alerts, delta_color="inverse")
    with col4:
        moat_vals = _col_or_none(filtered, "moat_score")
        avg_moat = moat_vals.mean() if not moat_vals.empty else 0
        avg_moat = 0 if pd.isna(avg_moat) else avg_moat
        st.metric("平均护城河分", f"{avg_moat:.0f}")
    with col5:
        fcf_vals = _col_or_none(filtered, "fcf_conversion")
        avg_fcf_conv = fcf_vals.mean() if not fcf_vals.empty else 0
        st.metric("平均FCF转换率", format_pct(avg_fcf_conv))

    st.divider()

    # ===== 主表格 =====
    st.subheader("📋 自选池健康度总表")

    # 选择显示列
    display_cols = [
        "ticker", "name", "sector", "priority",
        "report_date",
        "revenue", "net_profit_parent", "fcf",
        "roe", "roic", "gross_margin", "operating_margin",
        "fcf_conversion", "net_debt_to_ebitda",
        "cash_cycle_days", "ar_days", "inventory_days",
        "piotroski_f_score", "beneish_m_score", "beneish_flag",
        "moat_score", "moat_pricing_power", "moat_cost_advantage",
    ]
    avail_cols = [c for c in display_cols if c in filtered.columns]
    show_df = filtered[avail_cols].copy()

    # 条件高亮（基于原始数值，在格式化之前判断）
    def highlight_row(row):
        styles = []
        for col in show_df.columns:
            val = row[col]
            if col == "beneish_flag" and val == True:
                styles.append("background-color: #fff5f5")
            elif col == "piotroski_f_score" and pd.notna(val) and val < 5:
                styles.append("background-color: #fff5f5")
            elif col == "moat_score" and pd.notna(val) and val < 60:
                styles.append("background-color: #fffaf0")
            elif col == "fcf_conversion" and pd.notna(val) and val < 0.5:
                styles.append("background-color: #fffaf0")
            else:
                styles.append("")
        return styles

    # 格式化显示（通过 Styler.format 在渲染层处理，原始数值保留用于高亮比较）
    styler = show_df.style.apply(highlight_row, axis=1)
    fmt_map = {}
    for col in ["roe", "roic", "gross_margin", "operating_margin", "fcf_conversion"]:
        if col in show_df.columns:
            fmt_map[col] = format_pct
    for col in ["revenue", "net_profit_parent", "fcf"]:
        if col in show_df.columns:
            fmt_map[col] = lambda x: format_num(x, "亿")
    if "net_debt_to_ebitda" in show_df.columns:
        fmt_map["net_debt_to_ebitda"] = lambda x: f"{x:.1f}x" if pd.notna(x) else "—"
    if fmt_map:
        styler = styler.format(fmt_map)

    st.dataframe(
        styler,
        use_container_width=True,
        height=600,
        column_config={
            "ticker": st.column_config.TextColumn("代码", width=100),
            "name": st.column_config.TextColumn("名称", width=120),
            "sector": st.column_config.TextColumn("行业", width=120),
            "priority": st.column_config.TextColumn("优先级", width=80),
            "report_date": st.column_config.DateColumn("报告期", width=100),
        }
    )

    # ===== 详情面板 =====
    st.divider()
    st.subheader("🔍 单标的深度诊断")

    selected_ticker = st.selectbox(
        "选择标的",
        options=filtered["ticker"].tolist(),
        format_func=lambda x: (
            f"{x} - {filtered[filtered['ticker']==x]['name'].iloc[0]}"
            if len(filtered[filtered['ticker'] == x]) > 0 and pd.notna(filtered[filtered['ticker'] == x]['name'].iloc[0])
            else str(x)
        )
    )

    if selected_ticker:
        detail = filtered[filtered["ticker"] == selected_ticker].iloc[0]
        hist = financials[financials["ticker"] == selected_ticker].sort_values("report_date")

        # 关键指标卡片
        cols = st.columns(4)
        metrics = [
            ("ROE", detail.get("roe"), format_pct, "good" if (pd.notna(detail.get("roe")) and detail.get("roe") > 0.15) else "neutral"),
            ("ROIC", detail.get("roic"), format_pct, "good" if (pd.notna(detail.get("roic")) and detail.get("roic") > 0.15) else "bad" if (pd.notna(detail.get("roic")) and detail.get("roic") < 0.08) else "neutral"),
            ("FCF转换率", detail.get("fcf_conversion"), format_pct, "good" if (pd.notna(detail.get("fcf_conversion")) and detail.get("fcf_conversion") > 1) else "bad" if (pd.notna(detail.get("fcf_conversion")) and detail.get("fcf_conversion") < 0.5) else "neutral"),
            ("净负债/EBITDA", detail.get("net_debt_to_ebitda"),
             (lambda x: f"{x:.1f}x" if pd.notna(x) else "—"),
             "bad" if (pd.notna(detail.get("net_debt_to_ebitda")) and detail.get("net_debt_to_ebitda") > 3) else "good"),
        ]
        for i, (label, val, fmt, color) in enumerate(metrics):
            with cols[i]:
                st.markdown(f"""
                <div class="metric-card">
                    <div style="font-size:0.85rem;color:#718096">{label}</div>
                    <div class="{color}" style="font-size:1.5rem">{fmt(val)}</div>
                </div>
                """, unsafe_allow_html=True)

        # 趋势图
        tab1, tab2, tab3, tab4 = st.tabs(["📈 盈利/现金流", "📊 营运/偿债", "🛡️ 质量评分", "💰 估值参考"])

        with tab1:
            fig = go.Figure()
            if "revenue" in hist:
                fig.add_trace(go.Bar(x=hist["report_date"], y=hist["revenue"]/1e8, name="营收(亿)", yaxis="y", opacity=0.6))
            if "net_profit_parent" in hist:
                fig.add_trace(go.Scatter(x=hist["report_date"], y=hist["net_profit_parent"]/1e8, name="归母净利(亿)", yaxis="y2", mode="lines+markers"))
            if "fcf" in hist:
                fig.add_trace(go.Scatter(x=hist["report_date"], y=hist["fcf"]/1e8, name="FCF(亿)", yaxis="y2", mode="lines+markers", line=dict(dash="dot")))
            fig.update_layout(
                title="营收/利润/现金流趋势",
                yaxis=dict(title="营收(亿)", side="left"),
                yaxis2=dict(title="利润/FCF(亿)", side="right", overlaying="y"),
                hovermode="x unified",
                height=400
            )
            st.plotly_chart(fig, use_container_width=True)

        with tab2:
            col_a, col_b = st.columns(2)
            with col_a:
                if "cash_cycle_days" in hist:
                    fig = px.line(hist, x="report_date", y="cash_cycle_days", title="现金周期(天)", markers=True)
                    st.plotly_chart(fig, use_container_width=True)
            with col_b:
                if "ar_days" in hist and "inventory_days" in hist and "ap_days" in hist:
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=hist["report_date"], y=hist["ar_days"], name="应收天数", mode="lines+markers"))
                    fig.add_trace(go.Scatter(x=hist["report_date"], y=hist["inventory_days"], name="存货天数", mode="lines+markers"))
                    fig.add_trace(go.Scatter(x=hist["report_date"], y=hist["ap_days"], name="应付天数", mode="lines+markers"))
                    fig.update_layout(title="营运周转天数", height=350)
                    st.plotly_chart(fig, use_container_width=True)

        with tab3:
            col_a, col_b = st.columns(2)
            with col_a:
                piot_val = detail.get("piotroski_f_score")
                st.metric("Piotroski F-Score", (f"{int(piot_val)}/9" if pd.notna(piot_val) else "—") )
                bene_val = detail.get("beneish_m_score")
                bene_str = f"{float(bene_val):.2f}" if pd.notna(bene_val) else "—"
                st.metric("Beneish M-Score", bene_str + (" ⚠️" if detail.get("beneish_flag") == True else " ✅"))
            with col_b:
                moat_val = detail.get("moat_score")
                st.metric("护城河总分", (f"{float(moat_val):.0f}/100" if pd.notna(moat_val) else "—"))
                for comp in ["pricing_power", "switching_cost", "network_effect", "intangible_assets", "cost_advantage", "efficient_scale"]:
                    key = f"moat_{comp}"
                    if key in detail.index:
                        val = detail[key]
                        if pd.notna(val):
                            st.caption(f"{comp}: {float(val):.0f}/20")

        with tab4:
            st.info("💡 估值模块需接入实时行情数据（当前价、总股本），可在 `portfolio/decision_log.py` 中结合 `analysis/valuation.py` 计算")
            st.write("**所需输入**：当前股价、总股本")
            st.write("**输出**：反向DCF隐含增长、情景树估值、安全边际、建议仓位")

    # ===== 预警面板 =====
    st.divider()
    st.subheader("🚨 当前预警列表")

    # 只评估规则用于展示（不触发推送，避免每次刷新都发送通知）
    from monitoring.alerts import evaluate_rules, load_rules
    rules = load_rules()
    alerts = evaluate_rules(financials, rules)

    if alerts:
        alert_df = pd.DataFrame([{
            "代码": a.ticker,
            "名称": a.name,
            "严重度": a.severity,
            "规则": a.rule_id,
            "详情": a.message,
            "当前值": a.current_value,
            "阈值": a.threshold,
            "数据日期": a.report_date,
        } for a in alerts])
        st.dataframe(alert_df, use_container_width=True)
    else:
        st.success("✅ 暂无预警触发")

    # ===== 决策日志 =====
    st.divider()
    st.subheader("📝 决策日志")

    from portfolio.decision_log import get_portfolio_summary, generate_review_report
    portfolio = get_portfolio_summary()
    if not portfolio.empty:
        st.dataframe(portfolio, use_container_width=True)
    else:
        st.info("暂无活跃持仓记录，请在 `portfolio/decision_log.py` 中添加")

    with st.expander("📄 生成完整复盘报告"):
        if st.button("生成 Markdown 报告"):
            report = generate_review_report()
            st.download_button(
                "下载报告",
                report,
                file_name=f"review_{datetime.now().strftime('%Y%m%d')}.md",
                mime="text/markdown"
            )
            st.text_area("预览", report, height=400)


if __name__ == "__main__":
    main()