"""
Subtitle utilities module for generating subtitles using Whisper AI.
Модуль утилит для генерации субтитров с использованием Whisper AI.
"""

import os
import datetime
import subprocess
import sys
import random
import re
from typing import Optional, List, Dict
from utils.ffmpeg_utils import run_ffmpeg

LANGUAGE_NAME_TO_CODE = {
    'russian': 'ru',
    'english': 'en',
    'ukrainian': 'uk',
    'german': 'de',
    'french': 'fr',
    'spanish': 'es',
    'italian': 'it',
}


def extract_audio(
    video_path: str,
    audio_path: str,
    trim_start: Optional[float] = None,
    trim_duration: Optional[float] = None
) -> None:
    """
    Извлекает аудиодорожку из видеофайла в формате WAV для обработки в Whisper.
    
    Args:
        video_path: Путь к входному видеофайлу
        audio_path: Путь для сохранения извлеченного аудиофайла
    """
    cmd = ['-y']  # Перезаписать выходной файл если существует
    if trim_start is not None and trim_start > 0:
        cmd.extend(['-ss', f'{trim_start:.3f}'])

    cmd.extend(['-i', video_path])  # Входной видеофайл

    if trim_duration is not None and trim_duration > 0:
        cmd.extend(['-t', f'{trim_duration:.3f}'])

    cmd.extend([
        '-vn',               # Отключить видео (только аудио)
        '-ar', '16000',      # Частота дискретизации 16kHz (оптимально для Whisper)
        '-ac', '1',          # Моно (1 канал)
        '-c:a', 'pcm_s16le', # Кодек PCM 16-bit little endian
        audio_path           # Выходной аудиофайл
    ])
    
    run_ffmpeg(cmd, video_path)


def _format_time(seconds: float) -> str:
    """
    Форматирует время в секундах в формат SRT (HH:MM:SS,mmm).
    
    Args:
        seconds: Время в секундах (может быть дробным)
        
    Returns:
        Отформатированная строка времени в формате SRT
    """
    # Разделяем на часы, минуты и секунды
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    
    # Вычисляем миллисекунды
    ms = int((s - int(s)) * 1000)
    
    # Форматируем в строку HH:MM:SS,mmm
    return f"{int(h):02}:{int(m):02}:{int(s):02},{ms:03}"


def _parse_srt_time(time_str: str) -> float:
    """Парсит SRT время HH:MM:SS,mmm в секунды."""
    time_str = time_str.strip()
    hh_mm_ss, ms = time_str.split(',')
    hh, mm, ss = hh_mm_ss.split(':')
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def build_segment_srt(
    source_srt_path: str,
    out_srt_path: str,
    segment_start: float,
    segment_duration: float
) -> str:
    """
    Строит SRT для вырезанного сегмента, сдвигая тайминги к 00:00:00,000.
    """
    if not os.path.exists(source_srt_path):
        return source_srt_path

    segment_end = segment_start + segment_duration

    with open(source_srt_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    blocks = [b for b in content.split('\n\n') if b.strip()]
    new_blocks = []
    idx = 1

    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue

        timing_line = lines[1].strip()
        if '-->' not in timing_line:
            continue

        start_raw, end_raw = [x.strip() for x in timing_line.split('-->')]
        start_s = _parse_srt_time(start_raw)
        end_s = _parse_srt_time(end_raw)

        # Оставляем только пересекающиеся тайминги.
        if end_s <= segment_start or start_s >= segment_end:
            continue

        clipped_start = max(start_s, segment_start) - segment_start
        clipped_end = min(end_s, segment_end) - segment_start

        text_lines = lines[2:]
        if not text_lines:
            continue

        new_block = [
            str(idx),
            f'{_format_time(clipped_start)} --> {_format_time(clipped_end)}',
            *text_lines
        ]
        new_blocks.append('\n'.join(new_block))
        idx += 1

    with open(out_srt_path, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(new_blocks))

    return out_srt_path


def generate_srt_from_whisper(
    audio_path: str,
    srt_path: str,
    model_name: str,
    language: str,
    words_per_line: int,
    censor_words: Optional[List[str]] = None,
    _retry_with_openai: bool = False
) -> str:
    """
    Генерирует SRT файл субтитров из аудиофайла используя Whisper AI.
    
    Args:
        audio_path: Путь к аудиофайлу для транскрипции
        srt_path: Путь для сохранения SRT файла
        model_name: Название модели Whisper (tiny, base, small, medium, large)
        language: Язык для распознавания ("Auto-detect" для автоопределения)
        words_per_line: Количество слов в одной строке субтитров
        censor_words: Опциональный список слов для цензурирования
        
    Returns:
        Путь к созданному SRT файлу
        
    Raises:
        RuntimeError: Если не удалось загрузить модель Whisper
    """
    # Количество слов, которые показываются в одном "шаге" субтитра.
    words_per_line = max(1, int(words_per_line or 1))
    censor_words = censor_words or []

    # Определяем язык для транскрипции
    if language != 'Auto-detect':
        lang_code = LANGUAGE_NAME_TO_CODE.get(language.lower(), language.lower())
    else:
        lang_code = None

    print(f"Loading transcription model '{model_name}' (language={language}, code={lang_code})...")

    # Сначала пытаемся использовать faster-whisper (качественнее/стабильнее таймкоды),
    # затем fallback на openai-whisper.
    normalized_segments = _transcribe_with_best_available_backend(
        audio_path=audio_path,
        model_name=model_name,
        language_code=lang_code
    )

    def contains_cyrillic(text: str) -> bool:
        return bool(re.search(r'[а-яА-Я]', text))

    if lang_code == 'ru' and not _retry_with_openai:
        total_text = ' '.join((seg.get('text') or '') for seg in normalized_segments)
        if total_text and not contains_cyrillic(total_text):
            print('Warning: requested Russian transcription, but result has no Cyrillic characters. Retrying via openai-whisper...')
            try:
                normalized_segments = _transcribe_with_openai_whisper(audio_path, model_name, lang_code)
                total_text2 = ' '.join((seg.get('text') or '') for seg in normalized_segments)
                if total_text2 and contains_cyrillic(total_text2):
                    print('openai-whisper returned Cyrillic text as expected.')
                else:
                    print('openai-whisper did not return Cyrillic text either; using initial result.')
            except Exception as e:
                print(f'Warning: openai-whisper retry failed: {e}. Using initial transcription.')

    print('Transcription finished. Generating SRT file...')

    # Генерируем содержимое SRT файла.
    srt_content = ''
    sub_index = 1

    for segment in normalized_segments:
        words = segment.get('words') or []
        if not words:
            # Для fallback-веток без word timestamps разбиваем фразу по словам
            # и равномерно делим длительность сегмента, чтобы текст появлялся поочередно.
            seg_start = float(segment.get('start', 0.0))
            seg_end = float(segment.get('end', seg_start + 2.0))
            text = (segment.get('text') or '').strip()
            if not text:
                continue

            plain_words = [w for w in text.split() if w.strip()]
            if not plain_words:
                continue

            duration = max(0.25, seg_end - seg_start)
            chunk_count = max(1, (len(plain_words) + words_per_line - 1) // words_per_line)
            chunk_dur = duration / chunk_count

            for idx in range(chunk_count):
                chunk = plain_words[idx * words_per_line:(idx + 1) * words_per_line]
                if not chunk:
                    continue
                c_start = seg_start + idx * chunk_dur
                c_end = seg_end if idx == chunk_count - 1 else min(seg_end, c_start + chunk_dur)
                # Очищение текста от лишних пробелов и прижание пунктуации
                chunk_text = ' '.join(chunk)
                chunk_text = clean_subtitle_text(chunk_text)
                if censor_words:
                    chunk_text = censor_words_in_text(chunk_text, censor_words, '*')
                srt_content += f"{sub_index}\n"
                srt_content += f"{_format_time(c_start)} --> {_format_time(c_end)}\n"
                srt_content += f"{chunk_text}\n\n"
                sub_index += 1
            continue

        num_words = len(words)
        
        # Разбиваем слова на группы по words_per_line
        for i in range(0, num_words, words_per_line):
            chunk = words[i:i + words_per_line]
            
            if not chunk:
                continue
            
            # Получаем время начала и конца для данной группы слов
            start_time = _format_time(chunk[0]['start'])
            end_time = _format_time(chunk[-1]['end'])
            
            # Объединяем слова в текст с чисткой пробелов
            text = ' '.join([word['word'] for word in chunk]).strip()
            text = clean_subtitle_text(text)
            if censor_words:
                text = censor_words_in_text(text, censor_words, '*')
            
            # Добавляем субтитр в SRT формате
            srt_content += f"{sub_index}\n"
            srt_content += f"{start_time} --> {end_time}\n"
            srt_content += f"{text}\n\n"
            
            sub_index += 1
    
    # Сохраняем SRT файл
    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write(srt_content)
    
    print(f'SRT file saved to {srt_path}')
    
    # Применяем цензуру если были переданы слова для цензурирования
    if censor_words:
        print(f'Applying censorship to subtitles...')
        censor_srt_file(srt_path, censor_words, replacement='*')
        print(f'Censorship applied to {srt_path}')
    
    return srt_path


def _transcribe_with_best_available_backend(
    audio_path: str,
    model_name: str,
    language_code: Optional[str]
) -> List[Dict]:
    """
    Возвращает список сегментов в унифицированном формате:
    [{'start': float, 'end': float, 'text': str, 'words': [{'word': str, 'start': float, 'end': float}]}]
    """
    # 1) Пытаемся в первую очередь через faster-whisper (без torch).
    try:
        return _transcribe_with_faster_whisper(audio_path, model_name, language_code)
    except Exception as fw_error:
        print(f'Warning: faster-whisper unavailable/failed: {fw_error}.')

        # Если ошибка связана с torch/c10.dll, openai-whisper почти гарантированно
        # упадет так же, поэтому сразу пробуем безопасный ffmpeg+srt fallback.
        err_text = str(fw_error).lower()
        is_torch_dll_error = ('c10.dll' in err_text) or ('torch\\lib' in err_text) or ('torch/lib' in err_text)
        if is_torch_dll_error:
            print('Warning: torch DLL issue detected. Trying isolated whisper subprocess...')
            try:
                return _transcribe_with_external_whisper_subprocess(audio_path, model_name, language_code)
            except Exception as ext_error:
                print(f'Warning: external whisper failed: {ext_error}. Using ffmpeg fallback subtitles.')
                return _transcribe_with_ffmpeg_fallback(audio_path, language_code)

        # 2) Иначе пробуем openai-whisper.
        print('Falling back to openai-whisper...')
        try:
            return _transcribe_with_openai_whisper(audio_path, model_name, language_code)
        except Exception as ow_error:
            # 3) Последний fallback: ffmpeg srt.
            print(f'Warning: openai-whisper failed: {ow_error}. Trying external whisper subprocess...')
            try:
                return _transcribe_with_external_whisper_subprocess(audio_path, model_name, language_code)
            except Exception as ext_error:
                print(f'Warning: external whisper failed: {ext_error}. Using ffmpeg fallback subtitles.')
            try:
                return _transcribe_with_ffmpeg_fallback(audio_path, language_code)
            except Exception as ff_error:
                raise RuntimeError(
                    f'Не удалось выполнить транскрипцию.\n'
                    f'faster-whisper: {fw_error}\n'
                    f'openai-whisper: {ow_error}\n'
                    f'external-whisper: {ext_error}\n'
                    f'ffmpeg-fallback: {ff_error}'
                )


def _transcribe_with_faster_whisper(audio_path: str, model_name: str, language_code: Optional[str]) -> List[Dict]:
    from faster_whisper import WhisperModel

    # CPU-safe default; на GPU библиотека сама ускорится при доступности.
    model = WhisperModel(model_name, device='cpu', compute_type='int8')
    segments_iter, _info = model.transcribe(
        audio_path,
        language=language_code,
        beam_size=5,
        # В Windows-конфигурациях VAD может тянуть проблемные DLL-зависимости.
        vad_filter=False,
        word_timestamps=True,
        condition_on_previous_text=False
    )

    normalized = []
    for seg in segments_iter:
        words = []
        if getattr(seg, 'words', None):
            for w in seg.words:
                if w.start is None or w.end is None:
                    continue
                words.append({
                    'word': (w.word or '').strip(),
                    'start': float(w.start),
                    'end': float(w.end)
                })

        normalized.append({
            'start': float(seg.start),
            'end': float(seg.end),
            'text': (seg.text or '').strip(),
            'words': words
        })
    return normalized


def _transcribe_with_openai_whisper(audio_path: str, model_name: str, language_code: Optional[str]) -> List[Dict]:
    import whisper

    resolved_model = _resolve_openai_whisper_model_name(whisper, model_name)
    print(f"Using openai-whisper model: '{resolved_model}'")
    model = whisper.load_model(resolved_model)
    result = model.transcribe(
        audio_path,
        language=language_code,
        verbose=True,
        fp16=False,
        word_timestamps=True
    )

    normalized = []
    for seg in result.get('segments', []):
        words = []
        for w in seg.get('words', []) or []:
            w_start = w.get('start')
            w_end = w.get('end')
            if w_start is None or w_end is None:
                continue
            words.append({
                'word': (w.get('word') or '').strip(),
                'start': float(w_start),
                'end': float(w_end)
            })
        normalized.append({
            'start': float(seg.get('start', 0.0)),
            'end': float(seg.get('end', seg.get('start', 0.0) + 2.0)),
            'text': (seg.get('text') or '').strip(),
            'words': words
        })
    return normalized


def _resolve_openai_whisper_model_name(whisper_module, requested_model: str) -> str:
    """
    Подбирает совместимую модель openai-whisper.
    Если выбранная недоступна (например distil-large-v3), выбирает лучший доступный fallback.
    """
    try:
        available = list(whisper_module.available_models())
    except Exception:
        available = []

    if not available:
        # Старое поведение как fallback.
        return requested_model

    if requested_model in available:
        return requested_model

    # Частый кейс: distil-large-v3 нет в openai-whisper -> берем large-v3 / turbo.
    alias_candidates = {
        'distil-large-v3': ['large-v3', 'large-v3-turbo', 'turbo', 'large-v2', 'large'],
        'large-v3': ['large-v3-turbo', 'turbo', 'large-v2', 'large'],
        'large': ['large-v3', 'large-v3-turbo', 'turbo', 'large-v2'],
        'medium': ['medium', 'small', 'base'],
        'small': ['small', 'base', 'tiny'],
        'base': ['base', 'small', 'tiny'],
        'tiny': ['tiny', 'base']
    }
    for candidate in alias_candidates.get(requested_model, []):
        if candidate in available:
            return candidate

    # Универсальный приоритет лучшей доступной модели.
    global_priority = [
        'large-v3',
        'large-v3-turbo',
        'turbo',
        'large-v2',
        'large-v1',
        'large',
        'medium',
        'small',
        'base',
        'tiny.en',
        'tiny'
    ]
    for candidate in global_priority:
        if candidate in available:
            return candidate

    return available[0]


def _resolve_external_whisper_model_name(requested_model: str) -> str:
    """
    Модели для внешнего openai-whisper CLI.
    distil-large-v3 напрямую не поддерживается -> large-v3.
    """
    mapping = {
        # Для внешнего CLI large-v3 на CPU слишком медленный и выглядит как "зависание".
        # Берем более практичный fallback.
        'distil-large-v3': 'medium',
        'large-v3': 'large-v3',
        'large': 'large',
        'medium': 'medium',
        'small': 'small',
        'base': 'base',
        'tiny': 'tiny',
    }
    return mapping.get(requested_model, 'base')


def _transcribe_with_external_whisper_subprocess(
    audio_path: str,
    model_name: str,
    language_code: Optional[str]
) -> List[Dict]:
    """
    Запускает whisper в отдельном процессе.
    Это обходит DLL-конфликты текущего процесса.
    """
    import tempfile

    # PyInstaller: sys.executable — это сам .exe приложения, а не python.exe.
    # Команда `ReelsMakerPro.exe -m whisper` снова запускает GUI → второе окно.
    if getattr(sys, 'frozen', False):
        raise RuntimeError(
            'whisper subprocess недоступен в собранном exe (нельзя использовать тот же исполняемый файл).'
        )

    out_dir = tempfile.gettempdir()
    resolved_model = _resolve_external_whisper_model_name(model_name)
    base_name = os.path.splitext(os.path.basename(audio_path))[0]
    srt_candidate = os.path.join(out_dir, f'{base_name}.srt')

    # 1) Пробуем тот же интерпретатор (из venv), но в отдельном процессе.
    cmd = [
        sys.executable, '-m', 'whisper',
        audio_path,
        '--model', resolved_model,
        '--output_format', 'srt',
        '--output_dir', out_dir,
        '--fp16', 'False',
        '--verbose', 'True'
    ]
    if language_code:
        cmd.extend(['--language', language_code])

    # Важно: не capture_output, чтобы пользователь видел живой прогресс в консоли.
    result = subprocess.run(cmd, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        # 2) Fallback: если установлен launcher `py -3.10`, пробуем через него.
        alt_cmd = ['py', '-3.10', '-m', 'whisper'] + cmd[3:]
        alt = subprocess.run(alt_cmd, text=True, encoding='utf-8', errors='replace')
        if alt.returncode != 0:
            raise RuntimeError(f'whisper CLI failed ({result.returncode}/{alt.returncode})')

    if not os.path.exists(srt_candidate):
        raise RuntimeError(f'whisper CLI did not produce srt: {srt_candidate}')

    with open(srt_candidate, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read().strip()
    if not content:
        return []

    segments: List[Dict] = []
    blocks = [b for b in content.split('\n\n') if b.strip()]
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3 or '-->' not in lines[1]:
            continue
        start_raw, end_raw = [x.strip() for x in lines[1].split('-->')]
        start_s = _parse_srt_time(start_raw)
        end_s = _parse_srt_time(end_raw)
        text = ' '.join(line.strip() for line in lines[2:] if line.strip())
        # Очищаем текст от лишних пробелов и прижимаем пунктуацию
        text = " ".join(text.split())
        text = re.sub(r'\s+([,.!?])', r'\1', text)
        if not text:
            continue
        segments.append({
            'start': start_s,
            'end': end_s,
            'text': text,
            'words': []
        })

    return segments


def _transcribe_with_ffmpeg_fallback(audio_path: str, language_code: Optional[str]) -> List[Dict]:
    """
    Резервный путь без torch/whisper:
    используем ffmpeg lavfi asr (если доступен), далее нормализуем в сегменты.
    """
    import subprocess
    import json
    import re
    from utils.ffmpeg_utils import FFMPEG_PATH_EFFECTIVE

    if not FFMPEG_PATH_EFFECTIVE:
        raise RuntimeError('FFmpeg not found for fallback transcription.')

    # lavfi asr требует pocketsphinx в сборке ffmpeg; проверяем по output.
    asr_filter = 'asr'
    if language_code:
        asr_filter = f'asr=rate=16000:language={language_code}'

    cmd = [
        FFMPEG_PATH_EFFECTIVE,
        '-hide_banner',
        '-i', audio_path,
        '-af', asr_filter,
        '-f', 'null',
        '-'
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    output = (result.stderr or '') + '\n' + (result.stdout or '')
    if result.returncode != 0 and 'asr' not in output.lower():
        raise RuntimeError('ffmpeg asr filter is unavailable in current build.')

    # Ищем JSON-like куски с распознанным текстом.
    lines = output.splitlines()
    segments: List[Dict] = []
    text_items = []
    for line in lines:
        m = re.search(r'lavfi\.asr\.text=([^\r\n]+)', line)
        if m:
            text = m.group(1).strip()
            if text:
                text_items.append(text)

    # Если asr не дал результата, возвращаем пусто без исключения.
    if not text_items:
        return []

    # Разбиваем равномерно по времени аудио (грубый fallback).
    from utils.ffmpeg_utils import get_video_duration
    duration = max(1.0, get_video_duration(audio_path))
    chunk = duration / max(1, len(text_items))
    t = 0.0
    for text in text_items:
        start = t
        end = min(duration, t + chunk)
        segments.append({
            'start': start,
            'end': end,
            'text': text,
            'words': []
        })
        t += chunk
    return segments


# Дополнительные вспомогательные функции

def validate_whisper_model(model_name: str) -> bool:
    """
    Проверяет, является ли название модели валидным для Whisper.
    
    Args:
        model_name: Название модели для проверки
        
    Returns:
        True если модель валидна, False в противном случае
    """
    valid_models = ['tiny', 'base', 'small', 'medium', 'large', 'large-v2', 'large-v3']
    return model_name in valid_models


def get_available_languages() -> list:
    """
    Получает список доступных языков для Whisper.
    
    Returns:
        Список кодов языков, поддерживаемых Whisper
    """
    try:
        import whisper
        return list(whisper.tokenizer.LANGUAGES.keys())
    except ImportError:
        # Базовый список, если Whisper недоступен
        return [
            'en', 'ru', 'es', 'fr', 'de', 'it', 'pt', 'pl', 'tr', 'ko',
            'zh', 'ja', 'hi', 'ar', 'th', 'vi', 'ms', 'uk', 'cs', 'ro'
        ]


def estimate_transcription_time(audio_duration: float, model_name: str) -> float:
    """
    Оценивает примерное время транскрипции на основе длительности аудио и модели.
    
    Args:
        audio_duration: Длительность аудио в секундах
        model_name: Название модели Whisper
        
    Returns:
        Оценочное время транскрипции в секундах
    """
    # Примерные коэффициенты скорости для разных моделей
    # (время транскрипции / время аудио)
    speed_factors = {
        'tiny': 0.1,
        'base': 0.2,
        'small': 0.4,
        'medium': 0.8,
        'large': 1.5,
        'large-v2': 1.5,
        'large-v3': 1.5
    }
    
    factor = speed_factors.get(model_name, 1.0)
    return audio_duration * factor


def clean_subtitle_text(text: str) -> str:
    """
    Очищает текст субтитров от нежелательных символов и форматирует его.
    
    Args:
        text: Исходный текст субтитра
        
    Returns:
        Очищенный текст субтитра
    """
    # Убираем пробелы в начале и конце
    text = text.strip()
    
    # Убираем лишние пробелы (схлопываем двойные пробелы)
    text = ' '.join(text.split())
    
    # Прижимаем знаки препинания к словам (убираем пробел перед ними)
    text = re.sub(r'\s+([,.!?])', r'\1', text)
    
    # Убираем повторяющуюся пунктуацию
    text = re.sub(r'([.!?])\1+', r'\1', text)
    
    # Капитализируем первую букву
    if text:
        text = text[0].upper() + text[1:]
    
    return text


def split_long_subtitles(srt_path: str, max_chars: int = 80) -> str:
    """
    Разбивает длинные субтитры на более короткие строки.
    
    Args:
        srt_path: Путь к SRT файлу
        max_chars: Максимальное количество символов в строке
        
    Returns:
        Путь к обновленному SRT файлу
    """
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.strip().split('\n')
    new_lines = []
    
    i = 0
    while i < len(lines):
        if lines[i].strip().isdigit():  # Номер субтитра
            new_lines.append(lines[i])
            i += 1
            
            if i < len(lines) and '-->' in lines[i]:  # Временная метка
                new_lines.append(lines[i])
                i += 1
                
                # Текст субтитра
                subtitle_text = ''
                while i < len(lines) and lines[i].strip() and not lines[i].strip().isdigit():
                    subtitle_text += lines[i] + ' '
                    i += 1
                
                # Разбиваем длинный текст
                subtitle_text = subtitle_text.strip()
                # Очищаем текст от лишних пробелов и прижимаем пунктуацию
                subtitle_text = " ".join(subtitle_text.split())
                subtitle_text = re.sub(r'\s+([,.!?])', r'\1', subtitle_text)
                if len(subtitle_text) > max_chars:
                    words = subtitle_text.split()
                    current_line = ''
                    
                    for word in words:
                        if len(current_line + ' ' + word) <= max_chars:
                            current_line += (' ' + word) if current_line else word
                        else:
                            if current_line:
                                new_lines.append(current_line)
                                current_line = word
                            else:
                                new_lines.append(word)
                    
                    if current_line:
                        new_lines.append(current_line)
                else:
                    new_lines.append(subtitle_text)
                
                new_lines.append('')  # Пустая строка после субтитра
        else:
            i += 1
    
    # Сохраняем обновленный файл
    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(new_lines))
    
    return srt_path


def convert_srt_to_vtt(srt_path: str, vtt_path: str) -> str:
    """
    Конвертирует SRT файл в WebVTT формат.
    
    Args:
        srt_path: Путь к исходному SRT файлу
        vtt_path: Путь для сохранения VTT файла
        
    Returns:
        Путь к созданному VTT файлу
    """
    with open(srt_path, 'r', encoding='utf-8') as f:
        srt_content = f.read()
    
    # Заменяем запятые на точки в временных метках (SRT -> VTT)
    vtt_content = 'WEBVTT\n\n'
    vtt_content += srt_content.replace(',', '.')
    
    with open(vtt_path, 'w', encoding='utf-8') as f:
        f.write(vtt_content)
    
    return vtt_path


def merge_subtitle_files(srt_files: list, output_path: str) -> str:
    """
    Объединяет несколько SRT файлов в один.
    
    Args:
        srt_files: Список путей к SRT файлам для объединения
        output_path: Путь для сохранения объединенного файла
        
    Returns:
        Путь к объединенному SRT файлу
    """
    merged_content = ''
    subtitle_index = 1
    
    for srt_file in srt_files:
        if not os.path.exists(srt_file):
            continue
            
        with open(srt_file, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        
        if not content:
            continue
        
        # Перенумеровываем субтитры
        lines = content.split('\n')
        current_subtitle = []
        
        for line in lines:
            if line.strip().isdigit():
                if current_subtitle:
                    # Добавляем предыдущий субтитр
                    current_subtitle[0] = str(subtitle_index)
                    merged_content += '\n'.join(current_subtitle) + '\n\n'
                    subtitle_index += 1
                    current_subtitle = []
                current_subtitle.append(str(subtitle_index))
            else:
                current_subtitle.append(line)
        
        # Добавляем последний субтитр
        if current_subtitle:
            current_subtitle[0] = str(subtitle_index)
            merged_content += '\n'.join(current_subtitle) + '\n\n'
            subtitle_index += 1
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(merged_content.strip())
    
    return output_path


# ==============================================
# WORD CENSORSHIP AND METADATA CLEANUP FUNCTIONS
# ==============================================

def censor_words_in_text(text: str, words_to_censor: List[str], replacement: str = '*') -> str:
    """
    Цензурирует слова в тексте на основе черного списка.
    Поддерживает цензурирование слов со всеми их склонениями.
    Поддерживает как полное удаление, так и замену на символы.
    
    Args:
        text: Исходный текст для цензурирования
        words_to_censor: Список слов, которые нужно цензурировать
        replacement: Строка для замены (по умолчанию '*' для каждого символа слова)
                     Если пусто (''), слово полностью удаляется
        
    Returns:
        Текст с цензурированными словами
        
    Example:
        >>> censor_words_in_text("hello world", ["hello"], "*")
        "**** world"
        
        >>> censor_words_in_text("hellos", ["hello"], "*")
        "******" (со склонением)
        
        >>> censor_words_in_text("hello world", ["hello"], "")
        "world"
    """
    import re
    
    print(f"[DEBUG] censor_words_in_text() called with censor list size: {len(words_to_censor)}")
    
    if not words_to_censor:
        return text
    
    result = text
    
    for word in words_to_censor:
        if not word.strip():
            continue
            
        # Создаем паттерн для поиска слова и его склонений (case-insensitive)
        # \b + слово + любые буквы/цифры (склонения) + \b
        pattern = r'\b' + re.escape(word) + r'\w*\b'
        
        # Определяем функцию замены
        def replacer(match):
            found_word = match.group(0)
            
            if replacement:
                word_len = len(found_word)
                
                # Определяем количество букв для замены в зависимости от длины слова
                if word_len <= 3:
                    num_to_replace = 1
                elif word_len <= 6:
                    num_to_replace = 2
                else:
                    num_to_replace = 3
                
                # Убедимся, что не пытаемся заменить больше букв, чем есть
                num_to_replace = min(num_to_replace, word_len)
                
                # Выбираем случайные уникальные индексы для замены
                indices_to_replace = set(random.sample(range(word_len), num_to_replace))
                
                # Строим новое слово с замененными буквами на *
                result_chars = []
                for i, char in enumerate(found_word):
                    if i in indices_to_replace:
                        result_chars.append('*')
                    else:
                        result_chars.append(char)
                
                return ''.join(result_chars)
            else:
                # Удаляем слово полностью
                return ''
        
        # Заменяем со сменой регистра
        result = re.sub(pattern, replacer, result, flags=re.IGNORECASE)
        
        # Убираем двойные пробелы после удаления слов
        if not replacement:
            result = re.sub(r'\s+', ' ', result).strip()
    
    return result


def censor_srt_file(srt_path: str, words_to_censor: List[str], output_path: Optional[str] = None, 
                    replacement: str = '*') -> str:
    """
    Применяет цензурирование к SRT файлу и сохраняет результат.
    
    Args:
        srt_path: Путь к исходному SRT файлу
        words_to_censor: Список слов для цензурирования
        output_path: Путь для сохранения (если None, перезаписывает исходный файл)
        replacement: Символ/строка для замены
        
    Returns:
        Путь к цензурированному SRT файлу
    """
    if not os.path.exists(srt_path):
        raise FileNotFoundError(f'SRT файл не найден: {srt_path}')
    
    if output_path is None:
        output_path = srt_path
    
    # Читаем SRT файл
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Обрабатываем блоки субтитров
    blocks = content.strip().split('\n\n')
    censored_blocks = []
    
    for block in blocks:
        lines = block.split('\n')
        if len(lines) < 3 or '-->' not in lines[1]:
            censored_blocks.append(block)
            continue
        
        # Сохраняем номер субтитра и тайминг
        subtitle_num = lines[0]
        timing = lines[1]
        
        # Цензурируем текст субтитра (остальные строки после тайминга)
        subtitle_text = '\n'.join(lines[2:])
        censored_text = censor_words_in_text(subtitle_text, words_to_censor, replacement)
        
        # Собираем цензурированный блок
        if censored_text.strip():  # Пропускаем пустые субтитры
            censored_block = f'{subtitle_num}\n{timing}\n{censored_text}'
            censored_blocks.append(censored_block)
    
    # Сохраняем результат
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(censored_blocks))
    
    return output_path


def load_censor_list_from_file(file_path: str) -> List[str]:
    """
    Загружает черный список слов из файла.
    Поддерживает форматы: каждое слово на новой строке, слова через запятую, слова через пробел.
    
    Args:
        file_path: Путь к файлу со списком слов
        
    Returns:
        Список слов для цензурирования
    """
    if not os.path.exists(file_path):
        return []
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f'Ошибка при чтении файла черного списка: {e}')
        return []
    
    # Обработка разных форматов
    words = []
    
    # Если содержит запятые, разделяем по запятым
    if ',' in content:
        words = [w.strip() for w in content.split(',') if w.strip()]
    # Если содержит переносы строк
    elif '\n' in content:
        words = [w.strip() for w in content.split('\n') if w.strip()]
    # Иначе разделяем по пробелам
    else:
        words = [w.strip() for w in content.split() if w.strip()]
    
    return words


def clean_metadata(title: str, description: str, tags: str, 
                   words_to_censor: Optional[List[str]] = None) -> Dict[str, str]:
    """
    Очищает метаданные видео (название, описание, теги) и применяет цензуру.
    
    Args:
        title: Название видео
        description: Описание видео
        tags: Теги (строка через запятую или пространство)
        words_to_censor: Список слов для цензурирования (опционально)
        
    Returns:
        Словарь с очищенными метаданными:
            - 'title': Очищенное название (макс 100 символов для YouTube)
            - 'description': Очищенное описание (макс 5000 символов)
            - 'tags': Очищенные теги (список)
    """
    if words_to_censor is None:
        words_to_censor = []
    
    # Очищаем название
    cleaned_title = title.strip()
    # Убираем лишние пробелы
    cleaned_title = ' '.join(cleaned_title.split())
    # Прижимаем знаки препинания к словам
    cleaned_title = re.sub(r'\s+([,.!?])', r'\1', cleaned_title)
    # Убираем нежелательные символы в начале/конце (кроме букв, цифр, пробела и основной пунктуации)
    cleaned_title = cleaned_title.strip(' \t\n\r')
    # Ограничиваем длину (YouTube лимит - 100 символов)
    if len(cleaned_title) > 100:
        cleaned_title = cleaned_title[:97] + '...'
    
    # Применяем цензуру к названию
    if words_to_censor:
        cleaned_title = censor_words_in_text(cleaned_title, words_to_censor, '*')
    
    # Очищаем описание
    cleaned_description = description.strip()
    # Убираем лишние пробелы
    cleaned_description = ' '.join(cleaned_description.split())
    # Прижимаем знаки препинания к словам
    cleaned_description = re.sub(r'\s+([,.!?])', r'\1', cleaned_description)
    # Убираем множественные переносы строк (оставляем максимум 2 переноса подряд)
    cleaned_description = re.sub(r'\n\s*\n\s*\n+', '\n\n', cleaned_description)
    # Ограничиваем длину (YouTube лимит - 5000 символов)
    if len(cleaned_description) > 5000:
        cleaned_description = cleaned_description[:4997] + '...'
    
    # Применяем цензуру к описанию
    if words_to_censor:
        cleaned_description = censor_words_in_text(cleaned_description, words_to_censor, '*')
    
    # Очищаем теги
    # Разделяем теги (поддерживаем и запятую, и пробел)
    if ',' in tags:
        tag_list = [t.strip() for t in tags.split(',') if t.strip()]
    else:
        tag_list = [t.strip() for t in tags.split() if t.strip()]
    
    # Убираем дубликаты (но сохраняем порядок)
    seen = set()
    cleaned_tags = []
    for tag in tag_list:
        tag_lower = tag.lower()
        if tag_lower not in seen:
            # Применяем цензуру к тегу
            if words_to_censor:
                tag = censor_words_in_text(tag, words_to_censor, '*')
            
            cleaned_tags.append(tag)
            seen.add(tag_lower)
    
    # Ограничиваем количество тегов (YouTube рекомендует 5-15)
    # и длину каждого тега (макс 30 символов)
    final_tags = []
    for tag in cleaned_tags[:500]:  # YouTube допускает до 500 тегов
        if len(tag) > 30:
            tag = tag[:30]
        if tag:
            final_tags.append(tag)
    
    return {
        'title': cleaned_title,
        'description': cleaned_description,
        'tags': final_tags
    }


def clean_metadata_dict(metadata: Dict[str, str], 
                        words_to_censor: Optional[List[str]] = None) -> Dict[str, str]:
    """
    Очищает метаданные из словаря (удобная функция для использования с AI воркером).
    
    Args:
        metadata: Словарь с ключами 'title', 'description', 'tags'
        words_to_censor: Список слов для цензурирования
        
    Returns:
        Очищенный словарь метаданных
    """
    return clean_metadata(
        title=metadata.get('title', ''),
        description=metadata.get('description', ''),
        tags=metadata.get('tags', ''),
        words_to_censor=words_to_censor
    )