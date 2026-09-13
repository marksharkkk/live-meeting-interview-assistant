$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirements = Join-Path $projectRoot "requirements-local-translation.txt"
$installer = Join-Path $PSScriptRoot "install_opus_mt_models.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment not found: $python. Run install.bat first."
}

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    throw "uv was not found. Install uv or activate the project's normal setup first."
}

Write-Host "Installing the optional OPUS-MT runtime into the project virtual environment..."
& $uvCommand.Source pip install --python $python -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "OPUS-MT runtime installation failed."
}

Write-Host "Downloading and converting the OPUS-MT en->zh model..."
& $python $installer
if ($LASTEXITCODE -ne 0) {
    throw "OPUS-MT model installation failed."
}

Write-Host "Done. Restart the app and leave the translation toggle enabled."
