param([switch]$Force)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$dockerfile = Join-Path $repo "Dockerfile"
$compose = Join-Path $repo "docker-compose.yml"
$configManager = Join-Path $repo "bot/core/config_manager.py"
function Done($m) { Write-Host "[deploy-test] $m" -ForegroundColor Green }
function Info($m) { Write-Host "[deploy-test] $m" -ForegroundColor Cyan }
Info "Applying local deploy-test mode in $repo"
$dockerTest = @(
'FROM mysterysd/wzmlx:v3',
'',
'WORKDIR /usr/src/app',
'',
'# TEMP LOCAL TEST ONLY: base image can have dpkg metadata but miss runtime binaries.',
'# Revert before final source unless user explicitly wants to keep this runtime fix.',
'RUN apt-get update \',
'    && apt-get install -y --reinstall --no-install-recommends \',
'        aria2 \',
'        qbittorrent-nox \',
'        ffmpeg \',
'        rclone \',
'        sabnzbdplus \',
'        p7zip-full \',
'        coreutils \',
'    && rm -rf /var/lib/apt/lists/*',
'',
'COPY requirements.txt .',
'RUN uv pip install --python /wzvenv/bin/python --no-cache-dir -r requirements.txt',
'',
'COPY . .',
'',
'ENTRYPOINT ["bash", "start.sh"]',
''
) -join "`n"
$dockerText = Get-Content $dockerfile -Raw
if ($dockerText -notmatch 'TEMP LOCAL TEST ONLY') {
    Set-Content -Path $dockerfile -Value $dockerTest -Encoding utf8 -NoNewline
    Done 'Patched Dockerfile runtime packages'
} else { Done 'Dockerfile already in deploy-test mode' }
$composeText = Get-Content $compose -Raw
if ($composeText -match '(?ms)(  app:.*?\n    restart:)\s+unless-stopped') {
    $composeText = $composeText -replace '(?ms)(  app:.*?\n    restart:)\s+unless-stopped', '$1 "no"'
    Set-Content -Path $compose -Value $composeText -Encoding utf8 -NoNewline
    Done 'Patched docker-compose app restart policy to stop on error'
} elseif ($composeText -match '(?ms)  app:.*?\n    restart:\s+"no"') { Done 'docker-compose app already in deploy-test mode' }
else { Write-Host '[deploy-test] WARNING: restart policy not found' -ForegroundColor Yellow }
$configText = Get-Content $configManager -Raw
if ($configText -notmatch 'except ModuleNotFoundError') {
    $fallback = @(
'from os import getenv',
'',
'try:',
'    from wz_bin import bin_name',
'except ModuleNotFoundError:',
'    def bin_name(index):',
'        return (',
'            "/usr/bin/aria2c",',
'            "/usr/bin/qbittorrent-nox",',
'            "/usr/bin/ffmpeg",',
'            "/usr/bin/rclone",',
'            "/usr/bin/sabnzbdplus",',
'        )[index]'
) -join "`n"
    $configText = $configText -replace 'from os import getenv\r?\n\r?\nfrom wz_bin import bin_name', $fallback
    Set-Content -Path $configManager -Value $configText -Encoding utf8 -NoNewline
    Done 'Patched config_manager.py wz_bin fallback'
} else { Done 'config_manager.py already has deploy-test fallback' }
Write-Host ''
Done 'Deploy-test mode applied.'
Write-Host 'To revert: use IDE/Git Discard Changes for Dockerfile, docker-compose.yml, and bot/core/config_manager.py.' -ForegroundColor Yellow
