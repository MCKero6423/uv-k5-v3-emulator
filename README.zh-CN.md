# UV-K5 V3 模拟器

在 PC 上运行泉盛 UV-K5 V3 / UV-K1 固件。这台电台用的是普冉 PY32F071（Cortex-M0+），
QEMU 没有对应的机器模型，所以这里加了一个。

固件约五秒进入主循环，LCD 内容可读，键盘能驱动菜单。哪些建了模、哪些没有，见
[还原程度](#还原程度)。

*English: [README.md](README.md) · 本文档与英文版内容对应，改动请同步两份。*

| 主界面 | 菜单 | 按键导航后 |
| --- | --- | --- |
| ![主 VFO 界面](docs/screenshots/main-vfo.png) | ![菜单停在 Step](docs/screenshots/menu-step.png) | ![菜单停在 RxDCS](docs/screenshots/menu-navigated.png) |

这是真实截图，不是效果图：`tools/screenshot.py` 从 guest 内存里读出固件的
`gFrameBuffer` 再渲染，所以这些就是 LCD 驱动实际写下的像素。从左到右依次是双守候主界面、
用 `key.py MENU` 打开的菜单（第 01/79 项 Step）、以及 `key.py DOWN DOWN` 之后的 03/79。

## 它用来做什么

改一行固件就要重新烧进电台验证，太慢；而且有些 bug 从外面根本看不见。举个真实例子：
CW 宏录制看起来毫无反应，原因在三层之下 —— keyer 被一个后续调用拆掉了，那个调用从错误的
VFO 重算了状态。在真机上你只看到"什么都没发生"，在这里可以直接读那些变量。

它**不做**的事是复现电台行为。它复现固件**下达了什么命令** —— 频率、功率档位、什么时刻键控 ——
而不是模拟结果。键控包络、杂散发射、灵敏度都需要真机加频谱仪。这不是以后能补上的缺口：
收发芯片没有公开 datasheet，它的驱动是唯一可得的规格。

## 还原程度

| 项目 | 状态 |
| --- | --- |
| 启动到主循环 | 可用，约 5 秒 |
| LCD 内容 | 可用，经 `tools/screenshot.py` |
| SPI flash、设置、校准数据 | 可用，且断电保留 |
| 频率输入 | 可用，按波段分别存储并保留 |
| 键盘与菜单导航 | 可用，含从省电模式唤醒 |
| 串口输出（固件日志） | 可用，出现在网页日志里 |
| 串口输入、CPS 编程协议 | 可用，`-serial` 接任意字符设备 |
| BK4819 寄存器接口 | 可用，RSSI 和状态可读 |
| S 表 | 可用，经监听模式（SIDE1） |
| 信号强度 | 取决于调谐位置：虚拟电台 vs 噪底 |
| PTT 与发射 | 可用；TX 标识、计时器、话筒电平条 |
| 喇叭 / 麦克风音频 | **不存在可建模的采样**，见[音频](#音频) |
| `millis()` / TIM2 | 可用；按接近实际时间的速率递增 |
| 时序精度 | **故意是错的**，见[时序](#时序) |
| 模拟量射频行为 | **没建模，也永远不会有**，见 [AGENTS.md](AGENTS.md#the-bk4819-and-where-modelling-it-stops) |

短按 `tools/key.py MENU` 打开菜单，UP/DOWN 在其中移动，MENU 进入子菜单，直接输入菜单编号
可以跳到那一项。按键时长决定短按还是长按，而固件把这两者当成不同事件 —— 见[时序](#时序)。

**按键时长是最需要拿准的东西。** 按住 400 ms 以上算**长按**，处理函数的反应完全不同：
`MAIN_Key_MENU` 在短按松手时打开菜单，走长按路径则什么都不做。所以如果某个键像是被忽略了，
应该**缩短**按压时间而不是延长。从省电模式唤醒不需要任何特殊操作 —— 一次 200 ms 的按压
既能唤醒电台也能打开菜单，空闲 45 秒后实测有效。

`tools/keypad_test.py` 会在一个临时 QEMU 实例上验证以上全部。它之所以存在，是因为键盘有一个
很不直观的陷阱：键盘模型的 `row_out` 数组**必须保持 `volatile`**，否则 GCC 在 -O2 下会证明
那些线路始终为 NULL，从而删掉所有对 `keypad_update_rows()` 的调用 —— 结果是没有任何行被驱动，
按键静默失效。改动那部分代码后请跑这个测试；`AGENTS.md` 里有目标代码层面的证据。

## 目录结构

    qemu/                    需要复制进 QEMU 源码树的文件
      py32f071.c             SoC 与机器定义（主要工作量在这里）
      armv7m_systick.*.patched  加了 poll-boost 属性的 SysTick
    assets/                  flash.img，以及作为参考副本的 pristine/
      calibration.bin        从真机导出的 512 字节校准数据
    deploy/                  HTTPS 前端用的 nginx vhost
    docs/reverse-proxy.md    https://k6v3.mckero.dn42/ 是怎么提供服务的
    *.zh-CN.md               中文翻译，与英文版同步维护
    docs/screenshots/        本 README 用到的 LCD 截图
    tools/                   运行、截图、注入按键、探查状态
      keypad_test.py         键盘回归测试，自己启动实例
      test_flash_persist.py  flash 写入能跨断电保留
      test_freq_entry.py     输入的频率生效并保留
      test_serial_rx.py      固件会响应编程命令
      test_bk4819.py         BK4819 寄存器接口，RSSI 不再恒为零
      test_bk4819_readback.sh  寄存器读回的位对齐正确
      test_smeter.py         监听时 S 表能读到信号
      test_ptt.py            PTT 能键控电台并干净释放
      test_scan.py           繁忙频段不会卡死扫描
      test_audio_path.py     固件想出声时功放会打开
      test_battery.py        电量与低电告警跟随 ADC
      test_millis.py         millis() 会递增，超时才可能到期
      test_spectrum.py       RSSI 取决于调谐位置，不是常数
      run_tests.sh           跑上面全部，先检查构建
      test_run_tests.sh      验证 runner 真的能发现失败
      lib_kill_emulator.sh   只杀模拟器的清理逻辑
      webui.py               网页远控：实时 LCD 加可点击键盘
      dn42_firewall.sh       把网页端口限制到 DN42 来源
      restore_flash.sh       把 flash 镜像回滚到初始状态
      uvk5_qmp.py            QMP 客户端
      uvk5_lcd.py            帧缓冲解码、PNG 编码、抓帧
      uvk5_keys.py           键盘模型接受的按键名
      uvk5_logs.py           共享日志缓冲，含客户端 IP 归属
      uvk5_stream.py         /stream 背后的抓帧泵
      uvk5_supervisor.py     启动、停止、以及故障后恢复模拟器进程
      test_kill_emulator.sh  清理逻辑绝不会杀掉无关进程
      （另有一批临时探针脚本 —— scan_trace.sh、gpio_watch.py 等 ——
       留着是因为随手就能用，不是因为它们打磨过）
    harness/, stubs/, shim/, tests/   CW 时序链的宿主机构建（阶段 A）

## 构建

需要 QEMU 7.2 源码树，以及 `meson`、`ninja`、`libfdt-dev`、`libglib2.0-dev`、
`libpixman-1-dev`。

    # 1. 把源文件放进 QEMU 源码树
    cp qemu/py32f071.c                   $QEMU/hw/arm/
    cp qemu/armv7m_systick.c.patched     $QEMU/hw/timer/armv7m_systick.c
    cp qemu/armv7m_systick.h.patched     $QEMU/include/hw/timer/armv7m_systick.h

    # 2. 注册这台机器。在 $QEMU/hw/arm/Kconfig 里：
    #      config UVK5_V3
    #          bool
    #          default y
    #          depends on TCG && ARM
    #          select PY32F071_SOC
    #      config PY32F071_SOC
    #          bool
    #          select ARM_V7M
    #          select UNIMP
    #    在 $QEMU/hw/arm/meson.build 里：
    #      arm_ss.add(when: 'CONFIG_UVK5_V3', if_true: files('py32f071.c'))

    # 3. 只构建 ARM target
    cd $QEMU
    ./configure --target-list=arm-softmmu --disable-docs --disable-tools
    cd build && ninja qemu-system-arm

然后验证构建真的能用，大约一分钟：

    bash tools/run_tests.sh        # 全部，几分钟
    bash tools/run_tests.sh -q     # 只跑单元测试，约 15 秒，不需要模拟器

**runner 会先检查构建，失败就拒绝继续。** 因为 `ninja` 失败时会把上一个二进制留在原地，
否则测试会拿一份从未编译过的代码跑出"通过"。单个测试仍然可以独立运行：

    python3 tools/keypad_test.py
    python3 tools/test_flash_persist.py
    python3 tools/test_freq_entry.py
    python3 tools/test_serial_rx.py
    python3 tools/test_bk4819.py
    bash tools/test_bk4819_readback.sh
    python3 tools/test_smeter.py
    python3 tools/test_ptt.py
    python3 tools/test_scan.py
    python3 tools/test_audio_path.py
    python3 tools/test_battery.py
    python3 tools/test_millis.py
    python3 tools/test_spectrum.py

这件事比看起来重要。键盘可以在 -O2 下静默失效而**不产生任何编译警告** ——
见[还原程度](#还原程度)里关于 `volatile` 的说明 —— 所以"构建干净"不等于按键能用。
其中几个测试覆盖 flash 路径，那里有四个各自独立的故障，每一个都会把已存频率清零
而**不报任何错误**：细节见
[AGENTS.md](AGENTS.md#the-flash-bugs-four-faults-one-symptom)。

其余测试：

    cd tools && python3 -m unittest discover -p 'test_uvk5*.py' -v  # 快，不需要模拟器
    cd tools && python3 -m unittest test_webui -v                   # 快，不需要模拟器
    python3 tools/test_webui_e2e.py                                # 自己启动模拟器

## 运行

    python3 tools/make_flash.py     # 执行一次，生成 assets/flash.img

模拟器会写入这个镜像，所以一次会话可能留下改过的设置、甚至损坏的 EEPROM。
`assets/pristine/` 保存了镜像刚生成时的带校验和副本，`tools/restore_flash.sh` 负责还原：

    tools/restore_flash.sh --verify   # 参考副本本身是否完好
    tools/restore_flash.sh --diff     # 当前镜像变了没有，变了多少
    tools/restore_flash.sh            # 还原，并先保存当前镜像

参考副本以 gzip 存储，占 2.3 KiB 而不是 2 MiB —— 因为镜像几乎全是 0xFF，小到可以放进 git。
运行时的镜像仍被忽略：它是会被写入的构建产物。

    tools/run.sh                    # 启动机器

    tools/where.sh                  # 固件当前执行到哪里
    python3 tools/screenshot.py --frame-addr 0x200013DC \
        --status-addr 0x2000175C --port 1234 --out screen.png
    python3 tools/key.py MENU       # 注入一次按键
    tools/gpiob_dump.sh             # GPIOB 寄存器

这台机器在 1234 端口暴露 GDB stub，QMP socket 在 `/tmp/uvk5-qmp.sock`。它是无头的：
屏幕是从 guest 内存里读出来而不是画出来的，所以不需要任何显示后端。

截图需要 `gFrameBuffer` 和 `gStatusLine` 的地址，而它们在不同固件构建之间会变。这样找：

    arm-none-eabi-nm firmware.elf | grep -E 'gFrameBuffer|gStatusLine'

## 网页远控

`tools/webui.py` 提供 LCD 画面和一个可点击的键盘，这样就能在浏览器里操作电台，
不用反复敲 `key.py` 加 `screenshot.py`。

    tools/run.sh                                   # 先起模拟器
    python3 tools/webui.py --frame-addr 0x200013DC \
        --status-addr 0x2000175C                   # 再起服务

打开 <http://127.0.0.1:8080/>。键盘按电台的实际布局排列，侧键在旁边。方向键、
回车（MENU）、Esc（EXIT）和数字键都绑定到了对应的物理按键。

按键时长取自你实际按住的时间，因为固件把超过 400 ms 的按压当作**长按**并派发成不同事件。
浏览器分别发送两个边沿，而不是让服务端按固定时长模拟一次按压。

想脚本化的话，接口如下：

| 路由 | 用途 |
| --- | --- |
| `GET /` | 页面本身 |
| `GET /stream` | multipart PNG 流，最高 15 fps |
| `GET /frame.png` | 单帧 |
| `POST /api/key` | `{"key": "MENU", "action": "down"}` — 也可以是 `up` 或 `tap` |
| `POST /api/ptt` | `{"held": true}` — 按住 PTT，`false` 释放 |
| `POST /api/release-all` | 释放所有按键，万一有键卡住 |
| `GET /api/status` | QMP `query-status`，另含 `speaker` 字段 |

画面用 QMP `memsave` 读取，每帧约 1.35 ms，且 guest 全程继续运行。这里有两个细节很容易搞错：

- **必须用 `memsave`，不能用 `pmemsave`。** 帧缓冲符号是 CPU 虚拟地址。`pmemsave` 会把参数
  当成物理地址，返回一整块零 —— 于是画面渲染成全空白，而且哪里都不报错。
- **不要走 gdb。** `screenshot.py` 通过 gdb 读帧，而 gdb 每次 attach 都会暂停 guest。
  这对实时流完全不可用，而且会扰乱按键防抖的时序。

使用前值得知道的两个限制：

- **QMP socket 只接受一个客户端。** 服务运行期间，`tools/key.py` 无法连到同一个模拟器。
- **没有任何认证。** 任何能访问到这个端口的人都能完全控制这台模拟电台。正因如此，
  它默认只绑定 loopback。

### 从别处访问

这里的部署方式是服务只监听 loopback，前面放 nginx 做 TLS，对外是
`https://k6v3.mckero.dn42/`。vhost 配置见
[docs/reverse-proxy.md](docs/reverse-proxy.md)，其中对这个应用特别重要的两项是：
`proxy_buffering off`（否则画面流会一阵一阵地到）和 `X-Forwarded-For`（否则每条日志
都会被记成 127.0.0.1）。

用 `--host ::` 直接绑定也可以，但既然没有认证，那这个端口就必须按来源地址过滤。
`tools/dn42_firewall.sh` 把它限制到 DN42：

    tools/dn42_firewall.sh apply 8080     # 只允许 DN42 + loopback
    tools/dn42_firewall.sh show  8080     # 规则和包计数
    tools/dn42_firewall.sh remove 8080

一个容易搞错的细节：这台主机的 `INPUT` 默认策略是 `ACCEPT`，所以只**放行** DN42 的规则
什么都改变不了 —— 一条规则都没有的时候，那个端口本来就是可达的。真正起作用的是**最后那条
`DROP`**。请通过看计数器来验证，而不是靠假设：

    tools/dn42_firewall.sh show 8080
    # DROP 计数在涨，说明非 DN42 的流量真的被拒了

这些规则重启后不保留。重新执行 `apply`，或者用 `iptables-persistent` 持久化。

**PTT 不在键盘矩阵里**，因为固件是直接读它自己的引脚（PB10），而不是当成矩阵键去扫描。
它在界面上有独立按钮、也有独立接口，而且是"按住"而非"点一下"：

    curl -X POST -H 'Content-Type: application/json' \
        -d '{"held": true}' http://127.0.0.1:8080/api/ptt

任何结束会话的动作都会释放它 —— 把指针拖出按钮、关闭标签页、或者 `POST /api/release-all` ——
所以客户端消失不会让电台一直处于发射状态。`press` 属性仍然拒绝把 "PTT" 当作按键名；
未知按键返回 400 而不是转发出去。

## 音频

**没有音频，而且没有什么可加的。** 在真机上，喇叭和麦克风都不经过 MCU：接收音频在 BK4819
内部解调，以模拟信号从它的 AF 输出出来；发射音频从麦克风直接进入芯片自己的 ADC。
固件全部能碰到的只有三样东西：

| | |
|---|---|
| PA8 | 功放使能，开或关 |
| `REG_47` | 芯片路由哪一路 AF 源 |
| `REG_64` | 一个供固件显示的电平值 |

**MCU 的地址空间里任何地方都不存在音频采样**，所以模拟器没有东西可以采集或播放 ——
网页也不需要麦克风或播放权限，因为根本没有内容需要它承载。在这里生成声音，等于
编造固件从未产生过的数据。

**真实可用的**是"固件此刻是否想出声"，而 PA8 精确表达了这一点。界面把它显示成电源状态旁边的
一个喇叭图标，`/api/status` 以 `speaker` 字段上报。按 SIDE1 进入监听模式，它就会亮起。

## 这台机器是怎么搭起来的

寄存器布局来自固件自带的厂商 CMSIS 头文件
（`Drivers/CMSIS/Device/PY32F071/Include/py32f071xB.h`），不是猜的。

    FLASH  0x08000000  128 KB   应用在 +0x2800，bootloader 在它下面
    SRAM   0x20000000   16 KB
    RCC    0x40021000
    GPIO   0x50000000   端口 A、B、C、F，间隔 0x400
    SPI1   0x40013000   显示屏
    SPI2   0x40003800   flash
    ADC1   0x40012400

已建模：RCC、GPIO、ADC、两个 SPI 控制器、DMA1、TIM2，以及 PY25Q16 flash。
其余全部由一个带日志的兜底模块响应 —— **那份日志正是判断下一个值得建模的东西的依据。**

固件能启动之前，有七件事必须做对，每一件都是靠观察它停在哪里发现的：

- **在应用偏移处做 flash 别名。** 内核从地址 0 取向量表，而镜像加载在 0x08002800，
  所以 0 必须别名到那里，而不是 flash 基址。
- **时钟就绪位。** `BOARD_Init` 会轮询它们；每个使能位都要镜像到对应的就绪位。
- **ADC 校准。** `CR2.CAL` 是写 1 启动、硬件自清，所以绝不能把它存成置位状态，
  否则等待循环永远出不来。
- **SPI 标志位。** 传输在寄存器写入内部就完成了，所以 TXE 保持置位，RXNE 由写入抬起。
- **DMA。** flash 驱动从不碰 SPI 数据寄存器 —— 它武装通道 4 和 5、使能传输完成中断，
  然后自旋等待自己 ISR 设置的标志。
- **SysTick。** 见下文。
- **收发芯片数据线。** `RADIO_SetupRegisters` 等待 BK4819 REG_0C 的 bit 0 清零。
  那条总线是 GPIO 位操作驱动的，所以在这条总线有真实模型之前，PB9 保持低电平，
  让读取返回零。

## 时序

`SYSTICK_DelayUs` 轮询 SysTick 计数器并累加差值。在真机上每次循环迭代会让计数器前进
几十个 tick；在模拟环境下，一次寄存器读取相对 guest 时间的开销要大得多，所以每次读取
计数器几乎不动。实测：一个 120 ms 的延时，在四秒里只前进了所需 5,760,000 个 tick 中的
832 个 —— 照这个速度要 **7.7 小时**才能完成。

**降低时钟频率没有用**，这一点值得在尝试之前知道：瓶颈是每秒的循环迭代次数，不是计数器速度。
把 48 MHz 降到 200 Hz 只快了 32 倍。

真正有效的办法是报告一个跑在真实值前面、且每次读取都在增长的计数值。SysTick 上的
`poll-boost` 属性就是干这个的。**之前有两次尝试是把那个值写回定时器**，结果每次读取都会
重新锚定计数 —— 上报的值不再变化、固件的 `if (cur != prev)` 判断永远不成立、
循环彻底卡死。

代价是任何延时期间 guest 时间都跑得飞快。对于验证菜单和控制流没问题；用来判断信号时序则是错的。

`poll-boost` 只加速计数器**读取**。SysTick **中断**仍然接近实时触发，而正是它们驱动
`SysTick_Handler` → `gNextTimeslice` → `APP_TimeSlice10ms` → `CheckKeys`。所以固件的
10 ms 时间片阈值在实际时钟下是成立的：一个按键必须按下 20 ms 才会被登记，400 ms 则成为长按。

**把这两者分清很重要。** `tools/key.py` 最初按住按键 2500 ms，前提是以为 guest 时间在这里
也跑得快 —— 结果把每次按压都变成了长按。那些在短按松手时才动作的处理函数（`MAIN_Key_MENU`
就是其中之一）全都无视了它，于是键盘看起来是坏的，其实并没有。

## 阶段 A：宿主机上的 CW 时序链

`harness/`、`stubs/`、`shim/` 和 `tests/` 把 `app/cwkeyer.c` 与 `app/cwmacro.c`
**原样不改**地对着桩驱动编译，配一个虚拟时钟和脚本化的电键输入。喂进一串触点闭合的时间线，
就能对解码出的字符和码元时长做断言。

**固件源码原样编译是刻意的。** 为了让它们能在宿主机上构建而去修改，会让测试与电台实际运行的
代码逐渐脱节。`CW_ReadKeys` 里的防抖是**照抄**而不是打桩的，因为它的不对称性
（要连续三次读取才登记按下，而释放是立即的）本身就是被测时序行为的一部分。

## 许可

Apache 2.0，见 [LICENSE](LICENSE)。

一个例外：`qemu/py32f071.c` 按其文件头声明采用 GPL-2.0-or-later。它被编译进 QEMU，
并且派生自 QEMU 的设备模型（GPL-2.0），所以不可能是别的许可。工具、harness 和文档
是 Apache 2.0。

## 致谢

基础固件：[armel/uv-k1-k5v3-firmware-custom](https://github.com/armel/uv-k1-k5v3-firmware-custom)。
寄存器定义来自厂商 CMSIS 头文件。
