import logging
import os
import sys

logger = logging.getLogger("daily_holdings")


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"缺少必填环境变量: {key}")
    return val


FUND_URL = env("FUND_URL", "https://www.globalxetfs.com.hk/funds/hscei-covered-call-etf/")
FUND_NAME = env("FUND_NAME", "Global X HSCEI Covered Call Active ETF")

SMTP_HOST = env("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(env("SMTP_PORT", "465"))
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
MAIL_FROM = env("MAIL_FROM", SMTP_USER)
MAIL_TO = [a.strip() for a in env("MAIL_TO", "").split(",") if a.strip()]

DATA_DIR = env(
    "DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
)
FORCE_SEND = env("FORCE_SEND", "").lower() in ("1", "true", "yes")

HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
SMTP_TIMEOUT = 30
SMTP_RETRIES = 3

INDEX_MULTIPLIER = 50.0
IMPLIED_VOL = float(env("IMPLIED_VOL", "0.30"))
RISK_FREE = 0.0

INCOMPLETE_GRACE_HOURS = float(env("INCOMPLETE_GRACE_HOURS", "3"))
WEIGHT_DIFF_TOL_PP = 0.10

REQUIRED_COLS = ["Name of Securities", "Exchange Ticker", "Net Assets (%)"]


def setup_logging() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(DATA_DIR, "run.log"), encoding="utf-8"),
        ],
    )
