# Boot one firmware image in the emulator, paused, with a key held from reset.
#
#   powershell -File work/boot-radio.ps1 -Image <path> -Qmp <port> -Gdb <port> [-Flash <img>]
#
# The quoting matters: passing a Windows path with spaces or commas straight through
# cmd /c from a caller that is itself quoted mangles it, which is what "unsupported
# machine type" and friends look like from outside. A script keeps one layer of it.
param(
    [Parameter(Mandatory=$true)][string]$Image,
    [int]$Qmp = 4470,
    [int]$Gdb = 1260,
    [string]$Flash = "$PSScriptRoot\ms-scratch.img"
)
$env:PATH = 'F:\msys64\mingw64\bin;' + $env:PATH
$env:UVK5_FLASH_IMAGE = $Flash
$qemu = 'F:\dsh-build\qemu-7.2.0\build\qemu-system-arm.exe'
& $qemu -M uv-k5-v3 -S -nographic -monitor none -serial null `
    -qmp "tcp:127.0.0.1:$Qmp,server=on,wait=off" `
    -kernel $Image -gdb "tcp::$Gdb" 2> "$PSScriptRoot\boot-radio-err.log"
