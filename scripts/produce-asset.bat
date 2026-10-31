@echo off
setlocal

if "%~1"=="" (
    echo Usage: %~nx0 asset.uasset [output-directory]
    exit /b 2
)

set "ASSET=%~1"
if not defined KISMET_UE_VERSION set "KISMET_UE_VERSION=VER_UE4_25"

if not defined KISMET_ANALYZER_EXE set "KISMET_ANALYZER_EXE=%~dp0..\bin\kismet-analyzer.exe"
if not exist "%KISMET_ANALYZER_EXE%" (
    echo Analyzer not found: "%KISMET_ANALYZER_EXE%"
    echo Set KISMET_ANALYZER_EXE or build the sibling kismet-analyzer checkout.
    exit /b 3
)
if not exist "%ASSET%" (
    echo Asset not found: "%ASSET%"
    exit /b 4
)

if "%~2"=="" (
    set "OUTPUT=%~dp1%~n1-outputs"
) else (
    set "OUTPUT=%~2"
)
if not exist "%OUTPUT%" mkdir "%OUTPUT%"

echo Processing "%ASSET%"
echo Output: "%OUTPUT%"

"%KISMET_ANALYZER_EXE%" --ue-version %KISMET_UE_VERSION% to-json "%ASSET%" > "%OUTPUT%\%~n1.json"
if errorlevel 1 (
    echo JSON generation failed for "%ASSET%"
    exit /b 10
)

"%KISMET_ANALYZER_EXE%" --ue-version %KISMET_UE_VERSION% gen-cfg "%ASSET%" "%OUTPUT%"
if errorlevel 1 (
    echo CFG generation failed for "%ASSET%"
    exit /b 11
)

echo Generated:
echo   "%OUTPUT%\%~n1.json"
echo   "%OUTPUT%\%~n1.txt"
echo   "%OUTPUT%\%~n1.dot"
exit /b 0
