@echo off
chcp 65001 >nul
REM Двойной клик по этому файлу собирает WB_PVZ_Printer.exe — портативный,
REM единый exe со встроенным Python, Tesseract OCR и установщиком WebView2.
REM Реальная логика в build.ps1, этот .bat только обходит стандартную
REM политику Windows, запрещающую запуск .ps1 двойным кликом, и гарантирует,
REM что окно не закроется само, даже если PowerShell упадёт с ошибкой сразу
REM на старте (например, из-за старой версии PowerShell) — тогда вы сможете
REM прочитать и скопировать сообщение об ошибке ниже.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
set PS_EXIT_CODE=%ERRORLEVEL%

echo.
echo ------------------------------------------------
if "%PS_EXIT_CODE%"=="0" (
    echo Сборка завершена. Код выхода PowerShell: %PS_EXIT_CODE%
) else (
    echo PowerShell завершился с кодом %PS_EXIT_CODE% ^(не 0 = была ошибка^).
    echo Подробный журнал: %~dp0build_log.txt
)
echo ------------------------------------------------
echo.
pause
