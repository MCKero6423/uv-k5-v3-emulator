# 扫雷 —— F4HWN Labs 版的叠加应用

我们自己的应用 ✓：9×9 扫雷 ✓，跑在 `App/apps/app_overlay.h` 预留的 **4 KiB 叠加区**里 ✓。
它是按上游的 `App/apps/app_api.h` 写的 ✓、用上游的 `app.ld` 链接 ✓ —— **这两个上游文件没有随本仓库分发** ✗
（它们来自 [armel/uv-k1-k5v3-firmware-custom](https://github.com/armel/uv-k1-k5v3-firmware-custom)，Apache-2.0 ✓），
所以把这个目录放进 `App/apps/minesweeper/` 与它们并列 ✓，或把 `-I` 指向一份副本 ✓。

## 为什么它长这样

| 约束 | 后果 |
| --- | --- |
| 这台电台**没有左右键** ✓（只有 UP、DOWN、MENU、EXIT、STAR、F、0-9 ✓） | 光标用 UP/DOWN 走 ✓，数字**先选行再选列** ✓：`3` `5` = 第 3 行第 5 列 ✓ |
| **4 KiB**（text+rodata+data+bss 合计 ✓） | 不用查表 ✓、不用浮点 ✓、不链 libc ✓；邻雷数现算 ✓，每格一个 bit ✓ |
| 81 格放不进 16 位掩码 ✓ | 三个 9 字节位数组 ✓，按 `cell >> 3`、`cell & 7` 寻址 ✓ —— 早先的 `uint16_t` 版本**能编过但在第 15 格之后是错的** ✗ |
| 常驻的像素函数**不做边界检查** ✓ | `put()`/`invert()` 自己裁剪 ✓ |
| freestanding 里没有 `rand()` ✓ | 一个小 LCG ✓；雷在**第一次翻开之后**才布 ✓，并保证首翻周围 3×3 无雷 ✓ |

按键：UP/DOWN 移动 ✓、1-9 先选行再选列 ✓、MENU 翻开 ✓、F 插旗 ✓、STAR 重开 ✓、EXIT 退出 ✓。
角上的 `M` 是剩余雷数 ✓，`A1` 是光标位置 ✓。

## 构建

    arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -Os -std=gnu11 -ffreestanding \
        -nostdlib -nostartfiles -T app.ld -Wl,--defsym,APP_VMA=0x20000280 \
        -o minesweeper.elf minesweeper_app.c
    arm-none-eabi-objcopy -O binary minesweeper.elf minesweeper.bin
    pack_app.py minesweeper.bin Minesweeper.app --name Minesweeper --ver 1.0 \
        --vma 0x20000280 --api-min 1      # pack_app.py 在 App/apps/ 下

`build.sh` 做的就是这些 ✓，需要 PATH 上有 Arm GNU Toolchain ✓。

然后**从网页安装** ✓：*Overlay apps* → 选一个槽 → 选 `Minesweeper.app` → **Install** →
**Ask the radio** 应当回 `Minesweeper` ✓。在电台上按 **F** 再按 **7**、再按 **MENU** 运行 ✓。

## 验证到什么程度（`host_test.c` 可复现）

`host_test.c` 把应用源码直接包进来 ✓、给它一个假的 `app_api_t` ✓，在 PC 上**真的跑它的状态机** ✓
（记录它每一次画出的字符串、统计点亮的像素 ✓）：

    gcc -std=gnu11 -O1 -Wall -Wextra -Werror -I.. -o host_test host_test.c && ./host_test

实测输出 ✓：脚本一（翻开/插旗/重开/数字选格/退出）正常返回 ✓、标题与雷数都画了 ✓、有像素点亮 ✓；
脚本二（盲翻 85 格）到达终局 11 次 ✓、`BOOM` 已绘制 ✓、终局后按 MENU 回到新局 ✓。

**已经验证** ✓：源码在 `gcc -Wall -Wextra -Werror` 下干净通过 ✓（这一步抓出了 API 的真实成员名 ✓
`api->fb` ✓、`print_tiny(s, x, y, statusbar, fill)` **五个**参数 ✓，以及 `APP_KEY_LEFT`/`APP_KEY_RIGHT`
**根本不存在** ✓）；游戏逻辑在 PC 上跑通 ✓。

**没有验证** ✗：从未为 ARM 编译过 ✓、也没在电台上跑过 ✓ —— 写它的机器上既没有 `arm-none-eabi-gcc`
也没有 Docker ✓。**第一次构建与第一次上机就是真正的评审** ✓。
