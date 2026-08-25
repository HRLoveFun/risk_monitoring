#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global X HSCEI Covered Call Active ETF —— 每日持仓抓取 + 邮件日报

流程:
  1. 抓取基金页面 HTML(requests,带超时/重试/内容校验)
  2. 解析持仓表(拆正股/衍生品)+ 期权敞口表 + 页面公布的基金净值与指数收盘点位
  3. 计算邮件正文:
     a. 指数现价 → 行权价距离
     b. 风险暴露:正股敞口 / 期货多头敞口 / 期权空头敞口
        (期权同列「名义口径」与「Delta 调整后」两个值)
      c. 前五大重仓(标题含合计占比)
      d. 较上一交易日差异:汇总指标(NAV/指数收盘/正股市值/期货名义)、
         正股新增/剔除/权重变化(≥0.1pp)、期货与期权逐腿新增/剔除/张数变化。
         差异只如实罗列,不推断原始数据的对错
  4. 通过 SMTP 发邮件(主题「YYYYMMDD <FUND_NAME> 持仓」+ HTML 正文 + 完整持仓 CSV 附件)
  5. 全程日志;任一步失败发"失败告警"

期权 Delta:不用拍脑袋的假设波动率 —— 先用期权自身市价(持仓表的「指数点」报价)
      反解隐含波动率,再算 Black-Scholes Delta;反解不出来才退回 IMPLIED_VOL。

敞口来源:期货/期权敞口一律以「完整持仓表」的合约行为准(周度期权、场内/OTC 都在里面),
      官网那张「期权敞口表」只用来交叉校验;两者对不上会告警,敞口表缺失也不影响出数。

幂等:按页面上的「持仓截止日期」去重 —— 同一截止日期只发一次。
      这让脚本可以被高频触发(如每 10 分钟):没更新就跳过,更新了就发一封。
      调试可设 FORCE_SEND=1 强制发送。

分阶段发布:官网存在「先放正股、稍后才补期货/期权行」的分阶段发布。是否等待只看
      「与前一日快照对比」的硬信号 —— 整类工具消失(如昨日期货/期权合约行 >0、今日为 0)
      或关键计算前提缺失(腿无法计算、缺指数点位):
        - 命中且"等一等可能就好" → 先不发信也不写幂等标记,等下次触发(每 10 分钟);
        - 超过 INCOMPLETE_GRACE_HOURS → 先发一封【待补全】日报兜底;
        - 之后数据补齐 → 自动补发一封「【更新】」版(每个截止日最多补发一次)。
      其余一切数量变化(腿数增减、换仓滚动、张数变化等)不拦截、不下对错结论,
      只如实列入日报的「较上一交易日差异」。

依赖: requests, beautifulsoup4, pandas  (邮件用标准库)
配置: 全部走环境变量,见 .env.example
"""

import os
import re
import sys
import ssl
import json
import math
import time
import logging
import smtplib
from email.message import EmailMessage
from datetime import datetime, date, timezone

import requests
import pandas as pd
from bs4 import BeautifulSoup


# ----------------------------------------------------------------------------
# 配置(全部从环境变量读;敏感信息绝不写死在代码里)
# ----------------------------------------------------------------------------
def env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"缺少必填环境变量: {key}")
    return val


FUND_URL = env("FUND_URL", "https://www.globalxetfs.com.hk/funds/hscei-covered-call-etf/")
FUND_NAME = env("FUND_NAME", "Global X HSCEI Covered Call Active ETF")

SMTP_HOST = env("SMTP_HOST", "smtp.qq.com")        # Foxmail/QQ 邮箱
SMTP_PORT = int(env("SMTP_PORT", "465"))           # 465=SSL, 587=STARTTLS
SMTP_USER = env("SMTP_USER")                        # 发件邮箱地址
SMTP_PASS = env("SMTP_PASS")                        # 邮箱"授权码"(不是登录密码)
MAIL_FROM = env("MAIL_FROM", SMTP_USER)
MAIL_TO = [a.strip() for a in env("MAIL_TO", "").split(",") if a.strip()]

DATA_DIR = env("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
FORCE_SEND = env("FORCE_SEND", "").lower() in ("1", "true", "yes")  # 调试用:忽略幂等强制发送

HTTP_TIMEOUT = 60          # 秒,页面抓取读取超时(官网响应慢,放宽以减少误报)
HTTP_RETRIES = 3           # 下载失败重试次数
SMTP_TIMEOUT = 30          # 秒,SMTP 连接/读写超时
SMTP_RETRIES = 3           # 发信失败重试次数

# 仓位计算参数
INDEX_MULTIPLIER = 50.0    # HSCEI 期货/期权合约乘数:每点 HKD$50(页面脚注确认)
IMPLIED_VOL = float(env("IMPLIED_VOL", "0.30"))  # 兜底波动率:仅当期权市价反解不出 IV 时才用
RISK_FREE = 0.0            # 短端无风险利率,近似取 0

# 页面「分阶段发布」宽限:判定数据未就绪后最多等这么久,超时就先发带警告的日报兜底
INCOMPLETE_GRACE_HOURS = float(env("INCOMPLETE_GRACE_HOURS", "3"))
# 「较上一交易日差异」里,正股权重变化小于该值(百分点)的不列出。
WEIGHT_DIFF_TOL_PP = 0.10

# 持仓表必须包含的关键列(对不上就报错,绝不默默算错)
REQUIRED_COLS = ["Name of Securities", "Exchange Ticker", "Net Assets (%)"]

logger = logging.getLogger("daily_holdings")


# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(DATA_DIR, "run.log"), encoding="utf-8"),
        ],
    )


# ----------------------------------------------------------------------------
# 1. 抓取页面
# ----------------------------------------------------------------------------
def fetch_page(url):
    """下载页面 HTML;带重试、超时,并校验内容确实是目标页面。"""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            logger.info("抓取页面(第 %d 次): %s", attempt, url)
            # timeout=(连接超时, 读取超时):连不上 10s 就放弃,慢响应最多等 30s
            resp = requests.get(url, headers=headers, timeout=(10, HTTP_TIMEOUT))
            resp.raise_for_status()                      # 4xx/5xx 直接抛错进入重试
            html = resp.text
            # 内容健壮性校验:状态码 200 不等于拿到正确页面(可能是错误页/反爬页/空壳)
            if len(html) < 10000 or "holdingsList" not in html:
                raise ValueError(f"页面内容异常(长度 {len(html)},未含 holdingsList),疑似错误页")
            logger.info("页面抓取成功,大小 %d 字节", len(html))
            return html
        except Exception as e:
            last_err = e
            logger.warning("抓取失败: %s", e)
            if attempt < HTTP_RETRIES:
                time.sleep(2 ** attempt)  # 指数退避
    raise RuntimeError(f"页面抓取最终失败: {last_err}")


# ----------------------------------------------------------------------------
# 2. 解析
# ----------------------------------------------------------------------------
def _clean_num(series):
    """把 '1,438,766,521.20' / '6.49' / '-20.14%' 这类文本转成 float。"""
    return pd.to_numeric(
        series.astype(str)
              .str.replace(",", "", regex=False)
              .str.replace("%", "", regex=False)
              .str.replace("HKD$", "", regex=False)
              .str.strip()
              .replace({"": None, "-": None, "N/A": None, "n/a": None}),
        errors="coerce",
    )


def _num1(text):
    """从一段文本里取第一个数(如 'HKD 22,326,482,899.12' → 22326482899.12)。取不到返回 None。"""
    if text is None:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _table_to_df(table):
    """把一个 <table> 解析成 DataFrame(第一行表头,其余为数据)。"""
    rows = []
    for tr in table.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                 for c in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return pd.DataFrame()
    header, *body = rows
    # 对齐列数,避免个别行多/少一格导致崩溃
    width = len(header)
    body = [(r + [""] * width)[:width] for r in body]
    return pd.DataFrame(body, columns=header)


def parse_as_of_date(html):
    """取持仓表之前最近的 'As of <日期>' 作为持仓截止日期。"""
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


# ---- 工具类型识别(集中在这里;基金新增工具类型只需改这三个规则)-------------------
# 期货:HSCEI FUTURES 08/28/26          (Exchange Ticker 如 HCQ6)
# 期权:CALL HSCEI 08/28/26 C8700 OTC   (场外,Ticker 为空)
#      CALL HSCEI WEEKLY OPTION 08/07/26 8750   (周度期权,Ticker 为空)
#      CALL HANG SENG CHINA ENT 08/28/26 8700   (场内,Ticker 如 HSCEI 08/28/26 C8700)
EQUITY_TICKER_RE = r"^\d+\s*HK$"
FUTURE_RE = r"\bFUTURES?\b"
OPTION_RE = r"\b(?:CALL|PUT|OPTION)\b"     # 含 OPTION 才认得出「WEEKLY OPTION」这类新腿


def is_future(name):
    return bool(re.search(FUTURE_RE, str(name).upper()))


def is_option(name):
    return bool(re.search(OPTION_RE, str(name).upper()))


def parse_option_name(name):
    """从期权名称解析出 (行权价, 到期日)。名称是唯一同时带这两项的地方,
    所以周度/月度、场内/OTC 都靠它对齐。兼容以下写法:

        CALL HSCEI WEEKLY OPTION 08/07/26 8750   → (8750, 2026-08-07)
        CALL HSCEI 08/28/26 C8700 OTC            → (8700, 2026-08-28)
        CALL HSCEI 07/30/26 C770 0 OTC           → (7700, 2026-07-30)  页面会把 C7700 渲染断开
        CALL HANG SENG CHINA ENT 08/28/26 8700   → (8700, 2026-08-28)
        Short HSCEI WEEKLY OPTION 8,750 Call Option → (8750, None)     期权敞口表的写法
    """
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

    # 先把日期抠掉,免得 08/28/26 里的数字被当成行权价
    rest = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", " ", s)
    m = re.search(r"\b[CP]\s?(\d[\d ]*)", rest)          # C8700 / C870 0
    if not m:
        # 退而取剩下最后一个「≥1000 的数」(如 '... 8,750 Call Option')
        nums = [d for d in (re.sub(r"\D", "", t) for t in re.findall(r"\d[\d, ]*\d|\d", rest))
                if d and float(d) >= 1000]
        return (float(nums[-1]) if nums else None), expiry
    digits = re.sub(r"\D", "", m.group(1))
    return (float(digits) if digits else None), expiry


def split_equities(df):
    """把完整持仓表拆成『正股』和『衍生品行(期货/期权)』。

    正股:Exchange Ticker 形如 `939 HK`(纯数字 + HK);衍生品:期货(HCQ6)、
    期权(名称含 CALL/PUT/OPTION 或空代码)等。靠『格式/关键词』判断而非行数,
    所以正股或衍生品数量怎么变都不影响分类(50→48 只、多一条周度期权腿都没问题)。
    """
    ticker = df["Exchange Ticker"].astype(str).str.strip()
    name = df["Name of Securities"].astype(str).str.upper()
    is_deriv_name = name.str.contains(FUTURE_RE) | name.str.contains(OPTION_RE)
    is_eq = ticker.str.match(EQUITY_TICKER_RE) & ~is_deriv_name
    equities = df[is_eq].reset_index(drop=True)
    derivatives = df[~is_eq].reset_index(drop=True)

    # 健壮性:出现既不是期货也不是期权的"未知衍生品"(如 SWAP/BOND)时告警,
    # 以便及时发现页面新增了我们没处理的工具类型,而不是默默算错。
    unknown = []
    if not derivatives.empty:
        known = derivatives["Name of Securities"].astype(str).str.upper().str.contains(
            FUTURE_RE + "|" + OPTION_RE)
        for nm in derivatives.loc[~known, "Name of Securities"]:
            logger.warning("发现未识别的非正股工具:%s(未纳入期货/期权计算,请检查)", nm)
            unknown.append(str(nm))
    return equities, derivatives, unknown


def derive_nav(equities):
    """从正股反推基金总净值:NAV = 市值 / (净资产占比 / 100),取中位数抗异常。"""
    mv, w = "Market Value (in HKD)", "Net Assets (%)"
    if mv not in equities.columns or w not in equities.columns:
        return None
    valid = equities[(equities[w] > 0) & (equities[mv] > 0)]
    if valid.empty:
        return None
    nav = (valid[mv] / (valid[w] / 100.0)).median()
    return float(nav) if nav and nav > 0 else None


def parse_labeled_values(soup):
    """把页面上所有『两格一行』的表格行收成 {左格文本: 右格文本}。

    页面用这种行公布两个权威数字:基金总净值、HSCEI 收盘点位 —— 直接取用,
    比反推/取期权表里被四舍五入成整数的 Index Price 都准。
    """
    out = {}
    for tr in soup.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                 for c in tr.find_all(["th", "td"])]
        if len(cells) == 2 and cells[0]:
            out.setdefault(cells[0], cells[1])
    return out


def _lookup(values, keyword):
    """在 {标签: 值} 里按关键字模糊找一项(页面标签措辞偶有变化)。"""
    for k, v in values.items():
        if keyword.lower() in k.lower():
            return v
    return None


def option_legs(derivatives, index_price, as_of):
    """从完整持仓表的期权行提取每条腿 —— 这是敞口计算的**唯一权威来源**。

    为什么不用官网那张「期权敞口表」当主源:那张表和持仓表更新不同步,
    新腿(如 2026-08 新加的周度期权)可能晚一步才出现,主源用它就会静默漏算。
    持仓表则同时给出张数与期权市价(指数点),后者正好用来反解隐含波动率。

    返回 [{name, contracts, price, strike, expiry, days, problem}],张数为负=空头。
    **算不出来的腿也照样返回**,只是带上 problem 说明原因 —— 因为"少了一条腿"
    正是最危险的失效模式:悄悄跳过会让邮件里的敞口数字偏小却看不出任何异常,
    所以必须让 check_readiness 看得见、让告警条写得出。
    """
    legs = []
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

        # wait_helps=True 表示"多半是页面还没发全,等下次触发可能就好了";
        # False 表示等也没用(如出现模型不支持的看跌腿),不该白等一个宽限期。
        problem, wait_helps = None, True
        if pd.isna(contracts):
            problem = "合约张数缺失"
        elif strike is None:
            problem = "无法从名称解析出行权价"
        elif index_price and not (0.5 * index_price <= strike <= 1.5 * index_price):
            # 行权价离指数太远 → 多半是名称解析把几段数字粘错了,而不是真的深度实/虚值
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
            # 本基金是备兑开仓策略,只写看涨;真出现看跌腿,当前 Delta 公式不适用
            problem = "看跌期权,当前 Delta 模型只支持看涨"
            wait_helps = False

        if problem:
            logger.warning("期权腿无法纳入敞口计算(%s):%s", problem, nm)
        legs.append({
            "name": nm,
            "contracts": float(contracts) if pd.notna(contracts) else None,
            "price": float(price) if pd.notna(price) else None,
            "strike": float(strike) if strike is not None else None,
            "expiry": expiry,
            "days": days,
            "index": index_price,
            "problem": problem,
            "wait_helps": wait_helps,
        })
    return legs


def futures_legs(derivatives):
    """从完整持仓表的期货行提取每条腿:{name, contracts, index_pt}(张数为正=多头)。"""
    legs = []
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
        legs.append({"name": str(nm), "contracts": float(contracts),
                     "index_pt": float(index_pt)})
    return legs


def parse_holdings(html):
    """解析页面,返回结构化字典:
        {as_of, full, equities, derivatives, options, nav, nav_page,
         index_close, option_legs, futures_legs}

    说明:页面里持仓表的 id 属性重复(id=holdingsList 又 id=top-ten),
    会让某些解析器混乱;因此这里不靠 id,而是靠"表头列名"来认表 —— 更稳。
    """
    soup = BeautifulSoup(html, "html.parser")

    df = pd.DataFrame()
    options_df = pd.DataFrame()
    # 遍历所有 class 含 holdings 的表,按表头特征归类
    for t in soup.find_all("table", class_="holdings"):
        tmp = _table_to_df(t)
        if tmp.empty:
            continue
        cols = list(tmp.columns)
        if "Name of Securities" in cols and "Exchange Ticker" in cols:
            df = tmp                                   # 完整持仓表
        elif any("Option Position" in c for c in cols):
            options_df = tmp                           # 期权敞口表

    if df.empty:
        raise ValueError("找不到完整持仓表(表头应含 Name of Securities / Exchange Ticker)")

    # 列名校验
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"持仓表缺少关键列: {missing};实际列: {list(df.columns)}")

    # 持仓表数值列清洗
    for col in df.columns:
        if any(k in col for k in ["Price", "Shares", "Value", "Net Assets", "%"]):
            df[col] = _clean_num(df[col])
    df = df[df["Name of Securities"].astype(str).str.strip() != ""].reset_index(drop=True)
    if df.empty:
        raise ValueError("持仓表清洗后无有效数据行")

    # 期权敞口表数值列清洗(解析失败不影响主流程)
    if not options_df.empty:
        for col in options_df.columns:
            if any(k in col for k in ["Notional", "Strike", "Index Price",
                                      "Days", "Upside", "%"]):
                options_df[col] = _clean_num(options_df[col])

    equities, derivatives, unknown = split_equities(df)
    if equities.empty:
        raise ValueError("拆分后正股为空,持仓表代码格式可能已变化,请检查")

    # NAV:优先用页面公布的官方净值(期权敞口表的占比也是拿它算的),反推值仅作兜底/校验
    values = parse_labeled_values(soup)
    nav_page = _num1(_lookup(values, "Total Net Asset Value of the Fund"))
    nav_derived = derive_nav(equities)
    nav = nav_page or nav_derived
    if nav_page and nav_derived and abs(nav_page - nav_derived) / nav_page > 0.005:
        logger.warning("页面净值 %s 与「市值/权重」反推值 %s 相差 >0.5%%,请检查",
                       f"{nav_page:,.0f}", f"{nav_derived:,.0f}")

    index_close = _num1(_lookup(values, "Closing level of Hang Seng China Enterprises"))
    if index_close is None and not options_df.empty and "Index Price" in options_df.columns:
        idx = pd.to_numeric(options_df["Index Price"], errors="coerce").dropna()
        index_close = float(idx.iloc[0]) if not idx.empty else None   # 兜底:敞口表的整数点位

    as_of = parse_as_of_date(html)
    o_legs = option_legs(derivatives, index_close, as_of)
    f_legs = futures_legs(derivatives)
    logger.info("解析成功:总行数 %d(正股 %d / 衍生品 %d:期货 %d 腿 / 期权 %d 腿),"
                "NAV=%s(%s),指数收盘 %s,截止 %s",
                len(df), len(equities), len(derivatives), len(f_legs), len(o_legs),
                f"{nav:,.0f}" if nav else "未知", "页面公布" if nav_page else "反推",
                f"{index_close:,.2f}" if index_close else "未知", as_of)
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


# ----------------------------------------------------------------------------
# 3. 快照存档 + 找上一份用于对比
# ----------------------------------------------------------------------------
def snapshot_path(as_of):
    tag = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    return os.path.join(DATA_DIR, f"holdings_{tag}.csv")


def snapshot_meta_path(as_of):
    tag = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    return os.path.join(DATA_DIR, f"holdings_{tag}.meta.json")


def save_snapshot(df, as_of, meta=None):
    path = snapshot_path(as_of)
    df.to_csv(path, index=False, encoding="utf-8-sig")  # utf-8-sig 便于 Excel 直接打开
    logger.info("已存档: %s", path)
    # 附带一份当日汇总指标元数据,供次日的「较上一交易日差异」引用。
    # 持仓 CSV 本身不含 NAV/指数点位;旧快照没有 meta 文件时对应项显示 N/A。
    if meta is not None:
        try:
            meta_path = snapshot_meta_path(as_of)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            logger.info("已存档元数据: %s", meta_path)
        except Exception as e:
            logger.warning("元数据存档失败(忽略,不影响主流程): %s", e)
    return path


def load_previous(as_of):
    """找比当前截止日期更早的最近一份快照及其元数据,用于对比。

    返回 (DataFrame, dict 或 None);没有任何历史快照时返回 (None, None)。
    元数据缺失(旧版快照)不影响对比,只是汇总指标里 NAV/指数点位显示 N/A。
    """
    cur = snapshot_path(as_of)
    files = []
    for f in os.listdir(DATA_DIR):
        m = re.match(r"holdings_(\d{8})\.csv$", f)
        if m:
            full = os.path.join(DATA_DIR, f)
            if full != cur:
                files.append((m.group(1), full))
    if not files:
        return None, None
    files.sort()
    _, prev_path = files[-1]
    prev_meta = None
    try:
        with open(prev_path[:-4] + ".meta.json", encoding="utf-8") as f:
            prev_meta = json.load(f)
    except Exception:
        pass
    try:
        logger.info("对比基准: %s", prev_path)
        return pd.read_csv(prev_path), prev_meta
    except Exception as e:
        logger.warning("读取上一份快照失败(忽略对比): %s", e)
        return None, None


def _norm_label(s):
    return " ".join(str(s or "").split())


def _num_series(df, col):
    """按列取数值;列不存在时返回全 None 的 Series。"""
    if col not in df.columns:
        return pd.Series([None] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def diff_vs_previous(prev_full, prev_meta, cur_full, cur):
    """把当天完整持仓表与前一日快照逐项对比 —— 只列出显著不同,不对错下结论。

    对齐方式:正股按 Exchange Ticker;期货按归一化后的合约名称;
    期权按 (行权价, 到期日)(由合约名称解析;同一合约拆成多行的先合并张数)。
    换仓滚动(周度到期滚入月度)自然呈现为「一剔除一新增」,不加任何解读。

    参数:prev_full/cur_full 为完整持仓表 DataFrame;prev_meta 为前日快照元数据
    (旧快照可能没有);cur 为当天的 {nav, index_close}。
    返回结构化 dict,任何一节缺数据都安全降级(N/A / 空列表)。
    """
    diff = {
        "has_prev": prev_full is not None and not prev_full.empty,
        "equities": {"added": [], "removed": [], "weight_changes": []},
        "futures": {"added": [], "removed": [], "changed": [],
                    "rows_prev": 0, "rows_cur": 0},
        "options": {"added": [], "removed": [], "changed": [],
                    "rows_prev": 0, "rows_cur": 0,
                    "contracts_abs_prev": None, "contracts_abs_cur": None},
        "metrics": [],
    }
    if cur_full is None or cur_full.empty or "Name of Securities" not in cur_full.columns:
        return diff
    prev_eq = prev_deriv = None
    if diff["has_prev"] and "Name of Securities" in prev_full.columns:
        prev_eq, prev_deriv, _ = split_equities(prev_full)
    cur_eq, cur_deriv, _ = split_equities(cur_full)

    # ---- 正股 ----
    def equity_map(eq):
        out = {}
        if eq is None or eq.empty:
            return out
        wts = _num_series(eq, "Net Assets (%)")
        for (_, r), wt in zip(eq.iterrows(), wts):
            key = str(r.get("Exchange Ticker", "")).strip()
            if key:
                out[key] = {"name": str(r.get("Name of Securities")),
                            "weight": None if pd.isna(wt) else float(wt)}
        return out

    emap_cur, emap_prev = equity_map(cur_eq), equity_map(prev_eq)
    eq = diff["equities"]
    for k in sorted(emap_cur.keys() - emap_prev.keys()):
        v = emap_cur[k]
        eq["added"].append((v["name"], k, v["weight"]))
    for k in sorted(emap_prev.keys() - emap_cur.keys()):
        v = emap_prev[k]
        eq["removed"].append((v["name"], k, v["weight"]))
    for k in sorted(set(emap_cur) & set(emap_prev)):
        pw, cw = emap_prev[k]["weight"], emap_cur[k]["weight"]
        if pw is not None and cw is not None and abs(cw - pw) >= WEIGHT_DIFF_TOL_PP:
            eq["weight_changes"].append(
                {"name": emap_cur[k]["name"], "ticker": k,
                 "prev": pw, "cur": cw, "delta": cw - pw})
    eq["weight_changes"].sort(key=lambda d: abs(d["delta"]), reverse=True)

    # ---- 衍生品(期货/期权):先归一成 {对齐键: 行},再比集合 ----
    def deriv_rows(d):
        out = {}
        if d is None or d.empty or "Name of Securities" not in d.columns:
            return out
        cons = _num_series(d, "Number of Shares Held")
        pxs = _num_series(d, "Market Price (in HKD)")
        for (_, r), c, px in zip(d.iterrows(), cons, pxs):
            nm = _norm_label(r.get("Name of Securities"))
            if not nm:
                continue
            con = None if pd.isna(c) else float(c)
            price = None if pd.isna(px) else float(px)
            if is_option(nm):
                strike, expiry = parse_option_name(nm)
                key = ("OPT", strike, str(expiry))
            elif is_future(nm):
                key = ("FUT", nm)
            else:
                key = ("UNK", nm)
            if key in out:                      # 同一合约拆成多行的,合并张数
                base = out[key]
                base["contracts"] = ((base["contracts"] or 0.0)
                                     + (con or 0.0)) if con is not None else base["contracts"]
            else:
                out[key] = {"label": nm, "contracts": con, "price": price}
        return out

    dmap_prev, dmap_cur = deriv_rows(prev_deriv), deriv_rows(cur_deriv)
    for kind, sec in (("FUT", "futures"), ("OPT", "options")):
        kp = {k for k in dmap_prev if k[0] == kind}
        kc = {k for k in dmap_cur if k[0] == kind}
        s = diff[sec]
        s["rows_prev"], s["rows_cur"] = len(kp), len(kc)
        for k in sorted(kc - kp, key=str):
            s["added"].append(dict(dmap_cur[k]))
        for k in sorted(kp - kc, key=str):
            s["removed"].append(dict(dmap_prev[k]))
        for k in sorted(kp & kc, key=str):
            pc, cc = dmap_prev[k]["contracts"], dmap_cur[k]["contracts"]
            if pc != cc:                        # 任一侧缺失或数值不同都算变化
                s["changed"].append({"label": dmap_cur[k]["label"], "prev": pc, "cur": cc})

    def abs_contracts(dmap, kind):
        vals = [v["contracts"] for k, v in dmap.items()
                if k[0] == kind and v["contracts"] is not None]
        return float(sum(abs(v) for v in vals)) if vals else None

    diff["options"]["contracts_abs_prev"] = abs_contracts(dmap_prev, "OPT")
    diff["options"]["contracts_abs_cur"] = abs_contracts(dmap_cur, "OPT")

    # ---- 汇总指标(前日值来自快照元数据,旧快照没有则为 N/A)----
    def mv_sum(eqd):
        if eqd is None or eqd.empty:
            return None
        s = _num_series(eqd, "Market Value (in HKD)").dropna()
        return float(s.sum()) if not s.empty else None

    def fut_notional(dmap):
        tot, seen = 0.0, False
        for k, v in dmap.items():
            if (k[0] == "FUT" and v["contracts"] is not None
                    and v.get("price") is not None):
                tot += v["contracts"] * v["price"] * INDEX_MULTIPLIER
                seen = True
        return tot if seen else None

    pm = prev_meta if isinstance(prev_meta, dict) else {}
    diff["metrics"] = [
        {"label": "基金净值 NAV(HKD)", "prev": pm.get("nav"),
         "cur": cur.get("nav"), "fmt": ",.0f"},
        {"label": "HSCEI 收盘", "prev": pm.get("index_close"),
         "cur": cur.get("index_close"), "fmt": ",.2f"},
        {"label": "正股市值合计(HKD)", "prev": mv_sum(prev_eq),
         "cur": mv_sum(cur_eq), "fmt": ",.0f"},
        {"label": "期货名义合计(HKD)", "prev": fut_notional(dmap_prev),
         "cur": fut_notional(dmap_cur), "fmt": ",.0f"},
        {"label": "期权空头总张数(绝对值)", "prev": diff["options"]["contracts_abs_prev"],
         "cur": diff["options"]["contracts_abs_cur"], "fmt": ",.0f"},
    ]
    return diff


# ----------------------------------------------------------------------------
# 4. 仓位计算
# ----------------------------------------------------------------------------
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_price(S, K, T, sigma, r=RISK_FREE):
    """Black-Scholes 看涨期权理论价(与期权市价同为「指数点」口径)。"""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_call_delta(S, K, T, sigma, r=RISK_FREE):
    """Black-Scholes 看涨期权 Delta = N(d1)。

    边界:到期(T<=0)或波动率<=0 时退化为内在价值判断(实值=1,虚值=0)。
    注意这个退化分支只应出现在「当天到期」;若页面残留已过期合约,
    调用方须先剔除,否则 Delta 会被当成 1.0 让「Delta 调整后」≈ 名义值。
    """
    if not (S and K) or S <= 0 or K <= 0:
        return None
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1)


def implied_vol(price, S, K, T, r=RISK_FREE, lo=1e-4, hi=5.0):
    """由期权市价反解隐含波动率(二分法;BS 价格对 sigma 单调递增)。

    这是「Delta 调整后」这一格准不准的关键:持仓表已经给了期权自己的市价
    (Market Price 就是指数点报价,乘 50 即合约价值),没有任何理由再去假设一个 IV。
    价格越界(低于内在价值 / 高于 sigma=hi 的理论价)时返回 None,由调用方退回假设值。
    """
    if price is None or S is None or K is None or T is None:
        return None
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(S - K * math.exp(-r * T), 0.0)
    if price <= intrinsic + 1e-9 or price >= bs_call_price(S, K, T, hi, r):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_call_price(S, K, T, mid, r) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _option_exposure_from_page(opt, pct_col="Notional Exposure to NAV (%)"):
    """官网「期权敞口表」的名义占比合计 —— 只用于和持仓表算出来的结果交叉校验。"""
    if opt.empty or pct_col not in opt.columns:
        return None
    vals = pd.to_numeric(opt[pct_col], errors="coerce").dropna()
    return float(vals.sum()) if not vals.empty else None


def compute_positions(data):
    """算三个『真实仓位』+ 指数现价到行权价的距离。返回普通 dict,值缺失记为 None。

    期货/期权都以完整持仓表的合约行为准(见 option_legs/futures_legs 的说明),
    期权 Delta 用每条腿自己的市价反解 IV,再算 N(d1)。
    """
    nav = data["nav"]
    eq = data["equities"]
    as_of = data["as_of"]
    S = data.get("index_close")
    res = {"nav": nav, "index_close": S}

    # (1) 正股敞口 = 总正股市值 / NAV
    mv = "Market Value (in HKD)"
    eq_value = float(eq[mv].sum()) if mv in eq.columns else None
    res["equity_value"] = eq_value
    res["equity_pct"] = (eq_value / nav * 100) if (nav and eq_value is not None) else None

    # (2) 期货多头敞口 = Σ(合约张数 × 指数点 × 50) / NAV
    f_legs = data.get("futures_legs") or []
    fut_notional = sum(l["contracts"] * l["index_pt"] * INDEX_MULTIPLIER for l in f_legs)
    res["futures_notional"] = fut_notional if f_legs else None
    res["futures_pct"] = (fut_notional / nav * 100) if (nav and f_legs) else None
    res["futures_rows"] = f_legs

    # (3) 期权敞口:名义 = Σ(张数 × 指数 × 50)/NAV;Delta 调整 = Σ(名义占比 × N(d1))
    opt_notional_pct = 0.0
    opt_pressure_pct = 0.0
    opt_rows, dropped = [], []
    iv_used, iv_weight = 0.0, 0.0
    for leg in (data.get("option_legs") or []):
        K, days, px = leg["strike"], leg["days"], leg["price"]
        problem, wait_helps = leg.get("problem"), leg.get("wait_helps", True)
        if not problem and not (nav and S):
            problem = "缺少基金净值或指数收盘点位"
        if problem:
            # 丢腿必须留痕:调用方会把它变成邮件里的告警条,而不是让敞口悄悄算小
            dropped.append({"name": leg["name"], "reason": problem, "wait_helps": wait_helps})
            continue
        notional_pct = leg["contracts"] * S * INDEX_MULTIPLIER / nav * 100   # 张数为负 → 空头为负
        T = days / 365.0                       # days 已由 option_legs 保证非负且非 None
        iv = implied_vol(px, S, K, T)
        sigma, iv_src = (iv, "市价反解") if iv else (IMPLIED_VOL, "假设值")
        if iv is None and days > 0:
            # days==0 是到期日当天,内在价值即为准确 Delta,反解不出来属正常,不必告警
            logger.warning("期权市价(%s)反解 IV 失败,退回假设值 %.0f%%:%s",
                           px, IMPLIED_VOL * 100, leg["name"])
        delta = bs_call_delta(S, K, T, sigma)
        if delta is None:
            dropped.append({"name": leg["name"], "reason": "Delta 计算失败", "wait_helps": False})
            continue
        contrib = notional_pct * delta
        opt_notional_pct += notional_pct
        opt_pressure_pct += contrib
        iv_used += sigma * abs(notional_pct)
        iv_weight += abs(notional_pct)
        opt_rows.append({
            "pos": leg["name"], "strike": K, "expiry": leg["expiry"], "days": days,
            "price": px, "iv": sigma, "iv_src": iv_src, "delta": delta,
            "contracts": leg["contracts"], "notional_pct": notional_pct, "contrib": contrib,
        })

    res["option_notional_pct"] = opt_notional_pct if opt_rows else None   # 名义口径(未调整)
    res["option_pressure_pct"] = opt_pressure_pct if opt_rows else None   # Delta 调整后
    res["option_rows"] = opt_rows
    res["dropped_legs"] = dropped
    res["iv_avg"] = (iv_used / iv_weight) if iv_weight else None
    res["iv_all_from_market"] = bool(opt_rows) and all(r["iv_src"] == "市价反解" for r in opt_rows)

    # 官网「期权敞口表」与持仓表是同一页面的两个来源,这里只把两个数字并排列出
    # 供人工对照,不设阈值、不下"漏腿/不符"的结论;两个数都会随日报正文发出。
    page_pct = _option_exposure_from_page(data["options"])
    res["option_notional_pct_page"] = page_pct
    res["cross_check_gap"] = None
    if page_pct is not None and opt_rows:
        res["cross_check_gap"] = abs(page_pct - opt_notional_pct)
        logger.info("期权名义敞口两来源并列:持仓表逐腿加总 %.2f%% vs 官网敞口表 %.2f%%",
                    opt_notional_pct, page_pct)

    # (a) 指数现价到行权价的距离(按 行权价+到期日 去重)
    dists, seen = [], set()
    for r in opt_rows:
        key = (round(r["strike"], 2), r["expiry"])
        if key in seen:
            continue
        seen.add(key)
        dists.append({"index": S, "strike": r["strike"], "days": r["days"],
                      "dist_pct": (r["strike"] - S) / S * 100,
                      "iv": r["iv"], "delta": r["delta"]})
    dists.sort(key=lambda d: (d["days"] if d["days"] is not None else 10 ** 6))
    res["strike_distances"] = dists
    return res


# ----------------------------------------------------------------------------
# 5. 生成邮件正文
# ----------------------------------------------------------------------------
def build_summary(data, diff, positions=None, blocking=None, warnings=None,
                  update_mode=False):
    """生成 (主题, HTML 正文)。blocking/warnings 非空时在正文顶部挂告警条。"""
    blocking, warnings = list(blocking or []), list(warnings or [])
    eq = data["equities"]
    as_of = data["as_of"]
    weight_col, name_col, key_col = "Net Assets (%)", "Name of Securities", "Exchange Ticker"

    # ---- a. 前5大重仓 ----
    top = eq.sort_values(weight_col, ascending=False).head(5)
    top5_pct = float(top[weight_col].sum())
    top_rows = "".join(
        f"<tr><td>{i}</td><td>{r[name_col]}</td><td>{r[key_col]}</td>"
        f"<td style='text-align:right'>{r[weight_col]:.2f}%</td></tr>"
        for i, (_, r) in enumerate(top.iterrows(), 1)
    )

    # ---- b. 三个真实仓位(main 已经算过一遍并据此判过就绪,这里直接复用同一份结果)----
    p = positions if positions is not None else compute_positions(data)

    def pct(v):
        return "N/A" if v is None else f"{v:.2f}%"

    # 三列:名义口径 / Delta调整后 / 算法。正股、期货无需 Delta 调整,故标 "—"。
    nav_note = ("NAV 取自页面公布的基金总净值" if data.get("nav_page")
                else "NAV 由「市值/权重」反推(页面未公布)")
    iv_desc = (f"IV {p['iv_avg']:.1%} 由期权市价反解" if p.get("iv_all_from_market")
               else (f"IV≈{p['iv_avg']:.1%},部分腿反解失败退回假设 {IMPLIED_VOL:.0%}"
                     if p.get("iv_avg") else f"IV={IMPLIED_VOL:.0%}(假设)"))
    pos_rows = (
        f"<tr><td>正股敞口</td><td style='text-align:right'>{pct(p['equity_pct'])}</td>"
        f"<td style='text-align:right'>—</td><td>≈ 总正股市值 / 基金净值</td></tr>"
        f"<tr><td>期货多头敞口</td><td style='text-align:right'>{pct(p['futures_pct'])}</td>"
        f"<td style='text-align:right'>—</td><td>合约张数 × 指数点 × {INDEX_MULTIPLIER:.0f} / 净值</td></tr>"
        f"<tr><td>期权空头敞口</td><td style='text-align:right'>{pct(p['option_notional_pct'])}</td>"
        f"<td style='text-align:right'>{pct(p['option_pressure_pct'])}</td>"
        f"<td>名义 = Σ(张数 × 指数 × {INDEX_MULTIPLIER:.0f}) / 净值;"
        f"调整 = 名义 × Delta({iv_desc})</td></tr>"
    )
    net_directional = None
    if None not in (p["equity_pct"], p["futures_pct"], p["option_pressure_pct"]):
        net_directional = p["equity_pct"] + p["futures_pct"] + p["option_pressure_pct"]
    net_html = ("" if net_directional is None else
                f"<p><b>净方向性敞口(正股+期货+期权Delta)≈ {net_directional:.2f}%</b></p>")

    # ---- c. 指数现价 → 行权价距离 ----
    dist_rows = "".join(
        f"<tr><td style='text-align:right'>{d['index']:,.0f}</td>"
        f"<td style='text-align:right'>{d['strike']:,.0f}</td>"
        f"<td style='text-align:right'>{d['dist_pct']:+.2f}%</td>"
        f"<td style='text-align:right'>{d['days'] if d['days'] is not None else 'N/A'}</td></tr>"
        for d in p["strike_distances"]
    ) or "<tr><td colspan=4>无期权数据</td></tr>"

    # ---- 较上一交易日差异(只列事实,不下对错结论)----
    def _fmt_val(v, fmt):
        return "N/A" if v is None else format(v, fmt)

    def _fmt_contracts(v):
        if v is None:
            return "—"
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"

    changes_html = "<p>首次运行,无历史快照可对比。</p>"
    if diff and diff.get("has_prev"):
        eq_d, fu_d, op_d = diff["equities"], diff["futures"], diff["options"]

        def names(items):
            if not items:
                return "无"
            return ", ".join(f"{n}({k})" for n, k, *_ in items)

        metric_rows = "".join(
            f"<tr><td>{m['label']}</td>"
            f"<td style='text-align:right'>{_fmt_val(m['prev'], m['fmt'])}</td>"
            f"<td style='text-align:right'>{_fmt_val(m['cur'], m['fmt'])}</td></tr>"
            for m in diff["metrics"]
        )
        wc_rows = "".join(
            f"<tr><td>{d['name']}</td><td>{d['ticker']}</td>"
            f"<td style='text-align:right'>{d['prev']:.2f}%</td>"
            f"<td style='text-align:right'>{d['cur']:.2f}%</td>"
            f"<td style='text-align:right'>{d['delta']:+.2f}%</td></tr>"
            for d in eq_d["weight_changes"]
        ) or f"<tr><td colspan=5>权重变化均 &lt; {WEIGHT_DIFF_TOL_PP:.2f}pp</td></tr>"

        def leg_rows(added, removed, changed):
            rows = ([(d["label"], "—", _fmt_contracts(d.get("contracts")), "新增")
                     for d in added]
                    + [(d["label"], _fmt_contracts(d.get("contracts")), "—", "剔除")
                       for d in removed]
                    + [(d["label"], _fmt_contracts(d["prev"]),
                        _fmt_contracts(d["cur"]), "变化") for d in changed])
            return "".join(
                f"<tr><td>{lb}</td><td style='text-align:right'>{pv}</td>"
                f"<td style='text-align:right'>{cv}</td><td>{tag}</td></tr>"
                for lb, pv, cv, tag in rows
            ) or "<tr><td colspan=4>无变化</td></tr>"

        changes_html = (
            "<p><b>汇总指标:</b></p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            "<tr><th>指标</th><th>前日</th><th>当日</th></tr>"
            f"{metric_rows}</table>"
            f"<p><b>正股:</b>新增 {len(eq_d['added'])} 只({names(eq_d['added'])});"
            f"剔除 {len(eq_d['removed'])} 只({names(eq_d['removed'])})</p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            f"<tr><th>权重变化(≥{WEIGHT_DIFF_TOL_PP:.2f}pp)</th><th>代码</th>"
            "<th>前日</th><th>当日</th><th>变化</th></tr>"
            f"{wc_rows}</table>"
            f"<p><b>期货合约行:{fu_d['rows_prev']} → {fu_d['rows_cur']} 条</b></p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            "<tr><th>合约</th><th>前日张数</th><th>当日张数</th><th>备注</th></tr>"
            f"{leg_rows(fu_d['added'], fu_d['removed'], fu_d['changed'])}</table>"
            f"<p><b>期权合约行:{op_d['rows_prev']} → {op_d['rows_cur']} 条;"
            f"空头总张数(绝对值){_fmt_contracts(op_d['contracts_abs_prev'])} → "
            f"{_fmt_contracts(op_d['contracts_abs_cur'])}</b></p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            "<tr><th>合约(按行权价/到期日对齐)</th><th>前日张数</th>"
            "<th>当日张数</th><th>备注</th></tr>"
            f"{leg_rows(op_d['added'], op_d['removed'], op_d['changed'])}</table>"
        )

    # ---- 顶部告警条:拦截项(红) / 需注意(黄) / 补发更新版(绿)----
    def _bar(border, bg, fg, title, items, tail=""):
        li = "".join(f"<li>{i}</li>" for i in items)
        return (f"<div style='border:2px solid {border};background:{bg};color:{fg};"
                f"padding:8px 12px;margin-bottom:12px'><b>{title}</b>"
                f"<ul style='margin:6px 0 0 18px;padding:0'>{li}</ul>{tail}</div>")

    banner = ""
    if blocking:
        banner = _bar("#c00", "#fff4f4", "#c00",
                      "⚠️ 以下项目缺失或无法计算,相关口径未纳入本报告",
                      blocking + warnings,
                      "<div style='margin-top:6px'>若后续数据补齐,将自动补发一封「【更新】」版。</div>")
    else:
        if update_mode:
            banner = ("<div style='border:2px solid #0a0;background:#f3fff3;color:#070;"
                      "padding:8px 12px;margin-bottom:12px'><b>✅ 更新版</b> —— "
                      "此前一封因部分项目缺失而先行兜底发出;现相关数据已可完整计算,"
                      "本封为其替代版。</div>")
        if warnings:
            banner += _bar("#c80", "#fffbf0", "#a60", "⚠️ 以下情况请留意", warnings)

    as_of_str = as_of.strftime("%Y-%m-%d") if as_of else "未知"
    # 官网两个来源的期权名义敞口并排列出,只给数字不下结论。
    cross_note = ""
    if p.get("option_notional_pct_page") is not None and p.get("option_notional_pct") is not None:
        cross_note = (f"期权名义敞口两来源并列供对照:持仓表逐腿加总 "
                      f"{p['option_notional_pct']:.2f}% / 官网敞口表 "
                      f"{p['option_notional_pct_page']:.2f}%。<br>")
    body = f"""\
<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;font-size:14px">
{banner}
<p><b>持仓截止日期:</b>{as_of_str}</p>

<h3>a. 指数现价 → 行权价距离</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>指数现价</th><th>行权价</th><th>距离</th><th>剩余天数</th></tr>
{dist_rows}
</table>

<h3>b. 风险暴露</h3>
{net_html}
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>仓位</th><th>名义占净值</th><th>Delta调整后</th><th>算法</th></tr>
{pos_rows}
</table>

<h3>c. 前五大重仓占比 {top5_pct:.2f}%</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>#</th><th>名称</th><th>代码</th><th>权重</th></tr>
{top_rows}
</table>

<h3>较上一交易日差异</h3>
{changes_html}

<p style="color:#888;font-size:12px">
注:Delta = Black-Scholes N(d1),{iv_desc};期货/期权敞口取自完整持仓表的合约行
(名义 = 张数 × 指数 × {INDEX_MULTIPLIER:.0f}),{nav_note}。完整持仓见附件 CSV。<br>
{cross_note}数据来源:{FUND_URL}<br>本邮件由脚本自动生成于 {datetime.now():%Y-%m-%d %H:%M:%S}。</p>
</body></html>"""

    as_of_compact = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    prefix = "【待补全】" if blocking else ("【更新】" if update_mode else "")
    subject = f"{prefix}{as_of_compact} {FUND_NAME} 持仓"
    return subject, body


# ----------------------------------------------------------------------------
# 5. 发邮件
# ----------------------------------------------------------------------------
def send_email(subject, html_body, attachments=None):
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        raise RuntimeError("邮件配置不全(SMTP_USER / SMTP_PASS / MAIL_TO)")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(MAIL_TO)
    msg.set_content("本邮件为 HTML 格式,请使用支持 HTML 的客户端查看。")
    msg.add_alternative(html_body, subtype="html")

    for path in (attachments or []):
        try:
            with open(path, "rb") as f:
                data = f.read()
            msg.add_attachment(data, maintype="text", subtype="csv",
                               filename=os.path.basename(path))
        except Exception as e:
            logger.warning("附件添加失败(跳过)%s: %s", path, e)

    last_err = None
    for attempt in range(1, SMTP_RETRIES + 1):
        try:
            logger.info("发送邮件(第 %d 次)-> %s", attempt, MAIL_TO)
            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                                      context=ssl.create_default_context(),
                                      timeout=SMTP_TIMEOUT) as s:
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
            logger.info("邮件发送成功")
            return
        except Exception as e:
            last_err = e
            logger.warning("发送失败: %s", e)
            if attempt < SMTP_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"邮件最终发送失败: {last_err}")


def send_failure_alert(error_text):
    """发一封失败告警;按天去重(高频触发时官网长时间故障也只发一封),发不出去不再抛异常。"""
    today = date.today().isoformat()
    if not FORCE_SEND and read_state().get("last_alert_date") == today:
        logger.info("今日已发过失败告警,本次不重复发(避免高频触发刷屏)")
        return
    try:
        send_email(
            subject=f"[ETF日报-失败] {FUND_NAME} {today}",
            html_body=f"<p>每日持仓任务执行失败:</p><pre>{error_text}</pre>",
        )
        write_state(last_alert_date=today)   # 标记今天已告警
    except Exception as e:
        logger.error("连失败告警都发不出去: %s", e)


# ----------------------------------------------------------------------------
# 状态文件(幂等去重 + 告警去重)
# ----------------------------------------------------------------------------
def STATE_FILE():
    return os.path.join(DATA_DIR, "state.json")


def read_state():
    try:
        with open(STATE_FILE(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_state(**updates):
    """读-改-写,只更新传入的字段,不覆盖其它键(如 last_as_of 与 last_alert_date 互不影响)。"""
    st = read_state()
    st.update(updates)
    with open(STATE_FILE(), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def already_sent(as_of):
    """同一持仓截止日期只发一次 —— 这是高频触发不刷屏的关键。"""
    if as_of is None:
        return False
    return read_state().get("last_as_of") == as_of.isoformat()


def mark_sent(as_of, complete=True):
    """记下"这个截止日已发过",并记录当时数据是否完整(不完整则允许后续补发一次更新版)。"""
    if as_of is None:
        return
    write_state(last_as_of=as_of.isoformat(), sent_at=datetime.now().isoformat(),
                last_complete=bool(complete),
                pending_as_of=None, pending_since=None)   # 清掉等待计时,免得跨天残留


# ----------------------------------------------------------------------------
# 数据就绪判定 —— 只看「计算前提」与「整类工具相对前日消失」两类硬事实
# ----------------------------------------------------------------------------
def check_readiness(data, pos, diff):
    """判断本次数据能否直接出报告。返回 (blocking, warnings, wait_worth)。

    这里**不推断原始数据的对错**:与前一日快照的一切数量差异(腿数增减、
    换仓滚动、张数变化……)由 diff_vs_previous 中性列出、直接进邮件,
    既不拦截也不告警。只有两类事实会拦(blocking):

      1. 计算前提缺失 —— 某条腿无法纳入敞口计算、缺指数点位导致期权敞口算不出。
         这是"算不出来",不是"数据错了";
      2. 整类工具相对前一日消失(如昨日有期权合约行、今日为 0)。官网存在
         分阶段发布(先放正股、稍后补衍生品行),此时等一等大概率能等到全量。

    blocking —— 拦截后走宽限等待/兜底发送,并允许数据补齐后自动补发【更新】版。
    warnings —— 黄色告警条的事实性提示,不影响幂等。
    wait_worth —— blocking 里是否至少有一项可能"等一等就好"(分阶段发布);
                  否则(如官网彻底改版)白等没意义,直接发带提示的报告。

    关键:入参 pos 是 compute_positions 的结果。哪条腿因什么原因没算进去,
    只有它知道 —— 这些信号必须走到邮件里,光写 run.log 等于没写(CI 的日志随
    runner 一起销毁,而且 data/run.log 在 .gitignore 里)。
    """
    blocking, warnings, waitable = [], [], []

    def block(msg, wait=True):
        blocking.append(msg)
        waitable.append(wait)

    o_legs = data.get("option_legs") or []
    f_legs = data.get("futures_legs") or []

    # 整类工具相对前日消失:唯一基于数据量的拦截条件,文案只陈述差异本身。
    if diff and diff["options"].get("rows_prev") and not o_legs:
        block(f"较前一日:期权合约行 {diff['options']['rows_prev']}→0 条;"
              f"若为官网分阶段发布,稍后重试即可")
    if diff and diff["futures"].get("rows_prev") and not f_legs:
        block(f"较前一日:期货合约行 {diff['futures']['rows_prev']}→0 条;"
              f"若为官网分阶段发布,稍后重试即可")
    if o_legs and data.get("index_close") is None:
        block(f"未取到 HSCEI 收盘点位,{len(o_legs)} 条期权腿的敞口无法计算")

    # 被丢掉的腿:每一条都会让敞口偏小,必须显式列出来。
    # 一律算 blocking(否则会被当成"可完整计算",修好后再也不会补发);
    # wait_helps 只决定"要不要先等一等",不决定要不要拦截。
    for d in (pos.get("dropped_legs") or []):
        block(f"期权腿未计入敞口计算({d['reason']}):{d['name']}", wait=d.get("wait_helps", True))

    # 事实性提示(黄色条,不拦):两个来源的数字对照见正文注脚。
    if o_legs and data["options"].empty:
        warnings.append("官网「期权敞口表」缺失,敞口已由完整持仓表计算")
    if data.get("nav_page") is None:
        warnings.append("页面未公布基金总净值,已改用「市值 ÷ 权重」反推,数值可能略有偏差")
    for nm in (data.get("unknown_instruments") or []):
        warnings.append(f"发现既非期货也非期权的未识别工具,未纳入敞口计算:{nm}")

    return blocking, warnings, any(waitable)


def note_not_ready(as_of):
    """记录某个截止日首次被判定「数据未就绪」的时刻,返回已等待的小时数。

    时间戳一律用带时区的 UTC:本地(港时)手动跑一次、CI(UTC)再跑,
    naive 时间相减会得到负数,宽限期就永远等不完。
    """
    key = as_of.isoformat() if as_of else "unknown"
    st = read_state()
    since = st.get("pending_since") if st.get("pending_as_of") == key else None
    if not since:
        since = datetime.now(timezone.utc).isoformat()
        write_state(pending_as_of=key, pending_since=since)
    try:
        t0 = datetime.fromisoformat(since)
        if t0.tzinfo is None:                       # 兼容旧版写下的 naive 时间戳
            t0 = t0.astimezone()
        waited = (datetime.now(timezone.utc) - t0).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return max(waited, 0.0)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    setup_logging()
    logger.info("===== 任务开始 =====")
    try:
        html = fetch_page(FUND_URL)
        data = parse_holdings(html)
        as_of = data["as_of"]
        if as_of is None:
            # 幂等、快照命名、宽限计时全都以截止日期为键;它一旦为 None,去重就彻底失效,
            # 每 10 分钟一次的 cron 会把同一封错误日报发上几十遍。宁可走失败告警(按天去重)。
            raise ValueError("无法从页面解析出持仓截止日期(As of ...),疑似官网改版")

        st = read_state()
        last = st.get("last_as_of")
        if last and as_of < date.fromisoformat(last) and not FORCE_SEND:
            # 截止日期倒退 = 页面在更新中途 / 解析取错了那条 "As of";发出去就是一份过期日报
            logger.warning("页面截止日期 %s 早于已发送的 %s,判为页面异常,跳过本次", as_of, last)
            return 0

        prev_full, prev_meta = load_previous(as_of)
        diff = diff_vs_previous(prev_full, prev_meta, data["full"],
                                {"nav": data["nav"], "index_close": data["index_close"]})
        pos = compute_positions(data)          # 先算,因为丢腿结果要参与就绪判定
        blocking, warnings, wait_worth = check_readiness(data, pos, diff)
        ready = not blocking
        for w in warnings:
            logger.warning("数据告警(不阻断发信):%s", w)

        sent_before = already_sent(as_of)
        # 默认取 False:旧版 state.json 里没有 last_complete 这个键,
        # 而那份 state 恰恰就是 2026-07-31 那封残缺日报留下的 —— 默认 True 会让它永远补不上。
        sent_incomplete = sent_before and not st.get("last_complete", False)

        # 补发:上次发的是带【待补全】的兜底版,现在数据齐了 → 再发一封【更新】版(每个截止日只补一次)
        update_mode = sent_incomplete and ready
        if sent_before and not update_mode and not FORCE_SEND:
            logger.info("截止日期 %s 已发送过(可完整计算=%s),跳过(可设 FORCE_SEND=1 强制发送)",
                        as_of, st.get("last_complete", False))
            return 0

        if not ready and not update_mode:
            if not wait_worth:
                logger.warning("存在无法等待解决的拦截项(%s);直接发一封带提示的日报,"
                               "并留下 last_complete=False 以便修好后补发", ";".join(blocking))
            else:
                waited = note_not_ready(as_of)
                if waited < INCOMPLETE_GRACE_HOURS and not FORCE_SEND:
                    logger.warning("存在拦截项(%s);已等待 %.1fh < 宽限 %.1fh,本次不发信、"
                                   "不写幂等标记,等下次触发", ";".join(blocking), waited,
                                   INCOMPLETE_GRACE_HOURS)
                    return 0
                logger.warning("拦截项仍未消除(%s);已超过 %.1fh 宽限,先发一封【待补全】日报兜底,"
                               "补齐后会自动补发更新版", ";".join(blocking), INCOMPLETE_GRACE_HOURS)

        meta = {"as_of": as_of.isoformat(), "nav": data["nav"],
                "nav_page": data["nav_page"], "index_close": data["index_close"]}
        path = save_snapshot(data["full"], as_of, meta=meta)   # 附件存完整持仓(含衍生品),与官网导出一致
        subject, body = build_summary(data, diff, positions=pos, blocking=blocking,
                                      warnings=warnings, update_mode=update_mode)
        send_email(subject, body, attachments=[path])
        mark_sent(as_of, complete=ready)
        logger.info("===== 任务成功(%s)=====",
                    "更新版补发" if update_mode else ("可完整计算" if ready else "有拦截项,已带提示发送"))
        return 0
    except Exception as e:
        logger.exception("任务失败: %s", e)
        send_failure_alert(str(e))
        logger.info("===== 任务失败 =====")
        return 1


if __name__ == "__main__":
    sys.exit(main())
