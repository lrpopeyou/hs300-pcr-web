#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Streamlit trading dashboard styled after the uploaded TradingView screenshots."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import pcr_app_core as core

st.set_page_config(page_title="沪深300期权 PCR 交易系统", page_icon="📈", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
  .stApp { background: #0e1420; color: #e8edf5; }
  [data-testid="stSidebar"] { background: #111a29; }
  [data-testid="stMetric"] { background: #151f2f; border: 1px solid #27374a; border-radius: 8px; padding: 12px 14px; }
  div[data-testid="stAlert"] { border-radius: 8px; }
  h1,h2,h3 { letter-spacing: .2px; }
  .small-muted { color: #91a0b4; font-size: 0.82rem; }
  div[data-testid="stDataFrame"] { border: 1px solid #26364a; border-radius: 6px; }
</style>
""", unsafe_allow_html=True)

# Optional auto rerun: data is auto-synced at most once per China-market day after close.
auto_enabled_default = True
with st.sidebar:
    st.title("⚙️ 策略控制台")
    start_date = st.date_input("历史数据起始日期", value=pd.Timestamp(core.DEFAULT_START_DATE).date(), min_value=pd.Timestamp("2019-12-23").date())
    etf_weight = st.slider("ETF PCR 权重", 0.0, 1.0, float(core.ETF_WEIGHT), 0.05)
    bb_length = st.number_input("BB 周期", min_value=2, max_value=100, value=int(core.BB_LENGTH), step=1)
    bb_mult = st.number_input("标准差倍数", min_value=0.1, max_value=5.0, value=float(core.BB_MULT), step=0.1)
    allow_short = st.checkbox("允许 TOP 后持有空头（仅用于研究回测）", value=False)
    view_mode = st.radio("指标展示模式", ["自动选择", "综合 PCR（ETF + IO）", "ETF PCR 历史预览"], index=0)
    lookback = st.selectbox("图表显示区间", ["最近 3 个月", "最近 6 个月", "最近 1 年", "全部历史"], index=2)
    st.divider()
    full_backfill = st.checkbox("一键更新时补齐全部缺失 ETF 历史", value=True)
    update_button = st.button("🔄 一键更新数据 + 计算信号", type="primary", use_container_width=True)
    auto_refresh = st.checkbox("启用界面定时刷新", value=auto_enabled_default)
    st.caption("自动更新由启动脚本的每日守护进程负责；网页自动刷新只读取本地缓存。IO 历史必须来自真实快照或导入，不会用假数据补齐。")

if auto_refresh:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=5 * 60 * 1000, key="pcr_auto_refresh")
    except Exception:
        st.caption("安装 streamlit-autorefresh 后可启用定时页面刷新。")


def do_update(full: bool):
    with st.status("正在更新数据并计算交易信号……", expanded=True) as status:
        st.write("① 更新沪深300指数历史行情")
        st.write("② 补齐 510300 ETF 期权每日持仓 PCR")
        if full:
            st.write("③ 检查并采集 IO 当前持仓快照（历史 IO 需另行导入）")
        try:
            progress_bar = st.progress(0.0, text="准备更新")
            def progress(i, total, message):
                progress_bar.progress(min(i / max(total, 1), 1.0), text=f"{message} ({i}/{total})")
            result = core.update_pipeline(
                start_date=pd.Timestamp(start_date).strftime("%Y%m%d"),
                full_backfill=full,
                etf_weight=etf_weight,
                bb_length=int(bb_length),
                bb_mult=float(bb_mult),
                progress=progress,
            )
            status.update(label="数据更新与计算完成", state="complete", expanded=False)
            if result.get("io_error"):
                st.warning(f"IO 实时持仓更新失败，保留旧缓存：{result['io_error']}")
            st.success("处理完成。图表和数据状态已刷新。")
        except Exception as exc:
            status.update(label="更新未完全成功", state="error", expanded=True)
            st.error(f"本次数据更新失败：{exc}")
            st.info("如果网络或上游站点暂时异常，可先使用本地缓存；稍后再点一次更新。")

if update_button:
    do_update(full_backfill)

# 数据每日自动同步由 start.sh 启动的 daemon 进程负责；界面刷新只重读本地缓存，不反复请求上游网站。

# Always use parameters currently selected in UI.
signal_mode = "composite"
composite_df = core.calculate_signals(etf_weight, int(bb_length), float(bb_mult), mode="composite", allow_short=allow_short)
etf_preview_df = core.calculate_signals(etf_weight, int(bb_length), float(bb_mult), mode="etf", allow_short=allow_short, persist=False)
index_df = core.load_csv(core.INDEX_FILE)
status_data = core.data_status()

valid_composite = (not composite_df.empty and "upper" in composite_df and composite_df["upper"].notna().sum() >= 1)
if view_mode == "综合 PCR（ETF + IO）":
    selected_mode = "composite"
elif view_mode == "ETF PCR 历史预览":
    selected_mode = "etf"
else:
    selected_mode = "composite" if (not composite_df.empty and composite_df.get("io_pcr", pd.Series(dtype=float)).notna().sum() >= int(bb_length)) else "etf"

indicator_df = composite_df if selected_mode == "composite" else etf_preview_df
indicator_label = "综合 PCR" if selected_mode == "composite" else "ETF PCR 历史预览"
mode_is_preview = selected_mode == "etf"

# Apply chosen chart period.
if not index_df.empty:
    index_df["date"] = pd.to_datetime(index_df["date"], errors="coerce").dt.normalize()
    last_day = index_df["date"].max()
    if lookback == "最近 3 个月":
        cutoff = last_day - pd.DateOffset(months=3)
    elif lookback == "最近 6 个月":
        cutoff = last_day - pd.DateOffset(months=6)
    elif lookback == "最近 1 年":
        cutoff = last_day - pd.DateOffset(years=1)
    else:
        cutoff = index_df["date"].min()
    index_view = index_df[index_df["date"] >= cutoff].copy()
else:
    cutoff = pd.Timestamp("1900-01-01")
    index_view = pd.DataFrame()
if not indicator_df.empty:
    indicator_view = indicator_df[pd.to_datetime(indicator_df["date"]) >= cutoff].copy()
else:
    indicator_view = indicator_df.copy()

# 统一两个图表的日期类型，避免时间戳不一致导致悬停错位
for _df in (index_view, indicator_view):
    if not _df.empty:
        _df["date"] = (
            pd.to_datetime(_df["date"], errors="coerce")
            .dt.tz_localize(None)
            .dt.normalize()
        )
        _df.dropna(subset=["date"], inplace=True)
        _df.sort_values("date", inplace=True)
        _df.reset_index(drop=True, inplace=True)

st.title("沪深300期权 PCR 交易系统")
st.markdown("<div class='small-muted'>AkShare 免费公开数据 · 本地缓存 · BB(20,2) 可调 · 研究/回测系统，不自动下单</div>", unsafe_allow_html=True)

# Latest observations and explicit data coverage
latest_idx = index_df.iloc[-1] if not index_df.empty else None
latest_ind = indicator_df.iloc[-1] if not indicator_df.empty else None
latest_pcr = np.nan
if latest_ind is not None:
    latest_pcr = pd.to_numeric(pd.Series([latest_ind.get("composite_pcr", np.nan)]), errors="coerce").iloc[0]
latest_upper = pd.to_numeric(pd.Series([latest_ind.get("upper", np.nan) if latest_ind is not None else np.nan]), errors="coerce").iloc[0]
latest_lower = pd.to_numeric(pd.Series([latest_ind.get("lower", np.nan) if latest_ind is not None else np.nan]), errors="coerce").iloc[0]
latest_signal_name = str(latest_ind.get("signal_name", "") or "无新信号") if latest_ind is not None else "暂无信号"

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("沪深300最新收盘", f"{float(latest_idx['close']):,.2f}" if latest_idx is not None and pd.notna(latest_idx.get("close")) else "暂无")
m2.metric(f"{indicator_label}", f"{latest_pcr:.4f}" if np.isfinite(latest_pcr) else "暂无")
m3.metric("BB 上轨", f"{latest_upper:.4f}" if np.isfinite(latest_upper) else "样本不足")
m4.metric("BB 下轨", f"{latest_lower:.4f}" if np.isfinite(latest_lower) else "样本不足")
m5.metric("最新信号", latest_signal_name)

if mode_is_preview:
    st.warning("当前展示的是 **ETF PCR 历史预览通道**，不是 ETF/IO 加权后的正式综合 PCR 策略。原因是当前本地 IO 历史覆盖不足；请每天更新并积累 IO 数据，或在「数据管理」区导入历史 IO PCR CSV。")
if status_data["common_rows"] < int(bb_length):
    st.info(f"当前 ETF 与 IO 按日期真实匹配的样本只有 {status_data['common_rows']} 行，BB 周期设为 {int(bb_length)}。正式综合 PCR 的上下轨/信号尚未形成或不完整；系统不会把最新 IO 持仓快照反向填充成历史数据。")

# Main view mirrors the supplied second screenshot: PCR strategy on the left, HS300 price chart on the right.
st.subheader("期权 PCR 交易策略（左） · 沪深300指数走势（右）")
if index_view.empty:
    st.warning("本地还没有沪深300行情。点击左侧「一键更新数据 + 计算信号」初始化。")
else:
    chart_left, chart_right = st.columns(2, gap="medium")
    with chart_left:
        fig_pcr = go.Figure()
        if not indicator_view.empty:
            x = pd.to_datetime(indicator_view["date"])
            ycol = "etf_pcr" if mode_is_preview else "composite_pcr"
            if ycol in indicator_view:
                fig_pcr.add_trace(go.Scatter(
                    x=x, y=indicator_view[ycol], mode="lines", name=indicator_label,
                    line=dict(color="#e8eef8", width=1.8), connectgaps=False,
                ))
            for col, name, color, dash in [
                ("upper", "BB 上轨", "#438cf4", "solid"),
                ("basis", "BB 中轨", "#8391a5", "dot"),
                ("lower", "BB 下轨", "#438cf4", "solid"),
            ]:
                if col in indicator_view:
                    fig_pcr.add_trace(go.Scatter(
                        x=x, y=indicator_view[col], mode="lines", name=name,
                        line=dict(color=color, width=1.6, dash=dash), connectgaps=False,
                    ))
            if "top_signal" in indicator_view:
                tops = indicator_view[indicator_view["top_signal"].fillna(False)]
                bottoms = indicator_view[indicator_view["bottom_signal"].fillna(False)]
                if not tops.empty:
                    fig_pcr.add_trace(go.Scatter(
                        x=tops["date"], y=tops[ycol], mode="markers+text", name="TOP",
                        text=["TOP"] * len(tops), textposition="top center",
                        marker=dict(symbol="triangle-down", size=10, color="#ff6b5f"),
                    ))
                if not bottoms.empty:
                    fig_pcr.add_trace(go.Scatter(
                        x=bottoms["date"], y=bottoms[ycol], mode="markers+text", name="BOTTOM",
                        text=["BOTTOM"] * len(bottoms), textposition="bottom center",
                        marker=dict(symbol="triangle-up", size=10, color="#25c4a2"),
                    ))
        # fig_pcr.update_layout(
        #     template="plotly_dark", height=620,
        #     title=dict(text=f"{indicator_label} · BB({int(bb_length)}, {float(bb_mult):g})", x=0.02, y=0.97),
        #     paper_bgcolor="#0e1420", plot_bgcolor="#111a28",
        #     margin=dict(l=8, r=12, t=70, b=28), hovermode="x unified",
        #     legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=10)),
        # )
        # fig_pcr.update_xaxes(showgrid=True, gridcolor="#283343", rangeslider_visible=False)
        # fig_pcr.update_yaxes(showgrid=True, gridcolor="#283343", side="right", title_text="PCR")
        # ##
        fig_pcr.update_layout(
            template="plotly_dark",
            height=620,
            title=dict(
                text=f"{indicator_label} · BB({int(bb_length)}, {float(bb_mult):g})",
                x=0.02,
                y=0.97,
            ),
            paper_bgcolor="#0e1420",
            plot_bgcolor="#111a28",
            margin=dict(l=8, r=12, t=70, b=28),

            # 关键：鼠标移动时始终寻找最近的数据点
            hovermode="x unified",
            hoverdistance=-1,
            spikedistance=-1,

            hoverlabel=dict(
                bgcolor="#172438",
                bordercolor="#438cf4",
                font=dict(color="#ffffff", size=12),
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.01,
                xanchor="left",
                x=0,
                font=dict(size=10),
            ),
        )

        fig_pcr.update_xaxes(
            showgrid=True,
            gridcolor="#283343",
            rangeslider_visible=False,
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            spikethickness=1,
            spikecolor="#91a0b4",
            hoverformat="%Y-%m-%d",
        )

        fig_pcr.update_yaxes(
            showgrid=True,
            gridcolor="#283343",
            side="right",
            title_text="PCR",
        )
        ### 

        st.plotly_chart(fig_pcr, use_container_width=True, config={"displaylogo": False, "scrollZoom": True}, key="pcr_chart")
        if mode_is_preview:
            st.caption("⚠ ETF-only 历史参考通道；不是 ETF/IO 加权后的正式策略信号。")
        else:
            st.caption(f"ETF 权重 {etf_weight:.0%} · IO 权重 {1-etf_weight:.0%} · 仅绘制真实日期匹配的数据。")

    with chart_right:
        fig_price = go.Figure()

        # 日期统一、排序，确保每个交易日对应一个数据点
        price_data = index_view.copy()
        price_data["date"] = (
            pd.to_datetime(price_data["date"], errors="coerce")
            .dt.tz_localize(None)
            .dt.normalize()
        )
        price_data = (
            price_data.dropna(subset=["date", "open", "high", "low", "close"])
            .sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")
            .reset_index(drop=True)
        )

        # K线负责显示，不再负责悬停提示
        fig_price.add_trace(go.Candlestick(
            x=price_data["date"],
            open=price_data["open"],
            high=price_data["high"],
            low=price_data["low"],
            close=price_data["close"],
            name="沪深300",
            increasing_line_color="#f05b63",
            decreasing_line_color="#20a88a",
            increasing_fillcolor="#f05b63",
            decreasing_fillcolor="#20a88a",
            hoverinfo="skip",
        ))

        # 新增：每个交易日一个辅助悬停点
        # 透明点覆盖收盘价位置，使鼠标横向移动时能定位到对应交易日
        fig_price.add_trace(go.Scatter(
            x=price_data["date"],
            y=price_data["close"],
            mode="markers",
            name="每日行情",
            showlegend=False,
            customdata=price_data[["open", "high", "low", "close"]].to_numpy(),
            marker=dict(
                size=14,
                color="rgba(255,255,255,0.01)",
                line=dict(width=0),
            ),
            hovertemplate=(
                "日期：%{x|%Y-%m-%d}<br>"
                "开盘：%{customdata[0]:.2f}<br>"
                "最高：%{customdata[1]:.2f}<br>"
                "最低：%{customdata[2]:.2f}<br>"
                "收盘：%{customdata[3]:.2f}"
                "<extra></extra>"
            ),
        ))


        if not indicator_view.empty and "signal_name" in indicator_view:
            #price_markers = index_view.merge(indicator_view[["date", "signal_name"]], on="date", how="inner")
            price_markers = price_data.merge(
                indicator_view[["date", "signal_name"]],
                on="date",
                how="inner",
            )
            price_tops = price_markers[price_markers["signal_name"] == "TOP"]
            price_bottoms = price_markers[price_markers["signal_name"] == "BOTTOM"]
            if not price_tops.empty:
                fig_price.add_trace(go.Scatter(x=price_tops["date"], y=price_tops["high"] * 1.004, mode="markers+text",
                    name="TOP", text=["TOP"] * len(price_tops), textposition="top center",
                    marker=dict(symbol="triangle-down", size=10, color="#ff6b5f")))
            if not price_bottoms.empty:
                fig_price.add_trace(go.Scatter(x=price_bottoms["date"], y=price_bottoms["low"] * 0.996, mode="markers+text",
                    name="BOTTOM", text=["BOTTOM"] * len(price_bottoms), textposition="bottom center",
                    marker=dict(symbol="triangle-up", size=10, color="#25c4a2")))
        # fig_price.update_layout(
        #     template="plotly_dark", height=620,
        #     title=dict(text="沪深300指数（日 K）", x=0.02, y=0.97),
        #     paper_bgcolor="#0e1420", plot_bgcolor="#111a28",
        #     margin=dict(l=8, r=12, t=70, b=28), hovermode="x unified",
        #     xaxis_rangeslider_visible=False, showlegend=True,
        #     legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=10)),
        # )
        # fig_price.update_xaxes(showgrid=True, gridcolor="#283343", rangeslider_visible=False)
        # fig_price.update_yaxes(showgrid=True, gridcolor="#283343", side="right", title_text="点位")
        
        ###
        fig_price.update_layout(
            template="plotly_dark",
            height=620,
            title=dict(text="沪深300指数（日 K）", x=0.02, y=0.97),
            paper_bgcolor="#0e1420",
            plot_bgcolor="#111a28",
            margin=dict(l=8, r=12, t=70, b=28),

            # 按横坐标匹配最近交易日
            hovermode="x unified",
            hoverdistance=-1,
            spikedistance=-1,

            hoverlabel=dict(
                bgcolor="#172438",
                bordercolor="#438cf4",
                font=dict(color="#ffffff", size=12),
            ),

            xaxis_rangeslider_visible=False,
            showlegend=True,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.01,
                xanchor="left",
                x=0,
                font=dict(size=10),
            ),
        )

        fig_price.update_xaxes(
            showgrid=True,
            gridcolor="#283343",
            rangeslider_visible=False,
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            spikethickness=1,
            spikecolor="#91a0b4",
            hoverformat="%Y-%m-%d",
        )

        fig_price.update_yaxes(
            showgrid=True,
            gridcolor="#283343",
            side="right",
            title_text="点位",
        )
        ###
        
        
        st.plotly_chart(fig_price, use_container_width=True, config={"displaylogo": False, "scrollZoom": True}, key="hs300_chart")
        st.caption("红色 K 线代表上涨、绿色 K 线代表下跌；标记位置按信号日期映射到指数图。")

# Data status and controls
st.divider()
st.subheader("数据状态与自动化")
s1, s2, s3, s4, s5 = st.columns(5)
s1.metric("沪深300行情", f"{status_data['index_rows']} 条", status_data["index_last"])
s2.metric("ETF PCR 历史", f"{status_data['etf_rows']} 条", status_data["etf_last"])
s3.metric("IO PCR 快照/历史", f"{status_data['io_rows']} 条", status_data["io_last"])
s4.metric("ETF/IO 共同日期", f"{status_data['common_rows']} 条", status_data["common_last"])
s5.metric("自动更新设置", "已启用" if auto_refresh else "已关闭", "收盘后检查" if auto_refresh else "手动更新")

if status_data["io_rows"] <= 1:
    st.warning("当前 IO 数据还只是 1 条或很少的历史快照。可以从现在开始每日积累，或导入历史 IO PCR。没有真实历史 IO 数据时，不可能严格复刻历史综合 PCR 的标准差轨道。")

with st.expander("导入历史 IO PCR CSV（可选）", expanded=False):
    st.write("支持两种 CSV 格式：`date,io_pcr`，或者 `date,io_call_oi,io_put_oi`。日期需是交易日，数据必须是对应日期的历史持仓，不要将今天的持仓复制到过去日期。")
    uploaded = st.file_uploader("选择 IO 历史 CSV", type=["csv"], key="io_csv_uploader")
    if uploaded is not None and st.button("导入并重新计算", key="import_io_button"):
        temp_path = core.DATA_DIR / f"uploaded_{uploaded.name}"
        temp_path.write_bytes(uploaded.getvalue())
        try:
            imported = core.import_io_csv(str(temp_path))
            signals = core.calculate_signals(etf_weight, int(bb_length), float(bb_mult), mode="composite", allow_short=allow_short)
            _, summary = core.run_backtest(signals, mode="composite")
            st.success(f"导入完成，当前 IO 历史 {len(imported)} 行。")
            st.rerun()
        except Exception as exc:
            st.error(f"导入失败：{exc}")
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

with st.expander("最近交易信号", expanded=True):
    if not indicator_df.empty:
        signals = indicator_df[indicator_df.get("signal", pd.Series(0, index=indicator_df.index)).fillna(0) != 0].copy()
        if not signals.empty:
            cols = [c for c in ["date", "signal_name", "etf_pcr", "io_pcr", "composite_pcr", "basis", "upper", "lower"] if c in signals]
            st.dataframe(signals[cols].sort_values("date", ascending=False).head(30), use_container_width=True, hide_index=True)
        else:
            st.caption("所选模式当前没有新 TOP/BOTTOM 突破信号，或标准差样本不足。")
    else:
        st.caption("尚无可显示的指标数据。")

with st.expander("回测结果（研究用途）", expanded=False):
    _, summary = core.run_backtest(indicator_df, mode=selected_mode)
    if summary is not None and not summary.empty:
        if "status" in summary and summary.iloc[0].get("status") == "INCOMPLETE":
            st.warning(str(summary.iloc[0].get("reason", "回测样本不足")))
        st.dataframe(summary, use_container_width=True, hide_index=True)
    bt = core.load_csv(core.BACKTEST_FILE)
    if not bt.empty and "equity_curve" in bt:
        eq = go.Figure()
        eq.add_trace(go.Scatter(x=bt["date"], y=bt["equity_curve"], name="策略净值", line=dict(color="#4b93ff", width=2)))
        if "benchmark_curve" in bt:
            eq.add_trace(go.Scatter(x=bt["date"], y=bt["benchmark_curve"], name="沪深300买入持有", line=dict(color="#8e9bad", width=1.5)))
        eq.update_layout(template="plotly_dark", height=300, margin=dict(l=15,r=15,t=15,b=15), paper_bgcolor="#0e1420", plot_bgcolor="#111a28")
        st.plotly_chart(eq, use_container_width=True)

with st.expander("下载本地数据", expanded=False):
    download_files = [("ETF PCR 历史 CSV", core.ETF_FILE), ("IO PCR 数据 CSV", core.IO_FILE), ("沪深300行情 CSV", core.INDEX_FILE),
                      ("PCR+布林带+信号 CSV", core.SIGNAL_FILE), ("回测明细 CSV", core.BACKTEST_FILE), ("回测摘要 CSV", core.SUMMARY_FILE)]
    columns = st.columns(3)
    for i, (label, path) in enumerate(download_files):
        if path.exists() and path.stat().st_size > 0:
            columns[i % 3].download_button(label, data=path.read_bytes(), file_name=path.name, mime="text/csv", key=f"download_{path.name}")
        else:
            columns[i % 3].caption(f"{label}：暂无文件")

st.caption("研究系统，不会连接券商或自动下单。实盘前需核验数据源、合约口径、复权/交易价格、费用、滑点、换月和成交规则。")
