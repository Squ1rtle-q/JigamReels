# Внешние утилиты (локальная сборка)

Положите сюда файлы **перед** запуском `pyinstaller` (они попадут в один exe):

| Файл | Назначение |
|------|------------|
| `ffmpeg.exe` | Обязателен для обработки видео |
| `ffprobe.exe` | Обычно идёт в том же архиве, что и FFmpeg |
| `yt-dlp.exe` | Опционально, если нужна загрузка с YouTube без установки в PATH |

Скачать:

- FFmpeg: https://www.gyan.dev/ffmpeg/builds/ (essentials build достаточно)
- yt-dlp: https://github.com/yt-dlp/yt-dlp/releases

Папка `bin/` в репозитории пустая намеренно (бинарники не храним в Git).
