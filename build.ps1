# build.ps1
# Полностью автоматическая сборка WB_PVZ_Printer.exe — единственный exe-файл,
# который включает в себя Python, все библиотеки, портативный Tesseract OCR
# и установщик WebView2.
# Запускайте не напрямую, а через build.bat (двойной клик) — он обходит
# ограничение PowerShell на запуск неподписанных скриптов и гарантированно
# не даёт окну закрыться, даже если этот скрипт упадёт с ошибкой.

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$LogPath = Join-Path $PSScriptRoot 'build_log.txt'
try { Start-Transcript -Path $LogPath -Force | Out-Null } catch { }

# Настройка кодировки консоли под кириллицу — может упасть, если окно
# запущено нестандартным образом (нет привязанного хендла консоли), поэтому
# оборачиваем в try/catch и просто игнорируем сбой, а не роняем весь скрипт.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Write-Warn($text) {
    Write-Host "[!] $text" -ForegroundColor Yellow
}

function Write-Err($text) {
    Write-Host "[ОШИБКА] $text" -ForegroundColor Red
}

function Stop-WithMessage($text) {
    Write-Err $text
    Write-Host ""
    Write-Host "Полный журнал сборки сохранён в файл:" -ForegroundColor Yellow
    Write-Host "  $LogPath" -ForegroundColor Yellow
    Write-Host "Если проблема непонятна — пришлите содержимое этого файла для диагностики."
    throw $text
}

$exitCode = 0

try {
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  Сборка WB PVZ Printer в портативный exe" -ForegroundColor Green
    Write-Host "============================================"

    # -----------------------------------------------------------------------
    # 1. Python
    # -----------------------------------------------------------------------
    Write-Step "Проверяю Python"

    $pythonCmd = $null
    foreach ($candidate in @('python', 'py')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $pythonCmd = $candidate
            break
        }
    }
    if (-not $pythonCmd) {
        Stop-WithMessage "Python не найден. Установите Python 3.11+ с https://www.python.org/downloads/ и ОБЯЗАТЕЛЬНО поставьте галку `"Add python.exe to PATH`" при установке, затем запустите build.bat снова."
    }
    $pyVersion = & $pythonCmd --version 2>&1
    Write-Host "Найден: $pyVersion (командой '$pythonCmd')"

    # -----------------------------------------------------------------------
    # 2. Виртуальное окружение и зависимости
    # -----------------------------------------------------------------------
    Write-Step "Готовлю виртуальное окружение build_venv"

    $venvDir = Join-Path $PSScriptRoot 'build_venv'
    $venvPy = Join-Path $venvDir 'Scripts\python.exe'

    if (-not (Test-Path $venvPy)) {
        & $pythonCmd -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Не удалось создать виртуальное окружение (код $LASTEXITCODE)." }
    }

    Write-Step "Устанавливаю зависимости (requirements.txt + pyinstaller)"
    & $venvPy -m pip install --upgrade pip 2>&1 | Out-Null
    & $venvPy -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Не удалось установить зависимости из requirements.txt (код $LASTEXITCODE) — см. вывод pip выше." }
    & $venvPy -m pip install pyinstaller==6.10.0
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Не удалось установить pyinstaller (код $LASTEXITCODE)." }

    # pywin32 иногда требует пост-установочный шаг для регистрации DLL —
    # необязательный шаг, сбой здесь не должен останавливать сборку.
    $pywin32Post = Join-Path $venvDir 'Scripts\pywin32_postinstall.py'
    if (Test-Path $pywin32Post) {
        try { & $venvPy $pywin32Post -install 2>&1 | Out-Null } catch { }
    }

    # -----------------------------------------------------------------------
    # 3. Портативный Tesseract OCR (assets/tesseract)
    # -----------------------------------------------------------------------
    Write-Step "Проверяю портативный Tesseract (assets\tesseract)"

    $bundledTesseract = Join-Path $PSScriptRoot 'assets\tesseract\tesseract.exe'

    if (Test-Path $bundledTesseract) {
        Write-Host "Уже подготовлен: $bundledTesseract — пропускаю."
    }
    else {
        Write-Host "Не найден, готовлю портативную копию..."

        # Ищем уже установленный Tesseract: сначала в стандартных папках, потом в PATH.
        # ProgramFiles(x86) может отсутствовать на некоторых системах — проверяем
        # перед использованием, иначе Join-Path падает с ошибкой null-аргумента.
        $candidates = New-Object System.Collections.Generic.List[string]
        $candidates.Add((Join-Path $env:ProgramFiles 'Tesseract-OCR\tesseract.exe'))
        $pf86 = ${env:ProgramFiles(x86)}
        if ($pf86) { $candidates.Add((Join-Path $pf86 'Tesseract-OCR\tesseract.exe')) }
        if ($env:LOCALAPPDATA) { $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Tesseract-OCR\tesseract.exe')) }

        $found = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

        if (-not $found) {
            $cmd = Get-Command tesseract.exe -ErrorAction SilentlyContinue
            if ($cmd) { $found = $cmd.Source }
        }

        if (-not $found) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                Write-Host "Устанавливаю Tesseract OCR через winget (может появиться окно подтверждения)..."
                try {
                    winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements --silent
                } catch { }
                $global:LASTEXITCODE = 0
                Start-Sleep -Seconds 2
                $found = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
            }
            else {
                Write-Warn "winget не найден в системе."
            }
        }

        if (-not $found) {
            # Последний резерв: вдруг Tesseract стоит не в стандартной папке —
            # ищем tesseract.exe в пределах Program Files и AppData\Local\Programs
            # (глубина ограничена, чтобы не сканировать весь диск долго).
            Write-Host "Ищу tesseract.exe в Program Files / AppData\Local\Programs (может занять до минуты)..."
            $searchRoots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA 'Programs')) |
                Where-Object { $_ -and (Test-Path $_) }
            foreach ($root in $searchRoots) {
                $hit = Get-ChildItem -Path $root -Filter 'tesseract.exe' -Recurse -Depth 3 -ErrorAction SilentlyContinue -File | Select-Object -First 1
                if ($hit) { $found = $hit.FullName; break }
            }
        }

        if ($found) {
            $sourceDir = Split-Path $found -Parent
            Write-Host "Копирую Tesseract из '$sourceDir' в assets\tesseract ..."
            $destDir = Join-Path $PSScriptRoot 'assets\tesseract'
            New-Item -ItemType Directory -Force -Path $destDir | Out-Null
            # /E — с подпапками (включая tessdata), /XD script — не тащим редко нужные скрипт-модели.
            # robocopy возвращает НЕнулевые коды и при успехе (1,2,3...) — это нормально,
            # поэтому явно сбрасываем LASTEXITCODE, чтобы это не приняли за сбой.
            robocopy $sourceDir $destDir /E /XD script /NFL /NDL /NJH /NJS | Out-Null
            $global:LASTEXITCODE = 0

            # Оставляем только необходимые языковые модели (программе нужны только
            # цифры) — экономит место, если при установке были выбраны доп. языки.
            $tessdataDir = Join-Path $destDir 'tessdata'
            if (Test-Path $tessdataDir) {
                Get-ChildItem $tessdataDir -Filter '*.traineddata' |
                    Where-Object { $_.Name -notin @('eng.traineddata', 'osd.traineddata') } |
                    Remove-Item -Force -ErrorAction SilentlyContinue
            }

            if (Test-Path (Join-Path $destDir 'tesseract.exe')) {
                Write-Host "Готово: портативный Tesseract подготовлен в assets\tesseract."
            }
            else {
                Write-Warn "Копирование прошло, но tesseract.exe не найден в assets\tesseract — проверьте вручную."
            }
        }
        else {
            Write-Warn "Не удалось найти и автоматически подготовить Tesseract."
            Write-Host ""
            Write-Host "Сделайте один раз вручную:"
            Write-Host "  1. Установите Tesseract: https://github.com/UB-Mannheim/tesseract/wiki"
            Write-Host "  2. Скопируйте папку установки (обычно C:\Program Files\Tesseract-OCR)"
            Write-Host "     в проект как assets\tesseract — должен получиться файл"
            Write-Host "     assets\tesseract\tesseract.exe"
            Write-Host "  3. Запустите build.bat ещё раз."
            Write-Host ""
            Write-Warn "Сборка ПРОДОЛЖИТСЯ без Tesseract — распознавание номера работать не будет,"
            Write-Warn "пока Tesseract не появится в assets\tesseract или не будет установлен отдельно на целевом ПК."
        }
    }

    # -----------------------------------------------------------------------
    # 4. Microsoft Edge WebView2 Runtime — встроенный установщик (Bootstrapper)
    # -----------------------------------------------------------------------
    Write-Step "Проверяю встроенный установщик WebView2 (assets\webview2)"

    $webview2Dir = Join-Path $PSScriptRoot 'assets\webview2'
    $webview2Bootstrapper = Join-Path $webview2Dir 'MicrosoftEdgeWebview2Setup.exe'
    $webview2BootstrapperUrl = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'

    if (Test-Path $webview2Bootstrapper) {
        Write-Host "Уже подготовлен: $webview2Bootstrapper — пропускаю."
    }
    else {
        Write-Host "Не найден, скачиваю Evergreen Bootstrapper с сайта Microsoft..."
        New-Item -ItemType Directory -Force -Path $webview2Dir | Out-Null
        try {
            Invoke-WebRequest -Uri $webview2BootstrapperUrl -OutFile $webview2Bootstrapper -UseBasicParsing
            if ((Get-Item $webview2Bootstrapper).Length -lt 500KB) {
                throw "скачанный файл подозрительно маленький — похоже, не установщик"
            }
            Write-Host "Готово: $webview2Bootstrapper"
        }
        catch {
            Remove-Item $webview2Bootstrapper -ErrorAction SilentlyContinue
            Write-Warn "Не удалось автоматически скачать установщик WebView2: $($_.Exception.Message)"
            Write-Host ""
            Write-Host "Сделайте один раз вручную (необязательно, но желательно):"
            Write-Host "  1. Откройте https://developer.microsoft.com/microsoft-edge/webview2/"
            Write-Host "  2. Скачайте 'Evergreen Bootstrapper' (маленький файл, ~2 МБ)"
            Write-Host "  3. Сохраните его как assets\webview2\MicrosoftEdgeWebview2Setup.exe"
            Write-Host "  4. Запустите build.bat ещё раз."
            Write-Host ""
            Write-Warn "Сборка ПРОДОЛЖИТСЯ без него — программа по-прежнему будет работать на"
            Write-Warn "ПК, где WebView2 Runtime уже установлен (это большинство актуальных Windows 10/11)."
        }
    }

    # -----------------------------------------------------------------------
    # 5. Сборка exe
    # -----------------------------------------------------------------------
    Write-Step "Собираю exe через PyInstaller (может занять пару минут)"

    & $venvPy -m PyInstaller build.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) {
        Stop-WithMessage "Сборка PyInstaller завершилась с ошибкой (код $LASTEXITCODE) — см. вывод выше и $LogPath."
    }

    $exePath = Join-Path $PSScriptRoot 'dist\WB_PVZ_Printer.exe'
    if (-not (Test-Path $exePath)) {
        Stop-WithMessage "Сборка завершилась, но dist\WB_PVZ_Printer.exe не найден."
    }

    $sizeMb = [math]::Round((Get-Item $exePath).Length / 1MB, 1)

    $tesseractEmbedded = Test-Path (Join-Path $PSScriptRoot 'assets\tesseract\tesseract.exe')
    $webview2Embedded = Test-Path (Join-Path $PSScriptRoot 'assets\webview2\MicrosoftEdgeWebview2Setup.exe')

    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  ГОТОВО" -ForegroundColor Green
    Write-Host "============================================"
    Write-Host "Файл: $exePath ($sizeMb МБ)"
    Write-Host ""
    if ($tesseractEmbedded) {
        Write-Host "  [OK] Tesseract OCR встроен в exe" -ForegroundColor Green
    } else {
        Write-Host "  [НЕ ВСТРОЕН] Tesseract OCR — распознавание номера НЕ будет работать" -ForegroundColor Red
        Write-Host "               на ПК без отдельно установленного Tesseract. См. предупреждение" -ForegroundColor Red
        Write-Host "               выше по журналу (или в build_log.txt) — почему не подготовился." -ForegroundColor Red
    }
    if ($webview2Embedded) {
        Write-Host "  [OK] Установщик WebView2 встроен в exe" -ForegroundColor Green
    } else {
        Write-Host "  [НЕ ВСТРОЕН] Установщик WebView2 — сработает только на ПК, где WebView2" -ForegroundColor Yellow
        Write-Host "               Runtime уже есть (это большинство актуальных Windows 10/11)." -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "Это единственный файл — скопируйте его на любой Windows ПК и запустите."
}
catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Red
    Write-Host "  СБОРКА ОСТАНОВЛЕНА ИЗ-ЗА ОШИБКИ" -ForegroundColor Red
    Write-Host "============================================"
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Подробности:" -ForegroundColor Yellow
    Write-Host ($_ | Out-String)
    Write-Host ""
    Write-Host "Полный журнал сохранён в файл:" -ForegroundColor Yellow
    Write-Host "  $LogPath" -ForegroundColor Yellow
    Write-Host "Пришлите этот файл, если проблема непонятна."
}
finally {
    try { Stop-Transcript | Out-Null } catch { }
    Write-Host ""
    Read-Host "Нажмите Enter, чтобы закрыть окно"
}

exit $exitCode
