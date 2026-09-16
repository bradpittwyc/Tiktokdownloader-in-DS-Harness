# TikTok 下载器（TikTokBatchMVP）→ Tony Content Engine

Windows 桌面版 TikTok 批量下载工具：给一个博主主页链接，把 TA 的全部公开作品抓到本地，
按作品 ID 去重，画质可选，支持图文帖与字幕。

界面用 pywebview（EdgeChromium）承载，抓取用 Playwright 驱动**本机原生 Chrome**，
下载用 yt-dlp。全程不需要保持浏览器窗口打开。

> **v2.0（本分支 `feature/content-factory-ai-enrichment-mvp`）**：主界面已升级为
> **Tony Content Engine 内容工厂**（8 个主页面 + 7 个设置子页 + 视频库），
> 并跑通「下载 → 字幕 → AI 标注 → 本地保存 → 界面查看」的本地闭环。
> 原下载器界面**一行未改**，完整保留在「视频库」页内嵌显示。
> 架构、数据模型、桥接 API、踩坑记录见 **[docs/content-factory.md](docs/content-factory.md)**，
> UI 参考图在 `docs/ui-references/`，实现截图在 `docs/ui-screenshots/`。

## 功能

### 内容工厂（v2 主界面）

- **内容流水线**：下载 → 字幕 → AI 标注 → 本地保存 → 界面查看，全链路可追踪
- **AI 内容标注**：对每条内容输出固定结构 JSON（主题 / 子主题 / CEFR 难度 / 口音 /
  语速 / 学习价值 / 关键词 / 重点表达 / 语法点 / 重点句 / 中文摘要 / 推荐练习），
  落本地 sqlite，重启后仍在；失败会写明原因并可「重新分析」
- **异常处理页**：读本机真实日志与失败任务，可单条重试
- **设置页**：7 个子页（基础 / 采集 / AI 加工 / 发布 / 存储 / 通知 / 账号）本地可编辑持久化；
  AI 加工设置里可配服务商 / 模型 / API Key / max_tokens / temperature / 输出语言 / prompt 模板
- **演示数据**：首页一键生成（标记为 demo，可一键清除），方便没数据时看整体效果

### 下载器（保留在「视频库」页）

- **批量抓取**：读取博主全部公开作品（视频 + 图文），支持一次粘贴多个博主
- **本地记录优先**：抓过的博主存在本机，点「最近抓取的博主」直接进页面，不再重复抓取
- **追新**：只抓最新发布的内容，扫到已知作品就停
- **后台继续**：归档没抓完时，提示条上的「继续抓取」在后台补齐，不影响当前页面操作
- **抓取可中断**：抓取期间按钮变「暂停抓取」，已抓到的作品会保留
- **画质可选**：最佳画质 / 1080p / 720p / 540p，默认最高画质，并排除带水印的源
- **安全验证**：遇到滑块时打开验证窗口，通过后可直接关闭，软件在后台接着抓
- **下载队列**：并发数可调、暂停 / 继续 / 取消 / 失败重试 / 清除已完成
- **作品列表**：搜索、排序、按日期与数量筛选、导出 CSV
- **字幕与学习文档**：保存字幕轨，并可生成英文学习文档（需自备 API Key）
- **下载即入库**：下载完成的作品自动进入内容库，可直接去「AI 加工」做标注

## 运行

需要本机已安装 **Chrome 或 Edge**。抓取复用本机浏览器环境，浏览器停在屏幕外，不会弹窗。

直接运行源码：

```powershell
pip install -r requirements.txt
python outputs/TikTokBatchMVP/web_app.py
```

或双击 `启动.bat`。

想先看效果又没有真实数据：首页点「生成演示数据」。想跑真实 AI 标注：
`设置 → AI 加工设置` 填 API Key → `测试连接` → 回 `AI 加工` 点「重新分析」。
详细说明见 [docs/content-factory.md](docs/content-factory.md)；
外部服务配置与密钥管理见 [docs/provider-config.md](docs/provider-config.md)。

## 构建

产出**免安装单文件 EXE** 与 **Inno Setup 安装包**：

```powershell
installer\build.ps1 -Installer
```

- 免安装版：`outputs/TikTokBatchMVP/TikTokBatchMVP.exe`
- 安装包：`release/TikTokBatchMVP-Setup-<版本号>.exe`（版本号写在 `installer/TikTokBatchMVP.iss`）

构建流程会先跑一遍**打包自检**（`installer/frozen_self_test.py`，
即 `TikTokBatchMVP.exe --self-test <report.json>`），确认依赖、Playwright 驱动、
模板与 UI 资源都进了包，再交给 Inno Setup。构建安装包需要 Inno Setup 6。

> 注意：v2 新增的 `ui/app.html`、`app.css`、`app.js` 与 `content_factory/` 需要一并进包
> （`.spec` 的 datas 目前按 `ui/` 目录整体收集，`content_factory` 作为源码模块自动跟随）。
> 打包与安装包验证留待下一阶段单独做一轮。

## 测试

```powershell
python -m unittest discover -s tests -t tests -v
```

416 个测试，全部离线（`-t tests` 不能省）。其中 `test_login_ui.py` /
`test_continue_ui.py` / `test_status_ui.py` / `test_open_profile_ui.py` 用真实浏览器加载
`ui/index.html` 并注入假的 `window.pywebview.api`，断言零 JS 运行时错误；
`test_content_factory_ui.py` 用同样方式守住新的内容工厂外壳
（9 个主页面 + 7 个设置子页切换零报错）。

内容工厂相关：`test_content_factory.py`（服务层）、`test_content_bridge.py`（桥接暴露面契约）、
`test_content_download_handoff.py`（下载 → 内容库交接）、`test_content_factory_ui.py`（外壳界面）、
`test_provider_config.py`（provider 配置 / 密钥 / 连接测试 / fail-closed / 旧密钥迁移）。

## 目录

```
outputs/TikTokBatchMVP/   应用源码（web_app.py 为入口）
  ui/app.html             内容工厂外壳（v2 主界面）
  ui/index.html           原下载器（作为「视频库」页内嵌，未改动）
  content_factory/        内容工厂服务层（sqlite / 设置 / AI 标注 / 转写 / 编排）
    providers/            统一 Provider 配置 + 凭据库 + 连接测试（厂商差异全是数据）
  content_bridge.py       下载器 ↔ 内容工厂的桥接层
installer/                PyInstaller spec、Inno Setup 脚本、打包自检
tests/                    测试
scripts/capture_ui.py     开发期截图工具（对照参考图用）
docs/content-factory.md   内容工厂架构、数据模型、API 契约、踩坑记录
docs/provider-config.md   Provider 契约、密钥存储策略、连接测试与集成点
docs/ui-references/       UI 参考图（14 张）
docs/ui-screenshots/      实现截图（15 张）
架构与问题.md              原下载器的架构说明、问题清单与实测记录
```

## 说明

- 已下载作品按作品 ID 自动跳过，避免重复下载。
- 抓取复用本机 Chrome，因此**不需要**把浏览器 profile 打进包里。
- TikTok 会不定期调整页面或限制访问，失败时稍后重试通常即可。
- 请只下载你有权保存和使用的内容。
