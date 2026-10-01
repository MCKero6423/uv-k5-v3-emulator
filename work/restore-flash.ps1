# Put the SPI flash image back to its known-good state.
#
# The firmware writes settings back into assets/flash.img (the PY25Q16 model
# flushes on exit), so a session can leave it changed. Stop the emulator first:
# a running QEMU holds the old image in memory and overwrites the file on exit.
$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot '..'
Copy-Item "$PSScriptRoot\flash-base.img" (Join-Path $root 'assets\flash.img') -Force
Write-Host "restored assets/flash.img from work/flash-base.img"
