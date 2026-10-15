# 沪深300期权 PCR 图形化交易系统（AkShare 免费数据版）

这是一个本地运行的研究/回测系统，不需要 Tushare Token。界面用 Streamlit + Plotly 绘制，视觉上尽量接近你提供的 TradingView 截图：上半部分是沪深300日K，下半部分是 PCR 与 BB 上下轨，信号用 TOP/BOTTOM 三角标记。

## 一键启动

### macOS

首次启动（终端）：

```bash
cd /你的路径/hs300_pcr_akshare
chmod +x start.command start.sh
./start.command
```

或者双击 `start.command`。脚本会自动创建 `.venv`、安装依赖、更新数据、计算信号、启动每日收盘后的自动更新进程，并打开本地 Streamlit 应用。浏览器地址：`http://localhost:8501`。

后续仍然只需执行 `./start.command`。需要停止每日自动更新进程时，可执行 `./stop.sh`（图形界面在运行终端按 Ctrl+C 关闭）。如需把回补起点改为其他日期：

```bash
PCR_START_DATE=20240501 ./start.sh
```

### Windows

双击 `start.bat`，首次启动会建立虚拟环境、安装依赖、同步一次数据并启动页面。

### 命令行兼容

修复后，原来这条命令仍然可用，不会因为无共同日期直接抛出 `RuntimeError`：

```bash
python hs300_pcr_strategy.py run --full-backfill --etf-start 20240501
```

如果直接使用系统 Python，请先执行 `pip install -r requirements.txt`；推荐使用启动脚本自动管理虚拟环境。

## 图形界面功能

- 沪深300指数日K图，与 PCR 指标共享日期轴；
- 复刻 `BB(20, 2)` 的中轨、上轨、下轨，可调周期、标准差倍数、ETF 权重；
- TOP / BOTTOM 突破信号，并在 PCR 图和价格图标记；
- 自动选择模式：IO 历史不足时显示 ETF PCR 历史预览，并清楚标注它不是正式加权策略；
- 一键同步数据、重新计算、回测摘要、净值曲线；
- CSV 上传导入历史 IO PCR，CSV 下载导出本地数据；
- 启动后每日北京时间 15:20 自动检查并同步当天数据，日志写入 `logs/`。

## 数据与关键限制

1. **ETF PCR 历史回补**：基于 AkShare 的 `option_daily_stats_sse(date=YYYYMMDD)`，只对沪深300交易日请求数据，避免逐日请求周末/节假日。
2. **IO PCR**：基于 AkShare 的 `option_cffex_hs300_list_sina()` 和 `option_cffex_hs300_spot_sina(symbol=...)` 汇总可见月份的 Call/Put OI。系统仅在北京时间 15:20 后且当天已有沪深300行情时保存快照；这是当天持仓快照，不是对应过去每个交易日的 OI 历史。
3. 因为不能伪造历史 IO OI，默认数据源无法单独重建完整历史加权 PCR。程序会从首次运行开始保存 IO 快照；想进行历史综合 PCR 回测，需要导入真实历史 IO PCR/OI CSV。
4. 在 IO 历史尚不足 20 个共同交易日时，页面会展示 ETF-only 参考通道并给出提示；此通道只用来检查界面与参考走势，不能当成 ETF/IO 加权策略表现。

## 导入历史 IO CSV

格式一（直接提供 PCR）：

```csv
date,io_pcr
2026-05-06,0.79
2026-05-07,0.81
```

格式二（提供持仓量）：

```csv
date,io_call_oi,io_put_oi
2026-05-06,123456,98765
2026-05-07,124000,99500
```

在网页的「导入历史 IO PCR CSV」区域上传即可；或命令行：

```bash
python hs300_pcr_strategy.py import-io-csv io_history.csv
```

只能导入真实日期对应的历史持仓数据，不要把当前 IO PCR 复制到历史日期。

## 本地目录

```text
data/     # ETF PCR、IO PCR、沪深300行情、元数据
output/   # 信号序列、最新信号、回测明细与摘要
logs/     # 策略日志和自动更新日志
```

## 报错修复说明

旧代码在 `run_backtest()` 里对 PCR 和 HS300 使用 inner join，日期不重叠就直接抛出 `PCR 与沪深300没有共同交易日`。新版本：

- 日期统一标准化为日粒度，避免字符串/时间部分差异；
- IO 当前快照日期使用最近一个实际沪深300行情日期，而不是周末/节假日自然日期；
- 空的共同日期不会造成整个管线崩溃，会输出 `INCOMPLETE` 诊断摘要并提示 IO 历史覆盖不足；
- 只用真实重叠数据计算综合 PCR，不对缺失日期前向填充 IO 值。

## 策略口径

- ETF PCR = SSE 510300 期权未平仓认沽 / 未平仓认购；
- IO PCR = 各可见 IO 期限的 Put OI 合计 / Call OI 合计；
- Composite PCR = ETF 权重 × ETF PCR + (1 − ETF 权重) × IO PCR；
- BB = 20 周期 SMA ± 2 × 标准差（`ddof=0`）；
- 上穿上轨定义 TOP，下穿下轨定义 BOTTOM；
- 回测采用 T 日信号、下一交易日开盘之后的开盘到开盘收益作为研究近似。指数 OHLC 是交易代理，不代表真实 IF 合约或 ETF 的成交表现。

这是用于研究与回测的工具，不自动下单，不构成投资建议。上游公开数据接口可能发生变更、限流或返回空数据；请检查页面数据日期和日志后再使用结果。

## 本地自检

在已安装依赖的虚拟环境中执行：

```bash
python tests/test_core.py
```

测试覆盖 BB 标准差计算、日期标准化、无共同交易日期时不崩溃，以及存在共同交易日期时回测流程可运行。
