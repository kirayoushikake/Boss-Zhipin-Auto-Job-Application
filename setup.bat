@echo off
chcp 65001 >nul
echo ============================================
echo   BOSS直聘自动投递助手 - 一键安装
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.11+
    echo 下载地址：https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"
    pause
    exit /b 1
)
echo [OK] Python 已安装

:: Check uv
uv --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 正在安装 uv 包管理器...
    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    if %errorlevel% neq 0 (
        echo [错误] uv 安装失败，请手动安装：
        echo powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
        pause
        exit /b 1
    )
)
echo [OK] uv 已安装

:: Install dependencies
echo.
echo [安装] 正在安装项目依赖（首次约需 3-5 分钟）...
uv sync
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo [OK] 依赖安装完成
echo [OK] 简历向量器为本地内置版本，无需下载 Hugging Face 模型

:: Check .env
if not exist ".env" (
    echo.
    echo [配置] 未找到 .env 文件，正在从 .env.example 创建...
    copy .env.example .env >nul
    echo [提示] 请编辑 .env 文件，至少填写以下 3 项：
    echo   LLM_API_KEY=sk-你的API密钥
    echo   LLM_MODEL=deepseek-chat
    echo   LLM_BASE_URL=https://api.deepseek.com
    echo.
    echo API Key 可在以下地址免费申请：
    echo   DeepSeek: https://platform.deepseek.com/api_keys
    echo.
    start notepad .env
    echo 编辑完 .env 后请重新运行本脚本
    pause
    exit /b 0
)

:: Check resume
if not exist "resume\my_cover.pdf" (
    echo.
    echo [提示] 未找到简历文件 resume\my_cover.pdf
    echo 请将你的 PDF 简历放到 resume\ 目录并命名为 my_cover.pdf
    echo 或在 .env 中设置 RESUME_PATH=你的简历路径
    echo.
)

echo.
echo ============================================
echo   安装完成！
echo ============================================
echo.
echo 下一步：
echo   1. 编辑 .env 填写你的 API Key
echo   2. 把 PDF 简历放到 resume\my_cover.pdf
echo   3. Dry-Run 试跑（只生成不发送）：
echo      set DRY_RUN=1
echo      set PYTHONIOENCODING=utf-8
echo      uv run python -m boss_zhipin
echo   4. 正式运行：
echo      set PYTHONIOENCODING=utf-8
echo      uv run python -m boss_zhipin
echo.
pause
