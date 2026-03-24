"""
FFmpeg utilities module for video processing.
Модуль утилит для работы с FFmpeg для обработки видео.
"""

import os
import subprocess
import random
import platform
import shlex
import shutil
import re
import logging
from typing import List, Optional, Tuple, Dict, Callable

# Импорт констант (предполагаемые значения)
try:
    from utils.constants import (
        FFMPEG_EXE_PATH, 
        FILTERS, 
        OVERLAY_POSITIONS, 
        REELS_WIDTH, 
        REELS_HEIGHT, 
        REELS_FORMAT_NAME
    )
except ImportError:
    # Fallback значения если модуль constants недоступен
    FFMPEG_EXE_PATH = "bin/ffmpeg.exe"
    FILTERS = {}
    OVERLAY_POSITIONS = {}
    REELS_WIDTH = 1080
    REELS_HEIGHT = 1920
    REELS_FORMAT_NAME = f"Reels/TikTok ({REELS_WIDTH}x{REELS_HEIGHT})"


from utils.path_utils import get_ffmpeg_path



def find_executable(base_path: str, exe_name: str) -> Optional[str]:
    """
    Поиск исполняемого файла по указанному пути или в системном PATH.
    
    Args:
        base_path: Базовый путь к исполняемому файлу
        exe_name: Имя исполняемого файла
        
    Returns:
        Путь к найденному исполняемому файлу или None
    """
    if os.path.exists(base_path):
        return base_path
    
    logging.info(f"Info: Executable not found at '{base_path}'. Trying system PATH for '{exe_name}'...")
    
    exe_in_path = shutil.which(exe_name)
    if exe_in_path:
        logging.info(f"Info: Using '{exe_name}' found in system PATH: {exe_in_path}")
        return exe_in_path
    
    logging.warning(f"Warning: '{exe_name}' not found at '{base_path}' or in system PATH.")
    return None


# Настройка путей к FFmpeg и FFprobe
FFMPEG_PATH_BASE = get_ffmpeg_path()
FFPROBE_PATH_BASE = FFMPEG_PATH_BASE.replace('ffmpeg.exe', 'ffprobe.exe')

FFMPEG_PATH_EFFECTIVE = find_executable(FFMPEG_PATH_BASE, 'ffmpeg')
FFPROBE_PATH_EFFECTIVE = find_executable(FFPROBE_PATH_BASE, 'ffprobe')


def run_ffmpeg(cmd: List[str], input_file_for_log: str = "input", 
               duration: float = 0, progress_callback: Optional[Callable[[int], None]] = None) -> None:
    """
    Запуск команды FFmpeg с обработкой прогресса.
    
    Args:
        cmd: Список аргументов команды FFmpeg
        input_file_for_log: Имя входного файла для логирования
        duration: Продолжительность видео в секундах
        progress_callback: Функция обратного вызова для отчета о прогрессе
        
    Raises:
        FileNotFoundError: Если FFmpeg не найден
        subprocess.CalledProcessError: Если FFmpeg завершился с ошибкой
        RuntimeError: При других ошибках выполнения
    """
    if not FFMPEG_PATH_EFFECTIVE:
        raise FileNotFoundError('FFmpeg executable not found. Cannot run command.')
    
    # Настройка для Windows
    creationflags = 0
    startupinfo = None
    
    if platform.system() == 'Windows':
        creationflags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    
    # Построение финальной команды
    final_cmd = [FFMPEG_PATH_EFFECTIVE]
    
    # Добавление параметров логирования если их нет
    if '-loglevel' not in cmd:
        final_cmd.extend(['-loglevel', 'debug'])
    
    # Добавление прогресса если нужен
    if progress_callback:
        final_cmd.extend(['-progress', 'pipe:1'])
    
    if '-hide_banner' not in cmd:
        final_cmd.append('-hide_banner')
    
    final_cmd.extend(cmd)
    
    # Логирование команды
    command_for_log = ' '.join(shlex.quote(str(c)) for c in final_cmd)
    logging.info(f'Running FFmpeg command: {command_for_log}')
    
    try:
        process_cwd = os.path.dirname(FFMPEG_PATH_EFFECTIVE)
        
        process = subprocess.Popen(
            final_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=creationflags,
            startupinfo=startupinfo,
            bufsize=1,
            cwd=process_cwd
        )
        
        output_lines = []
        time_regex = re.compile(r'out_time_ms=(\d+)')
        
        # Чтение вывода и отслеживание прогресса
        while True:
            line = process.stdout.readline()
            if not line:
                break
                
            line = line.strip()
            if line:
                logging.debug(f'FFmpeg: {line}')
                output_lines.append(line)
                
                # Обработка прогресса
                if progress_callback and duration > 0 and line.startswith('out_time_ms'):
                    match = time_regex.search(line)
                    if match:
                        elapsed_ms = int(match.group(1))
                        progress = int(elapsed_ms / (duration * 1000000) * 100)
                        progress_callback(min(progress, 100))
        
        process.stdout.close()
        return_code = process.wait()
        
        if return_code != 0:
            error_message = (
                f'FFmpeg failed with exit code {return_code} for file \'{os.path.basename(input_file_for_log)}\'.\n'
                f'Command: {command_for_log}\n'
                f'Last lines of output:\n' + '\n'.join(output_lines[-15:])
            )
            raise subprocess.CalledProcessError(
                return_code, 
                final_cmd, 
                output='\n'.join(output_lines),
                stderr='\n'.join(output_lines)
            )
        
        logging.info(f"FFmpeg successfully processed '{os.path.basename(input_file_for_log)}'")
        
    except FileNotFoundError:
        raise FileNotFoundError(
            f"FFmpeg executable not found at '{FFMPEG_PATH_EFFECTIVE}'. "
            "Please ensure FFmpeg is installed and accessible."
        )
    except subprocess.CalledProcessError:
        # Сохраняем оригинальную ошибку ffmpeg (stderr/output) без маскировки.
        raise
    except Exception as e:
        raise RuntimeError(
            f"An error occurred while running FFmpeg for file '{os.path.basename(input_file_for_log)}': {e}"
        )


def detect_crop_dimensions(path: str) -> Optional[str]:
    """
    Определяет размеры обрезки, используя FFMPEG (а не FFPROBE), что является
    правильным подходом для применения видеофильтров.
    
    Args:
        path: Путь к видеофайлу
        
    Returns:
        Строка с параметрами обрезки в формате 'crop=w:h:x:y' или None
        
    Raises:
        FileNotFoundError: Если FFmpeg не найден
    """
    logging.info(f'Detecting crop dimensions for {os.path.basename(path)} using ffmpeg...')
    
    if not FFMPEG_PATH_EFFECTIVE:
        error_msg = 'FFmpeg executable not found. Cannot perform crop detection.'
        logging.error(error_msg)
        raise FileNotFoundError(error_msg)
    
    try:
        cmd = [
            FFMPEG_PATH_EFFECTIVE,
            '-hide_banner',
            '-ss', '5',  # Начинаем с 5-й секунды
            '-t', '10',   # Анализируем 10 секунд
            '-i', path,
            '-vf', 'cropdetect=limit=24:round=16',
            '-an',        # Без аудио
            '-f', 'null',
            '-'
        ]
        
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        _, stderr_output = process.communicate(timeout=60)
        
        # Поиск строк с информацией об обрезке
        crop_lines = [line for line in stderr_output.split('\n') if 'crop=' in line]
        
        if not crop_lines:
            logging.warning(f'cropdetect found no crop values for {os.path.basename(path)}')
            return None
        
        # Берем последнюю строку с параметрами обрезки
        last_crop_line = crop_lines[-1]
        crop_match = re.search(r'crop=(\d+:\d+:\d+:\d+)', last_crop_line)
        
        if crop_match:
            crop_params = crop_match.group(1)
            logging.info(f'Successfully detected crop dimensions: crop={crop_params}')
            return f'crop={crop_params}'
            
        return None
        
    except Exception as e:
        logging.error(f'An error occurred during crop detection for {os.path.basename(path)}: {e}')
        return None


def get_video_dimensions(path: str) -> Tuple[int, int]:
    """
    Получение размеров видео с помощью ffprobe.
    
    Args:
        path: Путь к видеофайлу
        
    Returns:
        Кортеж (ширина, высота) или (0, 0) при ошибке
    """
    if not FFPROBE_PATH_EFFECTIVE:
        logging.warning('ffprobe not found, cannot get video dimensions.')
        return (0, 0)
    
    cmd = [
        FFPROBE_PATH_EFFECTIVE,
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=s=x:p=0',
        path
    ]
    
    try:
        # Настройка для Windows
        creationflags = 0
        startupinfo = None
        
        if platform.system() == 'Windows':
            creationflags = subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        
        process_cwd = os.path.dirname(FFPROBE_PATH_EFFECTIVE)
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding='utf-8',
            errors='replace',
            creationflags=creationflags,
            startupinfo=startupinfo,
            cwd=process_cwd
        )
        
        dims = result.stdout.strip().split('x')
        if len(dims) == 2:
            return (int(dims[0]), int(dims[1]))
        
        logging.warning(f"Warning: Could not parse dimensions from ffprobe output: '{result.stdout.strip()}' for file '{os.path.basename(path)}'")
        return (0, 0)
        
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running ffprobe for '{os.path.basename(path)}': {e.stderr.strip()}")
        return (0, 0)
    except FileNotFoundError:
        logging.error(f"Error: ffprobe executable not found at '{FFPROBE_PATH_EFFECTIVE}'.")
        return (0, 0)
    except Exception as e:
        logging.error(f"Unexpected error getting dimensions for '{os.path.basename(path)}': {e}")
        return (0, 0)


def get_video_duration(path: str) -> float:
    """
    Получение продолжительности видео в секундах.
    
    Args:
        path: Путь к видеофайлу
        
    Returns:
        Продолжительность в секундах или 0 при ошибке
    """
    if not FFPROBE_PATH_EFFECTIVE:
        logging.warning('ffprobe not found, cannot get video duration.')
        return 0
    
    cmd = [
        FFPROBE_PATH_EFFECTIVE,
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        path
    ]
    
    try:
        # Настройка для Windows
        creationflags = 0
        startupinfo = None
        
        if platform.system() == 'Windows':
            creationflags = subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        
        process_cwd = os.path.dirname(FFPROBE_PATH_EFFECTIVE)
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding='utf-8',
            errors='replace',
            creationflags=creationflags,
            startupinfo=startupinfo,
            cwd=process_cwd
        )
        
        return float(result.stdout.strip())
        
    except Exception:
        return 0


def detect_viral_moments(path: str, clip_duration: int = 15, max_clips: int = 3) -> List[Tuple[float, float]]:
    """
    Находит самые динамичные/виральные участки по комбинации:
    - плотности смен сцен (scene score)
    - средней громкости звука (RMS level)

    Args:
        path: Путь к входному видео
        clip_duration: Длительность одного клипа в секундах
        max_clips: Максимальное количество клипов

    Returns:
        Список кортежей (start, duration)
    """
    duration = get_video_duration(path)
    if duration <= 0:
        return [(0, clip_duration)]

    # Ограничиваем параметры адекватными рамками.
    clip_duration = max(5, min(int(clip_duration), 60))
    max_clips = max(1, min(int(max_clips), 10))

    if duration <= clip_duration:
        return [(0, duration)]

    # Кандидаты стартовых точек окна.
    step = max(1.0, clip_duration / 3.0)
    candidates = []
    current = 0.0
    while current + 1.0 < duration:
        candidates.append(current)
        current += step

    scene_points = _collect_scene_change_timestamps(path)
    audio_levels = _collect_audio_rms_levels(path)

    # Зоны, которые обычно содержат интро/аутро, слегка штрафуем.
    edge_guard = min(12.0, max(4.0, duration * 0.08))

    scored = []
    for start in candidates:
        end = min(start + clip_duration, duration)
        scene_score = _scene_density_score(scene_points, start, end)
        audio_score = _audio_energy_score(audio_levels, start, end)
        motion_presence = _motion_presence_bonus(scene_points, start, end)

        edge_penalty = 0.0
        if start < edge_guard:
            edge_penalty += 0.22
        if end > (duration - edge_guard):
            edge_penalty += 0.22

        # Приоритет динамики сцены + наличие движения.
        score = (scene_score * 0.72) + (audio_score * 0.18) + (motion_presence * 0.10) - edge_penalty
        scored.append((score, start, end - start))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected: List[Tuple[float, float]] = []
    for _, start, seg_dur in scored:
        if len(selected) >= max_clips:
            break
        if _overlaps_with_selected(start, seg_dur, selected, min_gap=max(2.0, clip_duration * 0.25)):
            continue
        selected.append((start, seg_dur))

    if not selected:
        return [(0, min(duration, clip_duration))]

    return sorted(selected, key=lambda x: x[0])


def _collect_scene_change_timestamps(path: str) -> List[float]:
    """Собирает таймкоды резких смен сцен через ffmpeg showinfo + select(scene)."""
    if not FFMPEG_PATH_EFFECTIVE:
        return []

    cmd = [
        FFMPEG_PATH_EFFECTIVE,
        '-hide_banner',
        '-i', path,
        '-vf', "fps=2,select='gt(scene,0.28)',showinfo",
        '-an',
        '-f', 'null',
        '-'
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode != 0:
            return []

        points: List[float] = []
        for line in result.stderr.splitlines():
            match = re.search(r'pts_time:([0-9]+(?:\.[0-9]+)?)', line)
            if match:
                points.append(float(match.group(1)))
        return points
    except Exception:
        return []


def _collect_audio_rms_levels(path: str) -> List[Tuple[float, float]]:
    """Собирает RMS уровень аудио по временным окнам."""
    if not FFMPEG_PATH_EFFECTIVE:
        return []

    cmd = [
        FFMPEG_PATH_EFFECTIVE,
        '-hide_banner',
        '-i', path,
        '-af', 'astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level',
        '-vn',
        '-f', 'null',
        '-'
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode != 0:
            return []

        # Ищем пары: frame:<n> + lavfi.astats.Overall.RMS_level=<value>
        values: List[float] = []
        for line in result.stderr.splitlines():
            match = re.search(r'lavfi\.astats\.Overall\.RMS_level=([-]?[0-9]+(?:\.[0-9]+)?)', line)
            if match:
                values.append(float(match.group(1)))

        # Привязываем значения к примерному времени равномерно по длительности.
        total_duration = get_video_duration(path)
        if not values or total_duration <= 0:
            return []

        step = total_duration / max(1, len(values))
        points: List[Tuple[float, float]] = []
        for i, val in enumerate(values):
            points.append((i * step, val))
        return points
    except Exception:
        return []


def _scene_density_score(scene_points: List[float], start: float, end: float) -> float:
    if end <= start:
        return 0.0
    count = sum(1 for t in scene_points if start <= t < end)
    return count / (end - start)


def _motion_presence_bonus(scene_points: List[float], start: float, end: float) -> float:
    """
    Бонус за наличие хотя бы минимальных изменений в кадре.
    Отсекает окна со статичной картинкой + музыкой.
    """
    if end <= start:
        return 0.0
    count = sum(1 for t in scene_points if start <= t < end)
    if count <= 0:
        return 0.0
    if count == 1:
        return 0.35
    if count == 2:
        return 0.65
    return 1.0


def _audio_energy_score(audio_points: List[Tuple[float, float]], start: float, end: float) -> float:
    if end <= start or not audio_points:
        return 0.0

    # RMS level обычно в dBFS (отрицательные значения).
    in_window = [level for t, level in audio_points if start <= t < end]
    if not in_window:
        return 0.0

    avg_level = sum(in_window) / len(in_window)
    # Нормализация: -60..0 dB -> 0..1
    normalized = (avg_level + 60.0) / 60.0
    return max(0.0, min(1.0, normalized))


def _overlaps_with_selected(
    start: float,
    duration: float,
    selected: List[Tuple[float, float]],
    min_gap: float
) -> bool:
    end = start + duration
    for s, d in selected:
        e = s + d
        if not (end + min_gap <= s or start >= e + min_gap):
            return True
    return False


def _hex_to_ass_color(value: str, default_ass: str) -> str:
    """
    Конвертирует #RRGGBB в ASS-формат &HBBGGRR.
    """
    if not value:
        return default_ass
    normalized = value.strip().lstrip('#')
    if len(normalized) != 6:
        return default_ass
    try:
        r = normalized[0:2]
        g = normalized[2:4]
        b = normalized[4:6]
        return f'&H{b}{g}{r}'
    except Exception:
        return default_ass


def _escape_path_for_subtitles_filter(path: str) -> str:
    """
    Путь внутри subtitles='...' для libavfilter (в т.ч. FFmpeg 6+/N‑сборки на Windows).

    Вариант subtitles=filename=C\\:/... ломает разбор (ошибка «No option name near '/Users/...'»)
    из‑за двоеточия после буквы диска. Рабочая форма — краткая запись с кавычками:
    subtitles='C\\:/path/to/file.srt'
    """
    p = os.path.normpath(path or '').replace('\\', '/')
    p = p.replace(':', '\\:')
    p = p.replace("'", "\\'")
    return p


def _build_subtitles_vf(srt_path: str, force_style: Optional[str] = None) -> str:
    """Собирает аргумент -vf subtitles=... (без filename= на Windows)."""
    inner = _escape_path_for_subtitles_filter(srt_path)
    if not force_style:
        return f"subtitles='{inner}'"
    return f"subtitles='{inner}':force_style='{force_style}'"


def _escape_force_style_value(value: str) -> str:
    """
    Экранирует значение для force_style внутри filter_complex.
    """
    v = str(value)
    v = v.replace('\\', '\\\\')
    v = v.replace("'", "\\'")
    v = v.replace(',', '\\,')
    v = v.replace(';', '\\;')
    return v


def process_single(
    in_path: str,
    out_path: str,
    filters: List[str],
    zoom_p: int,
    speed_p: int,
    overlay_file: Optional[str] = None,
    overlay_pos: str = "center",
    output_format: str = "mp4",
    blur_background: bool = False,
    mute_audio: bool = False,
    strip_metadata: bool = False,
    codec: str = "libx264",
    srt_path: Optional[str] = None,
    subtitle_style: Optional[Dict] = None,
    crop_filter: Optional[str] = None,
    overlay_audio_path: Optional[str] = None,
    original_volume: float = 1.0,
    overlay_volume: float = 1.0,
    progress_callback: Optional[Callable[[int], None]] = None,
    trim_start: Optional[float] = None,
    trim_duration: Optional[float] = None,
    plain_subtitles: bool = False
) -> None:
    """
    Обработка одного видеофайла с применением различных эффектов.
    
    Args:
        in_path: Путь к входному файлу
        out_path: Путь к выходному файлу
        filters: Список названий фильтров для применения
        zoom_p: Процент увеличения (100 = без изменений)
        speed_p: Процент скорости (100 = нормальная скорость)
        overlay_file: Путь к файлу оверлея
        overlay_pos: Позиция оверлея
        output_format: Формат выходного файла
        blur_background: Размытие фона для формата reels
        mute_audio: Отключение звука
        strip_metadata: Удаление метаданных
        codec: Видеокодек
        srt_path: Путь к файлу субтитров
        subtitle_style: Стиль субтитров
        crop_filter: Фильтр обрезки
        overlay_audio_path: Путь к аудио оверлею
        original_volume: Громкость оригинального аудио
        overlay_volume: Громкость аудио оверлея
        progress_callback: Функция обратного вызова для прогресса
    """
    # Определение типов входных файлов
    is_gif_input = in_path.lower().endswith('.gif')
    is_gif_overlay = overlay_file and overlay_file.lower().endswith('.gif')
    
    cmd = []
    input_streams = []
    
    # Настройка входного потока
    if is_gif_input:
        cmd.extend(['-stream_loop', '-1', '-i', in_path])
        input_streams.append({'type': 'video', 'index': 0, 'path': in_path})
        has_real_audio = False
    else:
        if trim_start is not None and trim_start > 0:
            cmd.extend(['-ss', f'{trim_start:.3f}'])
        cmd.extend(['-i', in_path])
        if trim_duration is not None and trim_duration > 0:
            cmd.extend(['-t', f'{trim_duration:.3f}'])
        input_streams.append({'type': 'video+audio', 'index': 0, 'path': in_path})
        has_real_audio = True
    
    # Метки потоков
    main_video_stream_label = '[0:v]'
    main_audio_stream_label = '[0:a]' if has_real_audio else None
    overlay_stream_label = None
    
    # Добавление файла оверлея
    if overlay_file and os.path.exists(overlay_file):
        overlay_input_index = len(input_streams)
        
        if is_gif_overlay:
            cmd.extend(['-stream_loop', '-1', '-i', overlay_file])
        else:
            cmd.extend(['-i', overlay_file])
        
        input_streams.append({'type': 'overlay', 'index': overlay_input_index, 'path': overlay_file})
        overlay_stream_label = f'[{overlay_input_index}:v]'
    else:
        is_gif_overlay = False
    
    # Добавление аудио оверлея
    overlay_audio_stream_label = None
    if overlay_audio_path and os.path.exists(overlay_audio_path):
        overlay_audio_index = len(input_streams)
        cmd.extend(['-i', overlay_audio_path])
        input_streams.append({'type': 'audio_overlay', 'index': overlay_audio_index, 'path': overlay_audio_path})
        overlay_audio_stream_label = f'[{overlay_audio_index}:a]'
    
    # Построение filter_complex
    filter_complex_parts = []
    last_video_node = main_video_stream_label
    node_idx = 0
    
    # Применение фильтра обрезки
    if crop_filter:
        new_node_label = f'[v{node_idx}]'
        filter_complex_parts.append(f'{last_video_node}{crop_filter}{new_node_label}')
        last_video_node = new_node_label
        node_idx += 1
    
    # Настройка целевых размеров
    target_w, target_h = REELS_WIDTH, REELS_HEIGHT
    is_reels_format = output_format == REELS_FORMAT_NAME
    
    # Форматирование для reels
    if is_reels_format:
        if blur_background:
            # С размытым фоном
            filter_complex_parts.append(
                f'{last_video_node}split[original][original_copy];'
                f'[original_copy]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,'
                f'crop={target_w}:{target_h}:(in_w-{target_w})/2:(in_h-{target_h})/2,'
                f'gblur=sigma=25[bg];'
                f'[original]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg];'
                f'[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[formatted]'
            )
        else:
            # С черными полосами
            filter_complex_parts.append(
                f'{last_video_node}scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,'
                f'pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black[formatted]'
            )
        last_video_node = '[formatted]'
    
    # Применение фильтров
    for f_name in filters:
        f_template = FILTERS.get(f_name)
        if not f_template or f_name == 'Нет фильтра':
            continue
        
        final_template = ''
        
        if f_name == 'Случайный фильтр':
            # Выбор случайного фильтра
            possible_filters = [k for k, v in FILTERS.items() 
                              if v and k not in ('Нет фильтра', 'Случайный фильтр', 'Случ. цвет (яркость/контраст/...)')]
            if possible_filters:
                chosen_filter_name = random.choice(possible_filters)
                final_template = FILTERS[chosen_filter_name]
        elif f_name == 'Случ. цвет (яркость/контраст/...)':
            # Случайные цветовые параметры
            br = random.uniform(-0.15, 0.15)
            ct = random.uniform(0.8, 1.2)
            sat = random.uniform(0.8, 1.3)
            hue = random.uniform(-5, 5)
            final_template = f_template.format(br=br, ct=ct, sat=sat, hue=hue)
        else:
            final_template = f_template
        
        if final_template:
            new_node_label = f'[v{node_idx}]'
            filter_complex_parts.append(f'{last_video_node}{final_template}{new_node_label}')
            last_video_node = new_node_label
            node_idx += 1
    
    # Применение зума
    zoom_factor = zoom_p / 100
    if abs(zoom_factor - 1) > 1e-5:
        if zoom_factor >= 1:
            # Увеличение с последующей обрезкой
            scale_node = f'[v{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(f'{last_video_node}scale=iw*{zoom_factor}:ih*{zoom_factor}:flags=bicubic{scale_node}')
            
            crop_node = f'[v{node_idx}]'
            node_idx += 1
            
            if is_reels_format:
                filter_complex_parts.append(f'{scale_node}crop={target_w}:{target_h}:(in_w-{target_w})/2:(in_h-{target_h})/2{crop_node}')
            else:
                filter_complex_parts.append(f'{scale_node}crop=iw/{zoom_factor}:ih/{zoom_factor}:(in_w-iw/{zoom_factor})/2:(in_h-ih/{zoom_factor})/2{crop_node}')
            
            last_video_node = crop_node
        else:
            # Уменьшение
            scale_node = f'[v{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(f'{last_video_node}scale=iw*{zoom_factor}:ih*{zoom_factor}:flags=bicubic{scale_node}')
            last_video_node = scale_node
    
    # Добавление субтитров
    if srt_path:
        if os.path.exists(srt_path):
            if plain_subtitles:
                new_node_label = f'[v{node_idx}]'
                node_idx += 1
                filter_complex_parts.append(
                    f"{last_video_node}{_build_subtitles_vf(srt_path)}{new_node_label}"
                )
                last_video_node = new_node_label
            else:
                subtitle_style = subtitle_style or {}
                font_size = subtitle_style.get('font_size', 36)
                font_name = _escape_force_style_value(subtitle_style.get('font_name', 'Arial'))
                font_bold = -1 if subtitle_style.get('font_bold', False) else 0
                font_italic = -1 if subtitle_style.get('font_italic', False) else 0
                font_underline = -1 if subtitle_style.get('font_underline', False) else 0
                text_color_ass = _hex_to_ass_color(subtitle_style.get('text_color', '#FFFFFF'), '&HFFFFFF')
                outline_color_ass = _hex_to_ass_color(subtitle_style.get('outline_color', '#000000'), '&H000000')
                outline_size = subtitle_style.get('outline', 2)
                outline_mode = (subtitle_style.get('outline_mode', 'снаружи') or 'снаружи').lower()
                position_code = 2
                vertical_margin = 70

                base_style_params = [
                    f'Alignment={position_code}',
                    'MarginL=25',
                    'MarginR=25',
                    f'MarginV={vertical_margin}',
                    f'FontName={font_name}',
                    f'FontSize={font_size}',
                    f'Bold={font_bold}',
                    f'Italic={font_italic}',
                    f'Underline={font_underline}',
                    'BorderStyle=1',
                    'Shadow=1'
                ]

                effective_outline = outline_size
                if outline_mode == 'снаружи' and outline_size > 0:
                    effective_outline = max(1, outline_size + 1)

                style_string = '\\,'.join(base_style_params + [
                    f'PrimaryColour={text_color_ass}',
                    f'OutlineColour={outline_color_ass}',
                    f'Outline={effective_outline}'
                ])
                new_node_label = f'[v{node_idx}]'
                node_idx += 1
                filter_complex_parts.append(
                    f"{last_video_node}{_build_subtitles_vf(srt_path, force_style=style_string)}{new_node_label}"
                )
                last_video_node = new_node_label
        else:
            logging.warning(f"Subtitle file does not exist, skipping subtitles: {srt_path}")
    
    # Обработка аудио
    speed_factor = speed_p / 100
    audio_nodes_to_mix = []
    final_audio_node = None
    
    # Оригинальное аудио
    if has_real_audio and not mute_audio:
        vol_node = '[a_orig_vol]'
        filter_complex_parts.append(f'{main_audio_stream_label}volume={original_volume}{vol_node}')
        audio_nodes_to_mix.append(vol_node)
    
    # Аудио оверлей
    if overlay_audio_stream_label:
        vol_node = '[a_over_vol]'
        filter_complex_parts.append(f'{overlay_audio_stream_label}volume={overlay_volume}{vol_node}')
        audio_nodes_to_mix.append(vol_node)
    
    # Микширование аудио
    if len(audio_nodes_to_mix) > 1:
        mixed_audio_node = '[a_mixed]'
        filter_complex_parts.append(f'{"".join(audio_nodes_to_mix)}amix=inputs={len(audio_nodes_to_mix)}:duration=longest[a_mixed]')
        final_audio_node = mixed_audio_node
    elif len(audio_nodes_to_mix) == 1:
        final_audio_node = audio_nodes_to_mix[0]
    
    # Изменение скорости аудио
    if final_audio_node and abs(speed_factor - 1) > 1e-5:
        speed_audio_node_in = final_audio_node
        tempo_filters = []
        current_tempo = speed_factor
        
        # Разбиение больших изменений темпа
        while current_tempo > 2:
            tempo_filters.append('atempo=2.0')
            current_tempo /= 2
        
        min_tempo = 0.5
        while current_tempo < min_tempo:
            tempo_filters.append(f'atempo={min_tempo}')
            current_tempo /= min_tempo
        
        if abs(current_tempo - 1) > 1e-5 and min_tempo <= current_tempo <= 2:
            tempo_filters.append(f'atempo={current_tempo}')
        
        if tempo_filters:
            audio_filters_str = ','.join(tempo_filters)
            new_audio_node = '[a_speed]'
            filter_complex_parts.append(f'{speed_audio_node_in}{audio_filters_str}{new_audio_node}')
            final_audio_node = new_audio_node
    
    # Изменение скорости видео
    if abs(speed_factor - 1) > 1e-5:
        new_node_label = '[v_speed]'
        filter_complex_parts.append(f'{last_video_node}setpts=PTS/{speed_factor}{new_node_label}')
        last_video_node = new_node_label
    
    # Добавление видео оверлея
    if overlay_stream_label:
        pos_params = OVERLAY_POSITIONS.get(overlay_pos, 'x=(W-w)/2:y=(H-h)/2')
        
        alpha_node = f'[ovl{node_idx}]'
        node_idx += 1
        overlay_node = f'[v{node_idx}]'
        node_idx += 1
        
        filter_complex_parts.append(f'{overlay_stream_label}format=rgba{alpha_node}')
        filter_complex_parts.append(f'{last_video_node}{alpha_node}overlay={pos_params}{overlay_node}')
        last_video_node = overlay_node
    
    # Финальное форматирование
    filter_complex_parts.append(f'{last_video_node}format=pix_fmts=yuv420p[vout]')
    
    if final_audio_node:
        filter_complex_parts.append(f'{final_audio_node}anull[aout]')
    
    # Сборка filter_complex
    fc_string = ';'.join(filter(None, filter_complex_parts))
    cmd.extend(['-filter_complex', fc_string])
    cmd.extend(['-map', '[vout]'])
    
    # Настройка аудио
    if final_audio_node:
        cmd.extend(['-map', '[aout]'])
        cmd.extend(['-c:a', 'aac', '-b:a', '128k'])
    else:
        cmd.append('-an')
        if is_gif_input:
            cmd.extend(['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100', '-shortest'])
    
    # Настройка видеокодека
    cmd.extend(['-c:v', codec])
    
    if 'nvenc' in codec or 'amf' in codec:
        cmd.extend(['-cq', '24'])
    elif 'qsv' in codec:
        cmd.extend(['-global_quality', '24'])
    else:
        cmd.extend(['-preset', 'veryfast', '-crf', '24'])
    
    # Удаление метаданных
    if strip_metadata:
        cmd.extend(['-map_metadata', '-1', '-map_chapters', '-1'])
    
    # Дополнительные параметры
    if not is_gif_input and not overlay_audio_path:
        cmd.append('-shortest')
    
    # Финальная команда
    final_cmd = ['-y'] + cmd
    final_cmd.append(out_path)
    
    # Запуск FFmpeg
    duration = trim_duration if trim_duration and trim_duration > 0 else get_video_duration(in_path)
    run_ffmpeg(final_cmd, input_file_for_log=in_path, duration=duration, progress_callback=progress_callback)


def burn_subtitles_postprocess(
    in_path: str,
    out_path: str,
    srt_path: str,
    subtitle_style: Optional[Dict] = None,
    codec: str = "libx264",
    plain_subtitles: bool = True
) -> None:
    """
    Вшивает субтитры вторым проходом после основной обработки.
    Полезно как fallback, когда subtitles в длинном filter_complex нестабилен.
    """
    if not srt_path or not os.path.exists(srt_path):
        raise FileNotFoundError(f'SRT file not found for postprocess: {srt_path}')

    force_style = None
    if not plain_subtitles:
        subtitle_style = subtitle_style or {}
        font_size = subtitle_style.get('font_size', 36)
        font_name = _escape_force_style_value(subtitle_style.get('font_name', 'Arial'))
        font_bold = -1 if subtitle_style.get('font_bold', False) else 0
        font_italic = -1 if subtitle_style.get('font_italic', False) else 0
        text_color_ass = _hex_to_ass_color(subtitle_style.get('text_color', '#FFFFFF'), '&HFFFFFF')
        outline_color_ass = _hex_to_ass_color(subtitle_style.get('outline_color', '#000000'), '&H000000')
        outline_size = subtitle_style.get('outline', 2)
        # Минимальный набор стилей для совместимости разных сборок ffmpeg/libass.
        force_style = '\\,'.join([
            f'FontName={font_name}',
            f'FontSize={font_size}',
            f'Bold={font_bold}',
            f'Italic={font_italic}',
            f'PrimaryColour={text_color_ass}',
            f'OutlineColour={outline_color_ass}',
            'BorderStyle=1',
            f'Outline={outline_size}'
        ])

    vf_filter = _build_subtitles_vf(srt_path, force_style=force_style)

    cmd = ['-y', '-i', in_path, '-vf', vf_filter, '-c:v', codec]

    if 'nvenc' in codec or 'amf' in codec:
        cmd.extend(['-cq', '24'])
    elif 'qsv' in codec:
        cmd.extend(['-global_quality', '24'])
    else:
        cmd.extend(['-preset', 'veryfast', '-crf', '24'])

    cmd.extend(['-c:a', 'copy', out_path])
    run_ffmpeg(cmd, input_file_for_log=in_path, duration=get_video_duration(in_path), progress_callback=None)
    logging.info(
        'Subtitle postprocess OK (%s).',
        'plain' if plain_subtitles else 'styled',
    )


def generate_preview(
    in_path: str,
    out_path: str,
    filters: List[str],
    zoom_p: int,
    overlay_file: Optional[str] = None,
    overlay_pos: str = "center",
    output_format: str = "jpg",
    blur_background: bool = False,
    crop_filter: Optional[str] = None
) -> None:
    """
    Генерация превью (одного кадра) из видео с применением эффектов.
    
    Args:
        in_path: Путь к входному видеофайлу
        out_path: Путь к выходному файлу изображения
        filters: Список названий фильтров для применения
        zoom_p: Процент увеличения (100 = без изменений)
        overlay_file: Путь к файлу оверлея
        overlay_pos: Позиция оверлея
        output_format: Формат выходного файла
        blur_background: Размытие фона для формата reels
        crop_filter: Фильтр обрезки
    """
    is_gif_input = in_path.lower().endswith('.gif')
    
    # Определение времени для кадра
    duration = get_video_duration(in_path)
    if duration > 0 and not is_gif_input:
        mid_point = duration / 2  # Берем кадр из середины видео
    else:
        mid_point = 0
    
    cmd = ['-y']
    
    # Добавление времени начала для обычных видео
    if not is_gif_input:
        cmd.extend(['-ss', str(mid_point)])
    
    # Входные файлы
    input_files = ['-i', in_path]
    
    if overlay_file and os.path.exists(overlay_file):
        input_files.extend(['-i', overlay_file])
    
    cmd.extend(input_files)
    
    # Построение filter_complex
    filter_complex_parts = []
    main_video_stream_label = '[0:v]'
    
    overlay_stream_label = None
    if overlay_file and os.path.exists(overlay_file):
        overlay_stream_label = '[1:v]'
    
    last_video_node = main_video_stream_label
    node_idx = 0
    
    # Применение фильтра обрезки
    if crop_filter:
        new_node_label = f'[v{node_idx}]'
        filter_complex_parts.append(f'{last_video_node}{crop_filter}{new_node_label}')
        last_video_node = new_node_label
        node_idx += 1
    
    # Настройка целевых размеров
    target_w, target_h = REELS_WIDTH, REELS_HEIGHT
    is_reels_format = output_format == REELS_FORMAT_NAME

    # Форматирование для reels
    if is_reels_format:
        if blur_background:
            # С размытым фоном
            filter_complex_parts.append(
                f'{last_video_node}split[original][original_copy];'
                f'[original_copy]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,'
                f'crop={target_w}:{target_h}:(in_w-{target_w})/2:(in_h-{target_h})/2,'
                f'gblur=sigma=25[bg];'
                f'[original]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg];'
                f'[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2[formatted]'
            )
        else:
            # С черными полосами
            filter_complex_parts.append(
                f'{last_video_node}scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,'
                f'pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black[formatted]'
            )
        last_video_node = '[formatted]'
    
    # Проверка наличия случайного фильтра в списке
    is_random_filter_in_list = 'Случайный фильтр' in filters
    
    # Применение фильтров
    for f_name in filters:
        f_template = FILTERS.get(f_name)
        if not f_template or f_name == 'Нет фильтра':
            continue
        
        # Пропускаем обычные фильтры если есть случайный
        if is_random_filter_in_list and f_name != 'Случайный фильтр':
            continue
        
        final_template = ''
        
        if f_name == 'Случайный фильтр':
            # Для превью используем фиксированный фильтр "Сепия"
            final_template = FILTERS.get('Сепия', '')
        elif f_name == 'Случ. цвет (яркость/контраст/...)':
            # Фиксированные параметры для превью
            br = 0.1
            ct = 1.1
            sat = 1.1
            hue = 2
            final_template = f_template.format(br=br, ct=ct, sat=sat, hue=hue)
        else:
            final_template = f_template
        
        if final_template:
            new_node_label = f'[v{node_idx}]'
            filter_complex_parts.append(f'{last_video_node}{final_template}{new_node_label}')
            last_video_node = new_node_label
            node_idx += 1
    
    # Применение зума
    zoom_factor = zoom_p / 100
    if abs(zoom_factor - 1) > 1e-5:
        if zoom_factor >= 1:
            # Увеличение с последующей обрезкой
            scale_node = f'[v{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(f'{last_video_node}scale=iw*{zoom_factor}:ih*{zoom_factor}:flags=bicubic{scale_node}')
            
            crop_node = f'[v{node_idx}]'
            node_idx += 1
            
            if is_reels_format:
                filter_complex_parts.append(f'{scale_node}crop={target_w}:{target_h}:(in_w-{target_w})/2:(in_h-{target_h})/2{crop_node}')
            else:
                filter_complex_parts.append(f'{scale_node}crop=iw/{zoom_factor}:ih/{zoom_factor}:(in_w-iw/{zoom_factor})/2:(in_h-ih/{zoom_factor})/2{crop_node}')
            
            last_video_node = crop_node
        else:
            # Уменьшение
            scale_node = f'[v{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(f'{last_video_node}scale=iw*{zoom_factor}:ih*{zoom_factor}:flags=bicubic{scale_node}')
            last_video_node = scale_node
    
    # Добавление видео оверлея
    if overlay_stream_label:
        pos_params = OVERLAY_POSITIONS.get(overlay_pos, 'x=(W-w)/2:y=(H-h)/2')
        
        alpha_node = f'[ovl{node_idx}]'
        node_idx += 1
        overlay_node = f'[v{node_idx}]'
        node_idx += 1
        
        filter_complex_parts.append(f'{overlay_stream_label}format=rgba{alpha_node}')
        filter_complex_parts.append(f'{last_video_node}{alpha_node}overlay={pos_params}{overlay_node}')
        last_video_node = overlay_node
    
    # Финальное форматирование
    filter_complex_parts.append(f'{last_video_node}format=rgba[vout]')
    
    # Сборка filter_complex
    fc_string = ';'.join(filter(None, filter_complex_parts))
    
    if fc_string:
        cmd.extend(['-filter_complex', fc_string])
        cmd.extend(['-map', '[vout]'])
    
    # Генерация одного кадра
    cmd.extend(['-vframes', '1'])
    cmd.append(out_path)
    
    # Запуск FFmpeg
    run_ffmpeg(cmd, input_file_for_log=in_path)