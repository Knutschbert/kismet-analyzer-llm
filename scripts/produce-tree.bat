@echo off
setlocal EnableExtensions

if "%~1"=="" (
    echo Usage: %~nx0 input-directory [output-directory]
    exit /b 2
)

for %%S in ("%~1") do set "SOURCE=%%~fS"
if not exist "%SOURCE%\" (
    echo Input directory not found: "%SOURCE%"
    exit /b 3
)

if "%~2"=="" (
    set "OUTPUT=%SOURCE%-kismet-output"
) else (
    for %%O in ("%~2") do set "OUTPUT=%%~fO"
)
if not exist "%OUTPUT%\" mkdir "%OUTPUT%"

set "FAILED=0"
set "COUNT=0"
set "ASSET_SCRIPT=%~dp0produce-asset.bat"

echo Input:  "%SOURCE%"
echo Output: "%OUTPUT%"
echo.

for /r "%SOURCE%" %%F in (*.uasset *.umap) do call :process "%%~fF"
goto :complete

:process
set /a COUNT+=1
set "FULL=%~f1"
for %%R in ("%FULL%") do set "FULL_DIR=%%~dpR"
call set "REL_DIR=%%FULL_DIR:%SOURCE%=%%"
if "%REL_DIR:~0,1%"=="\" set "REL_DIR=%REL_DIR:~1%"
set "DEST=%OUTPUT%\%REL_DIR%"
if "%DEST:~-1%"=="\" set "DEST=%DEST:~0,-1%"
if not exist "%DEST%" mkdir "%DEST%"

call "%ASSET_SCRIPT%" "%FULL%" "%DEST%"
if errorlevel 1 (
    set /a FAILED+=1
    echo FAILED: "%FULL%"
)
echo.
goto :eof

:complete
echo Processed %COUNT% asset(s); %FAILED% failure(s).
if not "%FAILED%"=="0" exit /b 10
exit /b 0
