@echo off
rem Builds a standalone Screen2PDF.exe in the "dist" folder.
rem Tesseract OCR and the tessdata languages (Polish + English) are bundled inside,
rem so the exe runs on PCs without Python or Tesseract installed.
rem Building requires Tesseract installed at C:\Program Files\Tesseract-OCR.
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller pefile || goto :error
python bundle_tesseract.py "C:\Program Files\Tesseract-OCR" build\tesseract_bundle || goto :error
python -m PyInstaller --noconfirm --onefile --windowed --name Screen2PDF ^
    --add-data "build\tesseract_bundle;tesseract" ^
    --add-data "tessdata;tessdata" ^
    screen2pdf.pyw || goto :error
echo.
echo Done: %~dp0dist\Screen2PDF.exe
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
