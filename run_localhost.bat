@echo off
setlocal

cd /d "%~dp0"

set "PORT=5000"
set "ADMIN_PATH=adminpanel0109tpa2026"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python not found.
        echo Install Python or create the virtual environment first.
        pause
        exit /b 1
    )
)

if not exist "uploads" mkdir "uploads"
if not exist "uploads\student_photos" mkdir "uploads\student_photos"
if not exist "uploads\faculty_photos" mkdir "uploads\faculty_photos"
if not exist "uploads\results" mkdir "uploads\results"
if not exist "generated_forms" mkdir "generated_forms"

echo Starting The Professors Academy on localhost...
echo Public Site: http://127.0.0.1:%PORT%/
echo Admin Panel: http://127.0.0.1:%PORT%/%ADMIN_PATH%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$public='http://127.0.0.1:%PORT%/';" ^
    "$admin='http://127.0.0.1:%PORT%/%ADMIN_PATH%';" ^
    "try {" ^
    "  $response = Invoke-WebRequest -UseBasicParsing $public -TimeoutSec 2;" ^
    "  if($response.StatusCode -ge 200){" ^
    "    Start-Process $public;" ^
    "    Start-Process $admin;" ^
    "    exit 0;" ^
    "  }" ^
    "} catch {}" ^
    "exit 1"

if not errorlevel 1 (
    echo Localhost server is already running.
    endlocal
    exit /b 0
)

start "TPA Localhost Server" cmd /k ""%PYTHON_EXE%" app.py"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$public='http://127.0.0.1:%PORT%/';" ^
    "$admin='http://127.0.0.1:%PORT%/%ADMIN_PATH%';" ^
    "for($i=0;$i -lt 30;$i++){" ^
    "  try {" ^
    "    $response = Invoke-WebRequest -UseBasicParsing $public -TimeoutSec 2;" ^
    "    if($response.StatusCode -ge 200){" ^
    "      Start-Process $public;" ^
    "      Start-Process $admin;" ^
    "      exit 0;" ^
    "    }" ^
    "  } catch {}" ^
    "  Start-Sleep -Milliseconds 750" ^
    "}"

echo If the browser did not open automatically, use these links:
echo http://127.0.0.1:%PORT%/
echo http://127.0.0.1:%PORT%/%ADMIN_PATH%

endlocal
