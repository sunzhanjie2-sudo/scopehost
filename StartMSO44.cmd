@echo off
cd /d "%~dp0"
start "ScopeHost MSO44" "ScopeHost.exe" --visa "USB0::0x0699::0x0527::C049800::INSTR"
