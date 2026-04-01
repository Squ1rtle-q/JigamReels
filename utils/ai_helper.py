"""
AI helper module for generating smart titles and descriptions.
Модуль для работы с ИИ при генерации названий и описаний.
"""

import re
import logging
from typing import Optional


def generate_smart_title(transcription_text: str, timeout: int = 10) -> Optional[str]:
    """
    Отказ от g4f. Возвращает None, чтобы воркер использовал локальный fallback.
    """
    logging.warning('generate_smart_title: g4f отключён, используется локальный fallback')
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
