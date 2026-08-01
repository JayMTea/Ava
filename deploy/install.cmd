@echo off
REM Ava installer, for the shell Windows actually opens.
REM
REM   cd Ava\deploy && install.cmd
REM
REM install.sh is a bash script, and neither Command Prompt nor PowerShell can
REM run one: cmd fails on `./install.sh` with "'.' is not recognized", and
REM PowerShell cannot execute it at all. The documented answer was "use Git
REM Bash", which is correct and kept losing people -- including this project's
REM own maintainer, four times in a row, while knowing the rule. A doc line that
REM cannot hold its author is not going to hold a stranger.
REM
REM So: find the bash that ships with Git for Windows and hand the SAME script to
REM it. No second installer, no Windows-specific logic, nothing to keep in step.
REM Env knobs still work -- `set AVA_PROFILE=gpu` before running this is passed
REM through like any other environment variable.
setlocal
set "RC=1"

set "BASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined BASH if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not defined BASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "BASH=%LocalAppData%\Programs\Git\bin\bash.exe"

REM Git installed somewhere else entirely: derive bash from wherever git.exe is.
REM A per-user install puts it under %LocalAppData%, a portable one anywhere at
REM all, and `where` is the only thing that knows.
if not defined BASH for /f "delims=" %%G in ('where git 2^>nul') do call :from_git "%%~dpG"

if not defined BASH (
  echo.
  echo   Could not find Git for Windows, which is what provides the shell this
  echo   installer runs in ^(and the `git` used to clone this repo^).
  echo.
  echo   Install it from https://git-scm.com/download/win and run this again.
  echo.
  goto :finish
)

REM -l because install.sh needs the MSYS tools -- uname, sed, tr, seq -- and only
REM a LOGIN shell puts Git's usr\bin on PATH. Inheriting Command Prompt's PATH
REM finds git.exe and docker.exe but none of those, and the script dies partway
REM through on a missing `uname` rather than at the start on a missing shell.
REM
REM Backslashes to forward slashes so bash reads the path as one argument, and
REM single quotes so "Program Files" and a spaced username survive.
set "HERE=%~dp0"
set "HERE=%HERE:\=/%"
"%BASH%" -lc "cd '%HERE%' && ./install.sh"
set "RC=%ERRORLEVEL%"

:finish
REM Double-clicked from Explorer? Then this window closes the instant we exit,
REM taking the claim link -- the one thing the owner needs -- with it. When run
REM from a console there is nothing to hold, so only pause in the former case:
REM cmdcmdline carries this script's name only when Explorer launched it.
echo %cmdcmdline% | find /i "%~nx0" >nul && pause
exit /b %RC%

:from_git
if defined BASH goto :eof
if exist "%~1..\bin\bash.exe" set "BASH=%~1..\bin\bash.exe"
goto :eof
