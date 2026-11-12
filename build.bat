@echo off
REM Builds DualLink on your own PC (GitHub Actions does the same automatically).
REM Needs Rust (https://rustup.rs) with Visual Studio's C++ build tools; Inno Setup for the installer.
cargo test --workspace || exit /b 1
cargo build --release --workspace || exit /b 1
if not exist dist\DualLink mkdir dist\DualLink
copy /y target\release\DualLink.exe dist\DualLink\ >nul || exit /b 1
copy /y target\release\DualLinkWindow.exe dist\DualLink\ >nul || exit /b 1
where iscc >nul 2>nul && iscc installer\DualLink.iss
echo.
echo Done: dist\DualLink\DualLink.exe (and dist\DualLink-Setup-*.exe if Inno Setup is installed)
