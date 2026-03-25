import os
from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
import json
from typing import Optional, List


class AIWorkerSignals(QObject):
    """Сигналы для AI Worker"""
    finished = pyqtSignal(dict)  # Сигнал с результатом в виде словаря
    error = pyqtSignal(str)      # Сигнал с ошибкой в виде строки
    status_update = pyqtSignal(str)  # Сигнал статуса выполнения


class AIWorker(QRunnable):
    """Воркер для обработки видео с помощью AI"""
    
    def __init__(self, video_path, censor_words: Optional[List[str]] = None, 
                 apply_metadata_cleanup: bool = True):
        super().__init__()
        self.video_path = video_path
        self.censor_words = censor_words or []
        self.apply_metadata_cleanup = apply_metadata_cleanup
        self.signals = AIWorkerSignals()
    
    def run(self):
        """Основная функция выполнения AI обработки"""
        try:
            # Импортируем необходимые библиотеки
            import g4f
            import whisper
            from utils.subtitle_utils import clean_metadata
            
            # Проверяем существование видеофайла
            if not os.path.exists(self.video_path):
                raise FileNotFoundError(f'Видеофайл не найден: {self.video_path}')
            
            # Загружаем модель Whisper для распознавания речи
            model = whisper.load_model('tiny')
            
            # Транскрибируем видео (извлекаем текст из речи)
            result = model.transcribe(self.video_path, fp16=False)
            transcription = result['text']
            
            # Проверяем, что удалось получить текст
            if not transcription.strip():
                raise ValueError('Не удалось получить текст из видео. Возможно, в нем нет речи.')
            
            # Формируем промпт для AI
            prompt = (
                f"На основе следующей расшифровки видео, пожалуйста, создай краткий, цепляющий "
                f"заголовок (до 100 символов), подробное описание (2-3 абзаца) и 10-15 релевантных "
                f"тегов через запятую. Ответ дай в формате JSON с ключами 'title', 'description' и 'tags'.\n\n"
                f'Расшифровка: "{transcription}"'
            )
            
            # Отправляем запрос к AI
            response = g4f.ChatCompletion.create(
                model=g4f.models.default,
                messages=[{
                    'role': 'user',
                    'content': prompt
                }]
            )
            
            # Извлекаем JSON из ответа AI
            json_response_str = response[response.find('{'):response.rfind('}') + 1]
            
            if not json_response_str:
                raise ValueError('Не удалось извлечь JSON из ответа AI.')
            
            # Парсим JSON ответ
            metadata = json.loads(json_response_str)
            
            # Применяем очистку метаданных если требуется
            if self.apply_metadata_cleanup:
                metadata = clean_metadata(
                    title=metadata.get('title', ''),
                    description=metadata.get('description', ''),
                    tags=metadata.get('tags', ''),
                    words_to_censor=self.censor_words if self.censor_words else None
                )
            elif self.censor_words:
                # Если только цензура без полной очистки
                from utils.subtitle_utils import censor_words_in_text
                metadata['title'] = censor_words_in_text(
                    metadata.get('title', ''), 
                    self.censor_words, 
                    '*'
                )
                metadata['description'] = censor_words_in_text(
                    metadata.get('description', ''), 
                    self.censor_words, 
                    '*'
                )
                # Обработка тегов
                tags_str = metadata.get('tags', '')
                if isinstance(tags_str, list):
                    tags_str = ', '.join(tags_str)
                cleaned = censor_words_in_text(tags_str, self.censor_words, '*')
                metadata['tags'] = [t.strip() for t in cleaned.split(',') if t.strip()]
            
            # Отправляем сигнал об успешном завершении
            self.signals.finished.emit(metadata)
            
        except Exception as e:
            # Отправляем сигнал об ошибке
            self.signals.error.emit(str(e))