#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global X HSCEI Covered Call Active ETF —— 每日持仓抓取 + 邮件日报

流程:
  1. 抓取基金页面 HTML(requests,带超时/重试/内容校验)
  2. 解析持仓表(拆正股/衍生品)+ 期权敞口表,并由「市值 ÷ 权重」反推基金净值 NAV
  3. 计算邮件正文:
     a. 前五大重仓(标题含合计占比)
     b. 风险暴露:正股敞口 / 期货多头敞口 / 期权空头敞口
        (期权同列「名义口径」与「Delta 调整后」两个值)
     c. 指数现价 → 行权价距离
     并与上一份快照对比(新增/剔除/权重变化,仅正股)
  4. 通过 SMTP 发邮件(主题「YYYYMMDD <FUND_NAME> 持仓」+ HTML 正文 + 完整持仓 CSV 附件)
  5. 全程日志;任一步失败发"失败告警"

幂等:按页面上的「持仓截止日期」去重 —— 同一截止日期只发一次。
      这让脚本可以被高频触发(如每 10 分钟):没更新就跳过,更新了就发一封。
      调试可设 FORCE_SEND=1 强制发送。

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
from datetime import datetime, date

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

HTTP_TIMEOUT = 30          # 秒,连接+读取
HTTP_RETRIES = 3           # 下载失败重试次数
SMTP_RETRIES = 3           # 发信失败重试次数

# 仓位计算参数
INDEX_MULTIPLIER = 50.0    # HSCEI 期货/期权合约乘数:每点 HKD$50(页面脚注确认)
IMPLIED_VOL = float(env("IMPLIED_VOL", "0.22"))  # 估算 Delta 用的假设年化波动率(无市场 IV,只能假设)
RISK_FREE = 0.0            # 短端无风险利率,近似取 0

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


def split_equities(df):
    """把完整持仓表拆成『正股』和『衍生品行(期货/期权)』。

    正股:Exchange Ticker 形如 `939 HK`(纯数字 + HK);衍生品:期货(HCM6)、
    期权(名称含 CALL/PUT/FUTURE 或空代码)等。靠『格式/关键词』判断而非行数,
    所以正股或衍生品数量怎么变都不影响分类(50→48 只、多一个期货腿都没问题)。
    """
    ticker = df["Exchange Ticker"].astype(str).str.strip()
    name = df["Name of Securities"].astype(str).str.upper()
    is_eq = ticker.str.match(r"^\d+\s*HK$") & ~name.str.contains(r"CALL|PUT|FUTURE")
    equities = df[is_eq].reset_index(drop=True)
    derivatives = df[~is_eq].reset_index(drop=True)

    # 健壮性:出现既不是期货也不是期权的"未知衍生品"(如 SWAP/BOND)时告警,
    # 以便及时发现页面新增了我们没处理的工具类型,而不是默默算错。
    if not derivatives.empty:
        known = derivatives["Name of Securities"].astype(str).str.upper().str.contains(
            r"CALL|PUT|FUTURE")
        for nm in derivatives.loc[~known, "Name of Securities"]:
            logger.warning("发现未识别的非正股工具:%s(未纳入期货/期权计算,请检查)", nm)
    return equities, derivatives


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


def parse_holdings(html):
    """解析页面,返回结构化字典:
        {as_of, full, equities, derivatives, options, nav}

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

    equities, derivatives = split_equities(df)
    if equities.empty:
        raise ValueError("拆分后正股为空,持仓表代码格式可能已变化,请检查")
    nav = derive_nav(equities)

    as_of = parse_as_of_date(html)
    logger.info("解析成功:总行数 %d(正股 %d / 衍生品 %d),NAV≈%s,截止 %s",
                len(df), len(equities), len(derivatives),
                f"{nav:,.0f}" if nav else "未知", as_of)
    return {
        "as_of": as_of,
        "full": df,
        "equities": equities,
        "derivatives": derivatives,
        "options": options_df,
        "nav": nav,
    }


# ----------------------------------------------------------------------------
# 3. 快照存档 + 找上一份用于对比
# ----------------------------------------------------------------------------
def snapshot_path(as_of):
    tag = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    return os.path.join(DATA_DIR, f"holdings_{tag}.csv")


def save_snapshot(df, as_of):
    path = snapshot_path(as_of)
    df.to_csv(path, index=False, encoding="utf-8-sig")  # utf-8-sig 便于 Excel 直接打开
    logger.info("已存档: %s", path)
    return path


def load_previous(as_of):
    """找比当前截止日期更早的最近一份快照,用于对比;没有则返回 None。"""
    cur = snapshot_path(as_of)
    files = []
    for f in os.listdir(DATA_DIR):
        m = re.match(r"holdings_(\d{8})\.csv$", f)
        if m:
            full = os.path.join(DATA_DIR, f)
            if full != cur:
                files.append((m.group(1), full))
    if not files:
        return None
    files.sort()
    prev_path = files[-1][1]
    try:
        logger.info("对比基准: %s", prev_path)
        return pd.read_csv(prev_path)
    except Exception as e:
        logger.warning("读取上一份快照失败(忽略对比): %s", e)
        return None


# ----------------------------------------------------------------------------
# 4. 仓位计算
# ----------------------------------------------------------------------------
def bs_call_delta(S, K, T, sigma, r=RISK_FREE):
    """Black-Scholes 看涨期权 Delta = N(d1)。无市场 IV,sigma 为假设值。

    边界:到期(T<=0)或波动率<=0 时退化为内在价值判断(实值=1,虚值=0)。
    """
    if not (S and K) or S <= 0 or K <= 0:
        return None
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))


def compute_positions(data):
    """算三个『真实仓位』+ 指数现价到行权价的距离。返回普通 dict,值缺失记为 None。"""
    nav = data["nav"]
    eq, deriv, opt = data["equities"], data["derivatives"], data["options"]
    res = {"nav": nav}

    # (1) 正股敞口 = 总正股市值 / NAV
    mv = "Market Value (in HKD)"
    eq_value = float(eq[mv].sum()) if mv in eq.columns else None
    res["equity_value"] = eq_value
    res["equity_pct"] = (eq_value / nav * 100) if (nav and eq_value is not None) else None

    # (2) 期货多头敞口 = Σ(合约张数 × 指数点 × 50) / NAV
    fut_notional = 0.0
    fut_found = False
    if {"Number of Shares Held", "Market Price (in HKD)"}.issubset(deriv.columns):
        fut = deriv[deriv["Name of Securities"].astype(str).str.upper().str.contains("FUTURE")]
        for _, r in fut.iterrows():
            contracts = r["Number of Shares Held"]
            index_pt = r["Market Price (in HKD)"]
            if pd.notna(contracts) and pd.notna(index_pt):
                fut_notional += contracts * index_pt * INDEX_MULTIPLIER
                fut_found = True
    res["futures_notional"] = fut_notional if fut_found else None
    res["futures_pct"] = (fut_notional / nav * 100) if (nav and fut_found) else None

    # (3) 期权空头压力 = Σ(名义敞口/NAV × 估算Delta);名义敞口占比页面已给
    pct_col = "Notional Exposure to NAV (%)"
    opt_pressure = 0.0
    opt_notional = 0.0
    opt_rows = []
    if not opt.empty and pct_col in opt.columns:
        for _, r in opt.iterrows():
            S, K = r.get("Index Price"), r.get("Strike")
            days = r.get("Calendar Days to Expiry")
            T = (days / 365.0) if pd.notna(days) else 0.0
            delta = bs_call_delta(S, K, T, IMPLIED_VOL)
            notional_pct = r.get(pct_col)
            if pd.notna(notional_pct) and delta is not None:
                contrib = notional_pct * delta      # 名义占比(空头为负)× delta
                opt_pressure += contrib
                opt_notional += notional_pct
                opt_rows.append({
                    "pos": r.get("Option Position"), "delta": delta,
                    "notional_pct": notional_pct, "contrib": contrib,
                })
    res["option_notional_pct"] = opt_notional if opt_rows else None    # 名义口径(未调整)
    res["option_pressure_pct"] = opt_pressure if opt_rows else None    # Delta 调整后
    res["option_rows"] = opt_rows

    # (c) 指数现价到行权价的距离(去重)
    dists = []
    if not opt.empty and {"Strike", "Index Price"}.issubset(opt.columns):
        seen = set()
        for _, r in opt.iterrows():
            S, K, days = r.get("Index Price"), r.get("Strike"), r.get("Calendar Days to Expiry")
            if pd.notna(S) and pd.notna(K) and S > 0:
                key = (round(float(K), 2), round(float(S), 2))
                if key in seen:
                    continue
                seen.add(key)
                dists.append({"index": S, "strike": K,
                              "dist_pct": (K - S) / S * 100,
                              "days": int(days) if pd.notna(days) else None})
    res["strike_distances"] = dists
    return res


# ----------------------------------------------------------------------------
# 5. 生成邮件正文
# ----------------------------------------------------------------------------
def build_summary(data, prev_full):
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

    # ---- b. 三个真实仓位 ----
    p = compute_positions(data)

    def pct(v):
        return "N/A" if v is None else f"{v:.2f}%"

    # 三列:名义口径 / Delta调整后 / 算法。正股、期货无需 Delta 调整,故标 "—"。
    pos_rows = (
        f"<tr><td>正股敞口</td><td style='text-align:right'>{pct(p['equity_pct'])}</td>"
        f"<td style='text-align:right'>—</td><td>≈ 总正股市值 / 基金净值</td></tr>"
        f"<tr><td>期货多头敞口</td><td style='text-align:right'>{pct(p['futures_pct'])}</td>"
        f"<td style='text-align:right'>—</td><td>合约张数 × 指数点 × {INDEX_MULTIPLIER:.0f} / 净值</td></tr>"
        f"<tr><td>期权空头敞口</td><td style='text-align:right'>{pct(p['option_notional_pct'])}</td>"
        f"<td style='text-align:right'>{pct(p['option_pressure_pct'])}</td>"
        f"<td>名义 = Σ 名义占比;调整 = × 估算Delta(IV={IMPLIED_VOL:.0%})</td></tr>"
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

    # ---- 与上一交易日对比(只比正股)----
    changes_html = "<p>首次运行,无历史快照可对比。</p>"
    if prev_full is not None and key_col in prev_full.columns:
        prev_eq, _ = split_equities(prev_full)
        prev_eq = prev_eq.copy()
        prev_eq[weight_col] = pd.to_numeric(prev_eq[weight_col], errors="coerce")
        cur_keys, prev_keys = set(eq[key_col]), set(prev_eq[key_col])
        added = eq[eq[key_col].isin(cur_keys - prev_keys)]
        removed = prev_eq[prev_eq[key_col].isin(prev_keys - cur_keys)]
        merged = eq.merge(prev_eq[[key_col, weight_col]], on=key_col, suffixes=("", "_prev"))
        merged["delta"] = merged[weight_col] - merged[weight_col + "_prev"]
        movers = merged.reindex(merged["delta"].abs().sort_values(ascending=False).index).head(5)

        def names(d):
            return ", ".join(d[name_col].astype(str)) if len(d) else "无"

        movers_rows = "".join(
            f"<tr><td>{r[name_col]}</td><td style='text-align:right'>{r[weight_col + '_prev']:.2f}%</td>"
            f"<td style='text-align:right'>{r[weight_col]:.2f}%</td>"
            f"<td style='text-align:right'>{r['delta']:+.2f}%</td></tr>"
            for _, r in movers.iterrows() if pd.notna(r["delta"]) and abs(r["delta"]) > 0
        )
        changes_html = (
            f"<p><b>新增正股:</b>{names(added)}</p>"
            f"<p><b>剔除正股:</b>{names(removed)}</p>"
            f"<p><b>权重变化最大(Top5):</b></p>"
            f"<table border='1' cellspacing='0' cellpadding='4'>"
            f"<tr><th>名称</th><th>前次</th><th>本次</th><th>变化</th></tr>"
            f"{movers_rows or '<tr><td colspan=4>无明显变化</td></tr>'}</table>"
        )

    nav_str = f"{p['nav']:,.0f} HKD" if p["nav"] else "未知"
    as_of_str = as_of.strftime("%Y-%m-%d") if as_of else "未知"
    body = f"""\
<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;font-size:14px">
<h2>{FUND_NAME} 每日持仓日报</h2>
<p><b>持仓截止日期:</b>{as_of_str} &nbsp;|&nbsp; <b>基金净值(估):</b>{nav_str}</p>

<h3>a. 前五大重仓占比 {top5_pct:.2f}%</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>#</th><th>名称</th><th>代码</th><th>权重</th></tr>
{top_rows}
</table>

<h3>b. 风险暴露</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>仓位</th><th>名义占净值</th><th>Delta调整后</th><th>算法</th></tr>
{pos_rows}
</table>
{net_html}

<h3>c. 指数现价 → 行权价距离</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>指数现价</th><th>行权价</th><th>距离</th><th>剩余天数</th></tr>
{dist_rows}
</table>

<h3>较上一交易日变化(正股)</h3>
{changes_html}

<p style="color:#888;font-size:12px">
注:Delta 为假设波动率 IV={IMPLIED_VOL:.0%} 下的 Black-Scholes 估算值,仅供参考;
NAV 由"市值/权重"反推。完整持仓见附件 CSV。<br>
数据来源:{FUND_URL}<br>本邮件由脚本自动生成于 {datetime.now():%Y-%m-%d %H:%M:%S}。</p>
</body></html>"""

    as_of_compact = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    subject = f"{as_of_compact} {FUND_NAME} 持仓"
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
                                      timeout=HTTP_TIMEOUT) as s:
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT) as s:
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
    if FORCE_SEND or as_of is None:
        return False
    return read_state().get("last_as_of") == as_of.isoformat()


def mark_sent(as_of):
    if as_of is None:
        return
    write_state(last_as_of=as_of.isoformat(), sent_at=datetime.now().isoformat())


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

        if already_sent(as_of):
            logger.info("截止日期 %s 已发送过,跳过(可设 FORCE_SEND=1 强制发送)", as_of)
            return 0

        prev_full = load_previous(as_of)
        path = save_snapshot(data["full"], as_of)   # 附件存完整持仓(含衍生品),与官网导出一致
        subject, body = build_summary(data, prev_full)
        send_email(subject, body, attachments=[path])
        mark_sent(as_of)
        logger.info("===== 任务成功 =====")
        return 0
    except Exception as e:
        logger.exception("任务失败: %s", e)
        send_failure_alert(str(e))
        logger.info("===== 任务失败 =====")
        return 1


if __name__ == "__main__":
    sys.exit(main())
