@echo off
REM User projects generated from this template typically live OUTSIDE the engine tree,
REM so the engine root can't be derived from %~dp0. Fall back to the HAZEL_DIR env var
REM (set by the Launcher at install time or by the editor in-process). The value is
REM passed through as --hazel-dir so the lua side keeps a consistent precedence order.
pushd %~dp0
call "%HAZEL_DIR%\vendor\bin\premake5.exe" --hazel-dir="%HAZEL_DIR%" vs2022
popd
