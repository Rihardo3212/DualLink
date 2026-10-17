@echo off
REM Builds dist\DualLink.exe on your own PC (GitHub Actions does the same automatically).
python -m pip install -r requirements-dev.txt || exit /b 1
python -m pytest -q || exit /b 1
python tools\make_icon.py || exit /b 1
python -m PyInstaller --noconfirm --onefile --windowed --name DualLink --icon build\icon.ico ^
  --add-data "duallink\ui;duallink\ui" ^
  --hidden-import pystray._win32 --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium ^
  main.py || exit /b 1
echo.
echo Done: dist\DualLink.exe
