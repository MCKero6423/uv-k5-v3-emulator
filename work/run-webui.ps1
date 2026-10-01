# Serve the web remote control.
#
#   powershell -File work\run-webui.ps1              # the page starts the emulator
#   powershell -File work\run-webui.ps1 -Attach      # attach to run-emulator.ps1
#
# Owning the process is what puts the firmware's serial output in the page's log
# pane: the model prints it to stderr as "SERIAL ..." and the server reads QEMU's
# stderr. With -Attach the server never sees that stream, so the pane stays empty.
# Owning it also makes the On/Off buttons real.
param(
    [int]$Port      = 8080,
    [int]$QmpPort   = 4444,
    [string]$Frame  = '0x200012BE',   # gFrameBuffer, proven against the firmware source
    [string]$Status = '0x2000163E',   # gStatusLine
    [string]$Kernel = "$PSScriptRoot\f4hwn\EGZUMER+F4HWN-v5.9.0.CN.elf",
    [string]$Flash  = [System.IO.Path]::GetFullPath("$PSScriptRoot\..\assets\flash.img"),
    [string]$Qemu   = 'F:\dsh-build\qemu-7.2.0\build\qemu-system-arm.exe',
    [switch]$Attach
)
$ErrorActionPreference = 'Stop'
# The QEMU this server spawns is a native Windows build, so the MSYS2 mingw64 DLLs
# have to be on the child's PATH -- inherited from here, since the child gets this
# environment. Without it QEMU dies at load and "power on" reports only that the QMP
# port never appeared.
$env:PATH = 'F:\msys64\mingw64\bin;' + $env:PATH
$py = 'C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
Set-Location (Join-Path $PSScriptRoot '..')

$common = @('tools\webui.py', '--qmp', "127.0.0.1:$QmpPort",
            '--frame-addr', $Frame, '--status-addr', $Status, '--port', $Port)
if ($Attach) {
    Write-Host "attaching to an emulator already listening on 127.0.0.1:$QmpPort"
    & $py @common --attach
} else {
    Write-Host "the page will start: $Qemu"
    & $py @common --qemu $Qemu --elf $Kernel --flash $Flash
}
