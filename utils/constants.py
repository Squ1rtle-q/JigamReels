APP_NAME = "ReelsMaker Pro [mod by llimonix]"
APP_VERSION = "1.1.1"
LOG_FILE = "app.log"
FFMPEG_EXE_PATH = "bin/ffmpeg.exe"
YTDLP_EXE_PATH = "bin/yt-dlp.exe"

VIDEO_EXTENSIONS = [
    ".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv"
]
GIF_EXTENSIONS = [".gif"]
VALID_INPUT_EXTENSIONS = VIDEO_EXTENSIONS + GIF_EXTENSIONS

FILTERS = {
    "Нет фильтра": "",
    "Случ. цвет (яркость/контраст/...)": "eq=brightness={br}:contrast={ct}:saturation={sat},hue=h={hue}",
    "Черно-белое": "hue=s=0",
    "Сепия": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131:0",
    "Инверсия": "negate",
    "Размытие (легкое)": "gblur=sigma=2",
    "Размытие (сильное)": "gblur=sigma=10",
    "Отразить по горизонтали": "hflip",
    "Отразить по вертикали": "vflip",
    "Пикселизация": "scale=iw/10:ih/10,scale=iw*10:ih*10:flags=neighbor",
    "VHS (шум, сдвиг)": "chromashift=1:1,noise=alls=20:allf=t+u",
    "Повыш. контрастность": "eq=contrast=1.5",
    "Пониж. контрастность": "eq=contrast=0.7",
    "Повыш. насыщенность": "eq=saturation=1.5",
    "Пониж. насыщенность": "eq=saturation=0.5",
    "Повыш. яркость": "eq=brightness=0.15",
    "Пониж. яркость": "eq=brightness=-0.15",
    "Холодный фильтр": "curves=b='0/0 0.4/0.5 1/1':g='0/0 0.4/0.4 1/1'",
    "Теплый фильтр": "curves=r='0/0 0.4/0.5 1/1':g='0/0 0.6/0.6 1/1'",
    "Случайный фильтр": "RANDOM_PLACEHOLDER",
}

# Старые пресеты с полем overlay_pos (строка) → alignment 1..9 (как у субтитров)
OVERLAY_LEGACY_POS_TO_ALIGNMENT = {
    "Верх-Лево": 7,
    "Верх-Центр": 8,
    "Верх-Право": 9,
    "Середина-Лево": 4,
    "Середина-Центр": 5,
    "Середина-Право": 6,
    "Низ-Лево": 1,
    "Низ-Центр": 2,
    "Низ-Право": 3,
}

REELS_WIDTH = 1080
REELS_HEIGHT = 1920
REELS_FORMAT_NAME = f"Reels/TikTok ({REELS_WIDTH}x{REELS_HEIGHT})"
OUTPUT_FORMATS = ["Оригинальный", REELS_FORMAT_NAME]

CODECS = {
    "CPU (H.264 | libx264)": "libx264",
    "NVIDIA (H.264 | h264_nvenc)": "h264_nvenc",
    "NVIDIA (H.265 | hevc_nvenc)": "hevc_nvenc",
    "Intel (H.264 | h264_qsv)": "h264_qsv",
    "Intel (H.265 | hevc_qsv)": "hevc_qsv",
    "AMD (H.264 | h264_amf)": "h264_amf",
    "AMD (H.265 | hevc_amf)": "hevc_amf",
}

WHISPER_MODELS = [
    "distil-large-v3",
    "large-v3",
    "large",
    "medium",
    "small",
    "base",
    "tiny",
]

WHISPER_LANGUAGES = [
    "Auto-detect", "Russian", "English", "Ukrainian",
    "German", "French", "Spanish", "Italian"
]
