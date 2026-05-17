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
if not defined HF_HUB_ENABLE_HF_TRANSFER set "HF_HUB_ENABLE_HF_TRANSFER=1"
if not defined CHECK_CUDA set "CHECK_CUDA=1"
if not defined DRY_RUN set "DRY_RUN=0"

rem Force CUDA for backend/model loading.
set "OMNIVOICE_DEVICE=cuda"

echo [INFO] ROOT_DIR=%CD%
echo [INFO] OMNIVOICE_DEVICE=%OMNIVOICE_DEVICE%
echo [INFO] OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE%
echo [INFO] OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=%OMNIVOICE_MAX_CONCURRENT_LONG_JOBS%

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
start "OmniVoice API CUDA" /D "%CD%" cmd /k "set HF_HUB_ENABLE_HF_TRANSFER=%HF_HUB_ENABLE_HF_TRANSFER% && set OMNIVOICE_DEVICE=cuda && set OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE% && set OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=%OMNIVOICE_MAX_CONCURRENT_LONG_JOBS% && .venv\Scripts\python.exe -m uvicorn server.api:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload"

echo [STEP] Starting frontend on %FRONTEND_HOST%:%FRONTEND_PORT% ...
start "OmniVoice Frontend" /D "%CD%\frontend" cmd /k "npm run dev -- --host %FRONTEND_HOST% --port %FRONTEND_PORT%"

echo.
echo [OK] Started backend with CUDA and frontend in separate terminal windows.
echo [OK] Frontend: http://127.0.0.1:%FRONTEND_PORT%
echo [OK] API health: http://127.0.0.1:%BACKEND_PORT%/api/health
echo [INFO] Backend env: OMNIVOICE_DEVICE=cuda, OMNIVOICE_DTYPE=%OMNIVOICE_DTYPE%
exit /b 0
