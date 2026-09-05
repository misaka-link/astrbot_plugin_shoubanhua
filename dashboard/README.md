# 手办化Pro 控制台（React + Vite + Ant Design）

本目录是插件 WebUI 仪表盘的**前端源码**，技术方案与 [astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis) 一致：React 18 + TypeScript + Vite 5 + Ant Design 5。

- 运行于 AstrBot 插件管理页面的 iframe 内，通过 `window.AstrBotPluginPage` 桥接调用插件注册的 Web API；
- 自动跟随 AstrBot 宿主的暗黑/明亮主题（URL `?theme=dark|light` → localStorage → 父窗口 class）；
- 构建产物输出到 `../pages/usage-dashboard/`，AstrBot 会直接托管该目录。

## 本地开发

```bash
cd dashboard
pnpm install        # 或 npm install / yarn
pnpm dev            # 开发模式（仅用于 UI 调试，桥接数据需要 AstrBot 宿主）
```

## 构建部署

```bash
cd dashboard
pnpm install
pnpm build          # tsc 类型检查 + vite 构建
```

构建产物会写入 `../pages/usage-dashboard/`（`emptyOutDir` 会先清空该目录下的旧版原生 JS 仪表盘文件），随后在 AstrBot 管理面板重载插件即可生效。

> 构建要求 Node.js ≥ 18 与 pnpm（或 npm）。构建产物也可以在任意机器上构建后整体拷贝到服务器插件目录。

## 目录结构

```
dashboard/
├── index.html                 # 入口 HTML（含暗色主题预置脚本，防闪烁）
├── vite.config.ts             # base=./，产物输出 ../pages/usage-dashboard
└── src/
    ├── main.tsx               # React 挂载点
    ├── app/App.tsx            # 主题 Token、页头、卡片式 Tab 路由
    ├── shared/
    │   ├── api/bridge.ts      # AstrBotPluginPage 桥接封装（统一错误处理）
    │   ├── lib/theme.ts       # 跟随宿主暗黑模式 Hook
    │   ├── lib/format.ts      # 金额（厘↔元）与时间格式化
    │   └── ui/PrivacyText.tsx # 双击遮罩的隐私文本
    └── pages/
        ├── overview/          # 用量概览：指标卡、趋势图、模型明细、账本（按天/结果筛选+翻页）
        ├── subjects/          # 用户/群组用量：表格、搜索、余额调整弹窗
        ├── presets/           # 预设提示词管理（revision CAS 保存）
        └── config/            # 配置管理：模型路由、触发词绑定、热备映射、提示词模板、模型参数、功能设置、敏感配置
```

## 对应的后端 API

| 路径 | 方法 | 用途 |
| --- | --- | --- |
| `usage/overview` | GET | 概览汇总 / 趋势 / 模型聚合 |
| `usage/users` `usage/groups` | GET | 用户 / 群组余额与用量分页 |
| `usage/events` | GET | 账本明细（outcome、按天筛选、翻页） |
| `usage/adjust` | POST | 调整用户/群组余额（元，精确到 0.001） |
| `configuration` | GET/POST | 插件配置（revision CAS 防覆盖） |
| `configuration/sensitive` | GET/POST | 敏感配置（只写不读） |
| `presets` | GET/POST | 预设提示词管理 |

## 旧版原生 JS 仪表盘

`pages/usage-dashboard/` 中的 `index.html` + `app.js` + `style.css` 是旧版实现，
在首次执行 `pnpm build` 后会被新产物替换（历史可在 git 中查看）。
