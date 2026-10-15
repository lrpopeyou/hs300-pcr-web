#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core data and strategy functions for the HS300 PCR dashboard.

No Tushare token is required. Historical SSE 510300 ETF PCR is backfilled from
AkShare's daily statistics. CFFEX IO open interest is collected as a live daily
snapshot; this code deliberately does not fabricate historical IO values.
"""
from __future__ import annotations

import re
import time
import requests
import xml.etree.ElementTree as ET

import json
import logging
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import akshare as ak
except ImportError:  # Keep mathematical/unit tests runnable without AkShare installed
    ak = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"
ETF_FILE = DATA_DIR / "etf_pcr.csv"
IO_FILE = DATA_DIR / "io_pcr.csv"
INDEX_FILE = DATA_DIR / "hs300.csv"
SIGNAL_FILE = OUTPUT_DIR / "pcr_signal_series.csv"
LATEST_FILE = OUTPUT_DIR / "latest_signal.csv"
BACKTEST_FILE = OUTPUT_DIR / "hs300_backtest.csv"
SUMMARY_FILE = OUTPUT_DIR / "backtest_summary.csv"
WEIGHT_FILE = OUTPUT_DIR / "weight_fit.csv"
META_FILE = DATA_DIR / "meta.json"

DEFAULT_START_DATE = "20240501"
INDEX_SYMBOL = "000300"
ETF_SYMBOL = "510300"
BB_LENGTH = 20
BB_MULT = 2.0
ETF_WEIGHT = 0.50
INITIAL_CAPITAL = 1_000_000.0
COST_BPS = 3.0
REQUEST_SLEEP = 0.25
MAX_RETRY = 3

LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
logger = logging.getLogger("hs300_pcr")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    handler = logging.FileHandler(LOG_DIR / "strategy.log", encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)


def ensure_akshare():
    if ak is None:
        raise RuntimeError("尚未安装 AkShare。请执行：pip install -r requirements.txt")


def ensure_dirs():
    for p in (DATA_DIR, OUTPUT_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)


def now_cn() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def safe_float(value) -> float:
    try:
        result = float(str(value).replace(",", "").strip())
        return result if np.isfinite(result) else np.nan
    except Exception:
        return np.nan


def atomic_csv(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(path)


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    return df


def upsert_dates(old: pd.DataFrame, new: pd.DataFrame, keep: str = "last") -> pd.DataFrame:
    if new is None or new.empty:
        out = old.copy()
    elif old is None or old.empty:
        out = new.copy()
    else:
        # Align heterogeneous columns so imported/older cache schemas remain usable.
        out = pd.concat([old, new], ignore_index=True, sort=False)
    if out.empty:
        return out
    if "date" not in out.columns:
        raise ValueError("数据文件缺少 date 列")
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    out = out.dropna(subset=["date"]).sort_values("date")
    out = out.drop_duplicates(subset=["date"], keep=keep).reset_index(drop=True)
    return out


def save_meta(**values):
    current = {}
    if META_FILE.exists():
        try:
            current = json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(values)
    current["updated_at"] = now_cn().isoformat(timespec="seconds")
    META_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def read_meta() -> dict:
    if not META_FILE.exists():
        return {}
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def call_retry(fn, *args, label="", **kwargs):
    ensure_akshare()
    error = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            error = exc
            logger.warning("%s：第 %s/%s 次请求失败：%s", label or fn.__name__, attempt, MAX_RETRY, exc)
            time.sleep(attempt * 0.6)
    raise RuntimeError(f"{label or fn.__name__} 重试失败：{error}")


def fetch_index_history(start_date: str, end_date: Optional[str] = None) -> pd.DataFrame:
    """Fetch HS300 OHLCV; output normalized English column names."""
    ensure_akshare()
    start = pd.Timestamp(start_date).strftime("%Y%m%d")
    end = pd.Timestamp(end_date or now_cn().date()).strftime("%Y%m%d")
    last_error = None
    # Current AkShare interface and output fields documented in its index docs.
    try:
        raw = call_retry(
            ak.index_zh_a_hist, symbol=INDEX_SYMBOL, period="daily",
            start_date=start, end_date=end, label="沪深300历史行情",
        )
        if raw is not None and not raw.empty:
            rename = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
            df = raw.rename(columns=rename).copy()
            required = {"date", "open", "close", "high", "low"}
            if required.issubset(df.columns):
                df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
                for col in ("open", "close", "high", "low", "volume", "amount"):
                    if col in df:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["date", "open", "close"])
                return df[[c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df]].sort_values("date").drop_duplicates("date")
    except Exception as exc:
        last_error = exc
        logger.warning("index_zh_a_hist 不可用，尝试备用数据源：%s", exc)

    # Current alternative in AkShare: Tencent daily index endpoint.
    try:
        raw = call_retry(
            ak.stock_zh_index_daily_tx, symbol="sh000300",
            start_date=start, end_date=end, label="沪深300备用历史行情",
        )
        if raw is not None and not raw.empty:
            df = raw.copy().rename(columns={"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
            for col in ("open", "close", "high", "low", "volume", "amount"):
                if col in df:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["date", "open", "close"])
            return df[[c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df]].sort_values("date").drop_duplicates("date")
    except Exception as exc:
        last_error = exc

    raise RuntimeError(f"获取沪深300历史行情失败：{last_error}")


def update_index_history(start_date: str = DEFAULT_START_DATE, end_date: Optional[str] = None, full_backfill: bool = False) -> pd.DataFrame:
    ensure_dirs()
    old = load_csv(INDEX_FILE)
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date or now_cn().date()).normalize()
    fetch_start = start
    if not old.empty and not full_backfill:
        # Re-fetch a small recent window to refresh the latest partial bar/revisions.
        fetch_start = max(start, old["date"].max() - pd.Timedelta(days=7))
    try:
        fresh = fetch_index_history(fetch_start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        fresh = fresh[(fresh["date"] >= start) & (fresh["date"] <= end)]
        out = upsert_dates(old, fresh)
        # Preserve rows before requested start date if cache contains them.
        if not old.empty:
            prior = old[old["date"] < start]
            out = upsert_dates(prior, out)
        atomic_csv(out, INDEX_FILE)
        save_meta(index_last_date=str(out["date"].max().date()) if not out.empty else None,
                  index_rows=int(len(out)))
        logger.info("沪深300行情已更新，共 %s 行：%s", len(out), INDEX_FILE)
        return out
    except Exception:
        if not old.empty:
            logger.exception("指数行情刷新失败，继续使用本地缓存")
            return old
        raise


def _find_column(columns, includes: list[str], excludes: Optional[list[str]] = None) -> Optional[str]:
    excludes = excludes or []
    for col in columns:
        compact = str(col).replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
        if all(k.replace(" ", "") in compact for k in includes) and not any(k.replace(" ", "") in compact for k in excludes):
            return col
    return None


def fetch_etf_pcr_for_date(dt: pd.Timestamp) -> Optional[dict]:
    """Query SSE daily statistics for the 510300 ETF option OI ratio."""
    dt = pd.Timestamp(dt).normalize()
    raw = call_retry(ak.option_daily_stats_sse, date=dt.strftime("%Y%m%d"), label=f"510300 ETF期权统计 {dt:%Y-%m-%d}")
    if raw is None or raw.empty:
        return None
    raw = raw.copy()
    code_col = _find_column(raw.columns, ["合约标的代码"])
    call_col = _find_column(raw.columns, ["未平仓认购合约数"])
    put_col = _find_column(raw.columns, ["未平仓认沽合约数"])
    # Backward-compatible fallbacks for output-name variations.
    if not code_col:
        code_col = next((c for c in raw.columns if "代码" in str(c) and "标的" in str(c)), None)
    if not call_col:
        call_col = next((c for c in raw.columns if "认购" in str(c) and ("未平仓" in str(c) or "持仓" in str(c))), None)
    if not put_col:
        put_col = next((c for c in raw.columns if "认沽" in str(c) and ("未平仓" in str(c) or "持仓" in str(c))), None)
    if not all((code_col, call_col, put_col)):
        raise ValueError(f"SSE 字段识别失败：{list(raw.columns)}")
    selected = raw[raw[code_col].astype(str).str.strip().str.zfill(6) == ETF_SYMBOL]
    if selected.empty:
        return None
    call_oi = safe_float(selected.iloc[0][call_col])
    put_oi = safe_float(selected.iloc[0][put_col])
    if not np.isfinite(call_oi) or call_oi <= 0 or not np.isfinite(put_oi) or put_oi < 0:
        return None
    return {"date": dt, "etf_call_oi": call_oi, "etf_put_oi": put_oi,
            "etf_pcr": put_oi / call_oi, "source": "AkShare SSE daily stats"}


def update_etf_history(start_date: str = DEFAULT_START_DATE, end_date: Optional[str] = None,
                       full_backfill: bool = False, progress: Optional[Callable[[int, int, str], None]] = None) -> pd.DataFrame:
    """Backfill missing SSE data using actual HS300 trading dates, not calendar days."""
    ensure_dirs()
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date or now_cn().date()).normalize()
    index_df = load_csv(INDEX_FILE)
    if index_df.empty or index_df["date"].max() < end - pd.Timedelta(days=10):
        index_df = update_index_history(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    trading_dates = pd.to_datetime(index_df["date"], errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
    trading_dates = trading_dates[(trading_dates >= start) & (trading_dates <= end)]
    old = load_csv(ETF_FILE)
    existing = set(old["date"].dt.strftime("%Y-%m-%d")) if not old.empty else set()
    if full_backfill:
        targets = [pd.Timestamp(d) for d in trading_dates if pd.Timestamp(d).strftime("%Y-%m-%d") not in existing]
    else:
        recent_cutoff = end - pd.Timedelta(days=14)
        targets = [pd.Timestamp(d) for d in trading_dates
                   if pd.Timestamp(d) >= recent_cutoff or pd.Timestamp(d).strftime("%Y-%m-%d") not in existing]
    rows = []
    total = len(targets)
    logger.info("ETF PCR 计划抓取 %s 个交易日（%s 至 %s）", total, start.date(), end.date())
    for i, dt in enumerate(targets, start=1):
        try:
            row = fetch_etf_pcr_for_date(dt)
            if row:
                rows.append(row)
                logger.info("ETF PCR %s = %.5f", dt.date(), row["etf_pcr"])
        except Exception as exc:
            logger.warning("ETF PCR %s 抓取失败，将在后续运行重试：%s", dt.date(), exc)
        if progress:
            progress(i, max(total, 1), f"ETF PCR {dt:%Y-%m-%d}")
        time.sleep(REQUEST_SLEEP)
    new = pd.DataFrame(rows)
    out = upsert_dates(old, new)
    atomic_csv(out, ETF_FILE)
    save_meta(etf_last_date=str(out["date"].max().date()) if not out.empty else None,
              etf_rows=int(len(out)))
    logger.info("ETF PCR 本地历史共 %s 行", len(out))
    return out


def get_io_months() -> list[str]:
    raw = call_retry(ak.option_cffex_hs300_list_sina, label="CFFEX IO合约月份列表")
    values = []
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, (list, tuple)):
                values.extend(v)
    elif isinstance(raw, pd.DataFrame):
        for col in raw.columns:
            values.extend(raw[col].dropna().astype(str).tolist())
    months = []
    for value in values:
        code = str(value).strip().lower()
        if code.startswith("io") and code not in months:
            months.append(code)
    return months


def fetch_io_snapshot(as_of_date: pd.Timestamp) -> Optional[dict]:
    """Get a live snapshot of all listed IO expiries; not historical replay data."""
    months = get_io_months()
    if not months:
        raise RuntimeError("AkShare 未返回 IO 合约月份")
    total_call = 0.0
    total_put = 0.0
    used = []
    for month in months:
        try:
            raw = call_retry(ak.option_cffex_hs300_spot_sina, symbol=month, label=f"IO实时持仓 {month}")
            if raw is None or raw.empty:
                continue
            call_col = _find_column(raw.columns, ["看涨合约", "持仓量"])
            put_col = _find_column(raw.columns, ["看跌合约", "持仓量"])
            if call_col is None:
                call_col = next((c for c in raw.columns if "看涨" in str(c) and "持仓" in str(c)), None)
            if put_col is None:
                put_col = next((c for c in raw.columns if "看跌" in str(c) and "持仓" in str(c)), None)
            if call_col is None or put_col is None:
                logger.warning("IO %s 无法识别 OI 字段：%s", month, list(raw.columns))
                continue
            c_oi = pd.to_numeric(raw[call_col], errors="coerce").fillna(0).clip(lower=0).sum()
            p_oi = pd.to_numeric(raw[put_col], errors="coerce").fillna(0).clip(lower=0).sum()
            if c_oi > 0:
                total_call += float(c_oi)
                total_put += float(p_oi)
                used.append(month)
            time.sleep(0.08)
        except Exception as exc:
            logger.warning("IO 合约月份 %s 抓取失败：%s", month, exc)
    if total_call <= 0:
        return None
    return {"date": pd.Timestamp(as_of_date).normalize(), "io_call_oi": total_call,
            "io_put_oi": total_put, "io_pcr": total_put / total_call,
            "io_months": ",".join(used), "observed_at": now_cn().isoformat(timespec="seconds"),
            "source": "AkShare Sina CFFEX live snapshot"}


def _append_today_index_from_minutes(today: pd.Timestamp) -> bool:
    """
    日线接口延迟时，通过沪深300当日5分钟行情确认交易日，
    并临时聚合一条当日日线，避免错误地把 IO 快照标记到前一日。
    只有实际获取到 today 的分钟数据才会写入。
    """
    try:
        raw = call_retry(
            ak.stock_zh_a_minute,
            symbol="sh000300",
            period="5",
            adjust="",
            label="沪深300当日分钟行情",
        )

        if raw is None or raw.empty:
            return False

        df = raw.copy()

        date_col = next(
            (
                c for c in df.columns
                if str(c).strip().lower()
                in {"day", "date", "datetime", "日期", "时间"}
            ),
            None,
        )

        if date_col is None:
            logger.warning("分钟行情缺少日期字段：%s", list(df.columns))
            return False

        df["_dt"] = pd.to_datetime(df[date_col], errors="coerce")
        df["_date_key"] = df["_dt"].dt.strftime("%Y-%m-%d")

        target = pd.Timestamp(today).strftime("%Y-%m-%d")
        df = df[df["_date_key"] == target].copy()

        if df.empty:
            logger.warning("未获取到沪深300 %s 的分钟行情", target)
            return False

        aliases = {
            "open": {"open", "开盘"},
            "high": {"high", "最高"},
            "low": {"low", "最低"},
            "close": {"close", "收盘"},
            "volume": {"volume", "成交量"},
            "amount": {"amount", "成交额"},
        }

        col_map = {}
        for field, names in aliases.items():
            col_map[field] = next(
                (
                    c for c in df.columns
                    if str(c).strip().lower() in names
                ),
                None,
            )

        required = ("open", "high", "low", "close")

        if any(col_map[k] is None for k in required):
            logger.warning("分钟行情缺少 OHLC 字段：%s", list(df.columns))
            return False

        df = df.sort_values("_dt")

        for field in required:
            df[field] = pd.to_numeric(
                df[col_map[field]], errors="coerce"
            )

        df = df.dropna(subset=list(required))

        if df.empty:
            return False

        # 只聚合实际获取到的当日分钟线。
        daily = {
            "date": pd.Timestamp(today).normalize(),
            "open": float(df["open"].iloc[0]),
            "high": float(df["high"].max()),
            "low": float(df["low"].min()),
            "close": float(df["close"].iloc[-1]),
        }

        for field in ("volume", "amount"):
            source_col = col_map[field]
            if source_col is not None:
                values = pd.to_numeric(
                    df[source_col], errors="coerce"
                )
                if values.notna().any():
                    daily[field] = float(values.sum())

        old = load_csv(INDEX_FILE)
        out = upsert_dates(
            old,
            pd.DataFrame([daily]),
            keep="last",
        )
        atomic_csv(out, INDEX_FILE)

        logger.warning(
            "沪深300日线接口滞后，已使用当日分钟线临时聚合日线：%s",
            target,
        )
        return True

    except Exception as exc:
        logger.warning(
            "无法通过分钟行情确认当日交易：%s", exc
        )
        return False

def update_io_snapshot() -> pd.DataFrame:
    ensure_dirs()

    now = now_cn()
    today = pd.Timestamp(now.date()).normalize()

    # 日频持仓数据在收盘后采集，避免把盘中值当成正式日频值。
    if now.time() < dt_time(15, 20):
        raise RuntimeError(
            "当前时间早于北京时间 15:20，暂不采集 IO 日频持仓快照"
        )

    index = load_csv(INDEX_FILE)

    if index.empty:
        index = update_index_history(DEFAULT_START_DATE)

    if index.empty:
        raise RuntimeError("沪深300行情为空，无法确认交易日期")

    index["date"] = pd.to_datetime(
        index["date"], errors="coerce"
    ).dt.normalize()

    as_of = index["date"].dropna().max()

    if pd.isna(as_of):
        raise RuntimeError("沪深300行情日期无效")

    # 修复点：
    # 如果日线接口仍停留在前一交易日，先检查当日分钟行情。
    # 只有实际取得 today 的分钟数据，才允许把快照记到 today。
    if as_of.date() < today.date():

        logger.warning(
            "沪深300日线最新日期为 %s，当前日期为 %s，尝试使用分钟线确认当日交易",
            as_of.date(),
            today.date(),
        )

        if not _append_today_index_from_minutes(today):
            raise RuntimeError(
                f"沪深300日线最新日期是 {as_of.date()}，"
                f"当前日期是 {today.date()}，且未获取到当日分钟行情；"
                "本次不写入 IO 快照，请稍后重试"
            )

        index = load_csv(INDEX_FILE)

        if index.empty:
            raise RuntimeError("刷新后的沪深300行情为空")

        index["date"] = pd.to_datetime(
            index["date"], errors="coerce"
        ).dt.normalize()

        as_of = index["date"].dropna().max()

    # 不允许使用未来日期或把当前数据标记成旧交易日。
    if pd.isna(as_of) or as_of.date() != today.date():
        raise RuntimeError(
            f"无法确认当日交易日期：目标日期={today.date()}，"
            f"最新指数日期={as_of}"
        )

    # 日线回补原本依赖指数日期。
    # 若指数日线延迟，这里直接补抓当日 ETF PCR。
    etf = load_csv(ETF_FILE)

    has_today_etf = (
        not etf.empty
        and etf["date"].notna().any()
        and (
            etf["date"].dt.normalize() == today
        ).any()
        and pd.to_numeric(
            etf.loc[
                etf["date"].dt.normalize() == today,
                "etf_pcr",
            ],
            errors="coerce",
        ).notna().any()
    )

    if not has_today_etf:
        try:
            # etf_row = fetch_etf_pcr_for_date(today)

            # #if etf_row:
            # if etf_row is not None and not etf_row.empty:
            #     etf = upsert_dates(
            #         etf,
            #         pd.DataFrame([etf_row]),
            #         keep="last",
            #     )
            #     atomic_csv(etf, ETF_FILE)

            #     logger.info(
            #         "当日 ETF PCR 已补齐：%s，PCR=%.6f",
            #         today.date(),
            #         etf_row["etf_pcr"],
            #     )
            # else:
            #     logger.warning(
            #         "尚未获取到 %s 的 ETF PCR；"
            #         "IO 快照仍可保存，但当日综合 PCR 可能暂不可用",
            #         today.date(),
            #     )
            etf_row = fetch_etf_pcr_for_date(today)

            if etf_row is not None and not etf_row.empty:
                etf_row = etf_row.copy()

                etf_row["date"] = pd.to_datetime(
                    etf_row["date"], errors="coerce"
                ).dt.normalize()

                today_rows = etf_row[
                    etf_row["date"] == today
                ].copy()

                if not today_rows.empty:
                    etf = upsert_dates(
                        etf,
                        today_rows,
                        keep="last",
                    )

                    atomic_csv(etf, ETF_FILE)

                    logger.info(
                        "当日 ETF PCR 已补齐：%s，PCR=%.6f",
                        today.date(),
                        float(today_rows.iloc[-1]["etf_pcr"]),
                    )
                else:
                    logger.warning(
                        "ETF 接口未返回目标日期 %s 的数据",
                        today.date(),
                    )
            else:
                logger.warning(
                    "ETF 接口暂时没有返回 %s 的统计数据",
                    today.date(),
                )

        except Exception as exc:
            logger.warning(
                "补抓当日 ETF PCR 失败：%s；继续更新 IO 快照",
                exc,
            )

    # 获取当前所有可见 IO 合约月份的持仓量。
    row = fetch_io_snapshot(today)

    if not row:
        raise RuntimeError(
            f"未获取到 {today.date()} 的有效 IO 持仓快照"
        )

    old = load_csv(IO_FILE)

    out = upsert_dates(
        old,
        pd.DataFrame([row]),
        keep="last",
    )

    atomic_csv(out, IO_FILE)

    save_meta(
        io_last_date=str(today.date()),
        io_rows=int(len(out)),
        io_observed_at=row["observed_at"],
    )

    logger.info(
        "IO PCR %s = %.5f；Call OI=%.0f，Put OI=%.0f，月份=%s",
        today.date(),
        row["io_pcr"],
        row["io_call_oi"],
        row["io_put_oi"],
        row["io_months"],
    )

    return out
        
# def update_io_snapshot() -> pd.DataFrame:
#     ensure_dirs()
#     index = load_csv(INDEX_FILE)
#     if index.empty:
#         index = update_index_history(DEFAULT_START_DATE)
#     # Label the live OI snapshot with the latest index trading date so holiday/weekend
#     # quotes don't get falsely assigned to a calendar date with no HS300 bar.
#     now = now_cn()
#     as_of = pd.Timestamp(index["date"].max()).normalize()
#     if as_of.date() != now.date():
#         raise RuntimeError(f"最新沪深300行情日期是 {as_of.date()}，当前北京时间是 {now.date()}；可能尚未开市或处于休市日，本次不把快照错误写入旧交易日")
#     if now.time() < dt_time(15, 20):
#         raise RuntimeError("IO PCR 使用日频持仓口径，建议北京时间 15:20 后采集；本次不保存盘中快照，避免当作正式日线数据")
#     row = fetch_io_snapshot(as_of)
#     if not row:
#         raise RuntimeError("没有获取到有效的 IO 持仓快照")
#     old = load_csv(IO_FILE)
#     out = upsert_dates(old, pd.DataFrame([row]), keep="last")
#     atomic_csv(out, IO_FILE)
#     save_meta(io_last_date=str(as_of.date()), io_rows=int(len(out)), io_observed_at=row["observed_at"])
#     logger.info("IO PCR %s = %.5f（当前快照，不代表历史当日回放）", as_of.date(), row["io_pcr"])
#     return out


def import_io_csv(path: str) -> pd.DataFrame:
    src = pd.read_csv(path, comment="#")
    src.columns = [str(c).strip() for c in src.columns]
    aliases = {"日期": "date", "交易日": "date", "IO Call OI": "io_call_oi", "IO Put OI": "io_put_oi",
               "Call OI": "io_call_oi", "Put OI": "io_put_oi", "IO PCR": "io_pcr"}
    src = src.rename(columns=aliases)
    if "date" not in src:
        raise ValueError("CSV 需要包含 date / 日期 / 交易日")
    src["date"] = pd.to_datetime(src["date"], errors="coerce").dt.normalize()
    for col in ("io_call_oi", "io_put_oi", "io_pcr"):
        if col in src:
            src[col] = pd.to_numeric(src[col], errors="coerce")
    if "io_pcr" not in src:
        if not {"io_call_oi", "io_put_oi"}.issubset(src.columns):
            raise ValueError("CSV 至少需要 io_pcr，或 io_call_oi + io_put_oi")
        src["io_pcr"] = src["io_put_oi"] / src["io_call_oi"].replace(0, np.nan)
    if "source" not in src:
        src["source"] = f"CSV import: {Path(path).name}"
    old = load_csv(IO_FILE)
    out = upsert_dates(old, src)
    atomic_csv(out, IO_FILE)
    return out


def load_raw_pcr() -> pd.DataFrame:
    etf = load_csv(ETF_FILE)
    io = load_csv(IO_FILE)
    if etf.empty or io.empty:
        return pd.DataFrame(columns=["date", "etf_pcr", "io_pcr", "etf_call_oi", "etf_put_oi", "io_call_oi", "io_put_oi"])
    # Normalize date keys; only join dates for which both data providers actually have data.
    etf["date"] = pd.to_datetime(etf["date"]).dt.normalize()
    io["date"] = pd.to_datetime(io["date"]).dt.normalize()
    wanted_etf = [c for c in ["date", "etf_call_oi", "etf_put_oi", "etf_pcr"] if c in etf]
    wanted_io = [c for c in ["date", "io_call_oi", "io_put_oi", "io_pcr"] if c in io]
    merged = etf[wanted_etf].merge(io[wanted_io], on="date", how="inner", validate="one_to_one")
    return merged.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def add_bollinger_signals(df: pd.DataFrame, source_col: str = "composite_pcr",
                          length: int = BB_LENGTH, mult: float = BB_MULT,
                          allow_short: bool = False) -> pd.DataFrame:
    """Pure calculation function, convenient for tests and alternative chart modes."""
    out = df.copy().sort_values("date").reset_index(drop=True)
    if source_col not in out:
        out[source_col] = np.nan
    source = pd.to_numeric(out[source_col], errors="coerce")
    out["basis"] = source.rolling(int(length), min_periods=int(length)).mean()
    out["std"] = source.rolling(int(length), min_periods=int(length)).std(ddof=0)
    out["upper"] = out["basis"] + float(mult) * out["std"]
    out["lower"] = out["basis"] - float(mult) * out["std"]
    prev_source = source.shift(1)
    prev_upper = out["upper"].shift(1)
    prev_lower = out["lower"].shift(1)
    out["top_signal"] = (source > out["upper"]) & (prev_source <= prev_upper)
    out["bottom_signal"] = (source < out["lower"]) & (prev_source >= prev_lower)
    out["signal"] = 0
    out.loc[out["top_signal"].fillna(False), "signal"] = -1
    out.loc[out["bottom_signal"].fillna(False), "signal"] = 1
    state = 0
    states = []
    for val in out["signal"].fillna(0).astype(int):
        if val == 1:
            state = 1
        elif val == -1:
            state = -1 if allow_short else 0
        states.append(state)
    out["position_state"] = states
    out["signal_name"] = np.select([out["top_signal"].fillna(False), out["bottom_signal"].fillna(False)], ["TOP", "BOTTOM"], default="")
    return out


def calculate_signals(etf_weight: float = ETF_WEIGHT, bb_length: int = BB_LENGTH,
                      bb_mult: float = BB_MULT, mode: str = "composite",
                      allow_short: bool = False, persist: bool = True) -> pd.DataFrame:
    """mode=composite joins true ETF and IO PCR; mode=etf is explicitly preview-only."""
    if not 0 <= float(etf_weight) <= 1:
        raise ValueError("ETF 权重必须在 0~1")
    if int(bb_length) < 2 or float(bb_mult) <= 0:
        raise ValueError("BB 周期须 >=2，标准差倍数须 >0")
    if mode == "etf":
        etf = load_csv(ETF_FILE)
        if etf.empty:
            df = pd.DataFrame(columns=["date", "etf_pcr"])
        else:
            df = etf[[c for c in ["date", "etf_pcr", "etf_call_oi", "etf_put_oi"] if c in etf]].copy()
            df["composite_pcr"] = pd.to_numeric(df["etf_pcr"], errors="coerce")
    else:
        df = load_raw_pcr()
        if not df.empty:
            df["composite_pcr"] = float(etf_weight) * pd.to_numeric(df["etf_pcr"], errors="coerce") + (1 - float(etf_weight)) * pd.to_numeric(df["io_pcr"], errors="coerce")
    if df.empty:
        # Save a zero-row series with a stable schema; downstream UI can show the diagnosis.
        df = pd.DataFrame(columns=["date", "composite_pcr", "basis", "std", "upper", "lower", "top_signal", "bottom_signal", "signal", "position_state", "signal_name"])
    else:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
        df = add_bollinger_signals(df, "composite_pcr", bb_length, bb_mult, allow_short)
    df["mode"] = mode
    df["etf_weight"] = float(etf_weight)
    df["bb_length"] = int(bb_length)
    df["bb_mult"] = float(bb_mult)
    if persist:
        atomic_csv(df, SIGNAL_FILE)
        if not df.empty:
            atomic_csv(df[[c for c in ["date", "etf_pcr", "io_pcr", "composite_pcr", "basis", "upper", "lower", "signal", "signal_name", "position_state", "mode"] if c in df]].tail(1), LATEST_FILE)
        else:
            atomic_csv(df, LATEST_FILE)
    return df


def run_backtest(signal_df: Optional[pd.DataFrame] = None, initial_capital: float = INITIAL_CAPITAL,
                 cost_bps: float = COST_BPS, mode: str = "composite") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Never crash on empty dates: output diagnostic summary rather than raising."""
    if signal_df is None:
        signal_df = calculate_signals(mode=mode)
    index = load_csv(INDEX_FILE)
    if index.empty or signal_df.empty or "date" not in signal_df:
        reason = "缺少指数数据" if index.empty else "PCR/IO 没有可用于回测的共同日期"
        empty = pd.DataFrame()
        summary = pd.DataFrame([{"status": "INCOMPLETE", "reason": reason, "rows": 0}])
        atomic_csv(empty, BACKTEST_FILE)
        atomic_csv(summary, SUMMARY_FILE)
        logger.warning("回测未执行：%s。程序继续运行，图形界面会展示数据缺口。", reason)
        return empty, summary
    index["date"] = pd.to_datetime(index["date"], errors="coerce").dt.normalize()
    signal_df = signal_df.copy()
    signal_df["date"] = pd.to_datetime(signal_df["date"], errors="coerce").dt.normalize()
    # Do not fail hard if there is no exact overlap. The usual reason is IO history has not been accumulated.
    df = signal_df.merge(index[[c for c in ["date", "open", "close", "high", "low"] if c in index]], on="date", how="inner")
    if df.empty:
        summary = pd.DataFrame([{"status": "INCOMPLETE", "reason": "PCR 与沪深300没有共同交易日；检查数据源日期和 IO 历史覆盖范围", "rows": 0}])
        atomic_csv(df, BACKTEST_FILE)
        atomic_csv(summary, SUMMARY_FILE)
        logger.warning("PCR 与沪深300没有共同交易日；已跳过回测，不再抛出 RuntimeError")
        return df, summary
    df = df.sort_values("date").reset_index(drop=True)
    # T-day after-close signal executed at next available open; return open-to-next-open.
    df["position_at_open"] = pd.to_numeric(df["position_state"], errors="coerce").shift(1).fillna(0)
    df["next_open"] = pd.to_numeric(df["open"], errors="coerce").shift(-1)
    df["asset_return"] = df["next_open"] / pd.to_numeric(df["open"], errors="coerce") - 1
    df["gross_strategy_return"] = df["position_at_open"] * df["asset_return"]
    df["position_change"] = df["position_at_open"].diff().abs()
    if len(df):
        df.loc[0, "position_change"] = abs(float(df.loc[0, "position_at_open"]))
    df["cost"] = df["position_change"].fillna(0) * float(cost_bps) / 10000
    df["strategy_return"] = df["gross_strategy_return"] - df["cost"]
    df["benchmark_return"] = df["asset_return"]
    ret = df["strategy_return"].fillna(0)
    bm = df["benchmark_return"].fillna(0)
    df["equity_curve"] = float(initial_capital) * (1 + ret).cumprod()
    df["benchmark_curve"] = float(initial_capital) * (1 + bm).cumprod()
    peak = df["equity_curve"].cummax()
    df["drawdown"] = df["equity_curve"] / peak - 1
    valid = df["strategy_return"].replace([np.inf, -np.inf], np.nan).dropna()
    total_return = float(df["equity_curve"].iloc[-1] / initial_capital - 1)
    days = max((df["date"].iloc[-1] - df["date"].iloc[0]).days, 1)
    years = days / 365.25
    annual = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
    std = valid.std(ddof=0)
    sharpe = float(np.sqrt(252) * valid.mean() / std) if len(valid) > 1 and std > 0 else np.nan
    summary = pd.DataFrame([{
        "status": "OK" if len(df) > 20 else "INCOMPLETE",
        "reason": "" if len(df) > 20 else "可用于回测的 PCR 样本不足 21 个；结果仅供检查，不能视为有效绩效",
        "start": df["date"].iloc[0].date(), "end": df["date"].iloc[-1].date(), "rows": len(df),
        "initial_capital": initial_capital, "final_equity": df["equity_curve"].iloc[-1],
        "total_return": total_return, "annualized_return": annual,
        "sharpe": sharpe, "max_drawdown": float(df["drawdown"].min()),
        "signal_count": int((df["signal"].fillna(0) != 0).sum()),
        "benchmark_total_return": float(df["benchmark_curve"].iloc[-1] / initial_capital - 1),
        "mode": mode, "etf_weight": float(df.get("etf_weight", pd.Series([ETF_WEIGHT])).iloc[0]),
        "bb_length": int(df.get("bb_length", pd.Series([BB_LENGTH])).iloc[0]),
        "bb_mult": float(df.get("bb_mult", pd.Series([BB_MULT])).iloc[0]),
    }])
    atomic_csv(df, BACKTEST_FILE)
    atomic_csv(summary, SUMMARY_FILE)
    return df, summary


def fit_weight(anchors_path: Path = ROOT / "anchors.csv") -> pd.DataFrame:
    raw = load_raw_pcr()
    if raw.empty:
        raise RuntimeError("当前没有 ETF 与 IO 的历史重叠日期；需先导入历史 IO PCR CSV，才能拟合权重")
    if not anchors_path.exists():
        raise RuntimeError(f"找不到锚点文件：{anchors_path}")
    anchors = pd.read_csv(anchors_path, parse_dates=["date"])
    results = []
    for weight in np.arange(0, 1.0001, 0.01):
        df = raw.copy()
        df["composite_pcr"] = weight * df["etf_pcr"] + (1 - weight) * df["io_pcr"]
        calc = add_bollinger_signals(df, "composite_pcr", BB_LENGTH, BB_MULT)
        compared = anchors.merge(calc[["date", "upper", "lower"]], on="date", how="inner").dropna()
        if compared.empty:
            continue
        sse = float(((compared["upper"] - compared["upper_target"]) ** 2).sum() + ((compared["lower"] - compared["lower_target"]) ** 2).sum())
        results.append({"etf_weight": weight, "io_weight": 1 - weight, "anchor_count": len(compared), "sse": sse, "rmse": float(np.sqrt(sse / (2 * len(compared))))})
    if not results:
        raise RuntimeError("历史 PCR 没覆盖 anchors.csv 指定日期，无法拟合权重")
    result = pd.DataFrame(results).sort_values(["rmse", "etf_weight"]).reset_index(drop=True)
    atomic_csv(result, WEIGHT_FILE)
    return result

def _fetch_io_pcr_cffex_daily(trade_date):
    """
    从中金所官方历史日行情 XML 获取指定交易日的 IO 持仓量，
    汇总所有沪深300股指期权合约，计算 Put OI / Call OI。
    获取失败返回 None，不生成虚假数据。
    """
    d = pd.Timestamp(trade_date).normalize()
    ymd = d.strftime("%Y%m%d")
    ym = d.strftime("%Y%m")
    dd = d.strftime("%d")

    urls = [
        f"https://www.cffex.com.cn/fzjy/mrhq/{ym}/{dd}/index.xml",
        f"https://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{dd}/index.xml",
        f"http://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{dd}/index.xml",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.cffex.com.cn/",
    }

    def local_name(tag):
        return tag.rsplit("}", 1)[-1].lower()

    for url in urls:
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(5, 15),
            )

            if resp.status_code != 200 or not resp.content:
                continue

            root = ET.fromstring(resp.content)

            # 官方 XML 的逐合约记录通常为 dailydata 节点。
            nodes = [
                node for node in root.iter()
                if local_name(node.tag) == "dailydata"
            ]

            # 兼容 XML 结构发生变化的情况。
            if not nodes:
                nodes = [
                    node for node in root.iter()
                    if any(
                        local_name(child.tag) == "instrumentid"
                        for child in list(node)
                    )
                    and any(
                        local_name(child.tag) == "openinterest"
                        for child in list(node)
                    )
                ]

            call_oi = 0.0
            put_oi = 0.0
            matched = 0

            for node in nodes:
                fields = {
                    local_name(child.tag): (
                        child.text or ""
                    ).strip()
                    for child in list(node)
                }

                instrument = (
                    fields.get("instrumentid")
                    or fields.get("instrumentcode")
                    or ""
                ).upper().strip()

                if not instrument.startswith("IO"):
                    continue

                source_date = (
                    fields.get("tradingday")
                    or fields.get("date")
                    or ""
                )
                source_date = re.sub(
                    r"\D", "", source_date
                )

                if source_date and source_date != ymd:
                    continue

                oi_text = (
                    fields.get("openinterest")
                    or fields.get("open_interest")
                    or fields.get("position")
                )

                if oi_text is None:
                    continue

                oi = pd.to_numeric(
                    str(oi_text).replace(",", ""),
                    errors="coerce",
                )

                if pd.isna(oi) or oi < 0:
                    continue

                # IO2610-C-3800 -> IO2610C3800
                compact = re.sub(
                    r"[^A-Z0-9]", "", instrument
                )

                match = re.match(
                    r"^IO\d{4}([CP])\d+", compact
                )

                if not match:
                    continue

                if match.group(1) == "C":
                    call_oi += float(oi)
                else:
                    put_oi += float(oi)

                matched += 1

            if matched and call_oi > 0:
                return {
                    "date": d,
                    "io_call_oi": call_oi,
                    "io_put_oi": put_oi,
                    "io_pcr": put_oi / call_oi,
                    "io_months": "CFFEX_ALL",
                    "source": "CFFEX_OFFICIAL_DAILY_XML",
                    "observed_at": pd.Timestamp.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }

        except Exception as exc:
            logger.debug(
                "CFFEX IO 历史数据获取失败 %s: %s",
                ymd,
                exc,
            )

    logger.warning(
        "未取得 %s 的中金所 IO 历史持仓数据",
        ymd,
    )
    return None


def backfill_io_history():
    """
    按本地沪深300实际交易日回补 IO PCR。
    已有日期跳过，失败日期下次运行时重试。
    """
    ensure_dirs()

    index = load_csv(INDEX_FILE)

    if index.empty or "date" not in index.columns:
        raise RuntimeError(
            "沪深300行情为空，无法确定 IO 历史回补日期"
        )

    index["date"] = pd.to_datetime(
        index["date"], errors="coerce"
    ).dt.normalize()

    trading_dates = sorted(
        index["date"].dropna().unique()
    )

    old = load_csv(IO_FILE)

    if old.empty:
        old = pd.DataFrame(
            columns=[
                "date",
                "io_call_oi",
                "io_put_oi",
                "io_pcr",
                "io_months",
                "source",
                "observed_at",
            ]
        )
        existing_dates = set()
    else:
        old["date"] = pd.to_datetime(
            old["date"], errors="coerce"
        ).dt.normalize()

        old = old.dropna(subset=["date"])
        existing_dates = set(
            old["date"].dt.strftime("%Y-%m-%d")
        )

    new_rows = []

    for trade_date in trading_dates:
        key = pd.Timestamp(trade_date).strftime(
            "%Y-%m-%d"
        )

        if key in existing_dates:
            continue

        row = _fetch_io_pcr_cffex_daily(trade_date)

        if row is not None:
            new_rows.append(row)
            existing_dates.add(key)

            logger.info(
                "IO 历史回补 %s | Call OI=%.0f | "
                "Put OI=%.0f | PCR=%.6f",
                key,
                row["io_call_oi"],
                row["io_put_oi"],
                row["io_pcr"],
            )

        # 控制请求频率，减少对公开数据接口的压力。
        time.sleep(0.15)

    if new_rows:
        new_df = pd.DataFrame(new_rows)

        out = upsert_dates(
            old,
            new_df,
            keep="last",
        )

        out["date"] = pd.to_datetime(
            out["date"], errors="coerce"
        ).dt.normalize()

        out = out.dropna(
            subset=["date"]
        ).sort_values("date")

        atomic_csv(out, IO_FILE)

    else:
        out = old

    # 汇报真正能够参与综合 PCR 计算的共同日期数量。
    etf = load_csv(ETF_FILE)

    if not etf.empty and not out.empty:
        etf["date"] = pd.to_datetime(
            etf["date"], errors="coerce"
        ).dt.normalize()

        out["date"] = pd.to_datetime(
            out["date"], errors="coerce"
        ).dt.normalize()

        common_dates = set(
            etf["date"].dropna()
        ) & set(
            out["date"].dropna()
        )
    else:
        common_dates = set()

    logger.info(
        "IO 历史回补完成：IO=%d 行，PCR/IO 共同日期=%d",
        len(out),
        len(common_dates),
    )

    return out

def update_pipeline(start_date: str = DEFAULT_START_DATE, full_backfill: bool = False,
                    etf_weight: float = ETF_WEIGHT, bb_length: int = BB_LENGTH,
                    bb_mult: float = BB_MULT, progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """One-stop update: index -> ETF daily stats -> live IO snapshot -> signals -> backtest."""
    ensure_dirs()
    index = update_index_history(start_date, full_backfill=full_backfill)
    etf = update_etf_history(start_date, full_backfill=full_backfill, progress=progress)
    io_error = ""
    try:
        #update_io_snapshot()
        # 先回补历史 IO 持仓数据，再尝试更新当日快照。
        try:
            backfill_io_history()
        except Exception:
            logger.exception(
                "IO 历史数据回补失败；继续尝试更新当日快照"
            )

        try:
            update_io_snapshot()
        except Exception:
            logger.exception(
                "IO 快照更新失败；保留旧缓存并继续进行信号计算"
            )
        
    except Exception as exc:
        io_error = str(exc)
        logger.exception("IO 快照更新失败；保留旧缓存并继续进行信号计算")
    factor = calculate_signals(etf_weight, bb_length, bb_mult, mode="composite")
    backtest, summary = run_backtest(factor, mode="composite")
    save_meta(last_pipeline_at=now_cn().isoformat(timespec="seconds"),
              index_rows=len(index), etf_rows=len(etf), io_rows=len(load_csv(IO_FILE)),
              common_rows=len(factor), io_update_error=io_error)
    return {"index": index, "etf": etf, "io": load_csv(IO_FILE), "signals": factor,
            "backtest": backtest, "summary": summary, "io_error": io_error}


def daemon_loop(start_date: str = DEFAULT_START_DATE, run_time: str = "15:20", poll_seconds: int = 60):
    """Run at most once per China-market date after close; never stamps holidays as trade days."""
    hour, minute = [int(part) for part in run_time.split(":", 1)]
    logger.info("自动更新守护进程启动：每日北京时间 %s 后运行", run_time)
    last_done = ""
    last_attempt_day = ""
    last_attempt_at = None
    while True:
        now = now_cn()
        day_key = now.date().isoformat()
        after_close = now.time() >= dt_time(hour, minute)
        retry_due = (last_attempt_day != day_key or last_attempt_at is None or (now - last_attempt_at).total_seconds() >= 1800)
        if after_close and last_done != day_key and retry_due:
            last_attempt_day = day_key
            last_attempt_at = now
            try:
                # Refresh the index first; only collect an IO daily snapshot if today's index bar exists.
                update_index_history(start_date, full_backfill=False)
                idx = load_csv(INDEX_FILE)
                latest_index = str(idx["date"].max().date()) if not idx.empty else ""
                if latest_index == day_key:
                    update_pipeline(start_date=start_date, full_backfill=False)
                    save_meta(auto_daily_update_date=day_key)
                    last_done = day_key
                    logger.info("每日自动更新完成：%s", day_key)
                elif now.time() >= dt_time(20, 0):
                    last_done = day_key
                    logger.info("%s 未检测到沪深300当日行情，可能为休市日，今日跳过 IO 快照", day_key)
            except Exception:
                logger.exception("每日自动更新失败；稍后重试")
        time.sleep(max(10, int(poll_seconds)))


def data_status() -> dict:
    etf, io, index = load_csv(ETF_FILE), load_csv(IO_FILE), load_csv(INDEX_FILE)
    common = 0
    common_dates = []
    if not etf.empty and not io.empty:
        common_dates = sorted(set(etf["date"].dt.strftime("%Y-%m-%d")) & set(io["date"].dt.strftime("%Y-%m-%d")))
        common = len(common_dates)
    return {
        "etf_rows": len(etf), "io_rows": len(io), "index_rows": len(index), "common_rows": common,
        "etf_last": str(etf["date"].max().date()) if not etf.empty else "无",
        "io_last": str(io["date"].max().date()) if not io.empty else "无",
        "index_last": str(index["date"].max().date()) if not index.empty else "无",
        "common_first": common_dates[0] if common_dates else "无",
        "common_last": common_dates[-1] if common_dates else "无",
        "meta": read_meta(),
    }
