@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem OmniVoice Windows launcher: backend + frontend using CUDA
rem Usage:
rem   scripts\start_cuda.cmd
rem Optional env before running:
rem   set BACKEND_PORT=8000
rem   set FRONTEND_PORT=5173
rem   set OMNIVOICE_DTYPE=auto
rem   set CHECK_CUDA=0
rem   set DRY_RUN=1
rem   set BACKEND_WAIT_TIMEOUT=180
rem   set OMNIVOICE_CHUNK_WORKERS=2
rem   set ENABLE_CLOUDFLARE_TUNNEL=1
rem   set CLOUDFLARE_TUNNEL_TIMEOUT=30

cd /d "%~dp0\.."
if errorlevel 1 (
  echo [ERROR] Cannot switch to project root.
  exit /b 1
)

if not defined BACKEND_HOST set "BACKEND_HOST=127.0.0.1"
if not defined BACKEND_PORT set "BACKEND_PORT=8000"
if not defined FRONTEND_HOST set "FRONTEND_HOST=127.0.0.1"
if not defined FRONTEND_PORT set "FRONTEND_PORT=5173"
if not defined OMNIVOICE_DTYPE set "OMNIVOICE_DTYPE=auto"
if not defined OMNIVOICE_MAX_CONCURRENT_LONG_JOBS set "OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=3"
if not defined OMNIVOICE_CHUNK_WORKERS set "OMNIVOICE_CHUNK_WORKERS=1"
if not defined HF_HUB_ENABLE_HF_TRANSFER set "HF_HUB_ENABLE_HF_TRANSFER=1"
if not defined CHECK_CUDA set "CHECK_CUDA=1"
if not defined DRY_RUN set "DRY_RUN=0"
if not defined BACKEND_WAIT_TIMEOUT set "BACKEND_WAIT_TIMEOUT=180"
if not defined ENABLE_CLOUDFLARE_TUNNEL set "ENABLE_CLOUDFLARE_TUNNEL=0"
if not defined CLOUDFLARE_TUNNEL_TIMEOUT set "CLOUDFLARE_TUNNEL_TIMEOUT=30"

rem Force CUDA for backend/model loading.
set "OMNIVOICE_DEVICE=cuda"

echo [INFO] ROOT_DIR=%CD%
echo [INFO] OMNIVOICE_DEVICE=%OMNIVOICE_DEVICE%
echo [INFO] OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE%
echo [INFO] OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=%OMNIVOICE_MAX_CONCURRENT_LONG_JOBS%
echo [INFO] OMNIVOICE_CHUNK_WORKERS=%OMNIVOICE_CHUNK_WORKERS%
echo [INFO] BACKEND_WAIT_TIMEOUT=%BACKEND_WAIT_TIMEOUT%s
echo [INFO] ENABLE_CLOUDFLARE_TUNNEL=%ENABLE_CLOUDFLARE_TUNNEL%

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv was not found.
  echo [HINT] Run scripts\setup_and_run.cmd once first to create venv and install dependencies.
  exit /b 1
)

if not exist "frontend\package.json" (
  echo [ERROR] frontend\package.json was not found.
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js was not found in PATH.
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found in PATH.
  exit /b 1
)

where nvidia-smi >nul 2>nul
if errorlevel 1 (
  echo [WARN] nvidia-smi was not found in PATH. CUDA may not be installed correctly.
) else (
  echo [INFO] NVIDIA GPU detected:
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
)

if "%CHECK_CUDA%"=="1" (
  echo [STEP] Checking PyTorch CUDA availability...
  .venv\Scripts\python.exe -c "import torch, sys; print('[INFO] torch:', torch.__version__); print('[INFO] cuda_available:', torch.cuda.is_available()); print('[INFO] cuda_device_count:', torch.cuda.device_count()); sys.exit(0 if torch.cuda.is_available() else 2)"
  if errorlevel 2 (
    echo [ERROR] PyTorch cannot use CUDA on this machine/env.
    echo [HINT] Check NVIDIA driver, CUDA-compatible PyTorch install, or run with CPU using scripts\start_all.cmd.
    echo [HINT] To bypass this pre-check anyway: set CHECK_CUDA=0 ^&^& scripts\start_cuda.cmd
    exit /b 1
  )
  if errorlevel 1 exit /b 1
)

if "%DRY_RUN%"=="1" (
  echo [OK] Dry run passed. CUDA launcher is ready.
  exit /b 0
)

echo [STEP] Starting backend with CUDA on %BACKEND_HOST%:%BACKEND_PORT% ...
start "OmniVoice API CUDA" /D "%CD%" cmd /k "set HF_HUB_ENABLE_HF_TRANSFER=%HF_HUB_ENABLE_HF_TRANSFER% && set OMNIVOICE_DEVICE=cuda && set OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE% && set OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=%OMNIVOICE_MAX_CONCURRENT_LONG_JOBS% && set OMNIVOICE_CHUNK_WORKERS=%OMNIVOICE_CHUNK_WORKERS% && .venv\Scripts\python.exe -m uvicorn server.api:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload"

echo [STEP] Waiting for backend health before starting frontend...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $timeout=[int]$env:BACKEND_WAIT_TIMEOUT; $url='http://' + $env:BACKEND_HOST + ':' + $env:BACKEND_PORT + '/api/health'; $deadline=(Get-Date).AddSeconds($timeout); Write-Host ('[INFO] Health URL: ' + $url); while((Get-Date) -lt $deadline){ try { $r=Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2; if($r.StatusCode -ge 200 -and $r.StatusCode -lt 300){ Write-Host '[OK] Backend is ready.'; exit 0 } } catch { Write-Host '[WAIT] Backend is not ready yet...'; Start-Sleep -Seconds 2 } }; Write-Host ('[ERROR] Backend did not become ready within ' + $timeout + ' seconds.'); exit 1"
if errorlevel 1 (
  echo [ERROR] Frontend will not start because backend health check failed.
  echo [HINT] Check the "OmniVoice API CUDA" window for backend error logs.
  exit /b 1
)

echo [STEP] Starting frontend on %FRONTEND_HOST%:%FRONTEND_PORT% ...
start "OmniVoice Frontend" /D "%CD%\frontend" cmd /k "npm run dev -- --host %FRONTEND_HOST% --port %FRONTEND_PORT%"

if "%ENABLE_CLOUDFLARE_TUNNEL%"=="1" call :start_cloudflare_tunnels

echo.
echo [OK] Started backend with CUDA and frontend in separate terminal windows.
echo [OK] Frontend: http://127.0.0.1:%FRONTEND_PORT%
echo [OK] API health: http://127.0.0.1:%BACKEND_PORT%/api/health
echo [INFO] Backend env: OMNIVOICE_DEVICE=cuda, OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE%, OMNIVOICE_CHUNK_WORKERS=%OMNIVOICE_CHUNK_WORKERS%
if not "%ENABLE_CLOUDFLARE_TUNNEL%"=="1" echo [INFO] Public URL: set ENABLE_CLOUDFLARE_TUNNEL=1 ^&^& scripts\start_cuda.cmd
exit /b 0

:start_cloudflare_tunnels
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo [INFO] cloudflared was not found. Downloading cloudflared for Windows amd64...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; New-Item -ItemType Directory -Force -Path '.tools' | Out-Null; Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '.tools\cloudflared.exe'"
  if errorlevel 1 (
    echo [WARN] Could not download cloudflared. Skip public URL.
    exit /b 0
  )
  set "PATH=%CD%\.tools;%PATH%"
)

echo [STEP] Starting Cloudflare tunnel for API...
if exist "tmp_cf_api.log" del /f /q "tmp_cf_api.log" >nul 2>nul
if exist "tmp_cf_frontend.log" del /f /q "tmp_cf_frontend.log" >nul 2>nul
start "OmniVoice CF API" /D "%CD%" cmd /k "cloudflared tunnel --url http://%BACKEND_HOST%:%BACKEND_PORT% 1> tmp_cf_api.log 2>&1"

 echo [STEP] Starting Cloudflare tunnel for Frontend...
start "OmniVoice CF Frontend" /D "%CD%" cmd /k "cloudflared tunnel --url http://%FRONTEND_HOST%:%FRONTEND_PORT% 1> tmp_cf_frontend.log 2>&1"

echo [INFO] Cloudflare windows opened: OmniVoice CF API / OmniVoice CF Frontend
echo [INFO] Parsing public URLs from tunnel logs...
set "CF_URL="
for /f "delims=" %%U in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$log='%CD%\\tmp_cf_api.log'; $pattern='https://[-a-zA-Z0-9]+\.trycloudflare\.com'; $deadline=(Get-Date).AddSeconds([int]$env:CLOUDFLARE_TUNNEL_TIMEOUT); while((Get-Date) -lt $deadline){ if(Test-Path $log){ $m=Select-String -Path $log -Pattern $pattern | Select-Object -First 1; if($m){ $u=[regex]::Match($m.Line,$pattern).Value; if($u){ Write-Output $u; exit 0 } } } Start-Sleep -Milliseconds 500 }; exit 1"') do set "CF_URL=%%U"
if defined CF_URL (
  echo [OK] Public API URL: %CF_URL%
) else (
  echo [WARN] API public URL not parsed yet. Check window: OmniVoice CF API
)

set "CF_URL="
for /f "delims=" %%U in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$log='%CD%\\tmp_cf_frontend.log'; $pattern='https://[-a-zA-Z0-9]+\.trycloudflare\.com'; $deadline=(Get-Date).AddSeconds([int]$env:CLOUDFLARE_TUNNEL_TIMEOUT); while((Get-Date) -lt $deadline){ if(Test-Path $log){ $m=Select-String -Path $log -Pattern $pattern | Select-Object -First 1; if($m){ $u=[regex]::Match($m.Line,$pattern).Value; if($u){ Write-Output $u; exit 0 } } } Start-Sleep -Milliseconds 500 }; exit 1"') do set "CF_URL=%%U"
if defined CF_URL (
  echo [OK] Public Frontend URL: %CF_URL%
) else (
  echo [WARN] Frontend public URL not parsed yet. Check window: OmniVoice CF Frontend
)
exit /b 0
