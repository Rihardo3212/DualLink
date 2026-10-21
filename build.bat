@echo off
REM Builds DualLink on your own PC (GitHub Actions does the same automatically).
REM Needs Python 3.10+ and the .NET 10 SDK (Visual Studio 2026 has it); Inno Setup for the installer.
python -m pip install -r requirements-dev.txt || exit /b 1
python -m pytest -q || exit /b 1
python tools\make_icon.py || exit /b 1
python -m PyInstaller --noconfirm --onedir --windowed --name DualLink --icon build\icon.ico ^
  --distpath dist\folder --workpath build\folder --add-data "duallink\assets;duallink\assets" ^
  --hidden-import pystray._win32 ^
  main.py || exit /b 1
REM the window: ahead-of-time compiled if Visual Studio's C++ tools are installed, otherwise a normal build
dotnet publish native\DualLinkWindow\DualLinkWindow.csproj -c Release -r win-x64 -p:Platform=x64 -o dist\native ^
  || dotnet publish native\DualLinkWindow\DualLinkWindow.csproj -c Release -r win-x64 -p:Platform=x64 -p:PublishAot=false -o dist\native ^
  || exit /b 1
xcopy /e /i /y dist\native dist\folder\DualLink\DualLinkWindow >nul
where iscc >nul 2>nul && iscc installer\DualLink.iss
echo.
echo Done: dist\folder\DualLink\DualLink.exe (and dist\DualLink-Setup-*.exe if Inno Setup is installed)
