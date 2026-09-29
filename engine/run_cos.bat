@echo off
REM Chief of Staff - Task Scheduler entry point.
REM   run_cos.bat            ingest only (the 3-hourly job)
REM   run_cos.bat --brief    full pipeline + email (the morning job)
REM NOTE: this file MUST keep CRLF line endings. With bare LF, cmd mis-parses
REM the if/else block, runs nothing, and still reports success (0x0).
REM NOTE: do NOT redirect to logs\cos.log here. cmd takes that handle
REM exclusively for the whole run, so a second task launching while the
REM first still holds it silently skips its command and exits 0 - that is
REM how the 26/27 Aug briefs vanished. run.py appends to the log itself.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist logs mkdir logs
if "%~1"=="" goto :ingest
".venv\Scripts\python.exe" run.py %*
goto :done
:ingest
".venv\Scripts\python.exe" run.py --ingest
:done
exit /b %ERRORLEVEL%
