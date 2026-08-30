import json
import os
import re
from datetime import date
from typing import Any

import pandas as pd

from daily_holdings.config import DATA_DIR, INDEX_MULTIPLIER, WEIGHT_DIFF_TOL_PP, logger
from daily_holdings.parser import is_future, is_option, parse_option_name, split_equities


def snapshot_path(as_of: date | None) -> str:
    tag = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    return os.path.join(DATA_DIR, f"holdings_{tag}.csv")


def snapshot_meta_path(as_of: date | None) -> str:
    tag = as_of.strftime("%Y%m%d") if as_of else date.today().strftime("%Y%m%d")
    return os.path.join(DATA_DIR, f"holdings_{tag}.meta.json")


def save_snapshot(df: pd.DataFrame, as_of: date | None, meta: dict | None = None) -> str:
    path = snapshot_path(as_of)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("已存档: %s", path)
    if meta is not None:
        try:
            meta_path = snapshot_meta_path(as_of)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            logger.info("已存档元数据: %s", meta_path)
        except Exception as e:
            logger.warning("元数据存档失败(忽略,不影响主流程): %s", e)
    return path


def load_previous(as_of: date | None) -> tuple[pd.DataFrame | None, dict | None]:
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


def _norm_label(s: str | None) -> str:
    return " ".join(str(s or "").split())


def _num_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([None] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def diff_vs_previous(
    prev_full: pd.DataFrame | None, prev_meta: dict | None, cur_full: pd.DataFrame, cur: dict
) -> dict[str, Any]:
    diff: dict[str, Any] = {
        "has_prev": prev_full is not None and not prev_full.empty,
        "base_date": None,
        "equities": {"added": [], "removed": [], "weight_changes": []},
        "futures": {"added": [], "removed": [], "changed": [], "rows_prev": 0, "rows_cur": 0},
        "options": {
            "added": [],
            "removed": [],
            "changed": [],
            "rows_prev": 0,
            "rows_cur": 0,
            "contracts_abs_prev": None,
            "contracts_abs_cur": None,
        },
        "metrics": [],
    }
    if cur_full is None or cur_full.empty or "Name of Securities" not in cur_full.columns:
        return diff
    if diff["has_prev"] and isinstance(prev_meta, dict) and prev_meta.get("as_of"):
        diff["base_date"] = str(prev_meta["as_of"])

    prev_eq = prev_deriv = None
    if diff["has_prev"] and "Name of Securities" in prev_full.columns:
        prev_eq, prev_deriv, _ = split_equities(prev_full)
    cur_eq, cur_deriv, _ = split_equities(cur_full)

    def equity_map(eq: pd.DataFrame) -> dict:
        out = {}
        if eq is None or eq.empty:
            return out
        wts = _num_series(eq, "Net Assets (%)")
        for (_, r), wt in zip(eq.iterrows(), wts, strict=True):
            key = str(r.get("Exchange Ticker", "")).strip()
            if key:
                out[key] = {
                    "name": str(r.get("Name of Securities")),
                    "weight": None if pd.isna(wt) else float(wt),
                }
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
                {"name": emap_cur[k]["name"], "ticker": k, "prev": pw, "cur": cw, "delta": cw - pw}
            )
    eq["weight_changes"].sort(key=lambda d: abs(d["delta"]), reverse=True)

    def deriv_rows(d: pd.DataFrame | None) -> dict:
        out = {}
        if d is None or d.empty or "Name of Securities" not in d.columns:
            return out
        cons = _num_series(d, "Number of Shares Held")
        pxs = _num_series(d, "Market Price (in HKD)")
        for (_, r), c, px in zip(d.iterrows(), cons, pxs, strict=True):
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
            if key in out:
                base = out[key]
                base["contracts"] = (
                    ((base["contracts"] or 0.0) + (con or 0.0))
                    if con is not None
                    else base["contracts"]
                )
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
            if pc != cc:
                s["changed"].append({"label": dmap_cur[k]["label"], "prev": pc, "cur": cc})

    def abs_contracts(dmap: dict, kind: str) -> float | None:
        vals = [
            v["contracts"] for k, v in dmap.items() if k[0] == kind and v["contracts"] is not None
        ]
        return float(sum(abs(v) for v in vals)) if vals else None

    diff["options"]["contracts_abs_prev"] = abs_contracts(dmap_prev, "OPT")
    diff["options"]["contracts_abs_cur"] = abs_contracts(dmap_cur, "OPT")

    def mv_sum(eqd: pd.DataFrame | None) -> float | None:
        if eqd is None or eqd.empty:
            return None
        s = _num_series(eqd, "Market Value (in HKD)").dropna()
        return float(s.sum()) if not s.empty else None

    def fut_notional(dmap: dict) -> float | None:
        tot, seen = 0.0, False
        for k, v in dmap.items():
            if k[0] == "FUT" and v["contracts"] is not None and v.get("price") is not None:
                tot += v["contracts"] * v["price"] * INDEX_MULTIPLIER
                seen = True
        return tot if seen else None

    pm = prev_meta if isinstance(prev_meta, dict) else {}
    diff["metrics"] = [
        {"label": "基金净值 NAV(HKD)", "prev": pm.get("nav"), "cur": cur.get("nav"), "fmt": ",.0f"},
        {
            "label": "HSCEI 收盘",
            "prev": pm.get("index_close"),
            "cur": cur.get("index_close"),
            "fmt": ",.2f",
        },
        {
            "label": "正股市值合计(HKD)",
            "prev": mv_sum(prev_eq),
            "cur": mv_sum(cur_eq),
            "fmt": ",.0f",
        },
        {
            "label": "期货名义合计(HKD)",
            "prev": fut_notional(dmap_prev),
            "cur": fut_notional(dmap_cur),
            "fmt": ",.0f",
        },
        {
            "label": "期权空头总张数(绝对值)",
            "prev": diff["options"]["contracts_abs_prev"],
            "cur": diff["options"]["contracts_abs_cur"],
            "fmt": ",.0f",
        },
    ]
    return diff
