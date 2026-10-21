@echo off
REM Builds dist\DualLink.exe on your own PC (GitHub Actions does the same automatically).
python -m pip install -r requirements-dev.txt || exit /b 1
python -m pytest -q || exit /b 1
python tools\make_icon.py || exit /b 1
python -m PyInstaller --noconfirm --onefile --windowed --name DualLink --icon build\icon.ico ^
  --add-data "duallink\ui;duallink\ui" --add-data "duallink\assets;duallink\assets" ^
  --hidden-import pystray._win32 --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium ^
  main.py || exit /b 1
python -m PyInstaller --noconfirm --onedir --windowed --name DualLink --icon build\icon.ico ^
  --distpath dist\folder --workpath build\folder --add-data "duallink\ui;duallink\ui" --add-data "duallink\assets;duallink\assets" ^
  --hidden-import pystray._win32 --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium ^
  main.py || exit /b 1
where dotnet >nul 2>nul && (
  dotnet publish native\DualLinkWindow\DualLinkWindow.csproj -c Release -r win-x64 -p:Platform=x64 -o dist\native || exit /b 1
  xcopy /e /i /y dist\native dist\folder\DualLink\DualLinkWindow >nul
)
where iscc >nul 2>nul && iscc installer\DualLink.iss
echo.
echo Done: dist\DualLink.exe (and dist\DualLink-Setup-*.exe if Inno Setup is installed)
