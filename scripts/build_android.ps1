param([ValidateSet('debug', 'release')][string]$Configuration = 'debug')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$androidRoot = Join-Path $projectRoot 'android'
if (-not (Test-Path (Join-Path $projectRoot 'client/index.html'))) {
    throw 'Missing client/index.html. Add the shared frontend before building.'
}
if (-not $env:JAVA_HOME) { Write-Host 'JAVA_HOME is unset; Gradle will use java from PATH.' }
Push-Location $androidRoot
try {
    $task = if ($Configuration -eq 'release') { 'assembleRelease' } else { 'assembleDebug' }
    & .\gradlew.bat --no-daemon $task
    if ($LASTEXITCODE -ne 0) { throw "Gradle failed ($LASTEXITCODE)" }
    $apk = Join-Path $androidRoot "app/build/outputs/apk/$Configuration/app-$Configuration.apk"
    if (-not (Test-Path $apk)) { throw "APK missing: $apk" }
    Write-Host "APK: $apk"
    Write-Host 'Signing: Android SDK debug key (also for release); suitable for direct installation, not a production signing identity.'
} finally { Pop-Location }
