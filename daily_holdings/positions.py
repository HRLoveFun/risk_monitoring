import math
from typing import Any

import pandas as pd

from daily_holdings.config import IMPLIED_VOL, INDEX_MULTIPLIER, RISK_FREE, logger


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_price(s: float, k: float, t: float, sigma: float, r: float = RISK_FREE) -> float:
    if t <= 0 or sigma <= 0:
        return max(s - k, 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)


def bs_call_delta(s: float, k: float, t: float, sigma: float, r: float = RISK_FREE) -> float | None:
    if not (s and k) or s <= 0 or k <= 0:
        return None
    if t <= 0 or sigma <= 0:
        return 1.0 if s > k else 0.0
    d1 = (math.log(s / k) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    return _norm_cdf(d1)


def implied_vol(
    price: float | None,
    s: float | None,
    k: float | None,
    t: float | None,
    r: float = RISK_FREE,
    lo: float = 1e-4,
    hi: float = 5.0,
) -> float | None:
    if price is None or s is None or k is None or t is None:
        return None
    if price <= 0 or s <= 0 or k <= 0 or t <= 0:
        return None
    intrinsic = max(s - k * math.exp(-r * t), 0.0)
    if price <= intrinsic + 1e-9 or price >= bs_call_price(s, k, t, hi, r):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_call_price(s, k, t, mid, r) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _option_exposure_from_page(
    opt: pd.DataFrame, pct_col: str = "Notional Exposure to NAV (%)"
) -> float | None:
    if opt.empty or pct_col not in opt.columns:
        return None
    vals = pd.to_numeric(opt[pct_col], errors="coerce").dropna()
    return float(vals.sum()) if not vals.empty else None


def compute_positions(data: dict[str, Any]) -> dict[str, Any]:
    nav = data["nav"]
    eq = data["equities"]
    index_price = data.get("index_close")
    res: dict[str, Any] = {"nav": nav, "index_close": index_price}

    mv = "Market Value (in HKD)"
    eq_value = float(eq[mv].sum()) if mv in eq.columns else None
    res["equity_value"] = eq_value
    res["equity_pct"] = (eq_value / nav * 100) if (nav and eq_value is not None) else None

    f_legs = data.get("futures_legs") or []
    fut_notional = sum(leg["contracts"] * leg["index_pt"] * INDEX_MULTIPLIER for leg in f_legs)
    res["futures_notional"] = fut_notional if f_legs else None
    res["futures_pct"] = (fut_notional / nav * 100) if (nav and f_legs) else None
    res["futures_rows"] = f_legs

    opt_notional_pct = 0.0
    opt_pressure_pct = 0.0
    opt_rows, dropped = [], []
    iv_used, iv_weight = 0.0, 0.0
    for leg in data.get("option_legs") or []:
        strike = leg["strike"]
        days = leg["days"]
        px = leg["price"]
        problem, wait_helps = leg.get("problem"), leg.get("wait_helps", True)
        if not problem and not (nav and index_price):
            problem = "缺少基金净值或指数收盘点位"
        if problem:
            dropped.append({"name": leg["name"], "reason": problem, "wait_helps": wait_helps})
            continue
        notional_pct = leg["contracts"] * index_price * INDEX_MULTIPLIER / nav * 100
        t = days / 365.0
        iv = implied_vol(px, index_price, strike, t)
        sigma, iv_src = (iv, "市价反解") if iv else (IMPLIED_VOL, "假设值")
        if iv is None and days > 0:
            logger.warning(
                "期权市价(%s)反解 IV 失败,退回假设值 %.0f%%:%s", px, IMPLIED_VOL * 100, leg["name"]
            )
        delta = bs_call_delta(index_price, strike, t, sigma)
        if delta is None:
            dropped.append({"name": leg["name"], "reason": "Delta 计算失败", "wait_helpers": False})
            continue
        contrib = notional_pct * delta
        opt_notional_pct += notional_pct
        opt_pressure_pct += contrib
        iv_used += sigma * abs(notional_pct)
        iv_weight += abs(notional_pct)
        opt_rows.append(
            {
                "pos": leg["name"],
                "strike": strike,
                "expiry": leg["expiry"],
                "days": days,
                "price": px,
                "iv": sigma,
                "iv_src": iv_src,
                "delta": delta,
                "contracts": leg["contracts"],
                "notional_pct": notional_pct,
                "contrib": contrib,
            }
        )

    res["option_notional_pct"] = opt_notional_pct if opt_rows else None
    res["option_pressure_pct"] = opt_pressure_pct if opt_rows else None
    res["option_rows"] = opt_rows
    res["dropped_legs"] = dropped
    res["iv_avg"] = (iv_used / iv_weight) if iv_weight else None
    res["iv_all_from_market"] = bool(opt_rows) and all(r["iv_src"] == "市价反解" for r in opt_rows)

    page_pct = _option_exposure_from_page(data["options"])
    res["option_notional_pct_page"] = page_pct
    res["cross_check_gap"] = None
    if page_pct is not None and opt_rows:
        res["cross_check_gap"] = abs(page_pct - opt_notional_pct)
        logger.info(
            "期权名义敞口两来源并列:持仓表逐腿加总 %.2f%% vs 官网敞口表 %.2f%%",
            opt_notional_pct,
            page_pct,
        )

    dists, seen = [], set()
    for r in opt_rows:
        key = (round(r["strike"], 2), r["expiry"])
        if key in seen:
            continue
        seen.add(key)
        dists.append(
            {
                "index": index_price,
                "strike": r["strike"],
                "days": r["days"],
                "dist_pct": (r["strike"] - index_price) / index_price * 100,
                "iv": r["iv"],
                "delta": r["delta"],
            }
        )
    dists.sort(key=lambda d: d["days"] if d["days"] is not None else 10**6)
    res["strike_distances"] = dists
    return res
