# Start the emulated radio with the f4hwn 5.9.0.CN firmware (Windows build).
#
#   powershell -File work\run-emulator.ps1
#
# QEMU here is a native Windows build made with MSYS2, so it needs the MSYS2
# mingw64 DLLs on PATH. QMP is TCP: a Windows QEMU has no unix sockets, which is
# the one thing about this project that really was Linux-only.
param(
    [string]$Kernel = "$PSScriptRoot\f4hwn\EGZUMER+F4HWN-v5.9.0.CN.elf",
    [string]$Flash  = "$PSScriptRoot\..\assets\flash.img",
    [string]$Serial = "$PSScriptRoot\serial.log",
    [int]$QmpPort   = 4444,
    [int]$GdbPort   = 1234
)
$ErrorActionPreference = 'Stop'
$env:PATH = 'F:\msys64\mingw64\bin;' + $env:PATH
$qemu = 'F:\dsh-build\qemu-7.2.0\build\qemu-system-arm.exe'

Write-Host "kernel : $Kernel"
Write-Host "flash  : $Flash"
Write-Host "QMP    : tcp:127.0.0.1:$QmpPort    GDB: tcp::$GdbPort    serial: $Serial"
& $qemu -M "uv-k5-v3,flash-image=$Flash" -kernel $Kernel `
    -display none -monitor none `
    -serial "file:$Serial" `
    -qmp "tcp:127.0.0.1:$QmpPort,server=on,wait=off" `
    -gdb "tcp::$GdbPort"
