@echo off
cd /d "%~dp0"
"C:\Users\lclin\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" imax_disposal_to_supabase.py --store B0812001 --days 14 >> imax_disposal_run.log 2>&1
