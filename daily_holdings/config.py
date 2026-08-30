import logging
import os
import sys

logger = logging.getLogger("daily_holdings")

ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)


def load_env_file() -> None:
    """Load .env as a fallback only.

    Precedence is always: real environment variables > .env.
    ``override=False`` (python-dotenv's default) guarantees this, including for
    variables exported as an empty string -- they are present, so .env never
    overwrites them. A missing python-dotenv is non-fatal on purpose: GitHub
    Actions ships no .env and injects everything through secrets.
    """
    if not os.path.isfile(ENV_FILE):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional at runtime
        logger.warning(
            "检测到 .env 但 python-dotenv 未安装,已跳过(pip install python-dotenv)"
        )
        return
    load_dotenv(ENV_FILE, override=False)


def env(key: str, default: str | None = None, required: bool = False) -> str:
    # Treat an empty value as "unset" so a blank .env entry falls back to the
    # default instead of shadowing it or crashing int()/float() parsing.
    val = os.environ.get(key)
    if not val:
        val = default
    if required and not val:
        raise RuntimeError(f"缺少必填环境变量: {key}")
    return val


load_env_file()


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
WEIGHT_DIFF_TOL_PP = 0.50

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
