@echo off
echo ========================================
echo  Companies House Financial Extractor
echo ========================================
echo.
echo Installing required packages...
python -m pip install -r requirements.txt
echo.
echo Starting the web app...
echo.
echo The app will open in your browser at:
echo http://localhost:5000
echo.
echo Press Ctrl+C to stop the app
echo ========================================
echo.
python web_app.py
pause
