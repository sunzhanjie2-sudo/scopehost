@echo off
cd /d "%~dp0"
start "ScopeHost DS1152E" "ScopeHost.exe" --visa "USB0::0x1AB1::0x0588::DS1EI182307208::INSTR"
