@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Builds PCOptimizer.exe in an isolated .venv, so it works on any machine
rem with Python 3.10+ and does not depend on a particular conda install.
rem Set SKIP_SMOKE=1 to skip the window check (used on headless CI).

set /p VERSION=<VERSION
set "NAME=PCOptimizer-v%VERSION%-win64"

rem Python lookup order: PCOPT_PYTHON override, py launcher, PATH, then common
rem per-user installs (python.org, miniconda, anaconda). Must be 3.10 - 3.13.
set "PYTHON="
for %%C in ("%PCOPT_PYTHON%" "py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%USERPROFILE%\miniconda3\python.exe" "%USERPROFILE%\anaconda3\python.exe" "%ProgramData%\miniconda3\python.exe" "%ProgramData%\anaconda3\python.exe") do (
  if not defined PYTHON if not "%%~C"=="" (
    %%~C -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,14) else 1)" >nul 2>nul && set "PYTHON=%%~C"
  )
)
if not defined PYTHON (
  echo Python 3.10 - 3.13 was not found.
  echo Install it from https://www.python.org/downloads/ , or set PCOPT_PYTHON to a python.exe and run this again.
  exit /b 1
)
echo Using Python: %PYTHON%

echo [1/4] Preparing .venv and dependencies...
if not exist ".venv\Scripts\python.exe" (
  %PYTHON% -m venv .venv
  if errorlevel 1 exit /b 1
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -m pip install -q --disable-pip-version-check -r requirements-dev.txt
if errorlevel 1 exit /b 1

echo [2/4] Running tests...
"%VPY%" -m pytest tests -q
if errorlevel 1 exit /b 1

echo [3/4] Building PCOptimizer.exe ...
"%VPY%" -m PyInstaller --noconfirm --clean PCOptimizer.spec
if errorlevel 1 exit /b 1

if "%SKIP_SMOKE%"=="1" (
  echo Smoke test skipped.
) else (
  echo Smoke test: the exe must open its window...
  powershell -NoProfile -ExecutionPolicy Bypass -File "tools\smoke_exe.ps1" -Exe "dist\PCOptimizer.exe"
  if errorlevel 1 (
    echo Smoke test failed. The exe did not open the PC Optimizer window.
    exit /b 1
  )
)

echo [4/4] Assembling release folder...
if exist "release" rmdir /s /q "release"
mkdir "release\%NAME%"
copy /y "dist\PCOptimizer.exe" "release\%NAME%\PCOptimizer.exe" >nul
copy /y "使用说明.txt" "release\%NAME%\使用说明.txt" >nul
copy /y "README.md" "release\%NAME%\README.md" >nul
copy /y "LICENSE" "release\%NAME%\LICENSE" >nul

set "ZIP=%CD%\release\%NAME%.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'release\%NAME%\*' -DestinationPath $env:ZIP -Force"
if errorlevel 1 exit /b 1
powershell -NoProfile -Command "$h=(Get-FileHash $env:ZIP -Algorithm SHA256).Hash.ToLower(); \"$([IO.Path]::GetFileName($env:ZIP))`nSHA256: $h\" | Set-Content 'release\CHECKSUMS.txt' -Encoding UTF8"

echo.
echo Build finished.
echo   Zip:      %ZIP%
echo   Exe:      %CD%\release\%NAME%\PCOptimizer.exe
echo   Checksum: %CD%\release\CHECKSUMS.txt
endlocal
