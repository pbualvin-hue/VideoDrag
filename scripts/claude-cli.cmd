@echo off
rem Launch the Claude Code CLI: standalone install first (stable path),
rem then the desktop app's bundled copy (transient cache) as fallback.
if exist "%USERPROFILE%\.local\bin\claude.exe" (
  "%USERPROFILE%\.local\bin\claude.exe" %*
  exit /b %errorlevel%
)
for /f "delims=" %%d in ('dir /b /ad /o-n "%APPDATA%\Claude\claude-code" 2^>nul') do (
  "%APPDATA%\Claude\claude-code\%%d\claude.exe" %*
  exit /b %errorlevel%
)
echo claude CLI not found - run: irm https://claude.ai/install.ps1 ^| iex
exit /b 1
