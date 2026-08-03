# Codex Token Monitor for Waveshare AMOLED 2.16

## Version 1.0.6

### Figma update states and single-quota layout

- The dashboard follows the latest 480×480 Figma frame: a 70 px blue brand header, bottom activity text, 80 px quota cards and 12 px progress tracks.
- Live data with both 5H and WEEK limits keeps the two fixed cards. When the official `codex` bucket has no 5H window and only a WEEK window, the dashboard automatically uses the Figma single-card composition.
- `UPDATE AVAILABLE` and `DOWNLOADING` use SVG indicators in the dashboard header.
- `INSTALLING`, `VERIFYING` and `RESTARTING` use dedicated full-screen layouts. Their three dots animate continuously on a shared 1.2-second loop; installation progress is displayed from 0–100%.
- `UPDATE FAILED` uses the exact Figma warning SVG and 200×110 `RETRY` / `ROLLED BACK` cards. A separate `ROLLED BACK` result state is also supported.
- The formal Windows client sends update availability, download, verification, restart, failure and rollback display states over BLE.
- The development test client exposes 16 tap-to-advance screens, including `NEED APPROVAL`, all update states and the same single-quota layout used by weekly-only live accounts.
- The new battery uses a 36×36 SVG outline, a two-layer 24×24 low-battery warning and a 22×10 dynamic fill. Charging remains independent from task and update states.

### Display, settings and input

- Typography follows the Figma weights: Sofia Pro Medium for brand/metadata/cost, Bold for status, quota cards, settings and BACK, and Black for the primary token total.
- The ChatGPT, brightness, volume, battery and charging icons are generated from the exact Figma SVG paths into 4-bit antialiased coverage masks.
- THINKING keeps the dashboard visible and pulses the complete bottom activity indicator between 0.3 and 1.0 opacity.
- `NEED APPROVAL` uses the 230×36 red status card from Figma. A quota with less than 10% remaining independently switches its own card to the revised `LIMIT REACHED` treatment; exactly 10% remains normal.
- The cost prefix uses Sofia Pro Medium 26 for `RMB`; the 32 px amount shares the same baseline so both parts align at the bottom.
- The latest header battery is a 36×36 SVG outline with an antialiased dynamic fill; low battery uses the separate red/white warning layers from Figma.
- Charging is independent from AI task/update states and uses a 1.6-second left-to-right fill loop, so it can animate at the same time as THINKING.
- Automatic orientation includes the board's required 90° mounting offset. Each completed frame is rotated into a separate PSRAM output buffer; CO5300 axis flips are not used as 90° rotation.
- QMI8658 90°/270° results are reversed to match the clockwise convention of the software framebuffer rotator.
- CST9220 native coordinates are used with the CO5300 native orientation, then transformed only for the active software frame rotation.
- Serial output prints both base and mapped touch coordinates to simplify hardware calibration.
- Slider rendering is coalesced to about 30 FPS and NVS writes occur 500 ms after interaction stops, avoiding flash writes on every touch sample.
- BACK accepts the full bottom region and uses a navigation cooldown so one physical tap cannot bounce between pages.

- 单击板载 GPIO18 按键进入设置页；
- 触摸 `BRIGHTNESS` 或 `VOLUME` 卡片进入相应滑条；
- 触摸/拖动滑条即时修改，亮度范围 10–100%，音量范围 0–100%；
- 详情页 `BACK` 返回设置主页，设置主页 `BACK` 关闭设置；
- 设置保存在 ESP32 NVS，重启后保留；
- 亮度直接控制 CO5300，音量写入 ES8311 DAC 音量寄存器；
- 设置首页显示由蓝牙 MAC 后四位生成的唯一设备名，例如 `Codex Meter-6B4D`；
- QMI8658 根据重力方向自动选择 0°/90°/180°/270°，连续确认三次才旋转；
- CST9220 触摸坐标会随画面方向转换。

板级触摸、IMU、音频和电源共用 GPIO14/15 I²C 总线；项目使用
`SensorLib 0.4.1` 驱动 QMI8658 与 CST9220。

## Windows 托盘同步器（推荐）

直接双击根目录的 `CodexMeterSync.exe`。程序不会打开控制台窗口，只在 Windows
通知区域显示 Codex Meter 图标。右键图标可使用：

- `Sync now`：立即同步；
- `Device`：扫描、选择或解除绑定 Codex Meter；
- `AI Tool`：选择 `Auto`、`Codex` 或 `Claude Code` 数据源；
- `Updates`：检查并安装正式版客户端更新或设备固件 OTA；
- `Start with Windows`：开启或关闭当前用户的开机自动运行，无需管理员权限；
- `Open log`：打开 `%APPDATA%\CodexMeter\sync.log`；
- `Exit`：断开 BLE 并退出。

开发时使用独立的 `CodexMeterSync-Test.exe`。它不会注册开机启动，并增加
`Test mode (tap device to advance)` 菜单。开启后在设备屏幕任意位置每点一次
切换一个状态，不会自动轮播。状态顺序为：`DEFAULT` → `THINKING` →
`NEED APPROVAL` → `LIMIT REACHED` → `DATA STALE` → `WAITING FOR SYNC` → `CHARGING` →
`LOW BATTERY` → `UPDATE AVAILABLE` → `DOWNLOADING` → `INSTALLING` →
`VERIFYING` → `RESTARTING` → `UPDATE FAILED` → `ROLLED BACK` →
`SINGLE QUOTA`，之后回到 `DEFAULT`。测试数据不会写入设备 NVS；关闭测试模式或
退出测试同步器时会先恢复真实数据。

正式版与测试版共用单实例锁，不能同时运行，避免两个 BLE 客户端抢占设备。
切换版本前请先从托盘退出当前正在运行的同步器。

两个 EXE 使用 Windows GUI 子系统，不会创建控制台窗口，运行期间只保留通知区域图标；
运行日志统一写入 `%APPDATA%\CodexMeter`，不依赖控制台输出。

每台设备广播唯一名称，例如 `Codex Meter-6B4D`。未绑定时只发现一台设备会自动
绑定；同时发现多台时不会随机连接，必须在托盘 `Device` 菜单中选择。绑定保存到
`%APPDATA%\CodexMeter\.device-address`，之后严格按地址重连；目标设备离线时只会
等待该设备，不会回退并覆盖为附近另一台。使用 `Forget device` 可解除绑定后重新选择。

同步器会维持 BLE 长连接，监视 `~/.codex/sessions` 与 `~/.claude/projects`
的 JSONL 变化并在约 750ms
防抖后同步；Codex 账号数据通过一个隐藏、持久运行的官方 `codex app-server`
连接，每 5 秒调用 `account/rateLimits/read` 与 `account/usage/read`，并每 30 秒
发送一次保活心跳。只有 token、选中的主额度、状态或金额等有效数据发生变化时才立即
写入。设备完成 JSON
解析、保存和渲染后会返回带序列号的 ACK，电脑端收到 ACK 才把本次同步视为成功。
连接中断时会自动按指数退避重连。

状态以任务生命周期为准：任一会话出现 `task_started` 或 `turn_started` 后立即进入
`THINKING`；`task_complete`、取消/中止/失败事件或 `final_answer` 都会结束该会话的
活跃状态。这样 CLI 即使漏写 `task_complete`，最终回复后也不会卡在 `THINKING`。
多个会话并行时，只要其中一个任务仍在运行就保持 `THINKING`；任一会话存在尚未
返回结果的提权调用时，状态优先切换为 `NEED APPROVAL`，调用结束后恢复为
`THINKING`。在 `Auto` 模式下，可操作的 Codex 批准请求也不会被较新的 Claude
Code 日志遮住。30分钟
没有任何新写入的异常中断会话会自动失效。`archived_sessions` 完全移出实时热路径。
`session_meta.thread_source=subagent`、`source.subagent` 标记的 guardian 与其他内部
代理会话也会在候选上限、状态、型号、Token 和金额聚合之前排除；CLI、Desktop
等 `thread_source=user` 会话仍可并行聚合。
Token 事件不再负责推断任务是否仍在运行。状态变化先通过日志尾部
快速路径发送上一份数值快照，再在后台路径补齐 Token、RMB 和官方账号额度变化。

同步器直接定位当天、前一天及仍在写入的活跃会话文件；只在找不到热路径文件的冷启动
阶段做一次有限回退扫描。候选文件按修改时间从新到旧排序，只用于当前型号、任务状态、
本机当日所有模型的总 Token 与成本明细，不再从会话事件读取账号额度。每个文件保存读取
偏移，后续只读取新增尾部；累计计数按相邻差值计入，计数器重置后从新基线继续，完全
重复的事件只计一次。当日 Token 及逐模型成本明细使用进程内增量缓存，不再反复
解析整份日志。5 秒兜底校验先比较 JSONL 文件路径、修改时间与大小；文件未变化时复用
已解析快照。刷新由单飞锁串行化，文件事件在 500ms 以上的防抖窗口内合并，不会出现
多个刷新任务重叠。选择一个暂时没有
本地数据的工具时会保持 BLE 长连接并等待文件出现，不再把“无数据”误判为蓝牙故障。
ACK 使用毫秒级进程内单调序列号，连续点击 `Sync now` 也不会复用同一序列号。

`AI Tool > Auto` 会从 Codex 与 Claude Code 中选择最近活动的数据源；也可以固定
为其中一个品牌。切换只发生在 AI 编程工具层级，不提供 GPT/Claude 子型号筛选。
具体型号不参与左上角显示，仅作为内部计费与诊断元数据；Logo 旁显示月卡档位，
档位不可用时不显示。选择保存在
`%APPDATA%\CodexMeter\.source-selection.json`，也可以用 `--source codex`、
`--source claude-code` 或环境变量 `CODEX_METER_SOURCE` 设置。旧版按模型保存的设置
会自动迁移为 `Codex`。品牌注册与托盘选项集中在 `desktop/source_registry.py`，
具体型号及价格集中在 `desktop/model_registry.py`。

Claude Code 的本地日志目前提供当天 token 与最近型号，但不提供 Codex 风格的
5H/WEEK 配额及 banked resets，因此切换到 Claude Code 时这两张卡显示 `--`。
金额按模型分别估算；未知模型只跳过对应小计，不会让其他已知模型的 RMB 总额变成
`--`。当天只有未知模型用量时显示 `RMB 0.00`。

设备连续 1 小时没有 token、额度、RMB、状态、重置次数等有效数据变化时，会发送
`DISPOFF + SLPIN` 让 AMOLED 进入面板休眠并停止界面动画。同步器的 30 秒保活心跳
不会重置这项计时。BLE 在休眠期间保持监听；下一包发生变化的数据会先唤醒面板，
再刷新画面并返回 ACK。触摸屏幕或单击 GPIO18 也可手动唤醒。

如需自己重新打包 EXE，在 `desktop` 目录运行 `build-exe.cmd`。生成文件位于项目
根目录；构建依赖记录在 `requirements-build.txt`。完整发布可在项目根目录运行
`powershell -ExecutionPolicy Bypass -File .\build-release.ps1`，固定生成
`github-release`、`codex-token-monitor.zip` 和不带版本号的 EXE/BIN 文件。

这是一个可上板的首版工程：Windows/macOS/Linux 同步器通过官方 Codex App Server 读取账号额度，并从 Codex 或 Claude Code 的用户会话日志读取当日所有模型总 Token、任务状态、型号及成本明细，再通过 BLE 发送版本化紧凑 JSON；ESP32-S3 解析、持久化并在 480×480 AMOLED 上自行渲染。

目标硬件是 **Waveshare ESP32-S3-Touch-AMOLED-2.16**：ESP32-S3R8、16MB Flash、8MB PSRAM、480×480 CO5300 QSPI AMOLED、AXP2101 电源管理和 BLE 5。

## 目录

- `firmware/`：PlatformIO 固件
- `firmware/build_config.py`：ESP32-S3 多目标 Xtensa 工具链配置
- `firmware/src/renderer.cpp`：按最终 Figma 设计实现的英文 UI 与状态渲染
- `firmware/include/ui_theme.h`、`firmware/src/ui_theme.cpp`：主题契约与主题注册表
- `firmware/include/ai_model_catalog.h`：设备端提供器/模型显示目录
- `desktop/sync.py`：本地 Codex/Claude Code 数据读取与 BLE 同步
- `desktop/source_registry.py`：AI 编程工具品牌、别名与协议提供器注册表
- `desktop/model_registry.py`：内部具体型号、别名与计价注册表
- `PROTOCOL.md`：BLE JSON 协议的逐字段定义

## 1. 编译和烧录固件

推荐 VS Code + PlatformIO。打开 `firmware` 文件夹，连接开发板后执行：

```powershell
pio run
pio run -t upload
pio device monitor
```

也可以直接烧录已验证构建产物 `codex-token-monitor.factory.bin`：

```powershell
.\flash-firmware.cmd COM8
```

`.cmd` 不受 PowerShell 执行策略限制，并会优先复用 Codex 已安装的烧录工具。把 `COM8` 换成实际端口；不带端口运行脚本会列出当前串口。

如果已安装 Python，也可以使用 PowerShell 脚本：

```powershell
py -3 -m pip install esptool
powershell -ExecutionPolicy Bypass -File .\flash-firmware.ps1 -Port COM8
```

脚本内部执行的等价命令是：

```powershell
python -m esptool --chip esp32s3 --port COM5 write-flash 0x0 codex-token-monitor.factory.bin
```

把 `COM5` 换成实际串口。`factory.bin` 是包含 bootloader、分区表和应用的整合镜像；`codex-token-monitor.ota.bin` 仅用于后续应用分区升级，不应写到 `0x0`。

工程使用经同款硬件项目验证的 pioarduino 55.03.38-1（Arduino Core 3.3.8）、Core 内置 BLE、GFX 1.6.5 和微雪官方板级引脚。首次构建会下载工具链和三个外部库。

如果串口未出现：按住 BOOT，插入 USB，松开 BOOT，再烧录；烧录后重新上电。固件启动后广播名为 `Codex Meter-XXXX`，其中 `XXXX` 是蓝牙地址后四位；无需在系统蓝牙设置中预先配对。

## 2. 验证电脑端数据读取

需要 Python 3.10+。先做不使用蓝牙的测试：

```powershell
cd desktop
python sync.py --dry-run
```

输出应包含 5 小时/每周配额、已排除内部会话的当日所有模型总 token、当前账号可用的 banked reset 数量、紧凑 JSON 和实际字节数。客户端启动隐藏的官方 `codex app-server --listen stdio://`，使用 `account/rateLimits/read` 固定选择 `rateLimitsByLimitId["codex"]`；`account/usage/read.summary.lifetimeTokens` 仍作为账号诊断数据缓存，但不覆盖设备上的本地当日总数。客户端不会读取 `auth.json`，也不需要 OpenAI API Key。`codex_bengalfox` 等独立额度桶按键安全缓存，绝不会覆盖或替代主 `codex` 桶。`archived_sessions` 与 guardian/子代理会话不参与实时统计；网络或 App Server 暂时不可用时保留最近一次规范化账号快照。

同步器会自动查找 Codex 自带的可执行文件；非标准安装可将环境变量
`CODEX_CLI_PATH` 指向 `codex.exe`。

## 3. 启动 BLE 自动同步

Windows 推荐使用不受 PowerShell 执行策略影响的一键脚本；它会自动复用 Codex 自带的 Python、创建虚拟环境并安装 Bleak：

```powershell
.\start-sync.cmd
```

保持这个窗口运行即可监视数据变化，并至少每 5 秒校验一次；按 `Ctrl+C` 停止。只同步一次可运行 `start-sync.cmd --once`。

也可以手动运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python sync.py
```

默认每 5 秒通过官方 App Server 校验一次 `codex` 主额度与账号诊断数据；用户会话文件变化会在约 750ms 合并后更新当日所有模型总 Token 并立即同步，另每 30 秒发送一次保活心跳。未选中的模型额度桶发生变化只更新本地缓存，不触发主额度包；多任务的 `THINKING` 状态仍对所有当前活跃用户会话取并集。常用参数：

```powershell
python sync.py --once
python sync.py --interval 60
python sync.py --account-label "Work"
python sync.py --cost-cny 1829.34
python sync.py --usd-cny 7.20
python sync.py --no-live-fx
python sync.py --address "已知的蓝牙地址"
```

## 4. GitHub Releases 在线更新

正式版客户端使用公开仓库
[`VinsonKon/token-meter-releases`](https://github.com/VinsonKon/token-meter-releases)
的 latest stable release，不需要自建服务器。客户端启动约 15 秒后检查一次，此后每 6 小时
检查；也可从托盘 `Updates > Check for updates` 手动检查。
更新检查优先直接读取
`releases/latest/download/update.json`，不会占用 GitHub REST API 的未认证额度；
只有直连入口发生网络或 HTTP 故障时才使用 `api.github.com` 作为兼容备用。备用 API
遇到限流时会遵守冷却时间，并把限流余量、重试时间和 GitHub 请求编号写入错误信息。

首次安装必须用 USB 烧录 `1.0.6` 的 `codex-token-monitor.factory.bin`，建立双 OTA 分区。
之后：

- 客户端更新只下载 `CodexMeterSync.exe`。主 EXE 内置隐藏更新模式：它会先把自身复制
  为临时更新宿主，等待同步进程退出，再校验、替换正式版并重启；无需单独更新器文件；
- 固件更新下载 `codex-token-monitor.ota.bin`，校验 SHA-256 后通过 BLE 分块写入备用
  OTA 分区；设备再次校验大小与 SHA-256，通过后切换分区并重启；
- 测试版 `CodexMeterSync-Test.exe` 不检查更新，也不会发布到 GitHub Release。

发布新版本时先把 `VERSION`、`desktop/release_config.py` 和
`firmware/include/firmware_version.h` 改为同一个语义化版本，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-release.ps1
```

在 GitHub 打开
[`/releases/new`](https://github.com/VinsonKon/token-meter-releases/releases/new)，
创建标签（当前版本为 `v1.0.6`），将 `github-release` 内以下固定文件全部上传并发布：

- `CodexMeterSync.exe`
- `codex-token-monitor.ota.bin`
- `codex-token-monitor.factory.bin`
- `update.json`

发布资产文件名始终不带版本号；版本来自 Git tag 与 `update.json`。清单包含明确的
`tag`、文件大小和 SHA-256。客户端读取 latest 清单后，会从该 tag 的固定下载地址获取
EXE 与 OTA 固件，避免发布切换期间混用两个版本。当前实现使用 GitHub HTTPS 与双端
SHA-256 完整性校验，尚未加入独立发布签名。

同步器默认自动估算当天的人民币成本。它先按模型的标准 API 单价分别计算非缓存输入、缓存输入和输出 token 的美元成本，再乘以 USD/CNY 汇率。以 GPT-5.6 Sol 为例：

```text
USD = ((input - cached) × 5.00 + cached × 0.50 + output × 30.00) / 1,000,000
CNY = USD × USD/CNY
```

`output_tokens` 已包含 reasoning token，因此不会把 `reasoning_output_tokens` 再计费一次。模型单价参照 [OpenAI Codex rate card](https://help.openai.com/en/articles/20001106-codex-rate-card) 和 [GPT-5.6 Sol API pricing](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。这个数值是标准 API 等价成本估算，不是 Pro 订阅实际扣款；套餐内用量优先使用，超过额度后才可能消耗购买的 credits。

汇率规则参考 token-monitor v0.24.0：`--usd-cny` 手动值 > 24 小时在线/本地缓存 > 内置离线兜底 `6.8`。在线汇率失败不会影响 BLE 同步。`--no-live-fx` 禁用在线查询，`--cost-cny` 仍可直接覆盖最终显示金额。屏幕以 `RMB` 标识金额；每个已知模型按自己的价格计入总额，未知模型只跳过该项。没有新的可计价 Token 数据时，设备继续显示上一次有效值。

## Figma UI 状态

设备界面已经按最新 Figma 文件实现，使用 Sofia Pro 4-bit 抗锯齿字形，并只显示英文。70 px 蓝色顶部栏包含 `Codex`、月卡档位与 SVG 电池状态；配额卡片采用右对齐百分比、`5H`/`WEEK` 标签及内联重置时间。包含：

- 空闲默认态（无底部状态文字）；
- 任务运行态（蓝点与 `THINKING...`）；
- 等待权限批准态（红色 `NEED APPROVAL` 状态卡）；
- 自定义状态提示；
- 额度低于 10% 的告警态（只有对应卡片变红并保留实际百分比）；
- 低电量态（电池旁显示警告三角形）；
- 无同步数据与数据过期的保护提示；
- 更新可用、下载、安装、校验、重启、失败和回滚状态；
- 缺少 5H 额度时的单额度布局。

渲染使用 480×480 PSRAM 双缓冲：界面先在离屏帧中完整绘制，再一次提交到 AMOLED，避免逐项重绘造成的黑屏闪烁。字体与 SVG 图标覆盖率以 4-bit 灰度保存，并透明混合到 RGB565 背景，不再使用有明显锯齿的 1-bit 字形。额度进度条的胶囊端点也由设备端进行 4-bit 覆盖率混合。常规倒计时刷新周期为 10 秒，收到新 BLE 数据时立即刷新；当日所有模型 token 总数变化时，使用非阻塞的 1.5 秒 `easeOutQuart` 连续数字滚动动画，首次启动与数值未变化时不播放。`THINKING...` 使用连续余弦呼吸，让底部圆点和完整文字在 0.3–1.0 透明度间平滑变化；安装、校验和重启状态的三个省略号使用共享的 1.2 秒错相循环。状态与 token 动画均以约 25 FPS 独立更新，并只传输需要变化的局部矩形；旋转画面时局部区域会同步映射。

主题不再散落在渲染代码中。`ui_theme::Theme` 同时描述 `Colors`、`Typography`、
`Assets`、`Metrics` 与 `Motion`，`renderer.cpp` 只读取 `ui_theme::active()`。
新增主题时在 `ui_theme.cpp` 注册一项并补充 `Id` 即可；颜色、字体、光晕资源、
关键尺寸和动画节奏都可以替换，不需要复制页面绘制逻辑。

卡片百分比表示剩余额度：同步协议仍发送 Codex 的已使用百分比，设备渲染时转换为 `100 - used_percent`。数据和渲染保持分层：

- `main.cpp` 负责 BLE、JSON 解析、NVS 和电源状态；
- `codex_packet.h` 是设备端的强类型渲染数据模型；
- `renderer.cpp` 只负责显示。

## 已知边界

- Codex 账号额度和 banked reset 数量来自官方 App Server；显示的 `TODAY TOKENS` 来自所有用户会话累计计数器的当日差值汇总，guardian 与其他子代理会话不参与。App Server 的 `primary`/`secondary` 不等于固定 5H/WEEK，客户端按 `windowDurationMins` 分类：300 分钟进入 5H 槽，10080 分钟进入 WEEK 槽；没有 5H、只有 WEEK 时自动使用单额度布局。Claude Code 本地 JSONL 仅用于 token 统计，不包含 Codex 风格配额。
- `lt` 是协议 v1 的向后兼容可选字段；当前 Codex 与 Claude Code 的当日汇总均发送 `false`，设备显示 `TODAY TOKENS`。官方 `account/usage/read.summary.lifetimeTokens` 仅保留为账号诊断数据。
- 已保存的快照可在设备重启后继续显示；下一次 BLE 同步会刷新状态和准确倒计时。
- JSON 最大 512 字节。固件请求 MTU 247，超过单个 ATT 负载时由 Bleak/GATT 执行长写入。

## 参考实现

板级引脚和构建参数同时与微雪官方示例、[Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) 的 `waveshare_amoled_216` 硬件层交叉核对。本项目同样采用 JSON GATT，但扩展了版本、双配额和完整 token 分类，并保留独立设备端渲染层。
