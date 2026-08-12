# Package RetComM Studio Windows portable zip + Inno Setup installer.
#
# Expects PyInstaller onedir at dist/RetComM-Studio/
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$DistName = "RetComM-Studio",
    [string]$Arch = "x64",
    [string]$InnoSetup = ""
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$OutDir = Join-Path $Root "dist"
$Bundle = Join-Path $OutDir $DistName
$Stage = Join-Path $OutDir "windows-stage"
$PortableZipName = "RetComM-Studio-portable-windows.zip"
$FriendlyExe = "RetComM Studio.exe"

if (-not (Test-Path $Bundle)) {
    throw "Missing PyInstaller bundle: $Bundle (run packaging/build_pyinstaller.py first)"
}

Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Copy-Item -Path (Join-Path $Bundle "*") -Destination $Stage -Recurse -Force

$mainExe = Join-Path $Stage "$DistName.exe"
if (-not (Test-Path $mainExe)) {
    throw "Missing $mainExe in staged bundle"
}
# Friendly portable name alongside the canonical exe (installer uses RetComM-Studio.exe).
Copy-Item $mainExe (Join-Path $Stage $FriendlyExe) -Force

# Ensure icon is present for Inno SetupIconFile.
$ico = Join-Path $Stage "assets\retcomm-studio.ico"
if (-not (Test-Path $ico)) {
    $srcIco = Join-Path $Root "assets\retcomm-studio.ico"
    if (Test-Path $srcIco) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Stage "assets") | Out-Null
        Copy-Item $srcIco $ico -Force
    } else {
        throw "Missing retcomm-studio.ico (run packaging/make-icons.sh)"
    }
}

# Portable zip
$zipPath = Join-Path $OutDir $PortableZipName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Wrote $zipPath"

# Inno Setup
if (-not $InnoSetup) {
    $InnoSetup = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $InnoSetup)) {
    throw "ISCC.exe not found at $InnoSetup"
}
$iss = Join-Path $Root "packaging\windows\setup.iss"
& $InnoSetup `
    "/DMyAppVersion=$Version" `
    "/DStageDir=$Stage" `
    "/DOutputDir=$OutDir" `
    "/DArch=$Arch" `
    $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed: $LASTEXITCODE" }

$setup = Join-Path $OutDir "RetComM-Studio-windows-$Arch-setup.exe"
if (-not (Test-Path $setup)) { throw "Missing installer: $setup" }
Write-Host "Wrote $setup"
Get-ChildItem $OutDir | Format-Table Name, Length
