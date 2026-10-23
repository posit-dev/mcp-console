@echo off
python "%~dp0check-core" %*
exit /b %errorlevel%
