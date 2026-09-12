@echo off
REM Double-click launcher for ProjectZen - a thin wrapper around the real logic in
REM Start-ProjectZen.ps1 (PowerShell handles the JSON parsing, health-check polling
REM and error reporting far more reliably than batch).
REM
REM This file is what the Desktop and Start Menu shortcuts point at.
title ProjectZen
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-ProjectZen.ps1"
if errorlevel 1 (
  echo.
  echo ProjectZen exited with an error. The message above explains why.
  pause
)
