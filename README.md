# A股 · 历史底部倍数看板

实时展示全市场每只股票的 **最新价 ÷ Min(前复权历史最低价, 首发价格)**，按倍数从低到高排序，带板块（上证主板 / 深证主板 / 创业板 / 科创板 / 北交所）筛选、搜索和自动刷新。部署在 Cloudflare Workers 上，可绑定自己的域名，全程零服务器、零成本。

## 架构

核心思路：分母是慢变量（只在除权除息或创新低时变化），分子是快变量，两者分开处理。

```
GitHub Actions（每交易日 15:40，免费）
  └─ builder/build_denominator.py  用 akshare 抓全市场
     前复权历史最低价 + 首发价格 + 板块 → denominator.json (约500KB)
        └─ 提交回仓库 + wrangler deploy 到 Cloudflare Workers

Cloudflare Workers（免费）
  ├─ worker/public/index.html      看板页面（静态资产）
  ├─ worker/public/denominator.json 每日更新的分母数据
  └─ worker/src/worker.js          /api/quotes 行情代理（仅兜底用）

你的浏览器（打开页面时）
  └─ JSONP 直连东方财富行情接口，拉全市场实时价（走你自己的
     国内网络，快且稳），与分母合并 → 算倍数 → 排序渲染
     （直连失败才退回 Worker 代理）
```

实时行情由浏览器直连国内接口拉取，完全绕开了"Cloudflare 海外节点访问国内行情接口慢/不通"的问题；Cloudflare 只托管一个静态页面和一份每日更新的 JSON。

## 目录结构

```
├── builder/
│   ├── build_denominator.py   # 建库脚本（断点续跑、失败重试）
│   └── requirements.txt
├── worker/
│   ├── wrangler.toml           # Cloudflare 配置
│   ├── public/
│   │   ├── index.html          # 看板页面
│   │   └── denominator.json    # 分母数据（仓库自带一份示例，跑过 builder 后被覆盖）
│   └── src/worker.js           # 行情代理兜底
└── .github/workflows/
    ├── daily.yml               # 每日自动建库 + 部署（可手动试跑）
    └── deploy.yml              # 只重新部署页面，几十秒（改前端后用）
```

## 部署方式一：纯网页操作（无需任何本地环境，推荐）

只需要浏览器 + GitHub 账号 + Cloudflare 账号，建库和部署全部由 GitHub Actions 在云端完成。

### 1. 建仓库、传代码

1. github.com → **New repository** → 名字随意（如 `astock-ratio`）→ 选 **Public**（public 仓库的 Actions 完全免费、不限时长；建库每天要跑 1~2 小时，private 仓库每月 2000 分钟的免费额度撑不住）→ 勾选 "Add a README file" → **Create**。数据只是公开行情的加工结果，没有敏感信息。
2. 仓库页 → **Add file → Upload files** → 把解压后的 `builder`、`worker` 两个**文件夹整个拖进去**（Chrome/Edge 支持整夹拖拽，路径会保留），连同 `README.md` 一起 → **Commit changes**。`.gitignore` 是给本地开发用的，纯网页流程可不传。
3. 创建两个工作流（点开头的 `.github` 文件夹网页拖拽经常吃不进，直接在网页上建最稳）：仓库 → **Actions** → **set up a workflow yourself** → 文件名改成 `daily.yml` → 把压缩包里 `.github/workflows/daily.yml` 的内容整个粘贴进去 → **Commit**；再点 **New workflow** 重复一次，建 `deploy.yml`。

### 2. 拿 Cloudflare 两个凭证

1. dash.cloudflare.com 注册/登录。
2. **Account ID**：Dashboard 首页右侧栏，或浏览器地址栏 `dash.cloudflare.com/<这一串>/`。
3. **API Token**：`dash.cloudflare.com/profile/api-tokens` → **Create Token** → 用 **Edit Cloudflare Workers** 模板 → 创建后复制（只显示一次）。

### 3. 配 Secrets

仓库 → **Settings → Secrets and variables → Actions** → **New repository secret**，添加两条：`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`。

### 4. 首次上线（约 30 秒）

**Actions → deploy-only → Run workflow**。绿灯后站点就活了（此时展示的是仓库自带的示例数据，页面顶部会有黄色提示条）。部署日志的最后会打印 `https://astock-ratio.<你的子域>.workers.dev` 地址——注意这个默认域名在大陆无法直接访问，先别慌，第 6 步绑自己的域名就能打开。

### 5. 试跑建库（5~10 分钟）

**Actions → daily-denominator → Run workflow**，`limit` 填 **80** → 等绿灯。这一步验证 akshare 抓数据、生成 JSON、部署的整条链路。试跑的数据只部署不入库，不会污染仓库。

### 6. 绑自定义域名（大陆直接访问的关键）

1. Cloudflare Dashboard → **Add a site** 把你的域名加进来，按提示到域名注册商处把 NS 改成 Cloudflare 给的两个地址（生效几分钟到几小时）。没有域名的话，Cloudflare 首页的 Registrar 可以直接按成本价买一个。
2. **Workers & Pages** → `astock-ratio` → **Settings → Domains & Routes → Add → Custom domain** → 填个子域（如 `gu.example.com`），证书自动签发。
3. 大陆直接打开 `https://gu.example.com` 验证。

### 7. 全量建库

**Actions → daily-denominator → Run workflow**，`limit` 留 0 → 跑 1~2 小时。完成后每个交易日北京时间 15:40 全自动重建 + 部署，不用再管。

以后想改页面：直接在 GitHub 网页里编辑 `worker/public/index.html`（或在仓库页按 `.` 键进入 github.dev 网页版 VS Code），提交后 deploy-only 会自动重新部署，几十秒生效。

## 部署方式二：本地命令行

前置条件：Python 3.10+，Node.js 18+（只为运行 wrangler）。

### 第 1 步：本地跑通小样本

```bash
cd builder
pip install -r requirements.txt

# 只跑前 80 只，约 2~3 分钟，验证 akshare 接口正常
python build_denominator.py --limit 80 --out ../worker/public/denominator.json
```

本地预览页面：

```bash
cd ../worker
npx wrangler dev        # 首次会自动安装 wrangler
# 浏览器打开 http://localhost:8787 ，应能看到 80 只股票的实时榜单
```

### 第 2 步：首次部署到 Cloudflare

```bash
cd worker
npx wrangler login      # 弹出浏览器授权
npx wrangler deploy     # 完成后输出 https://astock-ratio.<你的子域>.workers.dev
```

注意：`*.workers.dev` 域名在大陆无法直接访问，需要挂代理验证，或直接做第 3 步绑自己的域名。

### 第 3 步：绑定自定义域名（大陆直接访问的关键）

1. Cloudflare Dashboard → **Add a site**，把你的域名加进来，按提示到域名注册商处把 NS 记录改成 Cloudflare 给的两个地址（生效需几分钟到几小时）。
2. Dashboard → **Workers & Pages** → 选中 `astock-ratio` → **Settings → Domains & Routes → Add → Custom domain**，填一个子域，比如 `gu.example.com`，确认即可（证书自动签发）。
3. 大陆直接打开 `https://gu.example.com` 验证。速度取决于线路，通常可用；如果特别慢，可考虑给这个子域关闭橙色云（仅 DNS）以外的优化选项，一般不需要。

### 第 4 步：本地全量建库一次

```bash
cd builder
python build_denominator.py --out ../worker/public/denominator.json
# 约 5400 只，1~2 小时。中断了直接重新执行，会从断点继续（当天缓存在 .cache/）
cd ../worker && npx wrangler deploy
```

### 第 5 步：接上 GitHub 自动化

1. 把整个目录推到一个 GitHub 仓库（建议 private）：

   ```bash
   git init && git add -A && git commit -m "init"
   git remote add origin git@github.com:<你>/astock-ratio.git
   git push -u origin main
   ```

2. 创建 Cloudflare API Token：`dash.cloudflare.com/profile/api-tokens` → **Create Token** → 用 **Edit Cloudflare Workers** 模板 → 创建并复制。
3. 找到 Account ID：Cloudflare Dashboard 首页右侧栏，或浏览器地址栏 URL 中 `dash.cloudflare.com/<这一串>/` 。
4. GitHub 仓库 → **Settings → Secrets and variables → Actions** → 添加两个 Secret：`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`。
5. 仓库 **Actions** 页 → 选 `daily-denominator` → **Run workflow** 手动触发一次，确认全流程绿灯。
6. 之后每个交易日北京时间 15:40 自动重建并部署，无需再管。

## 数据口径与已知限制

- 分母 = Min(上市以来前复权最低价, 首发价格)。除权除息会改变整条前复权序列，所以每天全量重算，而不是增量。
- 首发价格来自东财新股数据库，较老的股票可能缺失，此时分母只取历史最低价（老股票经年分红后前复权低点通常远低于发行价，影响很小）。
- 停牌股（最新价为 "-"）和当天建库失败的股票不参与排序，页面统计栏会显示剔除数量。
- 东财行情接口目前单页被限制在 100 条左右，页面用并发分页拉取（约 55 页，几秒完成），刷新间隔默认 60 秒，收盘后自动暂停。

## 常见问题

**建库时被限流 / 大量失败？** 调大间隔：`python build_denominator.py --sleep 0.8`。断点缓存会保住已完成的部分。

**GitHub Actions 的海外 IP 被东财拒绝？** 少见但可能。备选：在自己电脑/家里的机器上加个定时任务，跑完后 `cd worker && npx wrangler deploy` 即可，效果完全一样。

**JSONP 直连失效？** 页面会自动退回 Worker 代理（走 Cloudflare 海外线路访问东财）。如果两者都不行，说明东财接口改了，届时可换腾讯行情接口（`qt.gtimg.cn`）作为数据源。

**改了页面想重新部署？** 网页流程：直接在 GitHub 上编辑并提交，deploy-only 自动触发。本地流程：先 `git pull`（Actions 每天会把最新 denominator.json 提交回仓库），再 `npx wrangler deploy`，否则会把本地旧数据部署上去。

**免费额度够吗？** Workers 免费版每天 10 万次请求，个人使用绰绰有余；GitHub Actions 在 public 仓库完全免费、不限时长（这也是推荐建 public 仓库的原因）。private 仓库每月只有 2000 分钟免费额度，扛不住每天 1~2 小时的建库。

## 免责声明

数据来自公开接口，仅供个人研究，可能存在错误或延迟，不构成任何投资建议。
