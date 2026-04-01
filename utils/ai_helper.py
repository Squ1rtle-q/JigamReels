"""
AI helper module for generating smart titles and descriptions.
Модуль для работы с ИИ при генерации названий и описаний.
"""

import re
import logging
from typing import Optional


def generate_smart_title(transcription_text: str, timeout: int = 10) -> Optional[str]:
    """
    Генерирует название из текста транскрипта локальным способом (без g4f).
    """
    if not transcription_text or not transcription_text.strip():
        logging.warning('generate_smart_title: пустой текст, локальный fallback не найден')
        return None

    text = transcription_text.strip()

    # Детектируем русский текст (если есть кириллица) или английский
    has_cyrillic = bool(re.search(r'[а-яА-Я]', text))
    has_latin = bool(re.search(r'[a-zA-Z]', text))

    russian_stopwords = {
        'это', 'и', 'в', 'не', 'на', 'я', 'с', 'что', 'как', 'а', 'по', 'из', 'за',
        'для', 'к', 'о', 'то', 'его', 'ее', 'же', 'но', 'от', 'бы', 'у', 'же', 'ни',
        'мы', 'вы', 'он', 'она', 'они', 'там', 'тут', 'вам', 'меня', 'только', 'ещё',
        'так', 'еще', 'он', 'она', 'оно'
    }
    english_stopwords = {
        'the', 'and', 'for', 'that', 'this', 'with', 'from', 'have', 'not', 'you',
        'are', 'was', 'but', 'your', 'what', 'they', 'their', 'will', 'can', 'just',
        'when', 'there', 'about', 'which', 'other', 'these', 'into', 'because', 'also',
        'people', 'them'
    }

    # Убираем спецсимволы (остаются только буквы/цифры/пробелы)
    clean = re.sub(r"[^\w\s\u0400-\u04FF]", ' ', text, flags=re.UNICODE)
    clean = re.sub(r'\s+', ' ', clean).strip().lower()

    words = [w for w in clean.split() if len(w) > 1]
    if not words:
        return None

    stopwords = russian_stopwords if has_cyrillic else english_stopwords
    filtered = [w for w in words if w not in stopwords]
    if not filtered:
        filtered = words

    freq = {}
    for w in filtered:
        freq[w] = freq.get(w, 0) + 1

    # Парсим первые предложения, чтобы деревом слов не потерять смысл.
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    main_sentence = sentences[0] if sentences else ' '.join(words[:10])

    # если первое предложение получается слишком длинным, сокращаем
    main_words = [w for w in re.sub(r"[^\w\s\u0400-\u04FF]", ' ', main_sentence).split() if w]
    if len(main_words) > 10:
        main_words = main_words[:10]
    main_phrase = ' '.join(main_words)

    # Составляем вирусный хук на базе основы
    if has_cyrillic:
        hook_options = [
            f"Не поверишь, но {main_phrase}",
            f"Я сделал это, и {main_phrase}",
            f"Как {main_phrase} за 10 секунд",
            f"Секрет {main_phrase}",
            f"{main_phrase} — это шок"
        ]
    else:
        hook_options = [
            f"You won't believe: {main_phrase}",
            f"I tried this and {main_phrase}",
            f"How {main_phrase} in 10 seconds",
            f"The secret of {main_phrase}",
            f"{main_phrase} is insane"
        ]

    # Берем краткую клишированную формулу, но без пустоты
    for hook in hook_options:
        clean_hook = re.sub(r'\s+', ' ', hook).strip()
        if len(clean_hook) > 10:
            title = clean_hook
            break
    else:
        # fallback: топ-4 ключевых слова + сила
        top = sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:4]
        title = ' '.join([w for w, _ in top])
        if not title:
            title = main_phrase

    # Обрезаем лишнюю длину
    title = title[:80].strip()
    title = title[0].upper() + title[1:] if title else None

    if not title:
        return None

    logging.info(f"generate_smart_title: локальный title='{title}' (cyrillic={has_cyrillic}, latin={has_latin})")
    return title


def sanitize_title_for_filename(title: str) -> str:
    """
    Удаляет символы, запрещенные в именах файлов Windows, но сохраняет пробелы.
    
    Args:
        title: Исходный заголовок
        
    Returns:
        Очищенный заголовок, безопасный для использования в имени файла
    """
    # Удаляем только запрещенные символы: \ / : * ? " < > |
    # Пробелы остаются нетронутыми
    clean_title = re.sub(r'[\\/*?:"<>|]', '', title)
    # Убираем лишние пробелы в начале/конце
    clean_title = clean_title.strip()
    
    return clean_title or "Video"
