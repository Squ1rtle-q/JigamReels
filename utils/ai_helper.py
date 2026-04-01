"""
AI helper module for generating smart titles and descriptions.
Модуль для работы с ИИ при генерации названий и описаний.
"""

import re
import logging
from typing import Optional


def generate_smart_title(transcription_text: str, timeout: int = 10) -> Optional[str]:
    """
    Генерирует интригующий и кликабельный заголовок для видео на основе транскрипции.
    
    Использует библиотеку g4f для обращения к ИИ моделям (GPT-4o или GPT-3.5-turbo).
    
    Args:
        transcription_text: Текст транскрипции видео (из Whisper)
        timeout: Таймаут (в секундах) для ожидания ответа ИИ
        
    Returns:
        Сгенерированный заголовок видео (3-5 слов), None если произошла ошибка ИИ,
        или "Video" если транскрипция пустая
    """
    if not transcription_text or not transcription_text.strip():
        logging.warning("generate_smart_title: Пустая транскрипция, возвращаем fallback")
        return "Video"
    
    # Ограничиваем длину текста, отправляемого в ИИ (для экономии на токенах)
    max_chars = 2000
    text_for_ai = transcription_text[:max_chars].strip()
    
    try:
        # Импортируем g4f динамически, чтобы избежать зависимости если библиотека не установлена
        try:
            import g4f
        except ImportError:
            logging.error("generate_smart_title: Библиотека g4f не установлена")
            return "Video"
        
        logging.info("Запрос названия видео к ИИ...")
        
        # Упрощенный промт для лучшей работы
        prompt = f"Придумай один интригующий заголовок (3-5 слов) для видео по тексту: {text_for_ai}. Выдай ТОЛЬКО текст заголовка."
        
        # Отправляем запрос к AI через g4f с автоматическим списком провайдеров
        # Игнорируем проблемные провайдеры
        provider = None
        if hasattr(g4f, 'Provider'):
            provider = getattr(g4f.Provider, 'Bing', None) or getattr(g4f.Provider, 'Blackbox', None)

        request_kwargs = {
            'model': 'gpt-4o',
            'messages': [{
                'role': 'user',
                'content': prompt
            }],
            'stream': False,
            'ignored': ['Pollinations', 'GigaChat', 'Blackbox'],
            'timeout': timeout
        }

        if provider is not None:
            request_kwargs['provider'] = provider

        response = g4f.ChatCompletion.create(**request_kwargs)

        
        # Извлекаем текст из ответа
        title = response

        # Проверяем на ошибочные ключевые слова
        error_keywords = ['error', 'authentication', 'api key', 'type', 'pollinations legacy', 'deprecated']
        if isinstance(title, str) and any(keyword in title.lower() for keyword in error_keywords):
            logging.warning(f"ИИ вернул ошибку / устаревший API: {title[:120]}")
            return None

        # Очищаем заголовок: убираем звездочки (если ИИ вернул **Текст**)
        title = title.replace('*', '')
        # Убираем кавычки, точки, лишние пробелы
        title = title.strip().strip('"\'').rstrip('.')
        title = ' '.join(title.split())  # Нормализуем пробелы
        
        # Проверяем, что заголовок не пуст и имеет разумную длину
        if title and 3 <= len(title.split()) <= 10:
            logging.info(f"Сгенерировано название: {title}")
            return title
        else:
            logging.warning(f"Некорректный результат ИИ: '{title}', используем fallback")
            return None
            
    except TimeoutError:
        logging.error("generate_smart_title: Таймаут при ожидании ответа ИИ")
        return None
    except Exception as e:
        logging.error(f"generate_smart_title: Ошибка при запросе к ИИ: {e}")
        return None


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
