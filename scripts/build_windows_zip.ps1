param(
    [string]$Version = '1.0.0',
    [switch]$Resume
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$stage = Join-Path $project "dist\MeetingAssistant-$Version-win-x64"
$zip = "$stage.zip"

if ((Test-Path -LiteralPath $zip) -or ((Test-Path -LiteralPath $stage) -and -not $Resume)) {
    throw "Build output already exists: $stage (or ZIP). Move it aside before rebuilding."
}
if ($Resume -and -not (Test-Path -LiteralPath $stage)) { throw 'There is no build folder to resume.' }

$electron = Join-Path $project 'node_modules\electron\dist'
$venv = Join-Path $project '.venv'
$venvConfig = Join-Path $venv 'pyvenv.cfg'
if (-not (Test-Path -LiteralPath (Join-Path $electron 'electron.exe'))) {
    throw 'Electron is missing. Run install.bat first.'
}
if (-not (Test-Path -LiteralPath $venvConfig)) {
    throw 'Python environment is missing. Run install.bat first.'
}
$homeLine = Get-Content -LiteralPath $venvConfig | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
if (-not $homeLine) { throw 'Could not locate the base Python runtime in pyvenv.cfg.' }
$pythonHome = ($homeLine -split '=', 2)[1].Trim()
if (-not (Test-Path -LiteralPath (Join-Path $pythonHome 'python.exe'))) {
    throw "Base Python runtime is missing: $pythonHome"
}

function Copy-Tree([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    & robocopy $Source $Destination /E /NFL /NDL /NJH /NJS /NP /XD '__pycache__' 'test' 'tests' /XF '*.pyc' '*.pyo' | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "Copy failed: $Source ($LASTEXITCODE)" }
}

$app = Join-Path $stage 'resources\app'
$runtime = Join-Path $app 'runtime'
$python = Join-Path $runtime 'python'
if (-not $Resume) {
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Copy-Tree $electron $stage
    Move-Item -LiteralPath (Join-Path $stage 'electron.exe') -Destination (Join-Path $stage 'MeetingAssistant.exe')
    New-Item -ItemType Directory -Path $app -Force | Out-Null
    foreach ($file in @('main.js', 'preload.js', 'package.json', 'LICENSE', 'README.md', 'CHANGELOG.md')) {
        Copy-Item -LiteralPath (Join-Path $project $file) -Destination $app
    }
    $guide = Get-ChildItem -LiteralPath $project -File -Filter '*指南.html' | Select-Object -First 1
    if ($guide) { Copy-Item -LiteralPath $guide.FullName -Destination $app }
    Copy-Tree (Join-Path $project 'renderer') (Join-Path $app 'renderer')
    Copy-Tree (Join-Path $project 'docs') (Join-Path $app 'docs')
    New-Item -ItemType Directory -Path (Join-Path $app 'backend') -Force | Out-Null
    Copy-Item -Path (Join-Path $project 'backend\*.py') -Destination (Join-Path $app 'backend')
    New-Item -ItemType Directory -Path $python -Force | Out-Null
    foreach ($file in @('python.exe', 'python3.dll', 'python311.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt')) {
        $source = Join-Path $pythonHome $file
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $python }
    }
    Copy-Tree (Join-Path $pythonHome 'DLLs') (Join-Path $python 'DLLs')
    Copy-Tree (Join-Path $pythonHome 'Lib') (Join-Path $python 'Lib')
    Copy-Tree (Join-Path $venv 'Lib\site-packages') (Join-Path $runtime 'site-packages')
}

$env:PYTHONHOME = $python
$env:PYTHONPATH = Join-Path $runtime 'site-packages'
$env:PYTHONNOUSERSITE = '1'
$smokeData = Join-Path $project 'dist\smoke-data'
$env:MEETING_ASSISTANT_DATA_DIR = $smokeData
try {
    & (Join-Path $python 'python.exe') -c 'import fastapi, pyaudio, faster_whisper, ctranslate2, sentencepiece; from backend.main import app; print(12345)'
    if ($LASTEXITCODE -ne 0) { throw 'Bundled Python backend smoke test failed.' }
} finally {
    Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:\PYTHONNOUSERSITE -ErrorAction SilentlyContinue
    Remove-Item Env:\MEETING_ASSISTANT_DATA_DIR -ErrorAction SilentlyContinue
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
Get-FileHash -Algorithm SHA256 -LiteralPath $zip | Format-List Algorithm,Hash,Path
Write-Host "Windows ZIP ready: $zip"
