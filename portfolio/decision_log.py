"""
决策日志与仓位管理
存储：SQLite（本地文件）
"""

import sqlite3
import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime
from loguru import logger
import pandas as pd


DB_PATH = Path("data/portfolio.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class DecisionRecord:
    id: Optional[int] = None
    date: str = ""                    # YYYY-MM-DD
    ticker: str = ""
    name: str = ""
    action: str = ""                  # BUY / SELL / ADD / REDUCE / HOLD / WATCH
    position_pct: float = 0.0         # 目标仓位占比
    price: float = 0.0                # 决策参考价
    thesis: str = ""                  # 投资论点（核心逻辑）
    key_assumptions: str = ""         # 关键假设（JSON 字符串）
    risk_factors: str = ""            # 主要风险点（JSON 字符串）
    stop_review_trigger: str = ""     # 止损/复核触发条件
    exit_plan: str = ""               # 退出计划
    valuation_ref: str = ""           # 估值参考（JSON：expected_value, margin_pct 等）
    quality_scores: str = ""          # 质量评分快照（JSON）
    status: str = "active"            # active / closed / stopped
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        # JSON 序列化字段：dict / list 都转字符串，SQLite 不支持容器类型绑定
        for k in ["key_assumptions", "risk_factors", "valuation_ref", "quality_scores"]:
            if isinstance(d[k], (dict, list)):
                d[k] = json.dumps(d[k], ensure_ascii=False)
        return d


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            action TEXT NOT NULL,
            position_pct REAL DEFAULT 0,
            price REAL DEFAULT 0,
            thesis TEXT,
            key_assumptions TEXT,
            risk_factors TEXT,
            stop_review_trigger TEXT,
            exit_plan TEXT,
            valuation_ref TEXT,
            quality_scores TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_date ON decisions(date)")
    conn.commit()
    conn.close()


def add_decision(record: DecisionRecord) -> int:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    d = record.to_dict()
    d["created_at"] = datetime.now().isoformat()
    d["updated_at"] = d["created_at"]
    cols = ", ".join(d.keys())
    placeholders = ", ".join(["?"] * len(d))
    sql = f"INSERT INTO decisions ({cols}) VALUES ({placeholders})"
    cur = conn.execute(sql, list(d.values()))
    conn.commit()
    decision_id = cur.lastrowid
    conn.close()
    logger.info(f"决策记录已保存: {record.ticker} {record.action} @{record.price}")
    return decision_id


def update_decision(decision_id: int, updates: Dict[str, Any]):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join([f"{k}=?" for k in updates.keys()])
    sql = f"UPDATE decisions SET {set_clause} WHERE id=?"
    vals = list(updates.values()) + [decision_id]
    conn.execute(sql, vals)
    conn.commit()
    conn.close()


def get_decisions(ticker: str = None, status: str = "active") -> List[DecisionRecord]:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    sql = "SELECT * FROM decisions WHERE 1=1"
    params = []
    if ticker:
        sql += " AND ticker=?"
        params.append(ticker)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY date DESC, created_at DESC"
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    conn.close()

    records = []
    for row in rows:
        d = dict(zip(cols, row))
        # 解析 JSON 字段
        for k in ["key_assumptions", "risk_factors", "valuation_ref", "quality_scores"]:
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except:
                    pass
        records.append(DecisionRecord(**d))
    return records


def close_decision(decision_id: int, exit_reason: str = ""):
    update_decision(decision_id, {"status": "closed", "exit_plan": exit_reason})


def get_portfolio_summary() -> pd.DataFrame:
    """当前持仓汇总"""
    decisions = get_decisions(status="active")
    if not decisions:
        return pd.DataFrame()

    rows = []
    for d in decisions:
        rows.append({
            "ticker": d.ticker,
            "name": d.name,
            "action": d.action,
            "position_pct": d.position_pct,
            "entry_price": d.price,
            "entry_date": d.date,
            "thesis": d.thesis[:100] + "..." if len(d.thesis) > 100 else d.thesis,
        })
    return pd.DataFrame(rows)


def _safe_json(value, default=None):
    """安全解析 JSON：空串/None/解析失败均返回 default（不抛异常）"""
    if default is None:
        default = {}
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, str):
        return default
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else default
    except (json.JSONDecodeError, TypeError):
        return default


def generate_review_report(ticker: str = None) -> str:
    """生成复盘报告（Markdown）"""
    decisions = get_decisions(ticker=ticker, status=None)
    if not decisions:
        return "暂无决策记录"

    lines = [f"# 📋 投资决策复盘报告 ({datetime.now().strftime('%Y-%m-%d')})", ""]

    for d in decisions:
        lines.append(f"## {d.ticker} {d.name} - {d.action} ({d.date})")
        try:
            price_str = f"{d.price:.2f}" if isinstance(d.price, (int, float)) else str(d.price)
            pos_str = f"{d.position_pct:.1%}" if isinstance(d.position_pct, (int, float)) else str(d.position_pct)
            lines.append(f"- **参考价**: {price_str} | **目标仓位**: {pos_str} | **状态**: {d.status}")
        except Exception:
            lines.append(f"- **参考价**: {d.price} | **目标仓位**: {d.position_pct} | **状态**: {d.status}")
        lines.append(f"- **投资论点**: {d.thesis}")
        ka = d.key_assumptions if isinstance(d.key_assumptions, dict) else _safe_json(d.key_assumptions)
        rf = d.risk_factors if isinstance(d.risk_factors, dict) else _safe_json(d.risk_factors)
        lines.append(f"- **关键假设**: {json.dumps(ka, ensure_ascii=False)}")
        lines.append(f"- **风险因素**: {json.dumps(rf, ensure_ascii=False)}")
        lines.append(f"- **止损/复核触发**: {d.stop_review_trigger}")
        lines.append(f"- **退出计划**: {d.exit_plan}")
        if d.valuation_ref:
            vr = _safe_json(d.valuation_ref)
            try:
                ev = vr.get("expected_value", 0)
                ev_str = f"{ev/1e8:.1f}亿" if isinstance(ev, (int, float)) else str(ev)
                mp = vr.get("margin_pct", 0)
                mp_str = f"{mp:.1%}" if isinstance(mp, (int, float)) else str(mp)
                lines.append(f"- **估值参考**: 期望值 {ev_str}, 安全边际 {mp_str}")
            except Exception:
                lines.append(f"- **估值参考**: {vr}")
        if d.quality_scores:
            qs = _safe_json(d.quality_scores)
            lines.append(f"- **质量评分**: Piotroski {qs.get('piotroski', 'N/A')}, Moat {qs.get('moat', 'N/A')}, Beneish {'⚠️' if qs.get('beneish_flag') else '✅'}")
        lines.append("")

    return "\n".join(lines)


def export_decisions_csv(path: str = "data/decisions_export.csv"):
    """导出 CSV 供 Excel 复盘"""
    decisions = get_decisions(status=None)
    rows = []
    for d in decisions:
        row = d.to_dict()
        rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"决策记录已导出: {path}")


# ===== 仓位建议（结合估值结果）=====
def suggest_position(
    ticker: str,
    current_price: float,
    valuation_result: Dict,
    max_position: float = 0.15,
    conviction: float = 0.7
) -> Dict[str, Any]:
    """
    基于估值结果给出仓位建议
    valuation_result 需包含：expected_value, bear_value, current_price 等
    """
    expected = valuation_result.get("expected_value", 0)
    bear = valuation_result.get("bear_value", 0)
    market_cap = valuation_result.get("market_cap", current_price * 1)  # 占位

    if expected <= 0 or current_price <= 0:
        return {"suggested_pct": 0, "reason": "估值无效"}

    upside = (expected - current_price) / current_price
    downside = (current_price - bear) / current_price if bear > 0 else 0.3
    downside = max(downside, 0.2)  # 至少假设 20% 下行

    win_prob = valuation_result.get("mos_prob", 0.5)
    kelly = (win_prob * upside - (1 - win_prob) * downside) / max(upside, 0.01)
    kelly = max(0, kelly) * conviction * 0.5  # 半凯利
    suggested = min(kelly, max_position)

    reason = f"上行 {upside:.1%}, 下行 {downside:.1%}, 胜率 {win_prob:.1%}, 凯利 {kelly:.1%}, 限仓 {max_position:.1%}"

    return {
        "suggested_pct": round(suggested, 4),
        "kelly_raw": round(kelly, 4),
        "upside": round(upside, 4),
        "downside": round(downside, 4),
        "win_prob": round(win_prob, 4),
        "reason": reason,
        "max_position": max_position,
        "conviction": conviction,
    }


if __name__ == "__main__":
    init_db()
    # 测试
    rec = DecisionRecord(
        date="2025-01-15",
        ticker="600519.SH",
        name="贵州茅台",
        action="BUY",
        position_pct=0.10,
        price=1600,
        thesis="白酒龙头、定价权强、FCF转换率>120%、分红稳健、反向DCF隐含增长仅4%远低于历史8%",
        key_assumptions={"growth": "8% 5年", "wacc": "9%", "fade": "15年", "terminal_pe": 18},
        risk_factors=["消费降级", "塑化剂/食安风险", "高管变动"],
        stop_review_trigger="FCF连续2季降 或 ROIC<20% 或 分红比例下调",
        exit_plan="估值达P90 或 基本面假设破坏",
        valuation_ref={"expected_value": 2.1e11, "margin_pct": 0.35},
        quality_scores={"piotroski": 9, "moat": 88, "beneish_flag": False},
    )
    add_decision(rec)
    print(get_portfolio_summary())
    print(generate_review_report())