@echo off
cd /d "%~dp0"
echo Installing the optional local transcription engine...
python -m pip install faster-whisper
echo.
echo Installation finished. StoryCut will detect it automatically.
pause
