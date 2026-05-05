@echo off
title LOTUS Agent
echo.
echo  ██╗      ██████╗ ████████╗██╗   ██╗███████╗
echo  ██║     ██╔═══██╗╚══██╔══╝██║   ██║██╔════╝
echo  ██║     ██║   ██║   ██║   ██║   ██║███████╗
echo  ██║     ██║   ██║   ██║   ██║   ██║╚════██║
echo  ███████╗╚██████╔╝   ██║   ╚██████╔╝███████║
echo  ╚══════╝ ╚═════╝    ╚═╝    ╚═════╝ ╚══════╝
echo.
echo  Voice AI Agent — by Lotus Algebra
echo  =========================================
echo.

:: Check API key
if "%ANTHROPIC_API_KEY%"=="" (
    set /p ANTHROPIC_API_KEY="Enter Anthropic API Key: "
)

:: Launch
python agent.py

pause
