@echo off
python "%~dp0stage-sandbox-runner" %*
exit /b %errorlevel%
