from PyQt5.QtCore import QThread, pyqtSignal
from datetime import datetime
import logging
import os
import random
import subprocess
import uuid
import tempfile
import shutil
from typing import List, Optional, Dict

from utils.constants import REELS_FORMAT_NAME
from utils.ffmpeg_utils import (
    process_single,
    detect_crop_dimensions,
    detect_viral_moments,
    burn_subtitles_postprocess,
    get_video_dimensions,
    reels_letterbox_vertical_inset_px,
    remove_silence_from_video,
)
from utils.subtitle_utils import extract_audio, generate_srt_from_whisper, build_segment_srt, censor_words_in_text
from utils.ai_helper import generate_smart_title, sanitize_title_for_filename
from utils.file_utils import safe_filename


class Worker(QThread):
    # Сигналы для обратной связи с UI
    progress = pyqtSignal(int, int)  # (текущий файл, общее количество)
    file_progress = pyqtSignal(int)  # прогресс обработки одного файла
    finished = pyqtSignal()  # завершение работы
    error = pyqtSignal(str)  # ошибка
    file_processing = pyqtSignal(str)  # имя обрабатываемого файла
    status_update = pyqtSignal(str)  # обновление статуса
    
    def __init__(
        self,
        files: List[str],
        filters: List[str],
        zoom_mode: str,
        zoom_static: int,
        zoom_min: int,
        zoom_max: int,
        speed_mode: str,
        speed_static: int,
        speed_min: int,
        speed_max: int,
        overlay_file: Optional[str],
        overlay_alignment: int,
        overlay_margin_v: int,
        overlay_margin_lr: int,
        overlay_scale_p: int,
        overlay_chromakey: bool,
        overlay_chromakey_color: str,
        overlay_chromakey_similarity: float,
        overlay_chromakey_blend: float,
        out_dir: str,
        mute_audio: bool,
        output_format: str,
        blur_background: bool,
        strip_metadata: bool,
        codec: str,
        subtitle_settings: Dict,
        auto_crop: bool,
        overlay_audio: Optional[str],
        original_volume: int,
        overlay_volume: int,
        viral_clips_enabled: bool = False,
        viral_clip_duration: int = 15,
        viral_clip_count: int = 3,
        censor_words: Optional[List[str]] = None,
        jumpcut_enabled: bool = True,
        jumpcut_aggressiveness: int = 1,
        jumpcut_fade_duration: float = 0.3
    ):
        super().__init__()
        
        # Сохранение параметров обработки
        self.files = list(files)
        self.filters = list(filters)
        self.zoom_mode = zoom_mode
        self.zoom_static = zoom_static
        self.zoom_min = zoom_min
        self.zoom_max = zoom_max
        self.speed_mode = speed_mode
        self.speed_static = speed_static
        self.speed_min = speed_min
        self.speed_max = speed_max
        self.overlay_file = overlay_file
        self.overlay_alignment = overlay_alignment
        self.overlay_margin_v = overlay_margin_v
        self.overlay_margin_lr = overlay_margin_lr
        self.overlay_scale_p = overlay_scale_p
        self.overlay_chromakey = overlay_chromakey
        self.overlay_chromakey_color = overlay_chromakey_color
        self.overlay_chromakey_similarity = overlay_chromakey_similarity
        self.overlay_chromakey_blend = overlay_chromakey_blend
        self.out_dir = out_dir
        self.mute_audio = mute_audio
        self.output_format = output_format
        self.blur_background = blur_background
        self.strip_metadata = strip_metadata
        self.codec = codec
        self.subtitle_settings = subtitle_settings
        self.auto_crop = auto_crop
        self.overlay_audio = overlay_audio
        self.viral_clips_enabled = viral_clips_enabled
        self.viral_clip_duration = viral_clip_duration
        self.viral_clip_count = viral_clip_count
        self.censor_words = censor_words or []
        self.jumpcut_enabled = jumpcut_enabled
        self.jumpcut_aggressiveness = jumpcut_aggressiveness
        self.jumpcut_fade_duration = jumpcut_fade_duration
        
        # Конвертация процентов в десятичные значения
        self.original_volume = original_volume / 100
        self.overlay_volume = overlay_volume / 100
        
        # Флаг работы и результаты
        self._is_running = True
        self.output_paths = []
    
    def pick_zoom(self) -> int:
        """Выбирает значение zoom в зависимости от режима"""
        if self.zoom_mode == 'dynamic' and self.zoom_max >= self.zoom_min:
            try:
                return random.randint(self.zoom_min, self.zoom_max)
            except ValueError:
                return self.zoom_min
        return self.zoom_static
    
    def pick_speed(self) -> int:
        """Выбирает значение скорости в зависимости от режима"""
        if self.speed_mode == 'dynamic' and self.speed_max >= self.speed_min:
            try:
                return random.randint(self.speed_min, self.speed_max)
            except ValueError:
                return self.speed_min
        return self.speed_static
    
    def stop(self):
        """Остановка работы worker'а"""
        self._is_running = False
        print('Worker stop requested.')
    
    def run(self):
        """Основной метод обработки файлов"""
        print(f"[DEBUG] Jumpcut агрессивность: {self.jumpcut_aggressiveness}, затухание: {self.jumpcut_fade_duration}")
        total_files = len(self.files)
        
        # Проверка наличия файлов
        if total_files == 0:
            self.finished.emit()
            return
        
        # Создание выходной директории
        try:
            os.makedirs(self.out_dir, exist_ok=True)
        except OSError as e:
            self.error.emit(f'Не удалось создать выходную папку: {self.out_dir}\nОшибка: {e}')
            return
        
        # Обработка каждого файла
        for i, in_file_path in enumerate(self.files):
            # Проверка флага остановки
            if not self._is_running:
                print('Worker stopped.')
                break
            
            # Подготовка имён: дата-время + случайный хвост, чтобы выходы не затирали друг друга
            base_name = os.path.basename(in_file_path)
            name_part, _ = os.path.splitext(base_name)
            
            suffix = '_reels' if self.output_format != 'Оригинальный' else '_processed'
            file_unique = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{uuid.uuid4().hex[:8]}"
            out_file_name = f'{name_part}{suffix}_{file_unique}.mp4'
            out_file_path = os.path.join(self.out_dir, out_file_name)
            
            if os.path.abspath(in_file_path) == os.path.abspath(out_file_path):
                alt_out_file_name = f'{name_part}{suffix}_output_{file_unique}.mp4'
                out_file_path = os.path.join(self.out_dir, alt_out_file_name)
                print(f'Warning: Output path is same as input. Saving to: {alt_out_file_name}')
            
            # Уведомление о начале обработки файла
            self.file_processing.emit(base_name)
            self.file_progress.emit(0)
            
            # Проверка существования входного файла
            if not os.path.exists(in_file_path):
                self.error.emit(f'Входной файл не найден: {in_file_path}')
                continue
            
            # Инициализация переменных
            srt_path = None
            temp_audio_path = None
            temp_audio_paths = []
            full_audio_path = None
            full_srt_path = None
            crop_filter = None
            segment_srt_paths = []
            segment_titles = []
            full_transcription_text = []  # Для сбора текста из всех сегментов транскрипции
            smart_title = None  # Автоматически сгенерированное название
            
            try:
                try:
                    # Анализ черных полос если включен auto_crop
                    if self.auto_crop:
                        self.status_update.emit('Анализ черных полос...')
                        crop_filter = detect_crop_dimensions(in_file_path)
                        self.status_update.emit('Обработка...')
                    
                    subtitle_mode = self.subtitle_settings.get('mode')

                    segment_specs = [(None, None, '')]
                    if self.viral_clips_enabled:
                        self.status_update.emit('Поиск самых динамичных/виральных моментов...')
                        viral_segments = detect_viral_moments(
                            in_file_path,
                            clip_duration=self.viral_clip_duration,
                            max_clips=self.viral_clip_count
                        )
                        segment_specs = [
                            (start, seg_duration, f'_viral_{idx + 1}')
                            for idx, (start, seg_duration) in enumerate(viral_segments)
                        ]

                    # Обработка субтитров (проверка входных данных)
                    if subtitle_mode == 'srt_file':
                        # Использование готового SRT файла
                        srt_path = self.subtitle_settings.get('srt_path')
                        if not srt_path or not os.path.exists(srt_path):
                            raise FileNotFoundError(f'Файл субтитров не найден: {srt_path}')

                    for seg_i, (seg_start, seg_duration, seg_suffix) in enumerate(segment_specs, start=1):
                        current_zoom = self.pick_zoom()
                        current_speed = self.pick_speed()

                        current_out_file_path = out_file_path
                        if seg_suffix:
                            current_out_file_path = os.path.join(
                                self.out_dir,
                                f'{name_part}{suffix}{seg_suffix}_{file_unique}.mp4'
                            )
                        # Приводим путь к нормальному формату и убираем смешанные слэши
                        current_out_file_path = os.path.normpath(current_out_file_path)

                        # Этап 1: обрабатываем/режем БЕЗ баннера и без субтитров.
                        process_single(
                            in_path=in_file_path,
                            out_path=current_out_file_path,
                            filters=self.filters,
                            zoom_p=current_zoom,
                            speed_p=current_speed,
                            overlay_file=None,
                            overlay_alignment=self.overlay_alignment,
                            overlay_margin_v=self.overlay_margin_v,
                            overlay_margin_lr=self.overlay_margin_lr,
                            overlay_scale_p=self.overlay_scale_p,
                            overlay_chromakey=self.overlay_chromakey,
                            overlay_chromakey_color=self.overlay_chromakey_color,
                            overlay_chromakey_similarity=self.overlay_chromakey_similarity,
                            overlay_chromakey_blend=self.overlay_chromakey_blend,
                            output_format=self.output_format,
                            blur_background=self.blur_background,
                            mute_audio=self.mute_audio,
                            strip_metadata=self.strip_metadata,
                            codec=self.codec,
                            srt_path=None,
                            subtitle_style=self.subtitle_settings.get('style', {}),
                            crop_filter=crop_filter,
                            overlay_audio_path=self.overlay_audio,
                            original_volume=self.original_volume,
                            overlay_volume=self.overlay_volume,
                            progress_callback=self.file_progress.emit,
                            trim_start=seg_start,
                            trim_duration=seg_duration
                        )

                        # Удаляем тишину из готового фрагмента (Jump Cut)
                        if self.jumpcut_enabled:
                            try:
                                self.status_update.emit('Удаление тишины из ролика (Jump Cut)...')
                                trimmed_clip = os.path.join(tempfile.gettempdir(), f'{uuid.uuid4()}_jumpcut.mp4')
                                
                                # Настройки jumpcut
                                aggressiveness_levels = [-25.0, -35.0, -45.0]  # не сильно, средне, сильно
                                silence_db = aggressiveness_levels[min(self.jumpcut_aggressiveness, 2)]
                                padding = self.jumpcut_fade_duration
                                
                                remove_silence_from_video(
                                    input_path=current_out_file_path,
                                    output_path=trimmed_clip,
                                    silence_db=silence_db,
                                    silence_duration=1.0,
                                    padding=padding
                                )
                                if os.path.exists(trimmed_clip):
                                    # Перемещение файла возможное между дисками C: и D:
                                    try:
                                        shutil.move(trimmed_clip, current_out_file_path)
                                    except Exception:
                                        # fallback на replace, если тот же диск
                                        if os.path.exists(current_out_file_path):
                                            os.remove(current_out_file_path)
                                        os.replace(trimmed_clip, current_out_file_path)
                                    self.status_update.emit('Тишина удалена. Продолжаем обработку.')
                            except Exception as silence_err:
                                logging.warning(f'Не удалось выполнить удаление тишины: {silence_err}')
                        else:
                            self.status_update.emit('Jump Cut отключен. Продолжаем обработку.')

                        # Этап 1.5: наложение баннера после Jump Cut, чтобы баннер не обрезался.
                        if self.overlay_file and os.path.exists(current_out_file_path):
                            try:
                                self.status_update.emit('Накладываем баннер после Jump Cut...')
                                banner_tmp = os.path.join(tempfile.gettempdir(), f'{uuid.uuid4()}_banner.mp4')
                                process_single(
                                    in_path=current_out_file_path,
                                    out_path=banner_tmp,
                                    filters=[],
                                    zoom_p=100,
                                    speed_p=100,
                                    overlay_file=self.overlay_file,
                                    overlay_alignment=self.overlay_alignment,
                                    overlay_margin_v=self.overlay_margin_v,
                                    overlay_margin_lr=self.overlay_margin_lr,
                                    overlay_scale_p=self.overlay_scale_p,
                                    overlay_chromakey=self.overlay_chromakey,
                                    overlay_chromakey_color=self.overlay_chromakey_color,
                                    overlay_chromakey_similarity=self.overlay_chromakey_similarity,
                                    overlay_chromakey_blend=self.overlay_chromakey_blend,
                                    output_format=self.output_format,
                                    blur_background=False,
                                    mute_audio=False,
                                    strip_metadata=self.strip_metadata,
                                    codec=self.codec,
                                    srt_path=None,
                                    subtitle_style=None,
                                    crop_filter=None,
                                    overlay_audio_path=None,
                                    original_volume=1.0,
                                    overlay_volume=1.0,
                                    progress_callback=self.file_progress.emit,
                                )
                                if os.path.exists(banner_tmp):
                                    try:
                                        shutil.move(banner_tmp, current_out_file_path)
                                    except Exception:
                                        if os.path.exists(current_out_file_path):
                                            os.remove(current_out_file_path)
                                        os.replace(banner_tmp, current_out_file_path)
                                    self.status_update.emit('Баннер наложен.')
                            except Exception as banner_err:
                                logging.warning(f'Не удалось наложить баннер после Jump Cut: {banner_err}')

                        # Этап 2: отдельное распознавание/вшивание субтитров в уже нарезанный файл.
                        effective_srt_path = None
                        if subtitle_mode == 'whisper':
                            seg_audio_path = os.path.join(tempfile.gettempdir(), f'{uuid.uuid4()}.wav')
                            seg_srt_path = os.path.join(tempfile.gettempdir(), f'{uuid.uuid4()}.srt')
                            self.status_update.emit(
                                f'Распознавание речи для готового клипа {seg_i}/{len(segment_specs)}...'
                            )
                            if not os.path.exists(current_out_file_path):
                                logging.error(f'Output file not found for audio extraction: {current_out_file_path}')
                                continue
                            extract_audio(current_out_file_path, seg_audio_path)
                            generate_srt_from_whisper(
                                audio_path=seg_audio_path,
                                srt_path=seg_srt_path,
                                model_name=self.subtitle_settings.get('model'),
                                language=self.subtitle_settings.get('language'),
                                words_per_line=self.subtitle_settings.get('words_per_line'),
                                censor_words=self.censor_words if self.censor_words else None
                            )
                            temp_audio_paths.append(seg_audio_path)
                            segment_srt_paths.append(seg_srt_path)
                            effective_srt_path = seg_srt_path
                        elif subtitle_mode == 'srt_file':
                            effective_srt_path = srt_path
                            if seg_start is not None and seg_duration is not None:
                                seg_srt_path = os.path.join(tempfile.gettempdir(), f'{uuid.uuid4()}.srt')
                                effective_srt_path = build_segment_srt(
                                    source_srt_path=srt_path,
                                    out_srt_path=seg_srt_path,
                                    segment_start=seg_start,
                                    segment_duration=seg_duration
                                )
                                segment_srt_paths.append(seg_srt_path)

                        subtitle_style_for_burn = dict(self.subtitle_settings.get('style') or {})
                        if self.output_format == REELS_FORMAT_NAME:
                            sw, sh = get_video_dimensions(in_file_path)
                            lb_inset = reels_letterbox_vertical_inset_px(
                                sw, sh, crop_filter, current_zoom
                            )
                            if lb_inset > 0:
                                subtitle_style_for_burn['reels_letterbox_inset'] = lb_inset

                        if effective_srt_path and os.path.exists(effective_srt_path) and os.path.getsize(effective_srt_path) > 0:
                            try:
                                tmp_sub_out = os.path.join(self.out_dir, f'{uuid.uuid4()}_subpass.mp4')
                                burn_subtitles_postprocess(
                                    in_path=current_out_file_path,
                                    out_path=tmp_sub_out,
                                    srt_path=effective_srt_path,
                                    subtitle_style=subtitle_style_for_burn,
                                    codec=self.codec,
                                    plain_subtitles=False
                                )
                                if os.path.exists(tmp_sub_out):
                                    os.remove(current_out_file_path)
                                    os.replace(tmp_sub_out, current_out_file_path)
                            except Exception as post_err:
                                logging.exception('Postprocess subtitle burn failed (styled)')
                                try:
                                    # fallback: простой режим без force_style
                                    tmp_sub_out_plain = os.path.join(self.out_dir, f'{uuid.uuid4()}_subpass_plain.mp4')
                                    burn_subtitles_postprocess(
                                        in_path=current_out_file_path,
                                        out_path=tmp_sub_out_plain,
                                        srt_path=effective_srt_path,
                                        subtitle_style=subtitle_style_for_burn,
                                        codec=self.codec,
                                        plain_subtitles=True
                                    )
                                    if os.path.exists(tmp_sub_out_plain):
                                        os.remove(current_out_file_path)
                                        os.replace(tmp_sub_out_plain, current_out_file_path)
                                        self.status_update.emit('Субтитры вшиты в простом режиме.')
                                    else:
                                        raise RuntimeError('Plain subtitle postprocess did not produce output file.')
                                except Exception as plain_err:
                                    try:
                                        sidecar_srt = os.path.splitext(current_out_file_path)[0] + '.srt'
                                        shutil.copyfile(effective_srt_path, sidecar_srt)
                                        self.status_update.emit('Вшивание сабов не удалось, сохранен .srt рядом с видео.')
                                    except Exception:
                                        pass
                                    logging.exception('Postprocess subtitle burn failed (plain)')
                        elif subtitle_mode != 'none':
                            self.status_update.emit('Субтитры пустые/некорректные, вшивание пропущено.')
                        
                        # Собираем текст из субтитров для автоматического названия видео
                        segment_title = None
                        if effective_srt_path and os.path.exists(effective_srt_path):
                            try:
                                srt_text = self._extract_text_from_srt(effective_srt_path)
                                if srt_text:
                                    full_transcription_text.append(srt_text)
                                    title_candidate = generate_smart_title(srt_text)
                                    if title_candidate and title_candidate.lower() != 'video':
                                        if self.censor_words:
                                            title_candidate = censor_words_in_text(title_candidate, self.censor_words, '*')
                                        title_candidate = sanitize_title_for_filename(title_candidate)
                                        title_candidate = safe_filename(title_candidate)
                                        segment_title = title_candidate
                            except Exception as srt_read_err:
                                logging.warning(f'Не удалось прочитать текст из SRT: {srt_read_err}')

                        if not segment_title:
                            segment_title = f'{name_part} #{seg_i}'
                        segment_titles.append(segment_title)
                        self.output_paths.append(current_out_file_path)
                    
                    # Переименовываем каждый сегмент по своему индивидуальному названию
                    if self.output_paths:
                        time_part = file_unique.split('_')[1] if '_' in file_unique else '00-00-00'
                        unique_part = file_unique.split('_')[-1][:5] if '_' in file_unique else file_unique

                        for idx, original_output_path in enumerate(list(self.output_paths)):
                            original_dir = os.path.dirname(original_output_path)
                            _, ext = os.path.splitext(original_output_path)
                            ext = ext.strip()  # Убираем случайные пробелы
                            title_for_file = segment_titles[idx] if idx < len(segment_titles) else name_part
                            title_for_file = title_for_file.strip() or name_part
                            title_for_file = safe_filename(title_for_file)
                            if len(self.output_paths) == 1:
                                base_new_name = f'{title_for_file} ({time_part}_{unique_part}){ext}'
                            else:
                                base_new_name = f'{title_for_file} ({time_part}_{unique_part})_{idx + 1}{ext}'

                            base_new_name = ' '.join(base_new_name.split())
                            new_output_path = os.path.join(original_dir, base_new_name)

                            try:
                                if os.path.exists(original_output_path):
                                    os.replace(original_output_path, new_output_path)
                                    self.output_paths[idx] = new_output_path
                                    self.status_update.emit(f'Файл переименован: {title_for_file}')
                                    logging.info(f'Видео переименовано на: {base_new_name}')
                            except Exception as rename_err:
                                logging.warning(f'Не удалось переименовать файл: {rename_err}')
                    
                    # Обновление общего прогресса (по исходным файлам)
                    self.progress.emit(i + 1, total_files)
                    
                except Exception as e:
                    # Обработка ошибок
                    error_msg = f"Ошибка при обработке файла '{base_name}':\n{type(e).__name__}: {e}"
                    
                    # Дополнительная информация для ошибок subprocess
                    if isinstance(e, subprocess.CalledProcessError) and e.output:
                        error_msg += f'\n\nFFmpeg output:\n{e.output[-500:]}'
                    
                    print(f'Error in worker thread: {error_msg}')
                    self.error.emit(error_msg)
                    
            finally:
                # Очистка временных файлов
                import time
                def safe_remove(file_path):
                    if file_path and os.path.exists(file_path):
                        for _ in range(3):
                            try:
                                os.remove(file_path)
                                break
                            except OSError as e:
                                if _ == 2:
                                    print(f'Error removing temp file {file_path}: {e}')
                                time.sleep(0.5)
                
                safe_remove(temp_audio_path)
                
                for seg_audio in temp_audio_paths:
                    safe_remove(seg_audio)
                
                if srt_path and subtitle_mode == 'whisper' and os.path.exists(srt_path):
                    safe_remove(srt_path)
                
                for seg_srt in segment_srt_paths:
                    safe_remove(seg_srt)
        
        # Завершение работы
        if self._is_running:
            print('Worker finished processing all files.')
            self.finished.emit()
        else:
            print('Worker finished due to stop request.')
    
    def _extract_text_from_srt(self, srt_path: str) -> str:
        """
        Извлекает весь текст из SRT файла (без временных меток и индексов).
        
        Args:
            srt_path: Путь к SRT файлу
            
        Returns:
            Объединенный текст из всех субтитров
        """
        if not os.path.exists(srt_path):
            return ""
        
        try:
            with open(srt_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Парсим SRT: блоки разделены пустыми строками
            # Каждый блок: индекс\nвремя\nтекст
            blocks = [b.strip() for b in content.split('\n\n') if b.strip()]
            text_lines = []
            
            for block in blocks:
                lines = block.split('\n')
                if len(lines) >= 3:
                    # Текст идет с 3-й строки (индекс=0, время=1, текст=2+)
                    text_lines.extend(lines[2:])
            
            # Объединяем в один текст
            full_text = ' '.join(text_lines).strip()
            return full_text
        except Exception as e:
            logging.error(f'Ошибка при извлечении текста из SRT: {e}')
            return ""