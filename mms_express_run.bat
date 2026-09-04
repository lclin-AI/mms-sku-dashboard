@echo off
cd /d "%~dp0"
"C:\Users\lclin\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" mms_express_to_supabase.py --store B0812001 --days 1 --fwd 7 >> mms_express_run.log 2>&1
