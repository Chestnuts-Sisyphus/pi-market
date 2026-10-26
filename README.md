# pi-market

PI-Desktop 的**插件目录适配层**：把 [pi.dev](https://pi.dev/packages) / npm 上的 PI 包
（当前 5,3xx 个）转成 PI-Desktop 能读的 `catalog v2`，并把其中能打包的那些做成
`.piplug` 发布到 GitHub Release，让**原生市场**里可以直接搜索、看详情、**一键安装**。

- 目录地址（客户端"自定义"渠道填这个，**主用**）：
  `https://chestnuts-sisyphus.github.io/pi-market/catalog.json`（GitHub Pages，Fastly CDN）
- 备用（`raw.githubusercontent.com` 在部分网络被重置时才有用）：
  `https://fastly.jsdelivr.net/gh/Chestnuts-Sisyphus/pi-market@main/catalog.json`
- 目录是**精简版**（`slim.py`：只留卡片与安装必需字段，832 KB / 1,653 条），因为这条链路带宽被压到 ~26 KB/s，字节数直接等于刷新耗时。
- 安装动作完全由 PI-Desktop 原生安装器完成（含权限确认），本项目**不改客户端**、
  **不写它的数据库**、**不常驻任何进程**。

## 一分钟接入（一次性）

1. 设置 → 插件市场来源 → **自定义**
2. 填上面的 GitHub Pages 地址
3. 扩展 → 市场 → **刷新市场** → 计数变成 **1,653**

之后每次想装什么，市场里搜 → 点开 → **安装** → 权限弹窗确认，就装好了。
（如果从开始菜单启动时读不到环境变量，用一次 `start-pi-with-pi-market.cmd`。）

## 它做了什么

| PI 侧 | 适配后 |
| --- | --- |
| `pi.extensions: ["./dist/index.js"]` | `contributes.agentExtensions: ["src/dist/index.js"]` + `agent.extension` |
| `pi.skills: ["./skills"]` | `contributes.skills: ["src/skills"]` + `agent.prompt.inject` |
| 约定目录 `extensions/`、`skills/` | 同上（自动发现，目录展开成 `.ts/.js`，≤8） |
| npm 名称/描述/月下载量 | 市场卡片的名称/描述/下载量排序依据 |

一个包会被展开成：

```
plugins/pi-pkg.<slug>/
  manifest.json      schemaVersion 1，id 固定为 pi-pkg.<slug>
  main.cjs           空壳（真正的代码在 agentExtensions 指向的模块）
  src/               包内源码，保留相对结构（含被 import 的兄弟模块与 LICENSE）
```

## 覆盖范围（诚实的数字，2026-10-26 全量实测）

| 项 | 值 |
| --- | --- |
| 目录条目 | **1,412** |
| 可一键安装 | **1,412**（与目录条目**相等**：不再有"看得见但点不动"的条目） |
| 内置依赖的包 | 176 个（打包时跑 `npm install --omit=dev --ignore-scripts`，把 `node_modules` 塞进 `src/`） |
| 中文文案 | 1,412 条描述全部有 `i18n.zh-CN` |
| 目录体积 | 1.65 MB |

入选口径（`screen.py` 逐包静态体检，全部通过才打包）：

1. `package.json` 里有 `pi.extensions` 或 `pi.skills`，且声明的入口文件**真实存在**；
2. 源码里**没有** `pi-tui`，**没有**调用 16 个桌面惰性成员（`setWidget/setFooter/overlay/registerMessageRenderer/...`）；
3. 许可证在允许清单内；
4. ≤2000 文件、≤50 MB（打包后仍要满足）。

**从 5,348 到 1,412 的漏斗**：

| 阶段 | 数量 | 原因 |
| --- | --- | --- |
| pi.dev 全量 | 5,348 | — |
| 触碰终端渲染 | −2,650 | 桌面没有对应渲染面，装了也是空转 |
| 入口文件缺失 | −378 | `pi.extensions` 指向的文件不在包里 |
| 许可不明/受限 | −323 | 不能再分发（自用仍可导入） |
| 既无 extensions 也无 skills | −148 | 没有可贡献的资源 |
| 超 2000 文件 / 50 MB | −83 | 宿主硬限制 |
| **打包成功** | **1,412** | 每个都过 `verify`（sha256/尺寸/store-only zip/根 manifest） |

剩下没进目录的包不是"还没做"，是**做了也用不了**（56% 结构性依赖终端渲染）。
如果你确实想试某个不在目录里的包，用 App 内 **导入 pi 扩展**（宿主自己装依赖，但只覆盖 agent 层）。

## 中文文案

- 引擎：只用**免费额度**的通道（智谱 GLM-Flash / Groq / OpenRouter free / Gemini free），
  按 `FREE-FLEET` 里的 key 轮换；不消耗付费额度。
- 方式：`translate.py` 批量翻译 **描述**（包名/标识符不译），逐条写入 `cache/translations.json`，
  **可断点续跑**（已译的不再请求）。
- 合并：`pimarket.py publish` 把译文写成 catalog 的 `i18n.zh-CN` 块，宿主按界面语言自动切换。
- 复跑：`python translate.py --batch 20 --workers 8`（CI 每次也会补译新增包）。

## 自动化（无需人工）

`.github/workflows/refresh.yml`，每周一 03:17 UTC 运行（也可 `workflow_dispatch` 手动触发）：

1. `pimarket.py index` 重抓 pi.dev 目录
2. `pimarket.py versions` 解析 npm 版本（**结果缓存已入库**，否则会出现"只有已打包的条目"的目录）
3. `translate.py` 补译新增描述（免费通道，缓存入库）
4. `pimarket.py build-top --limit 150 --skip-built` 增量打包（有预算上限）
5. `pimarket.py publish` 生成 `catalog.json` + `lock.json`
6. 把本次新增的 `.piplug` 上传到一个**新的不可变 tag**（`pkgs-<run_id>`）
7. `pimarket.py verify --only-tag` 过发布闸（sha256 / sizeBytes / store-only zip / 根 manifest / id 规则）
8. 提交 `catalog.json`、`lock.json`、`cache/built.json`、`cache/translations.json`

## 可复现与可回滚

- `lock.json` 钉住每个包的 npm 版本、许可证、npm tarball integrity、我们产物的 sha256/size、
  所在 release tag。任何产物都能据此复现与核对。
- 回滚：删掉环境变量/把市场来源切回 Official（恢复原生 78 个）→ 已装插件不受影响；
  仓库 revert 一次提交即可撤回某次发布；Release 资产按 tag 不可变。

## 安全与合规

- **安装即信任**：`agent.extension` 让第三方代码跑进 Agent sidecar，与 bash/edit/write 同权限域。
  安装弹窗会把它标成高风险，请逐包确认。
- 只重新分发**许可允许**再分发的包（MIT / Apache-2.0 / BSD / ISC / AGPL / GPL / MPL 等）；
  `NONE`（无许可证）的包一律不进公开仓库。
- 生成器不执行被下载包的代码：只解包、读 `package.json`、复制文件、打 store-only zip。

## 目录检索（给 agent 用）

```powershell
python pimarket.py find 记忆 --installable
python pimarket.py find pdf 转换 --limit 20
```

`[INSTALL]` = 可一键安装。规则已写进全局 `~/.pi/agent/AGENTS.md` §17 与技能 `pi-market-lookup`：
**遇到本机没有的能力，先查这个目录，再回答"做不到"。**

## 本地使用（可选）

```powershell
cd D:\AI\PI\pi-market
python screen.py --meta cache/npm-latest.json --workers 12     # 静态体检 -> cache/screen.json
python build_from_screen.py --only ok --workers 8              # 零依赖批量打包
python build_from_screen.py --only ok-deps --workers 2         # 带依赖打包（npm 串行更稳）
python pimarket.py publish --releases-base <URL> --release-tag <tag> --only-installable
python pimarket.py verify
```

## 已知限制

1. 市场来源是单选：用本目录时看不到官方 78 个，反之亦然（已装插件不受影响）。
2. 目录只收"静态干净"的包；被筛掉的 2,650 个 TUI 类包在桌面上没有渲染面，装了也是空转。
3. 176 个包内置了 `node_modules`，总资产 769 MB（单包 p90 约 1 MB，安装仍是秒级）。
4. 版本漂移：桌面内核 Pi 1.3.0，npm 最新 1.4.0；按 1.4 写的包可能降级为 Unsupported。
5. GitHub Release 单个最多 1000 资产，因此分在 `pkgs-b3`(1000) / `pkgs-b4`(303) 两个 tag；URL 已按 tag 钉死，sha256 在 `lock.json`。
6. `raw.githubusercontent.com` 在国内网络会间歇被重置（2026-10-26 实测不可达）；备选
   `https://fastly.jsdelivr.net/gh/Chestnuts-Sisyphus/pi-market@main/catalog.json`。
