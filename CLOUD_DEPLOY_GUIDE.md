# 沪深300 PCR：GitHub + Streamlit 免费部署操作手册

本补丁是给现有 `hs300_pcr_akshare_gui` 项目用的，不包含整套应用代码。
把 ZIP 解压到项目根目录并选择“合并/替换”即可。需要确认根目录有：
`app.py`、`pcr_app_core.py`、`requirements.txt`。

## 1. 先复制项目，保留本地版本

在 Finder 复制当前项目文件夹，给副本命名为 `hs300_pcr_web`。
后面的操作都只在副本内进行。不要上传 `.venv`、本地日志和密钥文件。

## 2. 合并本补丁

将本 ZIP 内的内容复制到 `hs300_pcr_web/`：
- 用本文件夹里的 `.gitignore` 替换原 `.gitignore`。
- 将 `.github/workflows/update-data.yml` 放到同样的相对路径。
- `requirements.txt` 与原版依赖一致，可保留或覆盖。

重要：旧 `.gitignore` 有 `data/*.csv`，会把市场数据 CSV 忽略掉。本版已移除该规则；
GitHub Actions 才能把更新后的 CSV 保存回仓库。

## 3. 在 GitHub 建仓库

1. 登录 https://github.com/
2. 右上角 `+` → `New repository`。
3. Repository name 填 `hs300-pcr-web`。
4. 推荐先选 `Private`，保护源码；Streamlit 免费账户一次只能部署一个私有应用。
5. 不要勾选 `Add a README`、`.gitignore` 或 License，点击 `Create repository`。
6. 复制 GitHub 提供的 HTTPS 仓库地址，例如 `https://github.com/你的用户名/hs300-pcr-web.git`。

## 4. 从 Mac 上传项目

打开 Terminal，进入刚才复制的项目副本目录。例如：

```bash
cd "/Users/workspace/ai-code/doubao_work/stok/hs300_pcr_web"
```

先检查文件：

```bash
ls
```

必须能看到 `app.py`、`pcr_app_core.py`、`requirements.txt`、`.github`。
如果副本里没有历史数据 CSV，没关系；第一次运行工作流会尝试采集。
如果本地已有 `data/etf_pcr.csv`、`data/io_pcr.csv`、`data/hs300.csv`，可以一起提交作初始缓存。

执行：

```bash
git init -b main
git add .
git status --short
git commit -m "Initial HS300 PCR web app"
git remote add origin https://github.com/你的用户名/hs300-pcr-web.git
git push -u origin main
```

把命令中的 `你的用户名` 换成你自己的 GitHub 用户名。终端提示登录时，按 GitHub 提示完成授权。
如果提示 `remote origin already exists`，不要再 `git remote add`；执行
`git remote -v` 检查当前地址，需要时再改成新仓库地址。

上传前请检查 `git status --short`：
- 不应出现 `.venv/`、`venv/`、日志文件、`.streamlit/secrets.toml` 或个人密钥；
- `data/*.csv` 不应被忽略，因为自动更新要保存这些数据。

## 5. 打开 GitHub Actions，先手动跑一次

1. 打开刚建的仓库。
2. 点 `Actions`。
3. 如果看到提示，点 `I understand my workflows, go ahead and enable them`。
4. 左侧选择 `HS300 PCR - daily data refresh`。
5. 点击 `Run workflow`。
6. 首次初始化/回补缺失数据时，勾选 `full_backfill`。
7. 点击绿色 `Run workflow`。
8. 等工作流完成，进入运行详情，展开各步骤查看日志。

首次回补可能比日常增量更新慢一些。如果 AkShare 上游暂时无法访问，工作流日志会显示失败原因；可以稍后再 `Run workflow` 重试。

## 6. 部署 Streamlit 网页

1. 打开 https://share.streamlit.io/
2. 点击 `Continue with GitHub`，授权 GitHub 访问仓库。
3. 点击 `Create app` → `Yup, I have an app`。
4. Repository 选择 `你的用户名/hs300-pcr-web`。
5. Branch 选 `main`。
6. Main file path 填 `app.py`。
7. 点击 `Advanced settings`，Python version 选择 `3.11`。
8. 不需要填 Secrets（当前版本不需要 Tushare Token）。
9. 点击 `Deploy`，等构建日志完成。
10. 页面启动后复制 `https://...streamlit.app` 地址，可在其他电脑或手机浏览器打开。

私有仓库对应的应用通常也会是私有应用。若希望其他人查看，需要在 Streamlit 的 Sharing 设置中授权，或明确将应用改为公开。
不要为了方便就公开包含你不想公开的代码或数据的仓库。

## 7. 验证每日自动更新

- GitHub 仓库 → `Actions` → `HS300 PCR - daily data refresh`，看最近一次运行是否绿色成功。
- 仓库里的 `data/hs300.csv`、`data/etf_pcr.csv`、`data/io_pcr.csv` 应在成功采集后更新。
- 网页底部的“数据状态与自动化”查看各数据行数和最新日期。
- 工作流计划为每个工作日北京时间约 17:37。GitHub 定时任务可能延迟，不保证精确分钟。
- 中国市场休市日不会有新的指数交易日；当天 IO 数据也可能不更新。
- 历史 IO 持仓无法由当前 IO 快照倒推出。当前只有一条 IO 行时，综合 PCR 仍可能无法产生可靠的 20 日 BB 信号；不能通过复制当天数据补历史。

## 8. 重要工作方式和局限

这个入门版把更新后的 `data/` CSV 提交回 `main` 分支，Streamlit 会观察到仓库更新，可能触发应用重载/重新部署。这让数据持久化流程最简单，但每日更新后网页可能短暂重启。

网页端的文件上传只写在应用运行容器里，不保证永久保留。需要长期保留的历史 IO CSV，请在本地准备好后，放进项目的 `data/io_pcr.csv` 并提交到 GitHub；不要仅依赖网页上传后留下的临时文件。

GitHub Free 个人账户目前包含每月 2,000 分钟的私有仓库 Actions 标准运行器额度；公开仓库标准运行器使用免费的规则不同。请在 GitHub `Settings → Billing` 检查实际额度。不要选大型运行器。

如果工作流文件更新后仍未定时运行，先确认工作流位于默认分支 `main`，并在 `Actions` 页面手动触发一次。GitHub schedule 使用 UTC，且高峰时段可能延迟。
