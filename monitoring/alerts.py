"""
监控预警规则引擎 + 推送
配置文件：config/alert_rules.yaml
"""

import yaml
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from loguru import logger
import httpx
import asyncio


@dataclass
class Alert:
    ticker: str
    name: str
    rule_id: str
    severity: str  # critical / warning / info
    message: str
    current_value: Any
    threshold: Any
    report_date: str


def load_rules(path: str = "config/alert_rules.yaml") -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fmt_val(val: Any) -> str:
    """安全格式化指标值（bool/字符串/数值通吃）"""
    if isinstance(val, (bool, np.bool_)):
        return str(bool(val))
    if isinstance(val, (int, float)):
        return f"{val:.4f}"
    return str(val)


def _check_condition(condition: str, val: Any, threshold: Any) -> bool:
    """单值条件判断（bool 安全）"""
    if condition == "eq":
        if isinstance(val, (bool, np.bool_)) or isinstance(threshold, (bool, np.bool_)):
            return bool(val) == bool(threshold)
        try:
            return abs(float(val) - float(threshold)) < 1e-9
        except (TypeError, ValueError):
            return val == threshold
    if condition == "lt":
        return val < threshold
    if condition == "lte":
        return val <= threshold
    if condition == "gt":
        return val > threshold
    if condition == "gte":
        return val >= threshold
    return False


def evaluate_rules(df: pd.DataFrame, rules: Dict) -> List[Alert]:
    """
    对最新期数据评估所有规则
    df: 已包含 ratios、quality 评分的面板数据
    window > 1 时要求最近 N 期全部满足条件
    """
    alerts = []
    latest = df.sort_values("report_date").groupby("ticker").last().reset_index()

    # 合并 ticker 信息
    ticker_info = rules.get("ticker_info", {})

    # 预构建每只股票按日期排序的历史序列缓存
    hist_cache: Dict[str, pd.DataFrame] = {
        t: g.sort_values("report_date")
        for t, g in df.groupby("ticker")
    }

    for _, row in latest.iterrows():
        ticker = row["ticker"]
        info = ticker_info.get(ticker, {"name": ticker, "sector": "未知"})
        ticker_hist = hist_cache.get(ticker)

        for rule in rules.get("rules", []):
            rule_id = rule["id"]
            metric = rule["metric"]
            severity = rule.get("severity", "warning")
            condition = rule["condition"]  # lt/lte/gt/gte/eq/cross_down/cross_up
            threshold = rule["threshold"]
            window = int(rule.get("window", 1))  # 连续 N 期
            description = rule.get("description", "")

            if metric not in row.index:
                continue

            val = row[metric]
            if pd.isna(val):
                continue

            triggered = False
            if condition in ("cross_down", "cross_up"):
                # 需要历史序列判断穿越
                if ticker_hist is not None and metric in ticker_hist.columns:
                    series = ticker_hist[metric].dropna()
                    if len(series) >= 2:
                        prev, curr = series.iloc[-2], series.iloc[-1]
                        if condition == "cross_down":
                            triggered = prev > threshold and curr <= threshold
                        else:
                            triggered = prev < threshold and curr >= threshold
            else:
                if window > 1 and ticker_hist is not None and metric in ticker_hist.columns:
                    # 连续 N 期全部满足
                    series = ticker_hist[metric].dropna()
                    recent = series.tail(window)
                    if len(recent) >= window:
                        triggered = all(_check_condition(condition, v, threshold) for v in recent)
                else:
                    triggered = _check_condition(condition, val, threshold)

            if triggered:
                alerts.append(Alert(
                    ticker=ticker,
                    name=info["name"],
                    rule_id=rule_id,
                    severity=severity,
                    message=f"{info['name']}({ticker}) {description}: {metric}={_fmt_val(val)} ({condition} {threshold})",
                    current_value=val,
                    threshold=threshold,
                    report_date=str(row["report_date"])
                ))

    return alerts


def format_alert_message(alerts: List[Alert]) -> str:
    """格式化为 Markdown 推送文本"""
    if not alerts:
        return "✅ 本轮监控无异常触发"

    lines = ["🚨 **价值投资监控预警**", ""]
    by_severity = {"critical": [], "warning": [], "info": []}
    for a in alerts:
        by_severity[a.severity].append(a)

    for sev in ["critical", "warning", "info"]:
        if by_severity[sev]:
            icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}[sev]
            lines.append(f"{icon} **{sev.upper()}** ({len(by_severity[sev])})")
            for a in by_severity[sev]:
                lines.append(f"  - {a.message}")
            lines.append("")

    lines.append(f"📅 数据日期: {alerts[0].report_date if alerts else 'N/A'}")
    return "\n".join(lines)


class Notifier:
    """多渠道推送"""

    def __init__(self, config: Dict):
        self.config = config
        self.webhook_urls = config.get("webhooks") or {}
        self.telegram = config.get("telegram", {})
        self.email = config.get("email", {})

    async def send_telegram(self, message: str) -> bool:
        if not self.telegram.get("bot_token") or not self.telegram.get("chat_id"):
            return False
        url = f"https://api.telegram.org/bot{self.telegram['bot_token']}/sendMessage"
        payload = {
            "chat_id": self.telegram["chat_id"],
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"Telegram 推送失败: {e}")
            return False

    async def send_webhook(self, name: str, message: str) -> bool:
        url = self.webhook_urls.get(name)
        if not url:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json={"text": message})
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"Webhook {name} 推送失败: {e}")
            return False

    async def send_all(self, message: str):
        tasks = []
        if self.telegram.get("enabled", False):
            tasks.append(self.send_telegram(message))
        for name in self.webhook_urls:
            tasks.append(self.send_webhook(name, message))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def send_sync(self, message: str):
        """同步版本（用于脚本直接调用）"""
        try:
            asyncio.run(self.send_all(message))
        except RuntimeError:
            # 已在事件循环中
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self.send_all(message))


def run_monitoring(
    df: pd.DataFrame,
    rules_path: str = "config/alert_rules.yaml",
    notifier_config_path: str = "config/notifier.yaml"
) -> List[Alert]:
    """主入口：评估规则 -> 推送 -> 返回告警列表"""
    rules = load_rules(rules_path)
    alerts = evaluate_rules(df, rules)

    # 推送
    if alerts:
        notifier_cfg = {}
        if Path(notifier_config_path).exists():
            with open(notifier_config_path, "r", encoding="utf-8") as f:
                notifier_cfg = yaml.safe_load(f)
        notifier = Notifier(notifier_cfg)
        msg = format_alert_message(alerts)
        notifier.send_sync(msg)
        logger.info(f"已推送 {len(alerts)} 条预警")
    else:
        logger.info("无预警触发")

    return alerts


if __name__ == "__main__":
    import duckdb
    conn = duckdb.connect("data/financial.duckdb")
    df = conn.execute("SELECT * FROM financials").df()
    conn.close()

    if not df.empty:
        from analysis.ratios import compute_all_ratios
        from analysis.quality import piotroski_f_score, beneish_m_score, moat_scorecard
        df = compute_all_ratios(df)
        df = piotroski_f_score(df)
        df = beneish_m_score(df)
        df = moat_scorecard(df)
        run_monitoring(df)