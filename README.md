# TikTok 下载器（TikTokBatchMVP）

Windows 桌面版 TikTok 批量下载工具：给一个博主主页链接，把 TA 的全部公开作品抓到本地，
按作品 ID 去重，画质可选，支持图文帖与字幕。

界面用 pywebview（EdgeChromium）承载，抓取用 Playwright 驱动**本机原生 Chrome**，
下载用 yt-dlp。全程不需要保持浏览器窗口打开。

## 功能

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

## 运行

需要本机已安装 **Chrome 或 Edge**。抓取复用本机浏览器环境，浏览器停在屏幕外，不会弹窗。

直接运行源码：

```powershell
pip install -r requirements.txt
python outputs/TikTokBatchMVP/web_app.py
```

或双击 `启动.bat`。

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

## 测试

```powershell
python -m unittest discover -s tests -t tests -v
```

261 个测试，全部离线（`-t tests` 不能省）。其中 `test_login_ui.py` /
`test_continue_ui.py` / `test_status_ui.py` / `test_open_profile_ui.py` 用真实浏览器加载
UI 并注入假的 `window.pywebview.api`，断言零 JS 运行时错误。

## 目录

```
outputs/TikTokBatchMVP/   应用源码（web_app.py 为入口）
  ui/index.html           单文件前端
installer/                PyInstaller spec、Inno Setup 脚本、打包自检
tests/                    测试
架构与问题.md              架构说明、问题清单与实测记录
```

## 说明

- 已下载作品按作品 ID 自动跳过，避免重复下载。
- 抓取复用本机 Chrome，因此**不需要**把浏览器 profile 打进包里。
- TikTok 会不定期调整页面或限制访问，失败时稍后重试通常即可。
- 请只下载你有权保存和使用的内容。
