"""
A/H 股财报数据获取与标准化入库
依赖：akshare、pandas、duckdb、pyyaml
"""

import akshare as ak
import pandas as pd
import duckdb
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from loguru import logger
import time
import re


@dataclass
class FinancialStatement:
    """标准化后的财报字段（统一中文名，便于阅读）"""
    ticker: str
    report_date: str  # YYYY-MM-DD
    period_type: str  # 'annual' / 'quarterly'

    # 利润表
    revenue: Optional[float] = None           # 营业收入
    operating_cost: Optional[float] = None    # 营业成本
    gross_profit: Optional[float] = None      # 毛利
    operating_profit: Optional[float] = None  # 营业利润
    net_profit: Optional[float] = None        # 净利润
    net_profit_parent: Optional[float] = None # 归母净利润
    non_recurring_profit: Optional[float] = None  # 扣非净利润

    # 现金流量表
    ocf: Optional[float] = None               # 经营活动现金流
    icf: Optional[float] = None               # 投资活动现金流
    fcf: Optional[float] = None               # 自由现金流 = OCF - Capex
    capex: Optional[float] = None             # 资本支出（购建固定资产/无形资产支付的现金）

    # 资产负债表
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    total_equity: Optional[float] = None
    equity_parent: Optional[float] = None     # 归母权益
    cash_equiv: Optional[float] = None        # 货币资金
    short_term_debt: Optional[float] = None   # 短期借款
    long_term_debt: Optional[float] = None    # 长期借款
    accounts_receivable: Optional[float] = None
    inventory: Optional[float] = None
    accounts_payable: Optional[float] = None
    goodwill: Optional[float] = None          # 商誉

    # 衍生指标（入库后计算，不在此处）
    # roe, roic, fcf_conversion, cash_cycle, debt_to_equity 等


# ===== 科目映射表：AKShare 原始字段 -> 标准化字段 =====
# AKShare 股票财报主表字段随版本变化，此处维护常用映射，实际运行时会动态匹配

INCOME_MAP = {
    "营业收入": ["营业收入", "营业总收入", "revenue", "total_revenue"],
    "营业成本": ["营业成本", "营业总成本", "operating_cost", "cost_of_revenue"],
    "毛利": ["毛利", "gross_profit"],
    "营业利润": ["营业利润", "operating_profit", "operating_income"],
    "净利润": ["净利润", "net_profit", "net_income"],
    "归母净利润": ["归属于母公司股东的净利润", "归母净利润", "net_profit_parent", "profit_attributable_to_owners"],
    "扣非净利润": ["扣除非经常性损益后的净利润", "扣非净利润", "non_recurring_profit"],
}

CASHFLOW_MAP = {
    "经营活动现金流": ["经营活动产生的现金流量净额", "经营活动现金流", "ocf", "net_cash_operating"],
    "投资活动现金流": ["投资活动产生的现金流量净额", "投资活动现金流", "icf", "net_cash_investing"],
    "购建固定资产/无形资产支付的现金": [
        "购建固定资产、无形资产和其他长期资产支付的现金",
        "购建固定资产无形资产支付的现金",
        "购建固定资产、无形资产支付的现金",
        "capex", "capital_expenditure"
    ],
}

BALANCE_MAP = {
    "总资产": ["资产总计", "总资产", "total_assets"],
    "总负债": ["负债合计", "总负债", "total_liabilities"],
    "总权益": ["所有者权益合计", "总权益", "total_equity"],
    "归母权益": ["归属于母公司股东权益", "归母权益", "equity_parent"],
    "货币资金": ["货币资金", "现金及现金等价物", "cash_equiv", "cash_and_equivalents"],
    "短期借款": ["短期借款", "short_term_debt"],
    "长期借款": ["长期借款", "long_term_debt"],
    "应收账款": ["应收账款", "accounts_receivable"],
    "存货": ["存货", "inventory"],
    "应付账款": ["应付账款", "accounts_payable"],
    "商誉": ["商誉", "goodwill"],
}


def _find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """在 DataFrame 列名中模糊匹配候选字段（大小写不敏感、去空格）"""
    cols = {c.lower().replace(" ", ""): c for c in df.columns}
    for cand in candidates:
        key = cand.lower().replace(" ", "")
        if key in cols:
            return cols[key]
    # 部分匹配
    for cand in candidates:
        key = cand.lower().replace(" ", "")
        for col_low, col_orig in cols.items():
            if key in col_low or col_low in key:
                return col_orig
    return None


def _safe_float(val) -> Optional[float]:
    """安全转浮点数，处理 --、-、空字符串、逗号"""
    if val is None or val == "" or val == "--" or val == "-":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        # 去除逗号、单位（亿/万/元）
        s = str(val).replace(",", "").replace("亿", "").replace("万", "").replace("元", "").strip()
        return float(s)
    except Exception:
        return None


class AKShareProvider:
    """AKShare A/H 股财报数据获取器"""

    def __init__(self, db_path: str = "data/financial.duckdb"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS financials (
                ticker VARCHAR,
                report_date DATE,
                period_type VARCHAR,
                -- 利润表
                revenue DOUBLE,
                operating_cost DOUBLE,
                gross_profit DOUBLE,
                operating_profit DOUBLE,
                net_profit DOUBLE,
                net_profit_parent DOUBLE,
                non_recurring_profit DOUBLE,
                -- 现金流量表
                ocf DOUBLE,
                icf DOUBLE,
                capex DOUBLE,
                fcf DOUBLE,
                -- 资产负债表
                total_assets DOUBLE,
                total_liabilities DOUBLE,
                total_equity DOUBLE,
                equity_parent DOUBLE,
                cash_equiv DOUBLE,
                short_term_debt DOUBLE,
                long_term_debt DOUBLE,
                accounts_receivable DOUBLE,
                inventory DOUBLE,
                accounts_payable DOUBLE,
                goodwill DOUBLE,
                -- 元数据
                source VARCHAR,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, report_date, period_type)
            )
        """)

    def _normalize_ticker(self, ticker: str) -> Tuple[str, str]:
        """统一 ticker 格式，返回 (akshare_symbol, market)"""
        # 输入示例：600519.SH, 000858.SZ, 00700.HK
        ticker = ticker.strip().upper()
        if ticker.endswith(".SH"):
            return ticker.replace(".SH", ""), "SH"
        elif ticker.endswith(".SZ"):
            return ticker.replace(".SZ", ""), "SZ"
        elif ticker.endswith(".HK"):
            return ticker.replace(".HK", ""), "HK"
        else:
            # 尝试按代码前缀判断
            if ticker.startswith(("6", "9")):
                return ticker, "SH"
            elif ticker.startswith(("0", "3")):
                return ticker, "SZ"
            elif ticker.isdigit() and len(ticker) == 5:
                return ticker, "HK"
            raise ValueError(f"无法识别 ticker 格式: {ticker}")

    def fetch_a_share_financials(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """获取 A 股三大报表（合并报表）"""
        logger.info(f"Fetching A-share financials for {symbol}...")
        try:
            # 利润表
            income = ak.stock_financial_abstract(symbol=symbol)
            # 现金流量表
            cashflow = ak.stock_cash_flow_sheet_by_report_em(symbol=symbol)
            # 资产负债表
            balance = ak.stock_balance_sheet_by_report_em(symbol=symbol)
            return {"income": income, "cashflow": cashflow, "balance": balance}
        except Exception as e:
            logger.warning(f"AKShare 获取 {symbol} 失败: {e}")
            return {}

    def fetch_hk_financials(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """获取港股三大报表（AKShare 港股财报接口）"""
        logger.info(f"Fetching HK financials for {symbol}...")
        try:
            # 港股利润表
            income = ak.stock_financial_hk_analysis_indicator_em(symbol=symbol)
            # 注意：AKShare 港股现金流/资产负债表接口可能不同，需按版本调整
            # 这里尝试通用接口
            cashflow = ak.stock_cash_flow_hk_em(symbol=symbol) if hasattr(ak, 'stock_cash_flow_hk_em') else pd.DataFrame()
            balance = ak.stock_balance_hk_em(symbol=symbol) if hasattr(ak, 'stock_balance_hk_em') else pd.DataFrame()
            return {"income": income, "cashflow": cashflow, "balance": balance}
        except Exception as e:
            logger.warning(f"AKShare 港股获取 {symbol} 失败: {e}")
            return {}

    def parse_statement(self, df: pd.DataFrame, field_map: Dict[str, List[str]]) -> Dict[str, Optional[float]]:
        """将单期报表 DataFrame 解析为标准化字段字典"""
        if df.empty:
            return {k: None for k in field_map.keys()}

        # AKShare 返回格式：第一列为科目名，后续列为各期数值（最新期在左或右，需检测）
        # 统一转置为：index=科目，columns=报告期
        if df.shape[1] < 2:
            return {k: None for k in field_map.keys()}

        # 假设第一列是科目名
        item_col = df.columns[0]
        df = df.set_index(item_col)
        df.columns = [str(c) for c in df.columns]

        # 取最新一期（最左列或最右列，AKShare 通常最新在左）
        latest_col = df.columns[0]
        series = df[latest_col]

        # 科目是行标签（index），按行标签模糊匹配
        idx_map = {str(i).lower().replace(" ", ""): i for i in series.index}

        def _find_row(candidates):
            for cand in candidates:
                key = str(cand).lower().replace(" ", "")
                if key in idx_map:
                    return idx_map[key]
            for cand in candidates:
                key = str(cand).lower().replace(" ", "")
                for k_low, k_orig in idx_map.items():
                    if key in k_low:
                        return k_orig
            return None

        result = {}
        for std_field, candidates in field_map.items():
            row_name = _find_row(candidates)
            if row_name is not None:
                val = _safe_float(series.get(row_name))
                result[std_field] = val
            else:
                result[std_field] = None
        return result

    def build_standardized(self, ticker: str, raw: Dict[str, pd.DataFrame], period_type: str = "annual") -> Optional[FinancialStatement]:
        """将原始三大表合并为标准化 FinancialStatement"""
        if not raw or all(df.empty for df in raw.values()):
            return None

        fs = FinancialStatement(ticker=ticker, report_date="", period_type=period_type)

        # 统一映射：解析结果键 -> FinancialStatement 属性名
        FIELD_TO_ATTR = {
            "营业收入": "revenue",
            "营业成本": "operating_cost",
            "毛利": "gross_profit",
            "营业利润": "operating_profit",
            "净利润": "net_profit",
            "归母净利润": "net_profit_parent",
            "扣非净利润": "non_recurring_profit",
            "经营活动现金流": "ocf",
            "投资活动现金流": "icf",
            "购建固定资产/无形资产支付的现金": "capex",
            "总资产": "total_assets",
            "总负债": "total_liabilities",
            "总权益": "total_equity",
            "归母权益": "equity_parent",
            "货币资金": "cash_equiv",
            "短期借款": "short_term_debt",
            "长期借款": "long_term_debt",
            "应收账款": "accounts_receivable",
            "存货": "inventory",
            "应付账款": "accounts_payable",
            "商誉": "goodwill",
        }

        # 利润表
        income_data = self.parse_statement(raw.get("income", pd.DataFrame()), INCOME_MAP)
        for k, v in income_data.items():
            attr = FIELD_TO_ATTR.get(k)
            if attr:
                setattr(fs, attr, v)

        # 现金流量表
        cf_data = self.parse_statement(raw.get("cashflow", pd.DataFrame()), CASHFLOW_MAP)
        for k, v in cf_data.items():
            attr = FIELD_TO_ATTR.get(k)
            if attr:
                setattr(fs, attr, v)

        # 资产负债表
        bal_data = self.parse_statement(raw.get("balance", pd.DataFrame()), BALANCE_MAP)
        for k, v in bal_data.items():
            attr = FIELD_TO_ATTR.get(k)
            if attr:
                setattr(fs, attr, v)

        # 计算 FCF
        if fs.ocf is not None and fs.capex is not None:
            fs.fcf = fs.ocf - abs(fs.capex)  # capex 通常为负，取绝对值

        # 确定报告期（从列名提取）
        for df in raw.values():
            if not df.empty and df.shape[1] > 1:
                latest_col = str(df.columns[1]) if df.shape[1] > 1 else str(df.columns[0])
                # 尝试解析日期
                date_match = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2}|\d{4}年\d{2}月\d{2}日|\d{4}-\d{2})", latest_col)
                if date_match:
                    fs.report_date = date_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")[:10]
                    break

        if not fs.report_date:
            fs.report_date = pd.Timestamp.now().strftime("%Y-%m-%d")

        return fs

    def upsert(self, fs: FinancialStatement):
        """写入 DuckDB（upsert）- 显式列名，fetched_at 由 DEFAULT 填充"""
        cols = (
            "ticker, report_date, period_type, "
            "revenue, operating_cost, gross_profit, operating_profit, "
            "net_profit, net_profit_parent, non_recurring_profit, "
            "ocf, icf, capex, fcf, "
            "total_assets, total_liabilities, total_equity, equity_parent, "
            "cash_equiv, short_term_debt, long_term_debt, "
            "accounts_receivable, inventory, accounts_payable, goodwill, source"
        )
        placeholders = ", ".join(["?"] * 26)
        self.conn.execute(f"""
            INSERT INTO financials ({cols}) VALUES ({placeholders})
            ON CONFLICT (ticker, report_date, period_type) DO UPDATE SET
                revenue=excluded.revenue,
                operating_cost=excluded.operating_cost,
                gross_profit=excluded.gross_profit,
                operating_profit=excluded.operating_profit,
                net_profit=excluded.net_profit,
                net_profit_parent=excluded.net_profit_parent,
                non_recurring_profit=excluded.non_recurring_profit,
                ocf=excluded.ocf,
                icf=excluded.icf,
                capex=excluded.capex,
                fcf=excluded.fcf,
                total_assets=excluded.total_assets,
                total_liabilities=excluded.total_liabilities,
                total_equity=excluded.total_equity,
                equity_parent=excluded.equity_parent,
                cash_equiv=excluded.cash_equiv,
                short_term_debt=excluded.short_term_debt,
                long_term_debt=excluded.long_term_debt,
                accounts_receivable=excluded.accounts_receivable,
                inventory=excluded.inventory,
                accounts_payable=excluded.accounts_payable,
                goodwill=excluded.goodwill,
                source=excluded.source,
                fetched_at=now()
        """, [
            fs.ticker, fs.report_date, fs.period_type,
            fs.revenue, fs.operating_cost, fs.gross_profit, fs.operating_profit,
            fs.net_profit, fs.net_profit_parent, fs.non_recurring_profit,
            fs.ocf, fs.icf, fs.capex, fs.fcf,
            fs.total_assets, fs.total_liabilities, fs.total_equity, fs.equity_parent,
            fs.cash_equiv, fs.short_term_debt, fs.long_term_debt,
            fs.accounts_receivable, fs.inventory, fs.accounts_payable, fs.goodwill,
            "akshare"
        ])

    def fetch_and_store(self, ticker: str, market: str):
        """获取并存储单只股票财报"""
        symbol, mkt = self._normalize_ticker(ticker)
        if mkt in ("SH", "SZ"):
            raw = self.fetch_a_share_financials(symbol)
        elif mkt == "HK":
            raw = self.fetch_hk_financials(symbol)
        else:
            logger.warning(f"不支持的市场: {mkt}")
            return

        # 年报
        if raw.get("income") is not None:
            fs = self.build_standardized(ticker, raw, "annual")
            if fs:
                # 防止解析失败时写入全空行
                has_data = any(getattr(fs, f, None) is not None
                               for f in ["revenue", "net_profit_parent", "ocf", "total_assets"])
                if has_data:
                    self.upsert(fs)
                    logger.info(f"Stored {ticker} {fs.report_date} annual")
                else:
                    logger.warning(f"跳过 {ticker}: 解析结果无有效财务数据（接口字段可能已变更）")

        # 季报（AKShare 接口可能不同，这里简化只存年报）
        # 如需季报，需调用对应季报接口

    def load_panel(self, tickers: List[str], metrics: List[str] = None) -> pd.DataFrame:
        """加载面板数据用于分析"""
        if metrics is None:
            metrics = ["*"]
        cols = ", ".join(metrics) if metrics != ["*"] else "*"
        placeholders = ", ".join(["?"] * len(tickers))
        sql = f"SELECT ticker, report_date, {cols} FROM financials WHERE ticker IN ({placeholders}) ORDER BY ticker, report_date"
        return self.conn.execute(sql, tickers).df()

    def close(self):
        self.conn.close()


# ===== 手动补录接口（解决数据质量焦虑）=====
def manual_import_excel(excel_path: str, db_path: str = "data/financial.duckdb"):
    """从 Excel 导入手工整理的财报（列名需匹配标准化字段）"""
    df = pd.read_excel(excel_path)
    # 期望列：ticker, report_date, period_type, revenue, net_profit_parent, ocf, capex, ...
    conn = duckdb.connect(db_path)
    # 简单 upsert
    for _, row in df.iterrows():
        cols = [c for c in row.index if c in [
            'ticker','report_date','period_type','revenue','operating_cost','gross_profit',
            'operating_profit','net_profit','net_profit_parent','non_recurring_profit',
            'ocf','icf','capex','fcf','total_assets','total_liabilities','total_equity',
            'equity_parent','cash_equiv','short_term_debt','long_term_debt',
            'accounts_receivable','inventory','accounts_payable','goodwill'
        ]]
        vals = [row[c] for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        conflict_cols = "ticker, report_date, period_type"
        update_cols = ", ".join([f"{c}=excluded.{c}" for c in cols if c not in conflict_cols.split(", ")])
        sql = f"INSERT INTO financials ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_cols}, fetched_at=CURRENT_TIMESTAMP"
        conn.execute(sql, vals)
    conn.close()
    logger.info(f"Manual import from {excel_path} done.")


if __name__ == "__main__":
    # 简单测试
    provider = AKShareProvider()
    try:
        provider.fetch_and_store("600519.SH", "SH")
        provider.fetch_and_store("00700.HK", "HK")
        df = provider.load_panel(["600519.SH", "00700.HK"])
        print(df.head())
    finally:
        provider.close()