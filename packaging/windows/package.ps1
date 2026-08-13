# Package RetComM Studio Windows portable zip + Inno Setup installer.
#
# Expects cmake --install prefix with bin/RetComM-Studio.exe (+ SDL3.dll) and
# share/retcomm-studio/{toolkit,fonts,assets}.
param(
    [Parameter(Mandatory = $true)][string]$Prefix,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$VcpkgBin = "",
    [string]$Arch = "x64",
    [string]$InnoSetup = ""
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$OutDir = Join-Path $Root "dist"
$Stage = Join-Path $OutDir "windows-stage"
$PortableZipName = "RetComM-Studio-portable-windows.zip"
$FriendlyExe = "RetComM Studio.exe"
$MainExe = "RetComM-Studio.exe"

Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$PrefixBin = Join-Path $Prefix "bin"
$srcExe = Join-Path $PrefixBin $MainExe
if (-not (Test-Path $srcExe)) {
    throw "Missing $srcExe (cmake --install first)"
}
Copy-Item $srcExe (Join-Path $Stage $MainExe) -Force
Copy-Item $srcExe (Join-Path $Stage $FriendlyExe) -Force

Get-ChildItem -Path $PrefixBin -Filter "*.dll" -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item $_.FullName $Stage -Force
}

function Copy-Dlls([string]$Dir) {
    if (-not $Dir -or -not (Test-Path $Dir)) { return }
    foreach ($pat in @("SDL3.dll", "freetype.dll", "zlib1.dll", "brotlicommon.dll", "brotlidec.dll")) {
        Get-ChildItem -Path $Dir -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item $_.FullName $Stage -Force
        }
    }
}
@(
    $VcpkgBin,
    (Join-Path $Root "build\Release"),
    (Join-Path $Root "vcpkg_installed\x64-windows\bin"),
    (Join-Path $Root "retcomm-vcpkg\installed\x64-windows\bin"),
    (Join-Path $env:RETCOMM_VCPKG "installed\x64-windows\bin")
) | ForEach-Object { Copy-Dlls $_ }

if (-not (Test-Path (Join-Path $Stage "SDL3.dll"))) {
    throw "SDL3.dll missing from stage — pass -VcpkgBin or ensure cmake copies runtime DLLs"
}

# Toolkit + assets beside the exe (runtime resolves toolkit/ next to binary).
$share = Join-Path $Prefix "share\retcomm-studio"
if (-not (Test-Path (Join-Path $share "toolkit\project_studio"))) {
    throw "Missing toolkit under $share"
}
Copy-Item (Join-Path $share "toolkit") (Join-Path $Stage "toolkit") -Recurse -Force
if (Test-Path (Join-Path $share "fonts")) {
    Copy-Item (Join-Path $share "fonts") (Join-Path $Stage "fonts") -Recurse -Force
}
if (Test-Path (Join-Path $share "assets")) {
    Copy-Item (Join-Path $share "assets") (Join-Path $Stage "assets") -Recurse -Force
} elseif (Test-Path (Join-Path $Root "assets\retcomm-studio.ico")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Stage "assets") | Out-Null
    Copy-Item (Join-Path $Root "assets\retcomm-studio.ico") (Join-Path $Stage "assets\") -Force
    if (Test-Path (Join-Path $Root "assets\retcomm-studio.png")) {
        Copy-Item (Join-Path $Root "assets\retcomm-studio.png") (Join-Path $Stage "assets\") -Force
    }
}
Copy-Item (Join-Path $Root "VERSION") (Join-Path $Stage "VERSION") -Force

$channelPortable = @{
    app = "retcomm-studio"
    version = $Version
    channel = "portable"
    portable_exe = $FriendlyExe
} | ConvertTo-Json
Set-Content -Path (Join-Path $Stage "channel.json") -Value $channelPortable -Encoding UTF8

$zipPath = Join-Path $OutDir $PortableZipName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Wrote $zipPath"

$channelInstaller = @{
    app = "retcomm-studio"
    version = $Version
    channel = "installer"
} | ConvertTo-Json
Set-Content -Path (Join-Path $Stage "channel.json") -Value $channelInstaller -Encoding UTF8

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
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Wrote installer under $OutDir"
