# 桌面 Hub · 灵动岛 v0.3

一个自绘的 Windows 桌面灵动岛：顶部居中悬浮，悬停自动展开，
监控页四张卡片点进去就是对应指标的进程列表，还能切到音乐页、曲库页和剪贴板页。和 Wallpaper Engine 壁纸互不干扰。

## 现在能做什么

### 收起态（胶囊）
- CPU / GPU / 内存 / 显存 占用百分比 + 上下行速率 + 四段占用条
- **没在放音乐时是 52px 的细条；一旦有音乐在放，自动长到 68px，底部换成一条细频谱**，跟着节奏抖；停了自动缩回去
- 悬停展开，移开 0.5 秒收起（240ms 缓动）
- 拖动换位置，位置记住（`%APPDATA%\DesktopHub\config.json`）
- 不占任务栏、不进 Alt+Tab、点击不抢焦点

### 监控页
- CPU、GPU、内存、显存四张卡片：实时值 + 副标题（核心数 / 显卡型号与温度 / 已用与总量）
- 最近 60 秒折线
- 底栏：上传 / 下载 / 磁盘读 / 磁盘写

### 进程页（从监控卡片或底栏指标钻进来）
- 点监控页哪张卡片，就进哪个指标的进程列表：点 CPU 只列 CPU，点 GPU 只列 GPU，内存/显存同理
- 底栏的上传 / 下载 / 读盘 / 写盘四项也能点，各自钻到对应指标的进程列表
- 列表 = 进程名 + PID + 该项指标 + 结束按钮「×」，不掺别的数据
- 指标列是「条状图 + 数值」：条长按本视图最大值归一化，谁最猛一眼看得出
- 默认按该指标降序，点列头切换升/降序
- GPU / 显存 / 网络 / 磁盘视图自动隐藏没占用的进程，只看真正在用的
- 滚轮翻页；系统关键进程（explorer / dwm / lsass / svchost 等）按钮灰掉禁用
- 受保护进程（vmwp.exe 这类）CPU/内存显示「—」，但 GPU / 显存照常显示，结束按钮禁用
- 左上角「← 监控」一键返回卡片页
- 采集性能：Win32 批量枚举 + Toolhelp 全量进程名 + GetProcessIoCounters，**21ms/轮、310+ 进程**

### 音乐页（播放器 + 曲库同页）
- 读系统媒体会话（SMTC）：酷狗 / PotPlayer / Spotify / 浏览器 / VLC 等所有遵守 SMTC 的播放器
- 封面、歌名、歌手、专辑、进度条与时长
- 播放暂停 / 上一首 / 下一首
- **频谱律动**：32 段对数频带，抓系统输出（WASAPI loopback），不碰麦克风
- **跟封面配色**：从封面缩略图抽主色 + 均色做渐变，换歌就换色调
- **音量滑块**：只调当前播放器这个进程的会话音量，不动系统总音量
- 下方就是曲库：扫本地目录（默认 `D:\KuGou`），文件名按「歌手 - 歌名」解析
- 点一行就用 PotPlayer 打开播放，播放中的那首会高亮；滚轮翻页
- **后台播放**：点歌只出声不弹窗（`core/potplayer.py` 里复用实例换歌 + 隐藏主窗和「播放列表」窗）
- 播放中左下角有「显示播放器」，想看一眼 PotPlayer 界面就点它（暂停后这个按钮自动收起）
- 目录可在 `%APPDATA%\DesktopHub\config.json` 里用 `music_dirs` 改

### 剪贴板页
- 自动记录剪贴板：**文本和文件都记**，最多 30 条（`%APPDATA%\DesktopHub\clipboard.json`）
- 每条带**来源进程名**（谁复制的）和**时间**
- 点一行 = 复制回剪贴板（文件是整组路径写回，粘到资源管理器直接得到文件）
- 按住一行往外拖 = 直接拖出文件，可丢进资源管理器、聊天窗口、上传框
- 从外面拖文件进页面也会记进历史
- 右侧「×」删单条（24px 圆角按钮、手绘线条，鼠标移上去会亮）；同内容重复复制不新增，只提到最前
- **「存」**：一键把这条存进记事页（文本存在原样，文件条目存成一行一个路径），存完 toast 提示
- 滚轮翻页；来源用 `GetClipboardOwner` 取，不抢剪贴板

### 记事页（富文本 + 导出 Markdown）
- 真·可编辑：点进记事页时窗口临时允许被激活，可以正常打字、回车、Ctrl+A/C/V
- 富文本：粗体 / 斜体 / 下划线 / 二级标题 / 无序列表 / 一键清格式
- 左侧是笔记列表（标题取正文第一行前 24 字 + 更新时间），右侧是编辑区
- **列表上方有搜索框**：标题和正文都搜，实时过滤，顶上显示「命中 N / 总数」
- 剪贴板页每条右侧的「存」按钮，可以把剪贴板内容一键变成一条笔记
- 自动保存：停止输入 0.9 秒落盘，最多 60 篇（`%APPDATA%\DesktopHub\notes.json`）
- **「导出 MD」**：把当前笔记转成 Markdown 写到 `D:\Doubao\桌面Hub\notes\`，重名自动加序号
- 转换规则：标题 → `##`，粗体 → `**`，斜体 → `*`，下划线 → `<u>`，列表 → `-`
- 「＋ 新建」「删除」在顶栏右侧

## 启动

**日常就用 exe**（已装到用户目录，不需要管理员，不占项目目录）：

```
C:\Users\<你>\AppData\Local\DesktopHub\DesktopHub.exe
桌面上的「桌面 Hub」快捷方式指向它
```

第一次启动要 10 秒左右（单文件 exe 先解压到临时目录），之后就是常驻。

源码方式（改代码调试用）：

```powershell
# 双击 运行.bat（普通权限）
# 双击 管理员启动.bat（提权，网络列可看逐进程真实流量）
# 或命令行（能看到报错）
& "C:\Program Files\Python313\python.exe" main.py
```

自检命令：

```powershell
& "C:\Program Files\Python313\python.exe" main.py --check       # 采一次数据打印 JSON
& "C:\Program Files\Python313\python.exe" main.py --snapshot    # 渲染自检图到 tools/
```

## 托盘菜单

右键托盘图标：

| 菜单项 | 作用 |
| --- | --- |
| 显示 / 隐藏 | 手动收起或叫出胶囊（全屏时叫出来会临时“免打扰”，退出全屏后恢复自动判断） |
| 全屏时自动收起 | 检测到有窗口铺满整块显示器（游戏、全屏视频、F11 网页）就自动隐藏，退出全屏自动回来 |
| PotPlayer 后台播放 | 曲库点歌时不弹播放器窗口，声音照旧（该次换歌后 20 秒内持续按着，防止「播放列表」窗口半路冒出来） |
| 开机自启 | 勾上就写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，下次开机自动起 |
| 退出 | 停掉所有采集线程再退 |

全屏检测的实现在 `core/fullscreen.py`，两个坑都趟过了：Win11 上 `GetForegroundWindow()`
有时会长期返回一个 shell 的 `Windows.UI.Core.CoreWindow`，所以改成按 z-order 从最上层往下扫；
另外进程不是 DPI-aware 时窗口坐标是缩放后的逻辑值（150% 下 2560×1440 变 1707×960），
所以判断用「宽/高各自占显示器的比例是否一致」，而不是比像素。

## 打包

```powershell
$env:PYTHONPATH = "$env:APPDATA\Python\Python313\site-packages"
& "C:\Program Files\Python313\python.exe" -m PyInstaller --noconfirm --onefile --windowed `
    --name DesktopHub --icon tools\desktophub.ico main.py
# 产物 dist\DesktopHub.exe，拷到 %LOCALAPPDATA%\DesktopHub\ 即可
```

## 权限与网络列（重要）

| 启动方式 | 网络列内容 | 其它 |
| --- | --- | --- |
| 普通双击 `运行.bat` | 每个进程的连接数 | 全部功能可用 |
| 双击 `管理员启动.bat` | 每个进程的真实上下行速率（ETW） | 全部功能可用 |

ETW 内核网络事件需要管理员权限，拿不到会自动降级为连接数模式。
降级时上传和下载两个视图内容相同（都按连接数排），界面底部会写明当前模式。

## 依赖装在哪

包在 `%APPDATA%\Python\Python313\site-packages`（`main.py` 启动时自动加入 `sys.path`），
没写 Program Files，也没塞进项目目录。

依赖：`PySide6-Essentials`、`psutil`、`nvidia-ml-py`、`winrt-Windows.Media.Control`、
`winrt-Windows.Storage.Streams`、`winrt-runtime`、`pyetwkit`，
频谱与音量另需 `soundcard`、`numpy`、`pycaw`、`comtypes`。

## 和 Wallpaper Engine 共存

组件只创建普通顶层窗口（`Qt.Tool + Frameless + StaysOnTop`，附 `WS_EX_TOOLWINDOW` / `WS_EX_NOACTIVATE`），
**完全不碰 `Progman` / `WorkerW` 桌面层**，所以 Wallpaper Engine 的壁纸渲染不受影响。
窗口尺寸远小于屏幕，也不会触发 WPE 的"全屏时暂停"逻辑。这也是不用 360 / 腾讯桌面组件的原因。

## 文件结构

```
桌面Hub/
├── main.py              入口、托盘、单实例、--check / --snapshot
├── core/
│   ├── metrics.py       系统指标线程（psutil + NVML）
│   ├── winsys.py        Win32 批量进程枚举（EnumProcesses / GetProcessTimes）
│   ├── processes.py     进程页采集线程（CPU/内存/GPU/显存/网络合并）
│   ├── pdh.py           Windows 性能计数器（GPU 引擎利用率、按进程显存）
│   ├── netflow.py       网络：ETW 精确流量 / 连接数双模
│   ├── media.py         SMTC 媒体会话采集与播放控制
│   ├── audio.py         WASAPI loopback 频谱 + 按进程音量
│   ├── library.py       本地曲库扫描 + 调起 PotPlayer
│   ├── potplayer.py     PotPlayer 后台播放（/current 换歌、隐藏/还原窗口）
│   ├── clipstore.py     剪贴板历史（文本 + 文件，来源与时间，30 条）
│   ├── notes.py         记事本存储（HTML + 纯文本 + Markdown）
│   ├── mdconv.py        QTextDocument(HTML) → Markdown 转换
│   ├── fullscreen.py    全屏检测（z-order 扫描 + 缩放比例判断）
│   └── autostart.py     开机自启（HKCU Run 键读写）
├── ui/
│   ├── island.py        主窗口：四个页面、动画、交互、自绘图标
│   └── theme.py         配色 / 字体 / 数值格式化
├── notes/               「导出 MD」落盘目录
├── dist/                打包产物 DesktopHub.exe
└── tools/               自检脚本、图标源文件与自检截图
```

## 已知问题

- 第一次启动时 PDH 注册 GPU 计数器约需 2 秒（后台线程完成，界面不卡）
- 日志里偶发 `Windows fatal exception: code 0x8001010d`，属 Windows COM 在输入同步调用期间的一句噪音，程序照常运行
- 单文件 exe 每次启动要先解压到临时目录，约 10 秒才看到胶囊；想快可以改成 `--onedir`
- Markdown 转换是轻量实现：不覆盖表格、图片、多重嵌套列表，够日常写笔记导出到 Obsidian / Typora

## 下一版计划

- v0.4 / v0.5 已完成：笔记搜索、剪贴板↔记事互通、全屏自动隐藏、开机自启、打包 exe
- 待办：酷狗是否上报 SMTC 待确认；确认不了时酷狗只能当"有声音"，控制走 PotPlayer
- 想继续打磨的方向：多显示器跟随、`--onedir` 启动提速、笔记全文搜索高亮
