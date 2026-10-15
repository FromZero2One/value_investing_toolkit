# -*- coding: utf-8 -*-
"""
端到端回归测试：模拟 dashboard main() 的完整数据流 + 所有 CLI 命令核心逻辑
不含 Streamlit UI 层，专门抓运行时错误（NameError/TypeError/KeyError 等）
"""
import sys
import traceback

# GBK 控制台兼容
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASSED = []
FAILED = []

def check(name, func):
    try:
        func()
        PASSED.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAILED.append((name, e))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


print("=" * 70)
print("1. 数据加载与指标计算链（dashboard compute_all 路径）")
print("=" * 70)

_financials = {}

def t_load():
    import duckdb
    conn = duckdb.connect("data/financial.duckdb", read_only=True)
    df = conn.execute("SELECT * FROM financials").df()
    conn.close()
    assert not df.empty, "财报数据为空"
    _financials["raw"] = df

def t_compute_all():
    from analysis.ratios import compute_all_ratios
    from analysis.quality import piotroski_f_score, beneish_m_score, moat_scorecard
    df = _financials["raw"]
    df = compute_all_ratios(df)
    assert "ticker" in df.columns, "compute_all_ratios 丢失 ticker 列"
    assert "level_1" not in df.columns, "存在残留 level_1 列"
    df = piotroski_f_score(df)
    assert "ticker" in df.columns and "piotroski_f_score" in df.columns
    assert "level_1" not in df.columns, "piotroski 后存在残留 level_1"
    df = beneish_m_score(df)
    assert "ticker" in df.columns and "beneish_m_score" in df.columns
    df = moat_scorecard(df)
    assert "ticker" in df.columns and "moat_score" in df.columns
    _financials["computed"] = df

check("load_financials", t_load)
check("compute_all_chain", t_compute_all)

print()
print("=" * 70)
print("2. dashboard 数据准备路径（get_latest / merge / styler）")
print("=" * 70)

_latest = {}

def t_get_latest_merge():
    import pandas as pd
    import yaml
    df = _financials["computed"]
    latest = df.sort_values("report_date").groupby("ticker").last().reset_index()
    with open("config/tickers.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    tickers_df = pd.DataFrame(cfg["tickers"])
    latest = latest.merge(tickers_df[["ticker", "name", "sector", "priority"]], on="ticker", how="left")
    _latest["df"] = latest

def t_filter_sector():
    latest = _latest["df"]
    sectors = latest["sector"].dropna().unique().tolist()
    if sectors:
        filtered = latest[latest["sector"] == sectors[0]]
        assert "ticker" in filtered.columns
    # 空筛选（选一个不存在的行业）
    filtered_empty = latest[latest["sector"] == "不存在的行业"]
    assert len(filtered_empty) == 0

def t_styler_pipeline():
    """复刻 dashboard 的 highlight + format 流程"""
    import pandas as pd
    latest = _latest["df"]
    display_cols = [
        "ticker", "name", "sector", "priority", "report_date",
        "revenue", "net_profit_parent", "fcf",
        "roe", "roic", "gross_margin", "operating_margin",
        "fcf_conversion", "net_debt_to_ebitda",
        "cash_cycle_days", "ar_days", "inventory_days",
        "piotroski_f_score", "beneish_m_score", "beneish_flag",
        "moat_score", "moat_pricing_power", "moat_cost_advantage",
    ]
    avail_cols = [c for c in display_cols if c in latest.columns]
    show_df = latest[avail_cols].copy()

    def format_pct(x):
        if pd.isna(x): return "—"
        return f"{x:.1%}"

    def format_num(x, unit="亿"):
        if pd.isna(x): return "—"
        if unit == "亿": return f"{x/1e8:.1f}亿"
        return f"{x:,.0f}"

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
    # 触发实际渲染（会执行所有 formatter 和 highlight）
    html = styler.to_html()
    assert "66.7%" in html or "66.67%" in html, "格式化未生效"

check("get_latest_and_merge", t_get_latest_merge)
check("sector_filter", t_filter_sector)
check("styler_pipeline_render", t_styler_pipeline)

print()
print("=" * 70)
print("3. dashboard 详情面板路径（单标的诊断）")
print("=" * 70)

def t_detail_metrics():
    import pandas as pd
    financials = _financials["computed"]
    latest = _latest["df"]
    ticker = latest["ticker"].iloc[0]
    detail = latest[latest["ticker"] == ticker].iloc[0]
    hist = financials[financials["ticker"] == ticker].sort_values("report_date")
    assert len(hist) > 0
    # 指标卡片的取值与格式化（含 NaN 场景）
    for key in ["roe", "roic", "fcf_conversion", "net_debt_to_ebitda"]:
        val = detail.get(key)
        if pd.notna(val):
            _ = f"{float(val):.2f}"
    # 趋势图数据
    if "revenue" in hist:
        _ = hist["revenue"] / 1e8
    if "net_profit_parent" in hist:
        _ = hist["net_profit_parent"] / 1e8
    if "fcf" in hist:
        _ = hist["fcf"] / 1e8
    # tab3 评分展示
    piot = detail.get("piotroski_f_score")
    if pd.notna(piot):
        _ = f"{int(piot)}/9"
    bene = detail.get("beneish_m_score")
    if pd.notna(bene):
        _ = f"{float(bene):.2f}"
    moat = detail.get("moat_score")
    if pd.notna(moat):
        _ = f"{float(moat):.0f}/100"
    for comp in ["pricing_power", "switching_cost", "network_effect",
                 "intangible_assets", "cost_advantage", "efficient_scale"]:
        key = f"moat_{comp}"
        if key in detail.index:
            val = detail[key]
            if pd.notna(val):
                _ = f"{float(val):.0f}/20"

def t_name_format_func():
    import pandas as pd
    latest = _latest["df"]
    for x in latest["ticker"].tolist():
        sub = latest[latest["ticker"] == x]
        if len(sub) > 0 and pd.notna(sub["name"].iloc[0]):
            _ = f"{x} - {sub['name'].iloc[0]}"
        else:
            _ = str(x)

check("detail_panel_metrics", t_detail_metrics)
check("name_format_func", t_name_format_func)

print()
print("=" * 70)
print("4. 预警引擎（含 bool 安全、window 连续N期、cross 穿越）")
print("=" * 70)

def t_evaluate_rules():
    from monitoring.alerts import evaluate_rules, load_rules
    rules = load_rules()
    financials = _financials["computed"]
    alerts = evaluate_rules(financials, rules)
    # 消息必须是字符串且可格式化
    for a in alerts:
        assert isinstance(a.message, str)
        assert ".4f" not in a.message, f"bool 格式化失败残留: {a.message}"
    _financials["alerts"] = alerts
    print(f"    -> 触发 {len(alerts)} 条预警")

def t_alert_format_message():
    from monitoring.alerts import format_alert_message
    alerts = _financials.get("alerts", [])
    msg = format_alert_message(alerts)
    assert isinstance(msg, str)

check("evaluate_rules", t_evaluate_rules)
check("format_alert_message", t_alert_format_message)

print()
print("=" * 70)
print("5. 决策日志（closed-conn 修复、空 JSON、报告生成）")
print("=" * 70)

def t_decision_add():
    from portfolio.decision_log import DecisionRecord, add_decision, close_decision
    rec = DecisionRecord(
        date="2025-01-15", ticker="600519.SH", name="贵州茅台",
        action="BUY", position_pct=0.10, price=1600,
        thesis="测试论点",
        key_assumptions={"growth": "8%", "wacc": "9%"},
        risk_factors=["风险1"],
        stop_review_trigger="触发条件",
        exit_plan="退出计划",
        valuation_ref={"expected_value": 2.1e11, "margin_pct": 0.35},
        quality_scores={"piotroski": 9, "moat": 88, "beneish_flag": False},
    )
    did = add_decision(rec)
    assert isinstance(did, int)

def t_decision_get_and_summary():
    from portfolio.decision_log import get_decisions, get_portfolio_summary
    records = get_decisions(status="active")
    assert len(records) >= 1, "有记录但查询为空（closed-conn bug）"
    summary = get_portfolio_summary()
    assert not summary.empty

def t_review_report_with_data():
    from portfolio.decision_log import generate_review_report
    report = generate_review_report()
    assert "投资决策复盘报告" in report
    assert "估值参考" in report

def t_review_report_empty_json():
    """关键回归：valuation_ref 为空串时不得崩"""
    from portfolio.decision_log import DecisionRecord, add_decision, generate_review_report
    rec = DecisionRecord(
        date="2025-01-16", ticker="TEST.EMPTY", name="空值测试",
        action="WATCH", position_pct=0, price=0,
        thesis="空字段测试", key_assumptions="", risk_factors="",
        stop_review_trigger="", exit_plan="", valuation_ref="", quality_scores="",
    )
    add_decision(rec)
    report = generate_review_report()  # 不抛异常即通过
    assert "投资决策复盘报告" in report

check("decision_add", t_decision_add)
check("decision_get_and_summary", t_decision_get_and_summary)
check("review_report_with_data", t_review_report_with_data)
check("review_report_empty_json_fields", t_review_report_empty_json)

print()
print("=" * 70)
print("6. provider（upsert 参数数量、字段映射）")
print("=" * 70)

def t_upsert_param_count():
    """验证 upsert SQL 占位符与参数数量一致"""
    import re as _re
    from data.provider import AKShareProvider, FinancialStatement
    p = AKShareProvider()
    try:
        fs = FinancialStatement(
            ticker="UPsert.TEST", report_date="2023-12-31", period_type="annual",
            revenue=100.0, net_profit_parent=10.0, ocf=12.0,
        )
        fs.fcf = fs.ocf - 0
        p.upsert(fs)  # 参数不匹配会抛 InvalidInputException
    finally:
        p.close()

def t_field_mapping():
    """验证中文键能正确映射到 dataclass 属性"""
    from data.provider import AKShareProvider
    p = AKShareProvider.__new__(AKShareProvider)
    import pandas as pd
    # 构造一个最小 income 表
    income = pd.DataFrame({
        "指标": ["营业收入", "归母净利润"],
        "2023-12-31": [1000.0, 300.0],
    })
    result = p.parse_statement(income, {"营业收入": ["营业收入"], "归母净利润": ["归母净利润"]})
    assert result.get("营业收入") == 1000.0, f"income 解析失败: {result}"

check("upsert_param_count", t_upsert_param_count)
check("field_mapping", t_field_mapping)

print()
print("=" * 70)
print(f"结果: {len(PASSED)} 通过, {len(FAILED)} 失败")
print("=" * 70)
if FAILED:
    for name, e in FAILED:
        print(f"  ✗ {name}: {e}")
    sys.exit(1)
print("全部通过 ✓")
