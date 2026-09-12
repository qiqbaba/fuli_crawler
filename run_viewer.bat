@echo off
chcp 65001 >nul
title 资源库 PDF 封面预览
echo ========================================================
echo   🚀 启动数据库 PDF 封面预览 (Streamlit)...
echo ========================================================
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m streamlit run viewer.py
) else (
    python -m streamlit run viewer.py
)
pause
