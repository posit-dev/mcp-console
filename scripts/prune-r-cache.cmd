@echo off
python "%~dp0prune-r-cache" %*
exit /b %errorlevel%
