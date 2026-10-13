@echo off
rem ============================================================
rem  Make a long video: brief -> final film.
rem
rem  Two ways to use it:
rem    1) Double-click it and answer the four questions (normal use).
rem    2) Call it with arguments (for repeated / unattended runs):
rem         this-file.cmd <project-name> <brief-file> [minutes] [plan^|yes] [style]
rem       "plan" = only print the plan; "yes" = skip the confirmation.
rem       "style" = a profile name from config\styles.yaml (default: guofeng-cg).
rem
rem  Keep this file ASCII-only and CRLF: cmd.exe parses .cmd files with the
rem  system OEM code page (GBK here), so non-ASCII text gets mangled.
rem  Chinese instructions live in the manual file under docs\ (see the docs folder).
rem ============================================================
chcp 65001 >nul
pushd "%~dp0"
setlocal

echo ============================================================
echo   AI Video Auto - brief to final film
echo ============================================================
echo.
echo   Before you start: is ComfyUI running? (If not, start it first.)
echo.

if not "%~1"=="" goto fromargs

set "NAME="
set /p NAME=  [1/3] Project name (English/pinyin, e.g. my-film-01): 
if "%NAME%"=="" goto cancel

echo.
echo   [2/3] Brief file. Available files in data\briefs\ :
dir /b "data\briefs\*.txt" 2>nul
echo         Type a file name, or a full path:
set "BRIEF="
set /p BRIEF=       file: 
if "%BRIEF%"=="" goto cancel

echo.
set "MINUTES="
set /p MINUTES=  [3/3] Target length in minutes (Enter = 2): 
if "%MINUTES%"=="" set MINUTES=2
set "MODE="
set "STYLE="
goto prepare

:fromargs
set "NAME=%~1"
set "BRIEF=%~2"
set "MINUTES=%~3"
if "%MINUTES%"=="" set MINUTES=2
set "MODE=%~4"
set "STYLE=%~5"

:prepare
if not exist "%BRIEF%" if exist "data\briefs\%BRIEF%" set "BRIEF=data\briefs\%BRIEF%"
if not exist "%BRIEF%" goto nobrief

if not "%~1"=="" goto havestyle
echo.
echo   [4/4] Style profile. Available profiles:
echo         guofeng-cg          = 3D Chinese anime (default)
echo         photoreal-cinematic = live-action realistic film look
set /p STYLE=       profile (Enter = guofeng-cg): 

:havestyle
set "STYLEARG="
if not "%STYLE%"=="" set "STYLEARG=--style %STYLE%"

echo.
echo   --- plan (nothing is executed yet) ---
.venv\Scripts\python.exe scripts\make_video.py --name %NAME% --brief-file "%BRIEF%" --minutes %MINUTES% %STYLEARG% --plan
if /i "%MODE%"=="plan" goto end

if /i "%MODE%"=="yes" goto run
echo.
set "GO="
set /p GO=  Start now? (Enter = yes, n = cancel): 
if /i "%GO%"=="n" goto cancel

:run
.venv\Scripts\python.exe scripts\make_video.py --name %NAME% --brief-file "%BRIEF%" --minutes %MINUTES% %STYLEARG%
echo.
echo   Finished. If an error appeared above: fix it and run this file again -
echo   steps that are already done will be skipped automatically.
goto end

:nobrief
echo.
echo   Brief file not found: %BRIEF%
echo   Put your story into data\briefs\ as a .txt file, then run this file again.
goto end

:cancel
echo.
echo   Cancelled.

:end
echo.
pause
endlocal
popd
