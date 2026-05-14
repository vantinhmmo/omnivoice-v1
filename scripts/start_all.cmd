@echo off
setlocal
cd /d %~dp0\..
start "OmniVoice API" cmd /k scripts\start_backend.cmd
start "OmniVoice Frontend" cmd /k scripts\start_frontend.cmd
