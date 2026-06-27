#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global X HSCEI Covered Call Active ETF —— 每日持仓抓取 + 邮件日报

流程:
  1. 抓取基金页面 HTML(requests,带超时/重试)
  2. 解析页面里 id=holdingsList 的持仓表 + 期权敞口表(等价于网页上的 "FULL HOLDINGS .CSV" 按钮)
  3. 与上一份快照对比,生成摘要(总数 / Top10 / 新增剔除 / 权重变化)
  4. 通过 SMTP 发邮件(正文 + CSV 附件)
  5. 全程日志;任一步失败发"失败告警";同一截止日期不重复发(幂等)

依赖: requests, beautifulsoup4, pandas  (邮件用标准库)
配置: 全部走环境变量,见 .env.example
"""

import os
import re
import sys
import ssl
import json
import time
import logging
import smtplib
from io import StringIO
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
            resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
            # 内容健壮性校验:防止下到错误页/验证页却当成正常页面
            if "holdingsList" not in html:
                raise ValueError("页面中找不到持仓表(holdingsList),结构可能已变化")
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


def parse_holdings(html):
    """解析持仓表 + 期权敞口表,返回 (as_of_date, holdings_df, options_df)。

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

    # 数值列清洗
    for col in df.columns:
        if any(k in col for k in ["Price", "Shares", "Value", "Net Assets", "%"]):
            df[col] = _clean_num(df[col])

    df = df[df["Name of Securities"].astype(str).str.strip() != ""].reset_index(drop=True)
    if df.empty:
        raise ValueError("持仓表清洗后无有效数据行")

    as_of = parse_as_of_date(html)
    logger.info("解析成功:持仓 %d 行,截止日期 %s", len(df), as_of)
    return as_of, df, options_df


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
# 4. 生成摘要
# ----------------------------------------------------------------------------
def build_summary(df, prev_df, as_of, options_df):
    weight_col = "Net Assets (%)"
    name_col = "Name of Securities"
    key_col = "Exchange Ticker"

    total = len(df)
    weight_sum = df[weight_col].sum()

    top = df.sort_values(weight_col, ascending=False).head(10)
    top_rows = "".join(
        f"<tr><td>{i}</td><td>{r[name_col]}</td><td>{r[key_col]}</td>"
        f"<td style='text-align:right'>{r[weight_col]:.2f}%</td></tr>"
        for i, (_, r) in enumerate(top.iterrows(), 1)
    )

    # 与上一份对比
    changes_html = "<p>首次运行,无历史快照可对比。</p>"
    if prev_df is not None and key_col in prev_df.columns:
        prev_df = prev_df.copy()
        prev_df[weight_col] = pd.to_numeric(prev_df[weight_col], errors="coerce")
        cur_keys = set(df[key_col])
        prev_keys = set(prev_df[key_col])

        added = df[df[key_col].isin(cur_keys - prev_keys)]
        removed = prev_df[prev_df[key_col].isin(prev_keys - cur_keys)]

        merged = df.merge(prev_df[[key_col, weight_col]], on=key_col,
                          suffixes=("", "_prev"))
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
            f"<p><b>新增持仓:</b>{names(added)}</p>"
            f"<p><b>剔除持仓:</b>{names(removed)}</p>"
            f"<p><b>权重变化最大(Top5):</b></p>"
            f"<table border='1' cellspacing='0' cellpadding='4'>"
            f"<tr><th>名称</th><th>前次</th><th>本次</th><th>变化</th></tr>"
            f"{movers_rows or '<tr><td colspan=4>无明显变化</td></tr>'}</table>"
        )

    # 权重合计异常提示
    warn = ""
    if not (95 <= weight_sum <= 105):
        warn = (f"<p style='color:#c00'><b>⚠ 注意:股票持仓权重合计 {weight_sum:.2f}%,"
                f"偏离 100%(该基金为备兑看涨策略,含期权空头敞口,属正常;仅供留意)。</b></p>")

    options_html = ""
    if options_df is not None and not options_df.empty:
        options_html = "<h3>期权敞口</h3>" + options_df.to_html(index=False, border=1)

    as_of_str = as_of.strftime("%Y-%m-%d") if as_of else "未知"
    body = f"""\
<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;font-size:14px">
<h2>{FUND_NAME} 每日持仓日报</h2>
<p><b>持仓截止日期:</b>{as_of_str} &nbsp;|&nbsp; <b>持仓数量:</b>{total} &nbsp;|&nbsp;
   <b>股票权重合计:</b>{weight_sum:.2f}%</p>
{warn}
<h3>前十大持仓</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>#</th><th>名称</th><th>代码</th><th>权重</th></tr>
{top_rows}
</table>
<h3>较上一交易日变化</h3>
{changes_html}
{options_html}
<p style="color:#888;font-size:12px">数据来源:{FUND_URL}<br>
本邮件由脚本自动生成于 {datetime.now():%Y-%m-%d %H:%M:%S}。</p>
</body></html>"""

    subject = f"[ETF日报] {FUND_NAME} 持仓 {as_of_str}({total}只)"
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
    """尽力而为地发一封失败告警;发不出去也不再抛异常。"""
    try:
        send_email(
            subject=f"[ETF日报-失败] {FUND_NAME} {date.today():%Y-%m-%d}",
            html_body=f"<p>每日持仓任务执行失败:</p><pre>{error_text}</pre>",
        )
    except Exception as e:
        logger.error("连失败告警都发不出去: %s", e)


# ----------------------------------------------------------------------------
# 幂等:同一截止日期不重复发
# ----------------------------------------------------------------------------
STATE_FILE = lambda: os.path.join(DATA_DIR, "state.json")


def already_sent(as_of):
    if FORCE_SEND or as_of is None:
        return False
    try:
        with open(STATE_FILE(), encoding="utf-8") as f:
            return json.load(f).get("last_as_of") == as_of.isoformat()
    except (FileNotFoundError, ValueError):
        return False


def mark_sent(as_of):
    if as_of is None:
        return
    with open(STATE_FILE(), "w", encoding="utf-8") as f:
        json.dump({"last_as_of": as_of.isoformat(),
                   "sent_at": datetime.now().isoformat()}, f, ensure_ascii=False)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    setup_logging()
    logger.info("===== 任务开始 =====")
    try:
        html = fetch_page(FUND_URL)
        as_of, df, options_df = parse_holdings(html)

        if already_sent(as_of):
            logger.info("截止日期 %s 已发送过,跳过(可设 FORCE_SEND=1 强制发送)", as_of)
            return 0

        prev_df = load_previous(as_of)
        path = save_snapshot(df, as_of)
        subject, body = build_summary(df, prev_df, as_of, options_df)
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
