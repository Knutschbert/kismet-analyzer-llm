@echo off
setlocal

if "%~1"=="" (
    echo Usage: %~nx0 asset.uasset [output-directory]
    echo    or: %~nx0 asset-directory [output-directory]
    exit /b 2
)

set "INPUT=%~1"
if not defined KISMET_UE_VERSION set "KISMET_UE_VERSION=VER_UE4_25"
if not defined KISMET_ANALYZER_EXE set "KISMET_ANALYZER_EXE=%~dp0..\bin\kismet-analyzer.exe"

if not exist "%KISMET_ANALYZER_EXE%" (
    echo Analyzer not found: "%KISMET_ANALYZER_EXE%"
    echo Replace the contents of ..\bin or set KISMET_ANALYZER_EXE.
    exit /b 3
)

if exist "%INPUT%\" goto tree
if not exist "%INPUT%" (
    echo Asset or directory not found: "%INPUT%"
    exit /b 4
)

if "%~2"=="" (
    set "OUTPUT=%~dp1%~n1-llm-output"
) else (
    set "OUTPUT=%~2"
)
if not exist "%OUTPUT%" mkdir "%OUTPUT%"

echo Generating semantic LLM JSON for "%INPUT%"
echo Output: "%OUTPUT%\%~n1.llm.json"
"%KISMET_ANALYZER_EXE%" --ue-version %KISMET_UE_VERSION% to-llm-json "%INPUT%" > "%OUTPUT%\%~n1.llm.json"
if errorlevel 1 (
    echo Semantic export failed.
    exit /b 10
)
echo Done.
exit /b 0

:tree
for %%D in ("%INPUT%") do set "SOURCE=%%~fD"
if "%~2"=="" (
    set "OUTPUT=%SOURCE%-llm-output"
) else (
    set "OUTPUT=%~2"
)
if not exist "%OUTPUT%" mkdir "%OUTPUT%"

echo Generating semantic LLM JSON tree for "%SOURCE%"
echo Output: "%OUTPUT%"
"%KISMET_ANALYZER_EXE%" --ue-version %KISMET_UE_VERSION% gen-llm-tree "%SOURCE%" "%OUTPUT%"
if errorlevel 1 (
    echo Semantic tree export failed.
    exit /b 11
)
echo Done. See "%OUTPUT%\manifest.json".
exit /b 0
