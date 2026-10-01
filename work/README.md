# 让 f4hwn 5.9.0.CN 在本机（Windows）跑起来

本机实测记录。结论先说：**这个项目并不是"只能跑在 Linux"，只有外壳脚本和 unix socket 是
Linux 的；机器模型和工具本身跨平台。** 我在本机用 MSYS2 原生编了 QEMU 7.2，把
`qemu/py32f071.c` 编进去，用户的固件已经跑起来、能按键、能出画面。

## 固件是什么

`04-20260827_白头佬汉化版_f4hwn.5.9.0.bin`，114,324 字节。

* 身份串：`UV-K5 Firmware, EGZUMER+F4HWN v5.9.0.CN`
* 向量表：SP=0x20004000，Reset=0x08002d49 —— 与仓库参考固件逐字节相同，说明它是
  **PY32F071 的应用镜像**，链接基址 `0x08002800`（`F:\pi` 里的逆向报告独立确认了同一基址）
* 它是应用镜像、不含 0..0x27FF 的出厂引导程序，所以**可以直接当 `-kernel` 用**

## 为什么要包一层 ELF（重要）

仓库 `py32f071.c` 的注释说 "`.elf/.bin` 都能启动"，这句话对 `.bin` **不成立**：

* QEMU 7.2 的 `armv7m_load_kernel(cpu, file, mem_base=0x2800, size)` 对 ELF 用程序头里的地址，
  对裸 `.bin` 则按 `mem_base` 加载 —— 也就是**容器地址 0x2800**；
* 而容器地址 0..0x1D800 是 flash 的别名区（映射到 flash 偏移 0x2800 起），所以裸 bin 会被写到
  flash 偏移 0x5000，**整整偏移 0x2800**，CPU 从地址 0 取向量表时读到的是空白，直接跑飞。

`tools/bin2elf.py` 就是补这个：把 `.bin` 包成一个 `PT_LOAD` 在 `0x08002800`、
入口取镜像自身复位向量的 ELF32/ARM。

## 中文字体在 SPI flash 里，不在固件里

发行包里 `01/02/03` 三个文件是**首次刷机**才要的：`02` 是 16x16、`03` 是 8x8 的 GB2312 点阵
字体包（UF2 容器）。它们烧到 SPI NOR 的固定位置：

    UF2 头里的目标地址      内容            大小
    0x000A0000            16x16 字模       261,888 B（8184 字模 × 32 B）
    0x000E0000            8x8 字模          65,536 B（8192 字模 ×  8 B）

所以 `make_flash.py` 现在支持 `--blob`，`.uf2` 按自身块地址落盘：

    python tools/make_flash.py \
        --blob 0:work/f4hwn/02-大字体16x16.uf2 \
        --blob 0:work/f4hwn/03-小字体8x8.uf2

不写进去会怎样：字体区是空的（0xFF），汉字全变实心块。

## 关键地址（已用固件源码交叉验证）

| 符号 | 地址 | 大小 | 说明 |
| --- | --- | --- | --- |
| gFrameBuffer | `0x200012BE` | 896 = 7 行 × 128 | 显示区，面板第 1..7 页 |
| gStatusLine | `0x2000163E` | 128 | 状态行，面板第 0 页 |

两个地址**都不是 128 字节对齐**，相差正好 896 字节（帧缓冲在前、状态行在后，与声明顺序相反；
参考固件的 `0x200013DC / 0x2000175C` 也是这个关系）。

这两个值是**算出来的，不是看出来的**，方法可复用到任何新固件：

1. 抓 16 KB SRAM：`python work/qmp.py dump 0x20000000 0x4000 work/s4.bin`。
2. 从源码里挑"内容已知、落点已知"的位图（`App/driver/st7565.c`、`App/ui/status.c`、
   `App/bitmaps.c`）：
   * `gFontPowerSave` 在状态行 +0、`gFontDWR` +18、`gFontPttClassic` +54、
     `BITMAP_BatteryLevel1` +111（= `LCD_WIDTH - 17`）；
   * `BITMAP_VFO_Default`（VFO 箭头）由 `memcpy(p_line0 + 0, ...)` 画在**帧行偏移 0**。
3. 在 SRAM 里搜这些字节串：状态行四个位图的间距要**同时**成立，只有一个基址满足；
   箭头的落点直接给出帧缓冲基址。两者独立得出 `0x2000163E` 与 `0x200012BE`（相差 896，吻合）。

之前我把基址取成 128 对齐的 `0x20001280 / 0x20001600`，偏了 **0x3E = 62 字节**。后果不是整体平移，
而是**每行 62 字节环绕折行**：渲染出的每一行 = 上一真实行的尾部 62 列 + 本行的头部 66 列，
字会从第 66 列被劈开，状态行还混进了帧行尾部——看起来就是"没对准"。

快速自检（对齐正确时应成立）：帧行 3 是源码 `memset(gFrameBuffer[3], 0, 128)` 清掉的中缝，
应整行全 0；上/下 VFO 的帧行 0≡4、1≡5；帧行 2 与 6 只该在活动 VFO 的说明文字上不同。

面板侧两个细节（不影响取帧，但解释列号为什么 +4）：驱动写 `Line + 176`、`Column + 4`，
`cmds[]` 用 `0xA1`（SEG 反向）+ `0xC0`（COM 正常）。

## 怎么跑

    powershell -File work\run-emulator.ps1      # 起 QEMU（QMP tcp:4444，GDB tcp:1234）
    powershell -File work\run-webui.ps1         # 起网页遥控，然后开 http://127.0.0.1:8080/
    powershell -File work\restore-flash.ps1     # 还原 flash.img（先停模拟器）

命令行按键（走 TCP）：

    python tools/key.py --socket 127.0.0.1:4444 MENU DOWN DOWN

## 本机上的构件

| 路径 | 说明 |
| --- | --- |
| `F:\dsh-build\qemu-7.2.0` | QEMU 7.2.0 源码 + 打入的模型、patched SysTick、uv-k5-v3 注册 |
| `F:\dsh-build\qemu-7.2.0\build\qemu-system-arm.exe` | 编译产物；运行需要 `F:\msys64\mingw64\bin` 在 PATH |
| `F:\msys64` | MSYS2（gcc 16.2 / glib 2.90 / pixman / meson / ninja / gdb / perl） |
| `work/f4hwn/*.elf` | 由 .bin 包的 ELF |
| `assets/flash.img` | 校准 + 字体；`work/flash-base.img` 是干净副本 |

## 为了在 Windows 上跑起来，对仓库做了什么

1. `qemu/py32f071.c`：补 `#include "qapi/visitor.h"`。原文件用 `visit_type_uint64` 却没包含声明它的
   头，对**原版 QEMU 7.2 编译不过**（作者的环境里应该是被别的头间接带入的）。
2. `tools/bin2elf.py`：新增（见上）。
3. `tools/make_flash.py`：新增 `--blob ADDR:FILE`，`.uf2` 按块地址解析。
4. `tools/uvk5_qmp.py`、`tools/key.py`：QMP 端点除 unix 路径外接受 `host:port`。
5. `tools/uvk5_supervisor.py`：`wait_for_socket` 支持 TCP 端点。
6. `tools/uvk5_lcd.py`、`tools/uvk5_stream.py`：帧暂存目录不再硬编码 `/dev/shm`（Windows 没有），
   退回系统临时目录。

Linux 上的行为都没变：地址默认值仍是 unix 路径，`/dev/shm` 存在时仍用 `/dev/shm`。

## 面板级设置：对比度与反显

菜单里的 **SetCtr（对比度）** 和 **SetInv（反显）** 属于面板，不属于帧缓冲：

```c
gSetting_set_ctr = ...;                    // App/app/menu.c
ST7565_ContrastAndInv();                   // → 0xE2、0x81 + (21 + set_ctr)、0xA6|set_inv
```

它们只往 ST7565 发命令，`gFrameBuffer` 一个字节都不改。网页渲染的是帧缓冲，所以"改了没反应"是必然的
——除非把控制器本身也建模。现在 SPI1 后面挂了最小的 ST7565 模型（A0 接 PA6、CS 接 PB2，与
`App/driver/st7565.c` 一致），解析 `0xA6/0xA7`（反显）、`0xAE/0xAF`（开屏）、`0x81 <值>`（对比度），
并暴露三个**只读**属性：

    qom-get /machine/panel invert        反显位（0xA7 之后为真）
    qom-get /machine/panel contrast      0x81 后面那个值
    qom-get /machine/panel display-on    0xAF / 0xAE

* **反显会真的作用到画面**：`tools/uvk5_lcd.py` 渲染时按该位取反，所以 60 号设置现在看得见。
* **对比度只报告不渲染**：那是模拟量（玻璃多黑），渲染不出来。本机实测值 `36 = 21 + 15`，
  正好等于当时存着的对比度设置，说明命令一直都在发。
* **display-on 只报告不动作**：软复位 `0xE2` 是否清掉开屏位我无法确证，拿不确定的语义去把用户画面
  变黑比不动作更糟。

## 固件日志为什么要在网页里看得见

模型把串口按 `SERIAL <行>` 打到 **stderr**，而只有**服务器自己启动 QEMU** 时才会去读这个管道
（`uvk5_supervisor` 在连接成功后 `pump_stream` 到日志缓冲）。所以：

* `work/run-webui.ps1` 默认**自己启动模拟器**（页面上的 On/Off 也就是真的了），
  日志面板因此能看到固件串口（启动横幅 `UV-K5 Firmware, EGZUMER+F4HWN v5.9.0.CN` 就在里面）；
* 加 `-Attach` 则回到"附着到别处启动的模拟器"，此时服务器看不到那个 stderr 流，面板里只有
  QEMU/电源事件——**这正是之前看不到 debug 日志的原因**。

这条线上一根线上跑着两种东西：固件自己的可读输出，和 **CPS 编程协议（二进制）**。后者按文本解码会
把面板刷成一屏控制字符、把可读的那行埋掉。所以 `uvk5_logs.describe_line()` 现在对"大部分字节不可打印"
的行只给**长度 + 十六进制头**，可读行照旧——两种数据都还在，只是不再互相盖住：

    [serial] UV-K5 Firmware, EGZUMER+F4HWN v5.9.0.CN
    [serial] <binary 15 bytes> f0 aa 55 02 04 80 01 02 03 04 05 06 07 08 09
    [serial] <binary 255 bytes> 0e 0f 10 11 12 13 14 60 0c 1f 06 f0 15 c3 07 … +231 bytes

回归测试在 `tools/test_uvk5_logs.py::test_pump_stream_summarises_binary_serial`。

## 我在这一路上搞错和修掉的东西

* **更正：固件确实在读 SPI flash。** 我先前写的"26 秒零访问"是**测量假象**——PowerShell 的
  `2>` 重定向把 QEMU 的 stderr 写成了 UTF-16LE，而我的过滤器在找 `FLASHREAD`/`LCDW` 这样的
  ASCII 行，于是"什么都没找到"被我当成了"什么都没发生"。按 UTF-16 解码后：SPI1 上是完整的
  ST7565 序列（`e2 a2 c0 a1 a6 a4 24 81 1f 2b…`），SPI2 上读的全是 `0x00A0xx`（设置区，48 个不同
  地址，片选翻转 30 次）。
* **再更正一次（同一个坑的第二种形态）：字体包确实被读了。** 上面那句"字体包没被读"是我把探针
  **截断在前 80 次读**得出的——正是 AGENTS 里"没弄清数据形态之前不要截断诊断日志"警告过的错。
  把上限放到 4000 次、并顺手在菜单里转一圈让固件画汉字之后，实测 3168 次读里：
  `0x0A0000`(3)、`0x0B0000`(11)、`0x0C0000`(6)、`0x0E0000`(15) —— 都在字体区；另外
  `0x1E0000` 有 **1024 次、每步正好 32 字节**（= 连续走完 32 KB 的一张字体表）。
* **外置 SPI flash 的分区**（由 `gitee.com/oldlicn/betula-multi-system-tool` 的官方数据反推，
  不靠那张分区图）：

  | 偏移 | 大小 | 内容 |
  | --- | --- | --- |
  | `0x000000` | 128 KB | BL/多系统引导 + 设置（固件读 `0x00A0xx`）+ 校准（`0x010000`，与 `make_flash.py` 一致） |
  | `0x020000` | 4 × 128 KB | 四个固件槽（官方"清空 0x20000-0x40000 … 0x80000-0xA0000"正好四段） |
  | `0x0A0000` | 256 KB | 用户字体包 16x16（UF2 目标地址就是这里） |
  | `0x0E0000` | 64 KB | 用户字体包 8x8 |
  | `0x100000` | 1 MB | 出厂资源区：含拼音串，且 **`0x1E0000` 处有 32 KB 字体表**，固件开机会整张走过 |

  仓库里 16 份官方恢复数据（每份 128 KB）可以直接拼回**真机整片 2 MB 数据**；
  把这些字节拼出来放在 `F:\dsh-build\flashdump\factory-2MB.img`，补上它之后再跑，
  **画面上的字会变**——说明模拟器原来的镜像缺了固件真正在用的字体数据
  （`work/flash-with-factory-resource.img` 就是这个"原厂资源区 + 用户字体包 + 校准"的合成）。
  剩下没落实的是：屏幕上的 16 像素大字与 `0xA0000` 的字模包**仍不能逐字节对上**
  （2/24），与 `0x1E0000` 那张表也对不上（0/8），所以"哪个来源供哪一块文字"还没定论。
* **修：Windows 上 `rename()` 不覆盖已存在的文件。** flash 回写走"写临时文件再改名"，
  于是每一次回写都失败（`cannot replace`），设置因此**从不落盘**，而反复的告警把主循环挤住，
  连 QMP 的问候语都发不出来（表现为"power on failed: timed out"）。改用 `g_rename()`
  （需 `<glib/gstdio.h>`；Windows 上是 MoveFileEx+替换，Unix 上就是 rename）。现在电源能开、设置能存。
* **修：power on 失败时看不到原因。** supervisor 只在连接成功后才去读 QEMU 的 stderr，
  于是失败只剩一句"QMP 端口没出现"。现在会把 QEMU stderr 的**尾部**记进日志——上面那个 rename
  问题正是这样浮出来的。
* **修：启动器在 TCP 端点下用错参数。** `default_launcher` 现在按端点类型生成
  `unix:…` 或 `tcp:host:port`（Windows 的 QEMU 根本没有 unix socket）。
