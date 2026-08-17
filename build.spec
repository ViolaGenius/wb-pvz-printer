# build.spec — сборка в один exe-файл: pyinstaller build.spec
# Запускать из корня проекта, где лежит main.py.
# Проще всего — через build.bat (автоматизирует всё, включая подготовку
# портативного Tesseract в assets/tesseract перед вызовом pyinstaller).

# -*- mode: python ; coding: utf-8 -*-

import os

_project_dir = os.path.dirname(os.path.abspath(SPEC))
_tesseract_exe = os.path.join(_project_dir, 'assets', 'tesseract', 'tesseract.exe')
_webview2_bootstrapper = os.path.join(_project_dir, 'assets', 'webview2', 'MicrosoftEdgeWebview2Setup.exe')
if os.path.isfile(_tesseract_exe):
    print(f'[build.spec] Портативный Tesseract найден, будет встроен в exe: {_tesseract_exe}')
else:
    print(
        '[build.spec] ВНИМАНИЕ: assets/tesseract/tesseract.exe не найден — '
        'exe соберётся, но распознавание номера НЕ будет работать, пока на '
        'целевом ПК отдельно не установлен Tesseract. Запустите build.bat, '
        'он подготовит портативную копию автоматически.'
    )
if os.path.isfile(_webview2_bootstrapper):
    print(f'[build.spec] Установщик WebView2 найден, будет встроен в exe: {_webview2_bootstrapper}')
else:
    print(
        '[build.spec] ВНИМАНИЕ: assets/webview2/MicrosoftEdgeWebview2Setup.exe не найден — '
        'программа сможет открыть окно настроек, только если на целевом ПК уже стоит '
        'WebView2 Runtime (это большинство актуальных Windows 10/11). Запустите '
        'build.bat, он скачает установщик автоматически.'
    )

# ВАЖНО про numpy/cv2: НЕ используем здесь collect_all('numpy') — у PyInstaller
# уже есть собственный встроенный хук для numpy, который корректно собирает
# все его бинарники (.pyd/.dll). Если собрать numpy ЕЩЁ РАЗ вручную через
# collect_all поверх этого хука, в exe попадают ДВЕ копии одного и того же
# скомпилированного C-модуля numpy._core.multiarray. Начиная с недавних версий
# numpy сам детектирует повторную инициализацию и падает с
# "ImportError: cannot load module more than once per process" при втором
# import numpy в процессе — это ровно то, что было в этой сборке. Обычного
# 'numpy' в hiddenimports (см. ниже) вместе со встроенным хуком PyInstaller
# достаточно — дополнительно ничего собирать не нужно.
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('web_ui/static', 'web_ui/static'),
        # 'assets' целиком — сюда входят шрифты, звуки и, если подготовлены
        # заранее (см. build.bat), assets/tesseract/* (exe + dll + tessdata)
        # и assets/webview2/* (установщик WebView2 Runtime). Это data-файлы,
        # а не binaries: и tesseract.exe, и MicrosoftEdgeWebview2Setup.exe
        # запускаются как отдельные процессы (subprocess), а не подгружаются
        # как DLL в сам Python-процесс, поэтому анализ зависимостей
        # PyInstaller им не нужен — свои DLL они несут рядом с собой.
        ('assets', 'assets'),
    ],
    hiddenimports=[
        'win32timezone',  # частая проблема: pywin32 требует этот модуль в hidden imports для PyInstaller
        'numpy',
        'cv2',            # opencv-python-headless — у PyInstaller тоже есть встроенный хук
        'clr',                          # pythonnet — бэкенд pywebview на Windows
        'webview.platforms.winforms',   # pywebview импортирует его динамически внутри функции
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WB_PVZ_Printer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-сжатие иногда портит скомпилированные .dll numpy/opencv при
    # распаковке — выключено ради надёжности, цена — exe чуть крупнее.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # без консольного окна — это фоновое трей-приложение
    windowed=True,
    icon=None,       # при желании укажите путь к .ico
)
