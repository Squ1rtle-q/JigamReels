"""
FFmpeg utilities module for video processing.
Модуль утилит для работы с FFmpeg для обработки видео.
"""

import os
import subprocess
import tempfile
import random
import platform
import math
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
        REELS_WIDTH,
        REELS_HEIGHT,
        REELS_FORMAT_NAME,
        VIDEO_EXTENSIONS,
    )
except ImportError:
    # Fallback значения если модуль constants недоступен
    FFMPEG_EXE_PATH = "bin/ffmpeg.exe"
    FILTERS = {}
    REELS_WIDTH = 1080
    REELS_HEIGHT = 1920
    REELS_FORMAT_NAME = f"Reels/TikTok ({REELS_WIDTH}x{REELS_HEIGHT})"
    VIDEO_EXTENSIONS = ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v']


def _overlay_input_should_stream_loop(path: Optional[str]) -> bool:
    """GIF и видео-баннеры зацикливаем под длину основного ролика."""
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext == '.gif':
        return True
    return ext in VIDEO_EXTENSIONS


def _hex_to_chromakey_color(value: Optional[str]) -> str:
    """#RRGGBB → 0xRRGGBB для фильтра chromakey FFmpeg."""
    s = (value or '').strip().lstrip('#')
    if len(s) != 6:
        return '0x00FF00'
    try:
        int(s, 16)
    except ValueError:
        return '0x00FF00'
    return f'0x{s.upper()}'


def _clamp_chromakey_float(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


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
    def _normalize_ffmpeg_path(arg):
        if not isinstance(arg, str):
            return arg

        # Не трогаем ключи параметров
        if arg.startswith('-') and len(arg) > 1 and ' ' not in arg:
            return arg

        # Нормализуем разделители в путях, включая смешанные \ и /
        if re.search(r'[\\/]', arg):
            normalized = arg.replace('\\', '/').replace('//', '/')
            # FFmpeg понимает как обычный путь (не URI), кроме явных file:
            if normalized.startswith('file:'):
                return normalized
            return normalized

        return arg

    normalized_cmd = [_normalize_ffmpeg_path(a) for a in cmd]

    final_cmd = [FFMPEG_PATH_EFFECTIVE]

    # Добавление параметров логирования если их нет
    if '-loglevel' not in normalized_cmd:
        final_cmd.extend(['-loglevel', 'debug'])

    # Добавление прогресса если нужен
    if progress_callback:
        final_cmd.extend(['-progress', 'pipe:1'])

    if '-hide_banner' not in cmd:
        final_cmd.append('-hide_banner')

    # Не превращаем системные Windows пути в 'file:' URI, FFmpeg лучше принимает обычные пути.
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
            tail = '\n'.join(output_lines[-25:])
            error_message = (
                f'FFmpeg failed with exit code {return_code} for file \'{os.path.basename(input_file_for_log)}\'.\n'
                f'Command: {command_for_log}\n'
                f'Last lines of output:\n{tail}'
            )
            logging.error(error_message)
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


def _merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not intervals:
        return []

    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_intervals[0]]

    for start, end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1e-4:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def _parse_silencedetect_output(stderr: str) -> List[Tuple[float, float]]:
    silence_ranges = []
    current_silence_start = None

    for line in stderr.splitlines():
        line = line.strip()
        # FFmpeg silencedetect output contains строку 'silence_start:' и 'silence_end:'
        if 'silence_start:' in line:
            try:
                current_silence_start = float(line.split('silence_start:')[1].strip().split()[0])
            except Exception:
                current_silence_start = None
        elif 'silence_end:' in line:
            try:
                parts = line.split('silence_end:')[1].strip().split()
                silence_end = float(parts[0])
                if current_silence_start is not None and silence_end >= current_silence_start:
                    silence_ranges.append((current_silence_start, silence_end))
                current_silence_start = None
            except Exception:
                continue

    return silence_ranges


def remove_silence_from_video(
    input_path: str,
    output_path: str,
    silence_db: float = -30.0,
    silence_duration: float = 0.5,
    padding: float = 0.1
) -> str:
    """
    Удаляет паузы из видео с помощью FFmpeg silencedetect + trim/atrim/concat.

    Args:
        input_path: Исходный видеофайл.
        output_path: Результат (новый файл).
        silence_db: Уровень тишины в dB (по умолчанию -30dB).
        silence_duration: Минимальная длительность паузы, которую нужно удалить (в секундах).
        padding: Запас вокруг речи (по 0.1 сек до и после каждого сегмента).

    Returns:
        Путь к итоговому файлу.
    """
    import shutil

    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError('input_path и output_path не могут совпадать для удаления тишины')

    duration = get_video_duration(input_path)
    if duration <= 0:
        # fallback: просто копируем файл, если не удалось получить длительность
        shutil.copyfile(input_path, output_path)
        return output_path

    cmd = [
        FFMPEG_PATH_EFFECTIVE,
        '-hide_banner',
        '-nostats',
        '-i', input_path,
        '-af', f'silencedetect=n={silence_db}dB:d={silence_duration}',
        '-f', 'null',
        '-'
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    stderr = proc.stderr or ''

    silences = _parse_silencedetect_output(stderr)

    if not silences:
        # нет тишины для удаления
        shutil.copyfile(input_path, output_path)
        return output_path

    # Формируем сегменты речи
    speak_segments = []
    current = 0.0
    for silence_start, silence_end in silences:
        if silence_start > current + 1e-4:
            speak_segments.append((current, silence_start))
        current = max(current, silence_end)

    if current < duration - 1e-4:
        speak_segments.append((current, duration))

    # Применяем padding и ограничиваем границы
    padded_segments = []
    for start, end in speak_segments:
        start = max(0.0, start - padding)
        end = min(duration, end + padding)
        if end > start + 0.01:
            padded_segments.append((start, end))

    padded_segments = _merge_intervals(padded_segments)

    if not padded_segments:
        # если всё тихо - оставляем первые 0.5 сек
        padded_segments = [(0.0, min(0.5, duration))]

    # Построение filter_complex для соединения сегментов
    filter_parts = []
    concat_labels = []

    for idx, (start, end) in enumerate(padded_segments):
        filter_parts.append(f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{idx}]")
        filter_parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{idx}]")
        concat_labels.append(f"[v{idx}][a{idx}]")

    concat_chain = ''.join(concat_labels) + f"concat=n={len(padded_segments)}:v=1:a=1[outv][outa]"
    filter_complex = ';'.join(filter_parts + [concat_chain])

    ffmpeg_cmd = [
        '-y',
        '-i', input_path,
        '-filter_complex', filter_complex,
        '-map', '[outv]',
        '-map', '[outa]',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-crf', '23',
        '-c:a', 'aac',
        '-b:a', '192k',
        output_path
    ]

    run_ffmpeg(ffmpeg_cmd, input_path)

    return output_path


def _escape_drawtext_text(raw_text: str) -> str:
    escaped = raw_text.replace('\\', '\\\\')
    escaped = escaped.replace(':', '\\:')
    # Используем максимально безопасный вариант: удаляем апострофы из текста.
    escaped = escaped.replace("'", "")
    escaped = escaped.replace('%', '%%')
    escaped = escaped.replace('\n', '\\n')

    # Удаляем скобочные символы, чтобы исключить конфликт с метками фильтров.
    escaped = escaped.replace('[', '').replace(']', '').replace('{', '').replace('}', '')

    return escaped


def render_one_word_animation(
    input_path: str,
    output_path: str,
    words: List[Dict],
    fontfile: str = 'TheBoldFont.ttf',
    base_font_size: int = 70,
    zoom_font_size: int = 90,
    y_offset: int = 200,
    fallback_attempt: bool = False
) -> str:
    """
    Рендерит анимацию субтитров «одно слово на экране» с эффектом zoom.

    Args:
        input_path: исходный видеофайл
        output_path: выходной файл
        words: список слов с таймкодами [{'word': str, 'start': float, 'end': float}, ...]
        fontfile: путь до шрифта
        base_font_size: начальный размер
        zoom_font_size: размер в пике
        y_offset: смещение по вертикали относительно центра

    Returns:
        output_path
    """
    if not words:
        # Нечего рендерить, копируем вход в выход
        shutil.copyfile(input_path, output_path)
        return output_path

    # Ограничение на число фильтров в командной строке
    use_filter_script = len(words) > 80

    def build_filter_chain(words_list):
        chain = ''
        last_used_label = '[0:v]'
        current_out_label = last_used_label
        valid_words = 0

        for item in words_list:
            if not item.get('word') or item.get('start') is None or item.get('end') is None:
                continue

            # текущая входная метка для drawtext
            prev_label = current_out_label

            if prev_label.startswith('[v') and prev_label.endswith(']'):
                try:
                    base_idx = int(prev_label[2:-1])
                except ValueError:
                    base_idx = -1
            else:
                base_idx = -1

            candidate_label = f"[v{base_idx + 1}]"

            start = float(item['start'])
            end = float(item['end'])
            escaped_word = _escape_drawtext_text(item['word']).strip()
            if not escaped_word:
                # пропускаем некорректное/пустое слово и не увеличиваем метку
                continue

            current_out_label = candidate_label

            fontsize_expr = f"if(lt(t,{start + 0.1:.3f}),{base_font_size} + ({zoom_font_size} - {base_font_size})*(t-{start:.3f})/0.1,{zoom_font_size})"
            enable_expr = f"between(t,{start:.3f},{end:.3f})"

            drawtext = (
                f"{prev_label}drawtext=fontfile='{fontfile}':text='{escaped_word}':x=(W-tw)/2:y=(H-th)/2+{y_offset}"
                f":fontcolor=white:borderw=3:bordercolor=black:enable='{enable_expr}'"
                f":fontsize={fontsize_expr}{current_out_label};"
            )
            chain += drawtext

            # обновление метки, которая действительно была создана
            last_used_label = current_out_label
            valid_words += 1

        # если не было успешного добавления drawtext, возвращаем начальную метку
        return chain, last_used_label, valid_words

    filter_complex, final_label, used_words = build_filter_chain(words)

    if final_label == '[0:v]' or used_words == 0:
        # Нечего рендерить в drawtext, просто копируем исходник
        logging.info('render_one_word_animation: слова отсутствуют, копирование input->output')
        shutil.copyfile(input_path, output_path)
        return output_path

    # Явно создаем аудио-лейбл aout для 100% контроля на маппинг
    filter_complex += ';[0:a]anull[aout]'

    if use_filter_script:
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.txt', encoding='utf-8') as ff:
            ff.write(filter_complex)
            filter_script_path = ff.name
        ffmpeg_cmd = [
            '-y',
            '-i', input_path,
            '-filter_script', filter_script_path,
            '-map', final_label,
            '-map', '[aout]',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '192k',
            output_path
        ]
    else:
        ffmpeg_cmd = [
            '-y',
            '-i', input_path,
            '-filter_complex', filter_complex,
            '-map', final_label,
            '-map', '[aout]',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '192k',
            output_path
        ]

    print(f"DEBUG: Words count: {len(words)}, Used words: {used_words}, Final Label: {final_label}")
    print(f"DEBUG: FFmpeg command: {ffmpeg_cmd}")
    logging.debug(f"render_one_word_animation: final map label {final_label}, total words {len(words)}")
    logging.debug(f"render_one_word_animation: ffmpeg_cmd = {ffmpeg_cmd}")

    try:
        run_ffmpeg(ffmpeg_cmd, input_path)
    except Exception as ff_err:
        detail = ''
        if use_filter_script and os.path.exists(filter_script_path):
            try:
                with open(filter_script_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read(500)
                    detail = f"Filter script first 500 chars:\n{content}"
            except Exception as e:
                detail = f"Could not read filter script for debug: {e}"
        else:
            try:
                detail = f"Filter chain first 500 chars:\n{filter_complex[:500]}"
            except Exception as e:
                detail = f"Could not read filter_complex for debug: {e}"

        log_msg = (
            f"render_one_word_animation failed: {ff_err}\n"
            f"Final label: {final_label}\n"
            f"{detail}"
        )

        print(f"WARNING: render_one_word_animation failed (fallback): {ff_err}")
        logging.warning(log_msg)
        if detail:
            print(detail)

        # Если первая попытка упала, делаем второй проход с упрощенными словами (удаляем апострофы).
        if not fallback_attempt:
            logging.info('Retrying render_one_word_animation with apostrophes removed')
            cleaned_words = []
            for item in words:
                if not item.get('word') or item.get('start') is None or item.get('end') is None:
                    cleaned_words.append(item)
                    continue
                cleaned_word = item['word'].replace("'", "")
                cleaned_words.append({**item, 'word': cleaned_word})
            try:
                return render_one_word_animation(
                    input_path,
                    output_path,
                    cleaned_words,
                    fontfile,
                    base_font_size,
                    zoom_font_size,
                    y_offset,
                    fallback_attempt=True
                )
            except Exception as retry_err:
                logging.warning(f"Fallback attempt failed too: {retry_err}")
                print(f"WARNING: render_one_word_animation retry failed: {retry_err}")

        shutil.copyfile(input_path, output_path)
        return output_path
    finally:
        if use_filter_script and os.path.exists(filter_script_path):
            try:
                os.remove(filter_script_path)
            except Exception:
                pass

    return output_path


def detect_viral_moments(path: str, clip_duration: int = 15, max_clips: int = 3) -> List[Tuple[float, float]]:
    """
    Находит самые динамичные/виральные участки по комбинации:
    - плотности смен сцен (scene score)
    - громкости аудио (RMS) и её **изменчивости** (речь/сцены «пульсируют» сильнее, чем ровная музыка в опенинге)
    - штраф за пересечение с типичными зонами **опенинга/титров** в начале и **аутро** в конце

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

    # Типичные опенинги/заставки (быстрая нарезка + музыка без диалога) — первая минута-две и хвост серии.
    opening_guard = min(180.0, max(55.0, duration * 0.035))
    ending_guard = min(150.0, max(45.0, duration * 0.042))

    scored = []
    for start in candidates:
        end = min(start + clip_duration, duration)
        seg_len = end - start
        if seg_len <= 1e-6:
            continue

        scene_score = _scene_density_score(scene_points, start, end)
        audio_energy = _audio_energy_score(audio_levels, start, end)
        audio_dynamics = _audio_dynamics_score(audio_levels, start, end)
        motion_presence = _motion_presence_bonus(scene_points, start, end)

        # Доля окна в «опенинге» / «аутро» (0..1) — мягкий штраф пропорционально перекрытию.
        opening_overlap = max(0.0, min(end, opening_guard) - max(0.0, start))
        opening_overlap = min(opening_overlap, seg_len)
        opening_r = opening_overlap / seg_len

        tail = max(0.0, duration - ending_guard)
        ending_overlap = max(0.0, min(end, duration) - max(start, tail))
        ending_overlap = min(ending_overlap, seg_len)
        ending_r = ending_overlap / seg_len

        edge_penalty = 0.58 * opening_r + 0.52 * ending_r

        # В зоне опенинга много склеек под музыку даёт высокий scene_score — снижаем его вклад.
        scene_eff = scene_score * (1.0 - 0.72 * opening_r)
        scene_eff = max(scene_eff, scene_score * 0.12)

        audio_combined = 0.40 * audio_energy + 0.60 * audio_dynamics

        score = (scene_eff * 0.66) + (audio_combined * 0.20) + (motion_presence * 0.12) - edge_penalty
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


def _audio_dynamics_score(audio_points: List[Tuple[float, float]], start: float, end: float) -> float:
    """
    Оценка «пульсации» RMS в окне (дБ по отсчётам astats).
    Ровная музыкальная подложка в титрах часто даёт низкий разброс; речь и активные сцены — выше.
    """
    if end <= start or not audio_points:
        return 0.0
    levels = [level for t, level in audio_points if start <= t < end]
    n = len(levels)
    if n < 4:
        return 0.35
    mean = sum(levels) / n
    var = sum((x - mean) ** 2 for x in levels) / n
    stdev = math.sqrt(max(0.0, var))
    # Подобрано по dB-шкале RMS_level: ~1–2 — «ровно», 4–7+ — динамичнее
    return max(0.0, min(1.0, (stdev - 0.9) / 5.0))


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
    Конвертирует #RRGGBB в ASS PrimaryColour/OutlineColour: &HAABBGGRR (00 = непрозрачный).
    """
    def _norm_ass_default(d: str) -> str:
        if not d:
            return '&H00FFFFFF'
        s = d.replace('&H', '').replace('&h', '')
        if len(s) == 6:
            return f'&H00{s}'
        if len(s) == 8:
            return f'&H{s}'
        return '&H00FFFFFF'

    if not value:
        return _norm_ass_default(default_ass)
    normalized = value.strip().lstrip('#')
    if len(normalized) != 6:
        return _norm_ass_default(default_ass)
    try:
        r = normalized[0:2]
        g = normalized[2:4]
        b = normalized[4:6]
        return f'&H00{b}{g}{r}'
    except Exception:
        return _norm_ass_default(default_ass)


def _ass_font_size_for_video(font_size_ui: int, video_height: int) -> int:
    """
    Размер из UI в ASS FontSize. Без масштаба малое число (напр. 13) на 1920 даёт
    едва заметные субтитры — libass при PlayRes ≈ высоте кадра трактует FontSize в пикселях.
    """
    h = int(video_height) if video_height and video_height > 0 else int(REELS_HEIGHT)
    try:
        ui = int(font_size_ui)
    except (TypeError, ValueError):
        ui = 36
    scaled = int(round(float(ui) * float(h) / 1080.0))
    return max(14, min(scaled, 220))


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


def _windows_subtitles_fontsdir() -> Optional[str]:
    """Путь к Fonts для опции subtitles=fontsdir (Windows, libass)."""
    if platform.system() != 'Windows':
        return None
    windir = os.environ.get('WINDIR', r'C:\Windows')
    fonts = os.path.join(windir, 'Fonts')
    if not os.path.isdir(fonts):
        return None
    p = os.path.normpath(fonts).replace('\\', '/')
    return p.replace(':', '\\:')


def _build_subtitles_vf(
    srt_path: str,
    force_style: Optional[str] = None,
    video_w: int = 0,
    video_h: int = 0,
) -> str:
    """
    Собирает единый фильтр subtitles=... (без filename= на Windows).
    Для SRT: original_size и charenc помогают libass.
    Для .ass: не задаём charenc UTF-8 и original_size — иначе часть сборок libav/libass
    ломает стиль/цвета из файла и показывает дефолт (белый текст, другие поля).
    """
    inner = _escape_path_for_subtitles_filter(srt_path)
    parts = [f"subtitles='{inner}'"]
    is_ass = str(srt_path).lower().endswith('.ass')
    if force_style:
        fs = _force_style_argument_for_subtitles_filter(force_style)
        parts.append(f"force_style='{fs}'")
    if not is_ass:
        if video_w > 0 and video_h > 0:
            parts.append(f'original_size={int(video_w)}x{int(video_h)}')
        parts.append('charenc=UTF-8')
    fd = _windows_subtitles_fontsdir()
    if fd:
        # Без кавычек FFmpeg на Windows режет значение по ':' после диска (C: → ошибка парсера).
        parts.append(f"fontsdir='{fd}'")
    return ':'.join(parts)


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


def _force_style_argument_for_subtitles_filter(force_style: str) -> str:
    """
    Готовит всю строку force_style к передаче в subtitles=...:force_style='...'.
    Символ & в &HAABBGGRR иначе трактуется парсером фильтров FFmpeg и отрезает хвост
    опций — libass остаётся с дефолтным белым текстом и без части стиля.
    """
    return str(force_style).replace('&', r'\&')


def _parse_crop_wh_from_filter(crop_filter: Optional[str]) -> Optional[Tuple[int, int]]:
    """Из строки вида crop=w:h:x:y извлекает w,h (числа)."""
    if not crop_filter:
        return None
    m = re.search(r'crop=(\d+):(\d+):\d+:\d+', crop_filter.replace(' ', ''))
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None


def reels_letterbox_vertical_inset_px(
    src_w: int,
    src_h: int,
    crop_filter: Optional[str],
    zoom_p: int,
) -> int:
    """
    Высота нижней «лишней» зоны (размытие/чёрное) между низом чёткого видео и
    низом кадра Reels 9:16 после scale=decrease + overlay/pad и зума с
    центральным crop (как в process_single для is_reels_format).

    Нужна, чтобы Alignment «внизу» не опирался на низ всего 1920px.
    """
    target_w, target_h = float(REELS_WIDTH), float(REELS_HEIGHT)
    h_out = target_h
    if src_w <= 0 or src_h <= 0:
        return 0
    parsed = _parse_crop_wh_from_filter(crop_filter)
    if parsed:
        eff_w, eff_h = float(parsed[0]), float(parsed[1])
    else:
        eff_w, eff_h = float(src_w), float(src_h)
    if eff_w <= 0 or eff_h <= 0:
        return 0
    scale = min(target_w / eff_w, target_h / eff_h)
    fg_h = eff_h * scale  # высота вписанного видео до зума (как [fg] / pad)
    z = max(zoom_p / 100.0, 0.01)
    # Высота видимой нижней полосы в пикселях выхода (см. центральный crop после scale=z).
    bottom_extra = (h_out / 2.0) * max(0.0, 1.0 - (z * fg_h / h_out))
    return int(max(0, round(bottom_extra)))


def reels_preview_bars_heights(
    src_w: int,
    src_h: int,
    crop_filter: Optional[str],
    zoom_p: int,
) -> Tuple[float, float, int]:
    """
    Для превью в UI: высота вписанного видео fg_h, высота верхней/нижней полосы bar
    (до зума, симметрично), и нижняя компенсация субтитров (как reels_letterbox_vertical_inset_px).
    """
    target_w, target_h = float(REELS_WIDTH), float(REELS_HEIGHT)
    if src_w <= 0 or src_h <= 0:
        return target_h, 0.0, 0
    parsed = _parse_crop_wh_from_filter(crop_filter)
    if parsed:
        eff_w, eff_h = float(parsed[0]), float(parsed[1])
    else:
        eff_w, eff_h = float(src_w), float(src_h)
    if eff_w <= 0 or eff_h <= 0:
        return target_h, 0.0, 0
    scale = min(target_w / eff_w, target_h / eff_h)
    fg_h = eff_h * scale
    bar = (target_h - fg_h) / 2.0
    inset = reels_letterbox_vertical_inset_px(src_w, src_h, crop_filter, zoom_p)
    return fg_h, bar, inset


def build_overlay_position_params(
    alignment: int,
    margin_v: int,
    margin_lr: int,
) -> str:
    """
    Строка x=...:y=... для фильтра overlay (сетка 1..9, как ASS / субтитры в UI).
    margin_v: для низа — отступ от нижнего края кадра; для верха — от верхнего;
              для середины по вертикали — сдвиг относительно центра.
    margin_lr: отступ от левого/правого края.
    """
    try:
        a = int(alignment)
    except (TypeError, ValueError):
        a = 5
    a = max(1, min(9, a))
    try:
        mv = int(margin_v)
    except (TypeError, ValueError):
        mv = 0
    try:
        mlr = max(0, int(margin_lr))
    except (TypeError, ValueError):
        mlr = 0
    if a in (1, 2, 3):
        yexpr = f'H-h-{mv}'
    elif a in (7, 8, 9):
        yexpr = f'{mv}'
    else:
        yexpr = f'(H-h)/2+{mv}'
    if a in (1, 4, 7):
        xexpr = f'{mlr}'
    elif a in (2, 5, 8):
        xexpr = '(W-w)/2'
    else:
        xexpr = f'W-w-{mlr}'
    return f'x={xexpr}:y={yexpr}'


def _subtitle_layout_from_style(subtitle_style: Optional[Dict]) -> Tuple[int, int, int, int]:
    """
    ASS Alignment (1..9), MarginL, MarginR, MarginV из словаря настроек субтитров.
    """
    st = subtitle_style or {}
    try:
        alignment = int(st.get('alignment', 2))
    except (TypeError, ValueError):
        alignment = 2
    if alignment < 1 or alignment > 9:
        alignment = 2
    try:
        margin_lr = max(0, int(st.get('margin_lr', 25)))
    except (TypeError, ValueError):
        margin_lr = 25
    try:
        margin_v = int(st.get('margin_v', 105))
    except (TypeError, ValueError):
        margin_v = 105
    try:
        letterbox_inset = max(0, int(st.get('reels_letterbox_inset', 0)))
    except (TypeError, ValueError):
        letterbox_inset = 0
    if letterbox_inset > 0:
        if alignment in (1, 2, 3, 7, 8, 9):
            margin_v += letterbox_inset
    return alignment, margin_lr, margin_lr, margin_v


def _subtitle_effective_outline(subtitle_style: Optional[Dict]) -> int:
    st = subtitle_style or {}
    try:
        outline_size = int(st.get('outline', 2))
    except (TypeError, ValueError):
        outline_size = 2
    outline_mode = (st.get('outline_mode', 'снаружи') or 'снаружи').lower()
    if outline_mode == 'снаружи' and outline_size > 0:
        return max(1, outline_size + 1)
    return outline_size


def _srt_timestamp_to_seconds(ts: str) -> float:
    """HH:MM:SS,mmm → секунды."""
    ts = ts.strip()
    hh_mm_ss, ms_part = ts.split(',')
    hh, mm, ss = hh_mm_ss.split(':')
    ms = int(ms_part.ljust(3, '0')[:3])
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + ms / 1000.0


def _seconds_to_ass_timestamp(sec: float) -> str:
    """Секунды → H:MM:SS.cc (сантисекунды), как в ASS."""
    sec = max(0.0, float(sec))
    cs_total = int(round(sec * 100.0))
    h = cs_total // 360000
    cs_total %= 360000
    m = cs_total // 6000
    cs_total %= 6000
    s = cs_total // 100
    cc = cs_total % 100
    return f'{h:d}:{m:02d}:{s:02d}.{cc:02d}'


def _escape_ass_dialogue_text(text: str) -> str:
    """Экранирование текста в поле Dialogue (ASS)."""
    t = text.replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}')
    t = t.replace('\r\n', '\n').replace('\r', '\n')
    t = t.replace('\n', '\\N')
    t = t.replace(',', '\uFF0C')  # запятая как разделитель поля ASS
    return t


def write_styled_ass_from_srt(
    srt_path: str,
    ass_path: str,
    subtitle_style: Optional[Dict],
    playres_w: int,
    playres_h: int,
) -> None:
    """
    Полный ASS со стилем (обходит глючный на Windows парсинг subtitles:force_style=).
    """
    with open(srt_path, 'r', encoding='utf-8-sig') as f:
        raw = f.read().strip()
    if not raw:
        raise ValueError(f'Empty SRT: {srt_path}')

    st = dict(subtitle_style) if subtitle_style else {}
    font_name = (st.get('font_name') or 'Arial').replace('\n', ' ').replace('\r', ' ')
    font_name = font_name.replace(',', ' ').strip() or 'Arial'
    try:
        fs_ui = int(st.get('font_size', 36))
    except (TypeError, ValueError):
        fs_ui = 36
    font_size = _ass_font_size_for_video(fs_ui, playres_h)

    bold = -1 if st.get('font_bold') else 0
    italic = -1 if st.get('font_italic') else 0
    underline = -1 if st.get('font_underline') else 0

    primary = _hex_to_ass_color(st.get('text_color', '#FFFFFF'), '&H00FFFFFF')
    outline_c = _hex_to_ass_color(st.get('outline_color', '#000000'), '&H00000000')
    secondary = '&H00000000'
    back = '&H00000000'

    align, ml, mr, mv = _subtitle_layout_from_style(st)
    outline = int(_subtitle_effective_outline(st))

    fn_field = font_name.replace('"', '')
    if ',' in fn_field:
        fn_field = f'"{fn_field}"'

    events_lines: List[str] = []
    for block in raw.split('\n\n'):
        block = block.strip()
        if not block:
            continue
        lines = [ln.replace('\r', '') for ln in block.split('\n')]
        if len(lines) < 2:
            continue
        timing_line = lines[1].strip()
        if '-->' not in timing_line:
            continue
        left, right = timing_line.split('-->', 1)
        try:
            t0 = _srt_timestamp_to_seconds(left.strip())
            t1 = _srt_timestamp_to_seconds(right.strip())
        except (ValueError, IndexError):
            continue
        body = '\n'.join(lines[2:]).strip()
        if not body:
            continue
        etext = _escape_ass_dialogue_text(body)
        events_lines.append(
            f'Dialogue: 0,{_seconds_to_ass_timestamp(t0)},{_seconds_to_ass_timestamp(t1)},'
            f'Default,,0,0,0,,{etext}\n'
        )

    if not events_lines:
        raise ValueError(f'No subtitle cues parsed from SRT: {srt_path}')

    pw = max(1, int(playres_w))
    ph = max(1, int(playres_h))

    header = (
        '[Script Info]\n'
        'Title: ReelsMakerPro\n'
        'ScriptType: v4.00+\n'
        'WrapStyle: 0\n'
        'ScaledBorderAndShadow: yes\n'
        f'PlayResX: {pw}\n'
        f'PlayResY: {ph}\n'
        '\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, '
        'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, '
        'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: Default,{fn_field},{font_size},{primary},{secondary},{outline_c},{back},'
        f'{bold},{italic},{underline},0,100,100,0,0,1,{outline},0,{align},{ml},{mr},{mv},1\n'
        '\n'
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )

    with open(ass_path, 'w', encoding='utf-8-sig') as out:
        out.write(header)
        out.writelines(events_lines)


def process_single(
    in_path: str,
    out_path: str,
    filters: List[str],
    zoom_p: int,
    speed_p: int,
    overlay_file: Optional[str] = None,
    overlay_alignment: int = 5,
    overlay_margin_v: int = 0,
    overlay_margin_lr: int = 0,
    overlay_scale_p: int = 100,
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
    plain_subtitles: bool = False,
    overlay_chromakey: bool = False,
    overlay_chromakey_color: str = '#00FF00',
    overlay_chromakey_similarity: float = 0.15,
    overlay_chromakey_blend: float = 0.08,
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
        overlay_alignment: Сетка 1..9 (как у субтитров)
        overlay_margin_v: Отступ сверху/снизу (пиксели)
        overlay_margin_lr: Отступ слева/справа
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
    overlay_loop = bool(overlay_file and _overlay_input_should_stream_loop(overlay_file))
    
    cmd = []
    input_streams = []
    subtitle_ass_cleanup: List[str] = []

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
        
        if overlay_loop:
            cmd.extend(['-stream_loop', '-1', '-i', overlay_file])
        else:
            cmd.extend(['-i', overlay_file])
        
        input_streams.append({'type': 'overlay', 'index': overlay_input_index, 'path': overlay_file})
        overlay_stream_label = f'[{overlay_input_index}:v]'
    else:
        overlay_loop = False
    
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
    
    # Размер кадра для libass (SRT без PlayRes)
    _sub_vw, _sub_vh = (REELS_WIDTH, REELS_HEIGHT) if is_reels_format else get_video_dimensions(in_path)
    if _sub_vw <= 0 or _sub_vh <= 0:
        _sub_vw, _sub_vh = REELS_WIDTH, REELS_HEIGHT

    # Добавление субтитров
    if srt_path:
        if os.path.exists(srt_path):
            if plain_subtitles:
                new_node_label = f'[v{node_idx}]'
                node_idx += 1
                filter_complex_parts.append(
                    f"{last_video_node}{_build_subtitles_vf(srt_path, video_w=_sub_vw, video_h=_sub_vh)}{new_node_label}"
                )
                last_video_node = new_node_label
            else:
                ass_fd, ass_path = tempfile.mkstemp(suffix='.ass', text=True)
                os.close(ass_fd)
                try:
                    write_styled_ass_from_srt(
                        srt_path, ass_path, subtitle_style or {}, _sub_vw, _sub_vh
                    )
                except Exception:
                    try:
                        os.remove(ass_path)
                    except OSError:
                        pass
                    raise
                subtitle_ass_cleanup.append(ass_path)
                new_node_label = f'[v{node_idx}]'
                node_idx += 1
                filter_complex_parts.append(
                    f"{last_video_node}{_build_subtitles_vf(ass_path, video_w=_sub_vw, video_h=_sub_vh)}{new_node_label}"
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
    
    # Добавление видео оверлея (картинка / GIF / видео MP4-MOV с опц. chromakey)
    if overlay_stream_label:
        try:
            o_al = int(overlay_alignment)
        except (TypeError, ValueError):
            o_al = 5
        try:
            o_mv = int(overlay_margin_v)
        except (TypeError, ValueError):
            o_mv = 0
        try:
            o_mlr = int(overlay_margin_lr)
        except (TypeError, ValueError):
            o_mlr = 0
        mv_eff = o_mv
        if is_reels_format and o_al in (1, 2, 3, 7, 8, 9):
            _sw, _sh = get_video_dimensions(in_path)
            if _sw <= 0 or _sh <= 0:
                _sw, _sh = REELS_WIDTH, REELS_HEIGHT
            _lb = reels_letterbox_vertical_inset_px(_sw, _sh, crop_filter, zoom_p)
            if _lb > 0:
                mv_eff = o_mv + _lb
        pos_params = build_overlay_position_params(o_al, mv_eff, o_mlr)
        ovl_cur = overlay_stream_label
        if overlay_chromakey:
            ck = _hex_to_chromakey_color(overlay_chromakey_color)
            sim = _clamp_chromakey_float(overlay_chromakey_similarity, 0.01, 0.8)
            bl = _clamp_chromakey_float(overlay_chromakey_blend, 0.0, 0.5)
            ovl_ck = f'[ovl_ck{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(
                f'{ovl_cur}chromakey={ck}:{sim}:{bl}{ovl_ck}'
            )
            ovl_cur = ovl_ck
        ovl_fmt = f'[ovl_fmt{node_idx}]'
        node_idx += 1
        filter_complex_parts.append(f'{ovl_cur}format=rgba{ovl_fmt}')
        ovl_ready = ovl_fmt
        try:
            scale_ov = int(overlay_scale_p)
        except (TypeError, ValueError):
            scale_ov = 100
        sf = max(0.05, min(5.0, scale_ov / 100.0))
        if abs(sf - 1.0) > 1e-4:
            ovl_sc = f'[ovl_sc{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(
                f'{ovl_fmt}scale=iw*{sf}:ih*{sf}:flags=bicubic{ovl_sc}'
            )
            ovl_ready = ovl_sc
        overlay_node = f'[v{node_idx}]'
        node_idx += 1
        filter_complex_parts.append(
            f'{last_video_node}{ovl_ready}overlay={pos_params}:shortest=1{overlay_node}'
        )
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
    
    # Генерируем случайный битрейт от 4000k до 5000k для изменения хеша файла
    random_bitrate = random.randint(4000, 5000)
    
    if 'nvenc' in codec or 'amf' in codec:
        cmd.extend(['-b:v', f'{random_bitrate}k', '-cq', '24'])
    elif 'qsv' in codec:
        cmd.extend(['-b:v', f'{random_bitrate}k', '-global_quality', '24'])
    else:
        cmd.extend(['-preset', 'veryfast', '-b:v', f'{random_bitrate}k'])
    
    # Удаление метаданных (очистка для изменения хеша)
    cmd.extend(['-map_metadata', '-1', '-map_chapters', '-1'])
    
    # Дополнительные параметры
    if not is_gif_input and not overlay_audio_path:
        cmd.append('-shortest')
    
    # Финальная команда
    final_cmd = ['-y'] + cmd
    final_cmd.append(out_path)
    
    # Запуск FFmpeg
    duration = trim_duration if trim_duration and trim_duration > 0 else get_video_duration(in_path)
    try:
        run_ffmpeg(
            final_cmd,
            input_file_for_log=in_path,
            duration=duration,
            progress_callback=progress_callback,
        )
    finally:
        for _ap in subtitle_ass_cleanup:
            try:
                if _ap and os.path.isfile(_ap):
                    os.remove(_ap)
            except OSError:
                pass


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

    vw, vh = get_video_dimensions(in_path)
    if vw <= 0 or vh <= 0:
        vw, vh = int(REELS_WIDTH), int(REELS_HEIGHT)

    sub_path_for_filter = srt_path
    ass_tmp: Optional[str] = None
    if not plain_subtitles:
        ass_fd, ass_path = tempfile.mkstemp(suffix='.ass', text=True)
        os.close(ass_fd)
        ass_tmp = ass_path
        try:
            write_styled_ass_from_srt(
                srt_path, ass_path, subtitle_style or {}, vw, vh
            )
        except Exception:
            try:
                os.remove(ass_path)
            except OSError:
                pass
            raise
        sub_path_for_filter = ass_path

    try:
        vf_filter = _build_subtitles_vf(
            sub_path_for_filter, force_style=None, video_w=vw, video_h=vh
        )
        if codec and ('nvenc' in codec or 'amf' in codec or 'qsv' in codec):
            vf_filter = f'{vf_filter},format=yuv420p'

        cmd = ['-y', '-i', in_path, '-vf', vf_filter, '-c:v', codec]

        if 'nvenc' in codec or 'amf' in codec:
            cmd.extend(['-cq', '24', '-pix_fmt', 'yuv420p'])
        elif 'qsv' in codec:
            cmd.extend(['-global_quality', '24', '-pix_fmt', 'yuv420p'])
        else:
            cmd.extend(['-preset', 'veryfast', '-crf', '24'])

        cmd.extend(['-c:a', 'copy', out_path])
        run_ffmpeg(
            cmd,
            input_file_for_log=in_path,
            duration=get_video_duration(in_path),
            progress_callback=None,
        )
        logging.info(
            'Subtitle postprocess OK (%s).',
            'plain' if plain_subtitles else 'styled',
        )
    finally:
        if ass_tmp and os.path.isfile(ass_tmp):
            try:
                os.remove(ass_tmp)
            except OSError:
                pass


def _build_preview_filter_complex(
    in_path: str,
    filters: List[str],
    zoom_p: int,
    overlay_file: Optional[str],
    overlay_alignment: int,
    overlay_margin_v: int,
    overlay_margin_lr: int,
    overlay_scale_p: int,
    output_format: str,
    blur_background: bool,
    crop_filter: Optional[str],
    overlay_chromakey: bool,
    overlay_chromakey_color: str,
    overlay_chromakey_similarity: float,
    overlay_chromakey_blend: float,
    video_pixel_format: str,
) -> str:
    """
    Общий filter_complex для still/clip превью. video_pixel_format: 'rgba' | 'yuv420p'.
    """
    filter_complex_parts: List[str] = []
    main_video_stream_label = '[0:v]'
    overlay_stream_label = '[1:v]' if overlay_file and os.path.exists(overlay_file) else None

    last_video_node = main_video_stream_label
    node_idx = 0

    if crop_filter:
        new_node_label = f'[v{node_idx}]'
        filter_complex_parts.append(f'{last_video_node}{crop_filter}{new_node_label}')
        last_video_node = new_node_label
        node_idx += 1

    target_w, target_h = REELS_WIDTH, REELS_HEIGHT
    is_reels_format = output_format == REELS_FORMAT_NAME

    if is_reels_format:
        if blur_background:
            filter_complex_parts.append(
                f'{last_video_node}split[original][original_copy];'
                f'[original_copy]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,'
                f'crop={target_w}:{target_h}:(in_w-{target_w})/2:(in_h-{target_h})/2,'
                f'gblur=sigma=25[bg];'
                f'[original]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg];'
                f'[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2[formatted]'
            )
        else:
            filter_complex_parts.append(
                f'{last_video_node}scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,'
                f'pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black[formatted]'
            )
        last_video_node = '[formatted]'

    is_random_filter_in_list = 'Случайный фильтр' in filters

    for f_name in filters:
        f_template = FILTERS.get(f_name)
        if not f_template or f_name == 'Нет фильтра':
            continue
        if is_random_filter_in_list and f_name != 'Случайный фильтр':
            continue
        final_template = ''
        if f_name == 'Случайный фильтр':
            final_template = FILTERS.get('Сепия', '')
        elif f_name == 'Случ. цвет (яркость/контраст/...)':
            br, ct, sat, hue = 0.1, 1.1, 1.1, 2
            final_template = f_template.format(br=br, ct=ct, sat=sat, hue=hue)
        else:
            final_template = f_template
        if final_template:
            new_node_label = f'[v{node_idx}]'
            filter_complex_parts.append(f'{last_video_node}{final_template}{new_node_label}')
            last_video_node = new_node_label
            node_idx += 1

    zoom_factor = zoom_p / 100
    if abs(zoom_factor - 1) > 1e-5:
        if zoom_factor >= 1:
            scale_node = f'[v{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(
                f'{last_video_node}scale=iw*{zoom_factor}:ih*{zoom_factor}:flags=bicubic{scale_node}'
            )
            crop_node = f'[v{node_idx}]'
            node_idx += 1
            if is_reels_format:
                filter_complex_parts.append(
                    f'{scale_node}crop={target_w}:{target_h}:(in_w-{target_w})/2:(in_h-{target_h})/2{crop_node}'
                )
            else:
                filter_complex_parts.append(
                    f'{scale_node}crop=iw/{zoom_factor}:ih/{zoom_factor}:'
                    f'(in_w-iw/{zoom_factor})/2:(in_h-ih/{zoom_factor})/2{crop_node}'
                )
            last_video_node = crop_node
        else:
            scale_node = f'[v{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(
                f'{last_video_node}scale=iw*{zoom_factor}:ih*{zoom_factor}:flags=bicubic{scale_node}'
            )
            last_video_node = scale_node

    if overlay_stream_label:
        try:
            _oa = int(overlay_alignment)
        except (TypeError, ValueError):
            _oa = 5
        try:
            _omv = int(overlay_margin_v)
        except (TypeError, ValueError):
            _omv = 0
        try:
            _omlr = int(overlay_margin_lr)
        except (TypeError, ValueError):
            _omlr = 0
        _mv_eff = _omv
        if is_reels_format and _oa in (1, 2, 3, 7, 8, 9):
            _psw, _psh = get_video_dimensions(in_path)
            if _psw <= 0 or _psh <= 0:
                _psw, _psh = REELS_WIDTH, REELS_HEIGHT
            _plb = reels_letterbox_vertical_inset_px(_psw, _psh, crop_filter, zoom_p)
            if _plb > 0:
                _mv_eff = _omv + _plb
        pos_params = build_overlay_position_params(_oa, _mv_eff, _omlr)
        ovl_cur = overlay_stream_label
        if overlay_chromakey:
            ck = _hex_to_chromakey_color(overlay_chromakey_color)
            sim = _clamp_chromakey_float(overlay_chromakey_similarity, 0.01, 0.8)
            bl = _clamp_chromakey_float(overlay_chromakey_blend, 0.0, 0.5)
            ovl_ck = f'[pr_ovl_ck{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(
                f'{ovl_cur}chromakey={ck}:{sim}:{bl}{ovl_ck}'
            )
            ovl_cur = ovl_ck
        ovl_fmt = f'[ovl_fmt{node_idx}]'
        node_idx += 1
        filter_complex_parts.append(f'{ovl_cur}format=rgba{ovl_fmt}')
        ovl_ready = ovl_fmt
        try:
            scale_ov = int(overlay_scale_p)
        except (TypeError, ValueError):
            scale_ov = 100
        sf = max(0.05, min(5.0, scale_ov / 100.0))
        if abs(sf - 1.0) > 1e-4:
            ovl_sc = f'[ovl_sc{node_idx}]'
            node_idx += 1
            filter_complex_parts.append(
                f'{ovl_fmt}scale=iw*{sf}:ih*{sf}:flags=bicubic{ovl_sc}'
            )
            ovl_ready = ovl_sc
        overlay_node = f'[v{node_idx}]'
        node_idx += 1
        filter_complex_parts.append(
            f'{last_video_node}{ovl_ready}overlay={pos_params}:shortest=1{overlay_node}'
        )
        last_video_node = overlay_node

    vfmt = 'rgba' if video_pixel_format == 'rgba' else 'yuv420p'
    filter_complex_parts.append(f'{last_video_node}format={vfmt}[vout]')
    return ';'.join(filter(None, filter_complex_parts))


def generate_preview(
    in_path: str,
    out_path: str,
    filters: List[str],
    zoom_p: int,
    overlay_file: Optional[str] = None,
    overlay_alignment: int = 5,
    overlay_margin_v: int = 0,
    overlay_margin_lr: int = 0,
    overlay_scale_p: int = 100,
    output_format: str = "jpg",
    blur_background: bool = False,
    crop_filter: Optional[str] = None,
    overlay_chromakey: bool = False,
    overlay_chromakey_color: str = '#00FF00',
    overlay_chromakey_similarity: float = 0.15,
    overlay_chromakey_blend: float = 0.08,
) -> None:
    """
    Генерация превью (одного кадра) из видео с применением эффектов.
    
    Args:
        in_path: Путь к входному видеофайлу
        out_path: Путь к выходному файлу изображения
        filters: Список названий фильтров для применения
        zoom_p: Процент увеличения (100 = без изменений)
        overlay_file: Путь к файлу оверлея
        overlay_alignment: Сетка 1..9
        overlay_margin_v: Отступ сверху/снизу
        overlay_margin_lr: Отступ слева/справа
        output_format: Формат выходного файла
        blur_background: Размытие фона для формата reels
        crop_filter: Фильтр обрезки
    """
    is_gif_input = in_path.lower().endswith('.gif')

    duration = get_video_duration(in_path)
    if duration > 0 and not is_gif_input:
        mid_point = duration / 2
    else:
        mid_point = 0

    cmd = ['-y']
    if not is_gif_input:
        cmd.extend(['-ss', str(mid_point)])

    input_files = ['-i', in_path]
    if overlay_file and os.path.exists(overlay_file):
        if _overlay_input_should_stream_loop(overlay_file):
            input_files.extend(['-stream_loop', '-1', '-i', overlay_file])
        else:
            input_files.extend(['-i', overlay_file])
    cmd.extend(input_files)

    fc_string = _build_preview_filter_complex(
        in_path,
        filters,
        zoom_p,
        overlay_file,
        overlay_alignment,
        overlay_margin_v,
        overlay_margin_lr,
        overlay_scale_p,
        output_format,
        blur_background,
        crop_filter,
        overlay_chromakey,
        overlay_chromakey_color,
        overlay_chromakey_similarity,
        overlay_chromakey_blend,
        'rgba',
    )
    cmd.extend(['-filter_complex', fc_string])
    cmd.extend(['-map', '[vout]'])

    cmd.extend(['-frames:v', '1', '-update', '1'])
    cmd.append(out_path)

    run_ffmpeg(cmd, input_file_for_log=in_path)


def generate_preview_clip(
    in_path: str,
    out_path: str,
    filters: List[str],
    zoom_p: int,
    overlay_file: Optional[str] = None,
    overlay_alignment: int = 5,
    overlay_margin_v: int = 0,
    overlay_margin_lr: int = 0,
    overlay_scale_p: int = 100,
    output_format: str = "jpg",
    blur_background: bool = False,
    crop_filter: Optional[str] = None,
    overlay_chromakey: bool = False,
    overlay_chromakey_color: str = '#00FF00',
    overlay_chromakey_similarity: float = 0.15,
    overlay_chromakey_blend: float = 0.08,
    max_seconds: float = 8.0,
) -> None:
    """
    Короткий превью-клип с начала ролика (для анимированного баннера). Без аудио, H.264.
    """
    is_gif_input = in_path.lower().endswith('.gif')
    duration = get_video_duration(in_path)
    try:
        cap = float(max_seconds)
    except (TypeError, ValueError):
        cap = 8.0
    cap = max(0.5, min(30.0, cap))
    if duration > 0:
        clip_len = min(cap, duration)
    else:
        clip_len = cap

    cmd = ['-y']
    # С начала файла (без seek в середину)
    input_files = ['-i', in_path]
    if overlay_file and os.path.exists(overlay_file):
        if _overlay_input_should_stream_loop(overlay_file):
            input_files.extend(['-stream_loop', '-1', '-i', overlay_file])
        else:
            input_files.extend(['-i', overlay_file])
    cmd.extend(input_files)

    fc_string = _build_preview_filter_complex(
        in_path,
        filters,
        zoom_p,
        overlay_file,
        overlay_alignment,
        overlay_margin_v,
        overlay_margin_lr,
        overlay_scale_p,
        output_format,
        blur_background,
        crop_filter,
        overlay_chromakey,
        overlay_chromakey_color,
        overlay_chromakey_similarity,
        overlay_chromakey_blend,
        'yuv420p',
    )
    cmd.extend(['-filter_complex', fc_string])
    cmd.extend(['-map', '[vout]'])
    cmd.extend([
        '-t', f'{clip_len:.3f}',
        '-an',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-crf', '23',
        '-pix_fmt', 'yuv420p',
    ])
    cmd.append(out_path)

    run_ffmpeg(cmd, input_file_for_log=in_path)