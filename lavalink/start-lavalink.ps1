# Downloads Lavalink (first run only), loads ..\.env, and starts the audio server.
$ErrorActionPreference = "Stop"
$LavalinkVersion = "4.2.2"
Set-Location $PSScriptRoot

$jar = Join-Path $PSScriptRoot "Lavalink.jar"
if (-not (Test-Path $jar)) {
    Write-Host "Downloading Lavalink $LavalinkVersion..."
    Invoke-WebRequest "https://github.com/lavalink-devs/Lavalink/releases/download/$LavalinkVersion/Lavalink.jar" -OutFile $jar -UseBasicParsing
}

$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim('"'), "Process")
        }
    }
} else {
    Write-Warning "No .env found. Copy .env.example to .env and fill it in."
}

# Raise the audio engine to above-normal CPU priority once it starts, so a busy PC (games, browsers)
# can't starve it of CPU and cause stutters.
$null = Start-Job -ArgumentList $jar -ScriptBlock {
    param($jarPath)
    for ($i = 0; $i -lt 60; $i++) {
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'java.exe'" | Where-Object { $_.CommandLine -like "*$jarPath*" }
        if ($procs) {
            foreach ($p in $procs) { (Get-Process -Id $p.ProcessId).PriorityClass = "AboveNormal" }
            return
        }
        Start-Sleep -Seconds 1
    }
}

# Generational ZGC keeps GC pauses under a millisecond, so audio frames are never delayed by garbage collection.
java -Xms512M -Xmx1G -XX:+UseZGC -XX:+ZGenerational -jar $jar
