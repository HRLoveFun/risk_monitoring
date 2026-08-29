import re
from datetime import date, datetime
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from daily_holdings.config import REQUIRED_COLS, logger


def _clean_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace("HKD$", "", regex=False)
        .str.strip()
        .replace({"": None, "-": None, "N/A": None, "n/a": None}),
        errors="coerce",
    )


def _num1(text: str | None) -> float | None:
    if text is None:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _table_to_df(table: Any) -> pd.DataFrame:
    rows = []
    for tr in table.find_all("tr"):
        cells = [
            re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])
        ]
        if any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return pd.DataFrame()
    header, *body = rows
    width = len(header)
    body = [(r + [""] * width)[:width] for r in body]
    return pd.DataFrame(body, columns=header)


def parse_as_of_date(html: str) -> date | None:
    idx = html.find('id="holdingsList"')
    scope = html[:idx] if idx > 0 else html
    matches = re.findall(r"As of\s+([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})", scope)
    if not matches:
        return None
    raw = matches[-1]
    try:
        return datetime.strptime(raw, "%d %b %Y").date()
    except ValueError:
        try:
            return datetime.strptime(raw, "%d %B %Y").date()
        except ValueError:
            return None


EQUITY_TICKER_RE = r"^\d+\s*HK$"
FUTURE_RE = r"\bFUTURES?\b"
OPTION_RE = r"\b(?:CALL|PUT|OPTION)\b"
MM_FUND_RE = r"\b(?:LIQ|LIQUIDITY|MONEY\s*MARKET)\b"


def is_future(name: str) -> bool:
    return bool(re.search(FUTURE_RE, str(name).upper()))


def is_option(name: str) -> bool:
    return bool(re.search(OPTION_RE, str(name).upper()))


def parse_option_name(name: str) -> tuple[float | None, date | None]:
    s = str(name or "")
    expiry = None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            expiry = date(yy, mm, dd)
        except ValueError:
            expiry = None
    rest = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", " ", s)
    m = re.search(r"\b[CP]\s?(\d[\d ]*)", rest)
    if not m:
        nums = [
            d
            for d in (re.sub(r"\D", "", t) for t in re.findall(r"\d[\d, ]*\d|\d", rest))
            if d and float(d) >= 1000
        ]
        return (float(nums[-1]) if nums else None), expiry
    digits = re.sub(r"\D", "", m.group(1))
    return (float(digits) if digits else None), expiry


def split_equities(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    ticker = df["Exchange Ticker"].astype(str).str.strip()
    name = df["Name of Securities"].astype(str).str.upper()
    is_deriv_name = name.str.contains(FUTURE_RE) | name.str.contains(OPTION_RE)
    is_mm = name.str.contains(MM_FUND_RE)
    is_eq = (ticker.str.match(EQUITY_TICKER_RE) | is_mm) & ~is_deriv_name
    equities = df[is_eq].reset_index(drop=True)
    derivatives = df[~is_eq].reset_index(drop=True)
    unknown = []
    if not derivatives.empty:
        known = (
            derivatives["Name of Securities"]
            .astype(str)
            .str.upper()
            .str.contains(FUTURE_RE + "|" + OPTION_RE)
        )
        for nm in derivatives.loc[~known, "Name of Securities"]:
            logger.warning("发现未识别的非正股工具:%s(未纳入期货/期权计算,请检查)", nm)
            unknown.append(str(nm))
    return equities, derivatives, unknown


def derive_nav(equities: pd.DataFrame) -> float | None:
    mv, w = "Market Value (in HKD)", "Net Assets (%)"
    if mv not in equities.columns or w not in equities.columns:
        return None
    valid = equities[(equities[w] > 0) & (equities[mv] > 0)]
    if valid.empty:
        return None
    nav = (valid[mv] / (valid[w] / 100.0)).median()
    return float(nav) if nav and nav > 0 else None


def parse_labeled_values(soup: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = [
            re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])
        ]
        if len(cells) == 2 and cells[0]:
            out.setdefault(cells[0], cells[1])
    return out


def _lookup(values: dict[str, str], keyword: str) -> str | None:
    for k, v in values.items():
        if keyword.lower() in k.lower():
            return v
    return None


def option_legs(
    derivatives: pd.DataFrame, index_price: float | None, as_of: date | None
) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    if derivatives.empty:
        return legs
    need = {"Number of Shares Held", "Market Price (in HKD)"}
    if not need.issubset(derivatives.columns):
        logger.warning("持仓表缺少 %s,无法从持仓行计算期权敞口", need - set(derivatives.columns))
        return legs
    for _, r in derivatives.iterrows():
        nm = str(r.get("Name of Securities"))
        if not is_option(nm):
            continue
        strike, expiry = parse_option_name(nm)
        contracts, price = r.get("Number of Shares Held"), r.get("Market Price (in HKD)")
        days = (expiry - as_of).days if (expiry and as_of) else None

        problem, wait_helps = None, True
        if pd.isna(contracts):
            problem = "合约张数缺失"
        elif strike is None:
            problem = "无法从名称解析出行权价"
        elif index_price and not (0.5 * index_price <= strike <= 1.5 * index_price):
            problem = f"解析出的行权价 {strike:,.0f} 与指数 {index_price:,.0f} 明显不匹配"
            wait_helps = False
        elif expiry is None:
            problem = "无法从名称解析出到期日"
            wait_helps = False
        elif as_of is None:
            problem = "持仓截止日期未知,无法计算剩余期限"
        elif days < 0:
            problem = f"合约已于 {expiry} 到期,却仍挂在页面上"
        elif re.search(r"\bPUT\b", nm.upper()):
            problem = "看跌期权,当前 Delta 模型只支持看涨"
            wait_helps = False

        if problem:
            logger.warning("期权腿无法纳入敞口计算(%s):%s", problem, nm)
        legs.append(
            {
                "name": nm,
                "contracts": float(contracts) if pd.notna(contracts) else None,
                "price": float(price) if pd.notna(price) else None,
                "strike": float(strike) if strike is not None else None,
                "expiry": expiry,
                "days": days,
                "index": index_price,
                "problem": problem,
                "wait_helps": wait_helps,
            }
        )
    return legs


def futures_legs(derivatives: pd.DataFrame) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    if derivatives.empty:
        return legs
    if not {"Number of Shares Held", "Market Price (in HKD)"}.issubset(derivatives.columns):
        return legs
    for _, r in derivatives.iterrows():
        nm = r.get("Name of Securities")
        if not is_future(nm):
            continue
        contracts, index_pt = r.get("Number of Shares Held"), r.get("Market Price (in HKD)")
        if pd.isna(contracts) or pd.isna(index_pt):
            logger.warning("期货行张数/指数点缺失,跳过:%s", nm)
            continue
        legs.append({"name": str(nm), "contracts": float(contracts), "index_pt": float(index_pt)})
    return legs


def parse_holdings(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    df = pd.DataFrame()
    options_df = pd.DataFrame()
    for t in soup.find_all("table", class_="holdings"):
        tmp = _table_to_df(t)
        if tmp.empty:
            continue
        cols = list(tmp.columns)
        if "Name of Securities" in cols and "Exchange Ticker" in cols:
            df = tmp
        elif any("Option Position" in c for c in cols):
            options_df = tmp

    if df.empty:
        raise ValueError("找不到完整持仓表(表头应含 Name of Securities / Exchange Ticker)")

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"持仓表缺少关键列: {missing};实际列: {list(df.columns)}")

    for col in df.columns:
        if any(k in col for k in ["Price", "Shares", "Value", "Net Assets", "%"]):
            df[col] = _clean_num(df[col])
    df = df[df["Name of Securities"].astype(str).str.strip() != ""].reset_index(drop=True)
    if df.empty:
        raise ValueError("持仓表清洗后无有效数据行")

    if not options_df.empty:
        for col in options_df.columns:
            if any(k in col for k in ["Notional", "Strike", "Index Price", "Days", "Upside", "%"]):
                options_df[col] = _clean_num(options_df[col])

    equities, derivatives, unknown = split_equities(df)
    if equities.empty:
        raise ValueError("拆分后正股为空,持仓表代码格式可能已变化,请检查")

    values = parse_labeled_values(soup)
    nav_page = _num1(_lookup(values, "Total Net Asset Value of the Fund"))
    nav_derived = derive_nav(equities)
    nav = nav_page or nav_derived
    if nav_page and nav_derived and abs(nav_page - nav_derived) / nav_page > 0.005:
        logger.warning(
            "页面净值 %s 与「市值/权重」反推值 %s 相差 >0.5%%,请检查",
            f"{nav_page:,.0f}",
            f"{nav_derived:,.0f}",
        )

    index_close = _num1(_lookup(values, "Closing level of Hang Seng China Enterprises"))
    if index_close is None and not options_df.empty and "Index Price" in options_df.columns:
        idx = pd.to_numeric(options_df["Index Price"], errors="coerce").dropna()
        index_close = float(idx.iloc[0]) if not idx.empty else None

    as_of = parse_as_of_date(html)
    o_legs = option_legs(derivatives, index_close, as_of)
    f_legs = futures_legs(derivatives)
    logger.info(
        "解析成功:总行数 %d(正股 %d / 衍生品 %d:期货 %d 腿 / 期权 %d 腿),"
        "NAV=%s(%s),指数收盘 %s,截止 %s",
        len(df),
        len(equities),
        len(derivatives),
        len(f_legs),
        len(o_legs),
        f"{nav:,.0f}" if nav else "未知",
        "页面公布" if nav_page else "反推",
        f"{index_close:,.2f}" if index_close else "未知",
        as_of,
    )
    return {
        "as_of": as_of,
        "full": df,
        "equities": equities,
        "derivatives": derivatives,
        "options": options_df,
        "nav": nav,
        "nav_page": nav_page,
        "index_close": index_close,
        "option_legs": o_legs,
        "futures_legs": f_legs,
        "unknown_instruments": unknown,
    }
