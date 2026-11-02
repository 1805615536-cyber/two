@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "tools\ffmpeg.exe" (
  echo [1/2] 首次运行：正在获取 ffmpeg ...
  python tools\fetch_ffmpeg.py
  if errorlevel 1 echo [警告] ffmpeg 获取失败，剪辑功能不可用，请手动放入 tools\ffmpeg.exe
)

echo [2/2] 启动切影工坊 = http://127.0.0.1:8971
start "" http://127.0.0.1:8971
python backend.py 8971
pause
