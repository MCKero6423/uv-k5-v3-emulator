# Serve the web remote control.
#
#   powershell -File work\run-webui.ps1              # the page starts the emulator
#   powershell -File work\run-webui.ps1 -Attach      # attach to run-emulator.ps1
#
# Nothing here is machine-specific. The QEMU binary comes from QEMU or PATH, the
# interpreter is the one on PATH, the firmware is whatever the checkout has (or one
# uploaded from the page), the flash image is the last one a session used, and the
# screen-buffer addresses are read out of the running firmware rather than passed in.
#
# Owning the process is what puts the firmware's serial output in the page's log
# pane: the model prints it to stderr as "SERIAL ..." and the server reads QEMU's
# stderr. With -Attach the server never sees that stream, so the pane stays empty.
# Owning it also makes the On/Off buttons real.
param(
    [int]$Port      = 8080,
    [int]$QmpPort   = 4444,
    [string]$Frame  = '',       # only to override the discovered gFrameBuffer
    [string]$Status = '',       # only to override the discovered gStatusLine
    [string]$Kernel = '',       # empty: whatever the checkout has, or upload one
    [string]$Flash  = '',       # empty: the last image a session used
    [string]$Qemu   = '',       # empty: QEMU or PATH
    [switch]$Attach
)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

# The QEMU this server spawns is a native Windows build, so the MSYS2 mingw64 DLLs
# have to be on the child's PATH -- inherited from here, since the child gets this
# environment. Without it QEMU dies at load and "power on" reports only that the QMP
# port never appeared.
if (Test-Path 'F:\msys64\mingw64\bin') {
    $env:PATH = 'F:\msys64\mingw64\bin;' + $env:PATH
}

$py = if ($env:PYTHON) { $env:PYTHON } else {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) { $found = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $found) { throw 'no python on PATH; set PYTHON to one with flask installed' }
    $found.Source
}

$common = @('tools\webui.py', '--qmp', "127.0.0.1:$QmpPort", '--port', $Port)
if ($Frame)  { $common += @('--frame-addr',  $Frame) }
if ($Status) { $common += @('--status-addr', $Status) }

if (-not $Kernel -and $env:ELF) { $Kernel = $env:ELF }
if (-not $Flash) {
    $candidates = @($env:UVK5_FLASH_IMAGE, 'work\user-flash.img', 'assets\flash.img')
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { $Flash = $candidate; break }
    }
}
if (-not $Qemu -and $env:QEMU) { $Qemu = $env:QEMU }

if ($Attach) {
    Write-Host "attaching to an emulator already listening on 127.0.0.1:$QmpPort"
    & $py @common --attach
} else {
    if ($Qemu) { $common += @('--qemu', $Qemu) }
    if ($Kernel) { $common += @('--elf', $Kernel) }
    if ($Flash) { $common += @('--flash', $Flash) }
    Write-Host "the page will start: $(if ($Qemu) { $Qemu } else { 'qemu-system-arm from PATH' })"
    if ($Flash) { Write-Host "flash image: $Flash" }
    & $py @common
}
