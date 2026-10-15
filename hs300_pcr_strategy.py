#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI entrypoint. Existing command `run --full-backfill --etf-start ...` remains supported."""
from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path
import pandas as pd
import pcr_app_core as core


def main():
    parser = argparse.ArgumentParser(description="HS300 Option PCR Strategy (AkShare, no token)")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="一键更新数据、计算信号、运行回测")
    run.add_argument("--full-backfill", action="store_true", help="补齐起始日期以来缺失的 ETF PCR 历史")
    run.add_argument("--etf-start", default=core.DEFAULT_START_DATE)
    run.add_argument("--etf-weight", type=float, default=core.ETF_WEIGHT)
    run.add_argument("--bb-length", type=int, default=core.BB_LENGTH)
    run.add_argument("--bb-mult", type=float, default=core.BB_MULT)
    run.add_argument("--mode", choices=["composite", "etf"], default="composite")
    sub.add_parser("update-data", help="增量更新所有数据并重算")
    imp = sub.add_parser("import-io-csv", help="导入历史 IO PCR/OI CSV")
    imp.add_argument("path")
    sub.add_parser("signals", help="重新计算综合 PCR、BB 和信号")
    sub.add_parser("backtest", help="运行回测")
    sub.add_parser("fit-weight", help="用 anchors.csv 校准 ETF/IO 权重")
    daemon = sub.add_parser("daemon", help="每日自动更新守护进程")
    daemon.add_argument("--etf-start", default=core.DEFAULT_START_DATE)
    daemon.add_argument("--time", default="15:20")
    daemon.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()

    try:
        if args.cmd in (None, "run", "update-data"):
            if args.cmd is None:
                parser.print_help()
                return 0
            if args.cmd == "update-data":
                start_date = core.DEFAULT_START_DATE
                full_backfill = False
                etf_weight, bb_length, bb_mult = core.ETF_WEIGHT, core.BB_LENGTH, core.BB_MULT
            else:
                start_date = args.etf_start
                full_backfill = args.full_backfill
                etf_weight, bb_length, bb_mult = args.etf_weight, args.bb_length, args.bb_mult
            result = core.update_pipeline(start_date, full_backfill, etf_weight, bb_length, bb_mult)
            status = core.data_status()
            print("\n========== 运行结果 ==========")
            print(f"沪深300行情：{status['index_rows']} 行，最新 {status['index_last']}")
            print(f"ETF PCR：{status['etf_rows']} 行，最新 {status['etf_last']}")
            print(f"IO PCR：{status['io_rows']} 行，最新 {status['io_last']}")
            print(f"ETF/IO 共同日期：{status['common_rows']} 行")
            if status["common_rows"] < bb_length:
                print(f"提示：综合 PCR 共同样本少于 BB 周期 {bb_length}，暂不能生成可靠的完整策略信号。不会填造历史 IO 数据。")
            if result["io_error"]:
                print(f"IO 更新告警：{result['io_error']}")
            if not result["summary"].empty:
                print(result["summary"].to_string(index=False))
            print("图形界面：运行 ./start.sh 后在浏览器打开 http://localhost:8501")
            return 0
        if args.cmd == "import-io-csv":
            df = core.import_io_csv(args.path)
            print(f"导入完成：当前 IO 历史 {len(df)} 行")
            return 0
        if args.cmd == "signals":
            df = core.calculate_signals()
            print(f"信号序列已更新，共 {len(df)} 行；文件：{core.SIGNAL_FILE}")
            return 0
        if args.cmd == "backtest":
            _, summary = core.run_backtest()
            print(summary.to_string(index=False))
            return 0
        if args.cmd == "fit-weight":
            fit = core.fit_weight()
            print(fit.head(10).to_string(index=False))
            return 0
        if args.cmd == "daemon":
            core.daemon_loop(args.etf_start, args.time, args.poll_seconds)
            return 0
    except KeyboardInterrupt:
        print("已停止")
        return 130
    except Exception as exc:
        logging.exception("运行失败")
        print(f"运行失败：{exc}", file=sys.stderr)
        print("如果是数据源不可用，可先打开图形界面查看本地缓存；请勿把缺失 IO 历史补成虚假数据。", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
