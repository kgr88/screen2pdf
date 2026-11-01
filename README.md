# Screen2PDF

A small Windows app that captures screenshots and combines them into a single PDF,
optionally with an OCR text layer so the text can be selected, copied and searched.

## Features

- **Capture screen** (`Ctrl+Alt+P`): one monitor or all monitors together
- **Capture area** (`Ctrl+Alt+R`): drag a rectangle over a frozen copy of the screen;
  *Reuse last area* repeats the same rectangle without asking (handy for paging through a document)
- Hotkeys work from any app; the window hides itself during capture
- Optional capture delay, reorder / delete screenshots, live preview
- **Save as PDF**: one page per screenshot, images embedded losslessly
- **Selectable text (OCR)** via Tesseract, Polish + English by default (`pol+eng`)

## Run from source

Requires Python 3.10+ and, for OCR, [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
installed in `C:\Program Files\Tesseract-OCR`.

```bash
pip install -r requirements.txt
```

Then double-click `Start Screen2PDF.bat` (or run `pythonw screen2pdf.pyw`).

## Build a standalone .exe

Double-click `build_exe.bat`. It produces `dist\Screen2PDF.exe` with Tesseract and the
language data bundled in, so it runs on PCs without Python or Tesseract installed.
`bundle_tesseract.py` copies only the Tesseract files actually needed and strips their
debug sections to keep the exe small.

## OCR languages

Language data lives in `tessdata/` (from [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)).
To add a language, drop its `<code>.traineddata` file in that folder. It then appears in the
language dropdown, and languages can be combined, e.g. `pol+deu`.

## Third-party components

The built exe bundles [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) and its
language data, both licensed under the Apache License 2.0.
