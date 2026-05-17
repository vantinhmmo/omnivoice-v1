@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem OmniVoice one-command setup + run for Windows
rem Usage:
rem   scripts\setup_and_run.cmd
rem Optional env before running:
rem   set BACKEND_PORT=8000
rem   set FRONTEND_PORT=5173
rem   set OMNIVOICE_DEVICE=cpu
rem   set ENABLE_CLOUDFLARE_TUNNEL=1
rem   set DRY_RUN=1

cd /d "%~dp0\.."
if errorlevel 1 (
  echo [ERROR] Cannot switch to project root.
  exit /b 1
)

if not defined BACKEND_HOST set "BACKEND_HOST=127.0.0.1"
if not defined BACKEND_PORT set "BACKEND_PORT=8000"
if not defined FRONTEND_HOST set "FRONTEND_HOST=127.0.0.1"
if not defined FRONTEND_PORT set "FRONTEND_PORT=5173"
if not defined OMNIVOICE_DEVICE set "OMNIVOICE_DEVICE=cpu"
if not defined OMNIVOICE_DTYPE set "OMNIVOICE_DTYPE=auto"
if not defined OMNIVOICE_MAX_CONCURRENT_LONG_JOBS set "OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=2"
if not defined HF_HUB_ENABLE_HF_TRANSFER set "HF_HUB_ENABLE_HF_TRANSFER=1"
if not defined ENABLE_CLOUDFLARE_TUNNEL set "ENABLE_CLOUDFLARE_TUNNEL=0"
if not defined DRY_RUN set "DRY_RUN=0"

echo [INFO] ROOT_DIR=%CD%
echo [INFO] OMNIVOICE_DEVICE=%OMNIVOICE_DEVICE%
echo [INFO] OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE%
echo [INFO] OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=%OMNIVOICE_MAX_CONCURRENT_LONG_JOBS%

if not exist "requirements.txt" (
  echo [ERROR] requirements.txt not found. Please keep this script under scripts\ and run it from this project.
  exit /b 1
)

if not exist "frontend\package.json" (
  echo [ERROR] frontend\package.json not found.
  exit /b 1
)

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 --version >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  echo [ERROR] Python was not found.
  echo [HINT] Install Python 3.10+ from https://www.python.org/downloads/windows/ and tick "Add Python to PATH".
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js was not found.
  echo [HINT] Install Node.js 18+ or 20+ LTS from https://nodejs.org/ then run this script again.
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found.
  echo [HINT] Reinstall Node.js 18+ or 20+ LTS from https://nodejs.org/.
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [WARN] ffmpeg was not found in PATH. Audio features may fail.
  echo [HINT] Install ffmpeg and add it to PATH: https://ffmpeg.org/download.html
)

for /f "delims=" %%V in ('%PYTHON_CMD% --version 2^>^&1') do set "PYTHON_VERSION=%%V"
for /f "delims=" %%V in ('node --version 2^>^&1') do set "NODE_VERSION=%%V"
for /f "delims=" %%V in ('npm --version 2^>^&1') do set "NPM_VERSION=%%V"

echo [INFO] Python: %PYTHON_VERSION%
echo [INFO] Node: %NODE_VERSION% ^| npm: %NPM_VERSION%

if "%DRY_RUN%"=="1" (
  echo [OK] Dry run passed. Required tools and project files were found.
  exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
  echo [STEP] Creating Python virtual environment .venv ...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create Python virtual environment.
    exit /b 1
  )
)

set "VENV_PYTHON=.venv\Scripts\python.exe"

echo [STEP] Installing backend dependencies...
"%VENV_PYTHON%" -m pip install -U pip setuptools wheel
if errorlevel 1 exit /b 1
"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo [STEP] Installing frontend dependencies...
pushd frontend
if errorlevel 1 exit /b 1
if exist package-lock.json (
  npm ci
) else (
  npm install
)
if errorlevel 1 (
  popd
  exit /b 1
)
popd

if "%ENABLE_CLOUDFLARE_TUNNEL%"=="1" call :ensure_cloudflared

echo [STEP] Starting backend on %BACKEND_HOST%:%BACKEND_PORT% ...
start "OmniVoice API" /D "%CD%" cmd /k "set HF_HUB_ENABLE_HF_TRANSFER=%HF_HUB_ENABLE_HF_TRANSFER% && set OMNIVOICE_DEVICE=%OMNIVOICE_DEVICE% && set OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE% && set OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=%OMNIVOICE_MAX_CONCURRENT_LONG_JOBS% && .venv\Scripts\python.exe -m uvicorn server.api:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload"

echo [STEP] Starting frontend on %FRONTEND_HOST%:%FRONTEND_PORT% ...
start "OmniVoice Frontend" /D "%CD%\frontend" cmd /k "npm run dev -- --host %FRONTEND_HOST% --port %FRONTEND_PORT%"

if "%ENABLE_CLOUDFLARE_TUNNEL%"=="1" (
  echo [STEP] Starting Cloudflare quick tunnel for frontend...
  start "OmniVoice Cloudflare Tunnel" /D "%CD%" cmd /k "cloudflared tunnel --url http://%FRONTEND_HOST%:%FRONTEND_PORT%"
)

echo.
echo [OK] Setup complete. Services are starting in separate terminal windows.
echo [OK] Local frontend: http://127.0.0.1:%FRONTEND_PORT%
echo [OK] Local health API: http://127.0.0.1:%BACKEND_PORT%/api/health
if not "%ENABLE_CLOUDFLARE_TUNNEL%"=="1" echo [INFO] To also open a public Cloudflare URL, run: set ENABLE_CLOUDFLARE_TUNNEL=1 ^&^& scripts\setup_and_run.cmd
exit /b 0

:ensure_cloudflared
where cloudflared >nul 2>nul
if not errorlevel 1 exit /b 0

echo [INFO] cloudflared was not found. Downloading cloudflared for Windows amd64...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; New-Item -ItemType Directory -Force -Path '.tools' | Out-Null; Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '.tools\cloudflared.exe'"
if errorlevel 1 (
  echo [WARN] Could not download cloudflared. Cloudflare tunnel will be skipped.
  set "ENABLE_CLOUDFLARE_TUNNEL=0"
  exit /b 0
)
set "PATH=%CD%\.tools;%PATH%"
exit /b 0
