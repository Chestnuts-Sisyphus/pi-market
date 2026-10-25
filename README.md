# pi-market

PI-Desktop 的**插件目录适配层**：把 [pi.dev](https://pi.dev/packages) / npm 上的 PI 包
（当前 5,3xx 个）转成 PI-Desktop 能读的 `catalog v2`，并把其中能打包的那些做成
`.piplug` 发布到 GitHub Release，让**原生市场**里可以直接搜索、看详情、**一键安装**。

- 目录地址（客户端"自定义"渠道填这个）：
  `https://raw.githubusercontent.com/Chestnuts-Sisyphus/pi-market/main/catalog.json`
- 安装动作完全由 PI-Desktop 原生安装器完成（含权限确认），本项目**不改客户端**、
  **不写它的数据库**、**不常驻任何进程**。

## 一分钟接入（一次性）

1. 设置 → 插件市场来源 → **自定义**
2. 填上面的 `catalog.json` 地址
3. 扩展 → 市场 → **刷新市场** → 计数从几十变成 5,3xx

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

## 覆盖范围（诚实的数字）

| 项 | 值 |
| --- | --- |
| 市场里**可看/可搜/可查详情** | **全部 5,3xx 条**（目录全量列出；未打包的显示为不可安装） |
| **可一键安装** | 已打包并发布的部分（随每次 refresh 增长；见 `lock.json`） |
| 不可安装的原因 | ① 有运行时依赖（`.piplug` 不能装依赖，市场安装也不跑 npm）；② 超过 2000 文件 / 50 MB；③ 纯主题/提示词包；④ 无许可证（不公开分发） |
| 有依赖的包怎么装 | 用 App 内 **导入 pi 扩展**（宿主会用受限安装器装依赖），或本地 `pimarket.py build` 后走「安装插件包」 |

## 自动化（无需人工）

`.github/workflows/refresh.yml`，每周一 03:17 UTC 运行（也可 `workflow_dispatch` 手动触发）：

1. `pimarket.py index` 重抓 pi.dev 目录
2. `pimarket.py versions` 解析 npm 版本
3. `pimarket.py build-top --limit 150 --skip-built` 增量打包（有预算上限）
4. `pimarket.py publish` 生成 `catalog.json` + `lock.json`
5. 把本次新增的 `.piplug` 上传到一个**新的不可变 tag**（`pkgs-<run_id>`）
6. `pimarket.py verify --only-tag` 过发布闸（sha256 / sizeBytes / store-only zip / 根 manifest / id 规则）
7. 提交 `catalog.json`、`lock.json`、`cache/built.json`

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

## 本地使用（可选）

```powershell
cd D:\AI\PI\pi-market
python pimarket.py index
python pimarket.py build pi-simplify pi-memory
python pimarket.py catalog                 # 相对 URL，配 serve 或本地目录
python pimarket.py verify
python pimarket.py serve --port 8765       # 仅开发兜底
```

## 已知限制

1. 市场来源是单选：用本目录时看不到官方 78 个，反之亦然（已装插件不受影响）。
2. TUI 类插件装上后界面部分是惰性空实现（状态栏/footer/overlay/消息渲染器等）。
3. 版本漂移：桌面内核 Pi 1.3.0，npm 最新 1.4.0；按 1.4 写的包可能降级为 Unsupported。
4. 目录无 i18n 块，卡片名称/描述用 npm 原文（多为英文）。
5. 无许可证的包只能本地用，不能进公开仓库。
