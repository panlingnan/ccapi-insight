# CloudControl OpenAPI 覆盖率

展示各服务全量 OpenAPI，并标记哪些 Action 已被 Volcengine CloudControl 资源类型的 handlers 引用；同时支持按 API 下钻，将 OpenAPI 参数与资源类型属性逐一匹配，找出「有参数但无对应资源属性」的缺口。

线上（生产）：部署在 Vercel，`panlingnans-projects` 团队下。
仓库：https://github.com/panlingnan/ccapi-insight

---

## 功能

- **覆盖率总览**：按服务列出全量 OpenAPI，标记已覆盖 / 未覆盖，可按 Action、中文名过滤，可手动排除非资源类 API（如分页查询）后重算覆盖率。排除项**持久化**到仓库内的 `excluded-apis.json`，每次部署不丢失、跨浏览器共享（见下）。
- **资源类型详情**：点资源类型查看其 handlers（create/read/update/delete/list）与所需权限。
- **API 参数下钻**：每个 API 行有「⤵ 参数」按钮，点开后从 API Explorer 拉取该 API 的完整参数（递归展开嵌套对象/数组），与该 API 关联资源类型的属性做匹配，分为：
  - ⚠ 无对应资源属性的参数
  - ✓ 有对应资源属性的参数
  - 非属性参数（分页 / 幂等等，自动从匹配中排除）

---

## 架构

| 部分 | 本地 (`python3 server.py`) | 线上 (Vercel) |
|------|---------------------------|---------------|
| 静态页面 + 数据 JSON | 由 server.py 直接提供 | 作为静态资源由 CDN 提供 |
| `GET /api/apiparams` | server.py 内置处理 | `api/apiparams.py` serverless 函数（**无需密钥**） |
| `POST /api/refresh` | server.py 跑完整流水线，重写数据 JSON | 不部署；点刷新会提示「需管理员在本地更新」 |

数据文件（构建产物，已提交到仓库）：
- `coverage-data.json` — 覆盖率数据（前端直接读）
- `ccapi-resourcetype-details.json` — 各资源类型完整 Schema
- `ccapi-resourcetypes.json` — 资源类型原始清单
- `excluded-apis.json` — 持久化的「已排除 API」列表（前端加载时作为基线）

---

## 已排除 API 的持久化

「排除」用于把分页查询等非资源类 API 从覆盖率分母中剔除。其存储分两层：

- **`excluded-apis.json`（提交进仓库）**：权威基线，**部署不丢失、跨浏览器/访客共享**。前端加载时若该文件非空，即以它为准。
- **浏览器 localStorage**：仅作每个浏览器的临时层。线上只读站点上访客的勾选只存本地，不影响他人。

**管理员如何更新排除列表**（仅本地）：

```bash
python3 server.py            # 本地起服务
# 浏览器打开 http://127.0.0.1:8765，用每行的开关勾选/取消「计入覆盖率分母」
```

本地每次切换开关，server.py 会通过 `POST /api/exclusions` 把列表写回 `excluded-apis.json`。
之后提交并部署（`./refresh_and_deploy.sh` 已包含该文件，或手动 `git add excluded-apis.json && git commit && git push`），线上即生效且后续部署保留。

> 首次迁移：若你之前的排除项只存在某个浏览器的 localStorage 里，用**本地** server 打开页面一次，它会自动把这些项种入 `excluded-apis.json`，随后提交即可固化。

---

## 本地开发

```bash
cd ccapi-insight
python3 server.py            # http://127.0.0.1:8765
```

仅浏览数据与使用参数下钻，无需任何凭证。

若要在本地点「刷新数据」按钮跑完整流水线，需先配置 Volcengine 凭证（见下）。

---

## 管理员：刷新数据并更新线上

> 线上是**只读快照**。普通用户无需任何操作即可看到最新已发布数据。
> 更新数据是**管理员**操作：在本地用真实凭证重新生成数据，再重新部署。

### 一键脚本（推荐）

凭证从环境变量读取，**不写入文件、不提交到 git**：

```bash
cd ccapi-insight
export VOLCENGINE_ACCESS_KEY="你的 AccessKey"
export VOLCENGINE_SECRET_KEY="你的 SecretKey"
./refresh_and_deploy.sh
```

脚本依次执行：

1. 重新抓取全量资源类型 + 各服务 OpenAPI 列表，重新生成三个数据 JSON
2. 若数据有变化，commit 并 `git push`
3. `vercel --prod` 部署到生产

### 手动分步（等价）

```bash
export ACCESS_KEY="$VOLCENGINE_ACCESS_KEY"   # 流水线脚本读取 ACCESS_KEY / SECRET_KEY
export SECRET_KEY="$VOLCENGINE_SECRET_KEY"
python3 fetch_ccapi_resourcetypes.py         # -> ccapi-resourcetypes.json / ccapi-resourcetype-details.json
python3 build_coverage_data.py               # -> coverage-data.json
git add coverage-data.json ccapi-resourcetype-details.json ccapi-resourcetypes.json
git commit -m "data: refresh CloudControl coverage"
git push
vercel --prod --yes
```

> 前提：本地已 `vercel login` 且项目已 link 到 `panlingnans-projects`。
> 若 Vercel 已连接 GitHub 仓库（Settings → Git），则 `git push` 会自动触发部署，脚本的 `vercel --prod` 是冗余保险。

---

## 部署到 Vercel（首次）

```bash
vercel login
vercel link --repo --scope panlingnans-projects   # 关联 GitHub 仓库，开启 push 自动部署
vercel --prod --scope panlingnans-projects
```

### 访问控制

默认部署带 **Deployment Protection (SSO)**，所有 URL 会跳转 Vercel 登录，仅团队成员可见。如需对外分享：

- **公开访问**：项目 Settings → Deployment Protection → 关闭 Vercel Authentication
- **临时分享**：项目 Settings → Deployment Protection → Protection Bypass / Shareable Links → Create Link，复制带 token 的链接发给访问者

---

## 安全说明

- 仓库中**不含任何 AK/SK 明文**，代码里只出现环境变量名。
- 凭证仅在本地刷新时通过环境变量临时使用；`refresh_and_deploy.sh` 不会把它们写入文件或日志。
- `.gitignore` 已排除 `.env`、`.vercel/` 等；`.vercelignore` 排除本地流水线脚本，不上传到 Vercel。

---

## 文件结构

```
index.html                       前端单页（含覆盖率视图、详情抽屉、参数下钻抽屉）
coverage-data.json               覆盖率数据（构建产物）
ccapi-resourcetype-details.json  资源类型完整 Schema（构建产物）
ccapi-resourcetypes.json         资源类型原始清单（构建产物）
excluded-apis.json               持久化的已排除 API 列表（前端基线）
api/apiparams.py                 Vercel serverless 函数：/api/apiparams（无需密钥）
server.py                        本地开发服务器（静态 + /api/apiparams + /api/refresh + /api/exclusions）
fetch_ccapi_resourcetypes.py     调 CloudControl API 抓全量资源类型（需 AK/SK）
build_coverage_data.py           调 API Explorer 算覆盖率，生成 coverage-data.json
analyze_coverage.py              命令行版覆盖率分析（单服务）
refresh_and_deploy.sh            管理员：刷新数据 + push + 部署（本地用，不上传）
```
