# Creates the virtual env on first run, installs dependencies, and starts the bot.
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing dependencies failed." }
& .\.venv\Scripts\python.exe bot.py
