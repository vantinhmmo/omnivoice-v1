@echo off
setlocal
cd /d %~dp0\..
set HF_HUB_ENABLE_HF_TRANSFER=1
if not defined OMNIVOICE_DEVICE set OMNIVOICE_DEVICE=cpu
if not defined OMNIVOICE_DTYPE set OMNIVOICE_DTYPE=auto
if not defined OMNIVOICE_MAX_CONCURRENT_LONG_JOBS set OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=2
.venv\Scripts\python.exe -m uvicorn server.api:app --host 127.0.0.1 --port 8000 --reload
