@echo off
python "%~dp0preflight" %*
exit /b %errorlevel%
