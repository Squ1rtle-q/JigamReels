# Сборка ReelsMakerPro.exe (Windows)

## 1. Окружение

```powershell
cd D:\ReelsMakerPro
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-build.txt
```

## 2. FFmpeg в `bin\`

См. [bin/README.md](bin/README.md). Минимум: `ffmpeg.exe` и `ffprobe.exe` рядом с проектом в `bin\`.

## 3. Сборка одним файлом

```powershell
pyinstaller ReelsMakerPro.spec
```

Готовый файл: `dist\ReelsMakerPro.exe`.

Первый запуск exe может быть медленным (распаковка). Размер большой, если в окружение установлены `torch` / Whisper — это нормально для авто-субтитров.

## 4. Загрузка на GitHub

1. Создайте **новый репозиторий** на GitHub (без README, если уже есть локальный проект).
2. В корне проекта:

```powershell
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/ВАШ_ЛОГИН/ИМЯ_РЕПО.git
git push -u origin main
```

3. Бинарник **не коммитьте** (в `.gitignore` есть `*.exe`). Выкладывайте `ReelsMakerPro.exe` в **Releases**: репозиторий → **Releases** → **Draft a new release** → прикрепите файл.

Если Git спросит логин: для HTTPS используйте **Personal Access Token** вместо пароля (GitHub → Settings → Developer settings → Personal access tokens).
