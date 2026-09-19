# ScreenCapture

Утилита в трее для Windows: скриншот выделенной области и запись области экрана со звуком.

- **PrtScn** — скриншот выделенной области: сохраняется в файл и копируется в буфер обмена.
- **Ctrl+PrtScn** — старт/стоп записи выделенной области (50 fps, x264, системный звук).
- Папки сохранения и хоткеи меняются из меню в трее.
- Проверка обновлений и установка новой версии — пункт меню «Проверить обновления».

Настройки и логи: `%APPDATA%\ScreenCapture\`.

## Сборка

Нужен Python 3.11+, `ffmpeg.exe` рядом с исходником и Inno Setup 6.

```powershell
pip install pyinstaller pystray pillow numpy soundcard
python -m PyInstaller --onefile --noconsole --icon icon.ico --name ScreenCapture capture.py
Move-Item dist\ScreenCapture.exe .\ScreenCapture.exe -Force
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer.iss
```

`ffmpeg.exe` в exe не зашит — он должен лежать рядом с ним (установщик кладёт его сам).

## Релиз

Версия задаётся в `APP_VERSION` (`capture.py`) и `MyAppVersion` (`installer.iss`) — поднимать обе.
Собранный `ScreenCapture-Setup.exe` прикладывается к релизу на GitHub с тегом `vX.Y.Z`:
установленное приложение берёт обновления оттуда.
