import os
import sys
import random
import tempfile
import uuid
import shutil
import logging
import copy
from typing import Optional, Tuple

from PyQt5.QtCore import Qt, QPoint, QPointF, QRectF, QUrl, pyqtSignal, QThread, QTimer

try:
    from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
    from PyQt5.QtMultimediaWidgets import QVideoWidget
    PREVIEW_CLIP_AVAILABLE = True
except ImportError:
    QMediaPlayer = None  # type: ignore
    QMediaContent = None  # type: ignore
    QVideoWidget = None  # type: ignore
    PREVIEW_CLIP_AVAILABLE = False
from PyQt5.QtGui import (
    QFontMetrics, QIcon, QPixmap, QFont, QFontDatabase,
    QColor, QPainter, QPainterPath, QPen
)
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QAbstractItemView, QFileDialog, QSpinBox, QDoubleSpinBox, QLineEdit,
    QMessageBox, QProgressBar, QComboBox, QGroupBox, QRadioButton,
    QButtonGroup, QCheckBox, QSplitter, QListWidgetItem, QTabWidget,
    QMenu, QFrame, QStackedWidget, QInputDialog, QPlainTextEdit, QColorDialog,
    QSlider, QApplication, QSizePolicy, QScrollArea
)

import qtawesome as qta
from uploader_core.config_manager import ConfigManager
from workers.worker import Worker
from utils.file_utils import is_video_file, find_videos_in_folder
from utils.constants import (
    FILTERS, OVERLAY_LEGACY_POS_TO_ALIGNMENT, REELS_FORMAT_NAME, OUTPUT_FORMATS,
    CODECS, WHISPER_MODELS, WHISPER_LANGUAGES, APP_NAME, APP_VERSION
)
from utils.ffmpeg_utils import (
    generate_preview,
    generate_preview_clip,
    get_video_duration,
    detect_crop_dimensions,
    get_video_dimensions,
    reels_preview_bars_heights,
    _ass_font_size_for_video,
    _subtitle_effective_outline,
)
from uploader_ui.uploader_widget import UploaderWidget
from utils.path_utils import resource_path


class PreviewWorker(QThread):
    finished_signal = pyqtSignal(str, str)
    error_signal = pyqtSignal(str)
    
    def __init__(self, params):
        super().__init__()
        self.params = params
    
    def run(self):
        try:
            p = self.params
            common = {
                k: v for k, v in p.items()
                if k not in ('out_path_png', 'out_path_clip', 'clip_seconds', 'enable_video_clip')
            }
            generate_preview(out_path=p['out_path_png'], **common)
            clip_dest = p.get('out_path_clip') or ''
            clip_written = ''
            if clip_dest and p.get('enable_video_clip', True):
                generate_preview_clip(
                    out_path=clip_dest,
                    max_seconds=float(p.get('clip_seconds', 8.0)),
                    **common,
                )
                clip_written = clip_dest
            self.finished_signal.emit(p['out_path_png'], clip_written)
        except Exception as e:
            self.error_signal.emit(str(e))


class DropListWidget(QListWidget):
    files_dropped = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            added_files = False
            
            for url in event.mimeData().urls():
                fp = url.toLocalFile()
                
                if os.path.isdir(fp):
                    vids = find_videos_in_folder(fp)
                    for v in vids:
                        if is_video_file(v) and not self.is_already_added(v):
                            it = QListWidgetItem(v)
                            it.setData(Qt.UserRole, v)
                            self.addItem(it)
                            added_files = True
                elif is_video_file(fp) or fp.lower().endswith('.gif'):
                    if not self.is_already_added(fp):
                        it = QListWidgetItem(fp)
                        it.setData(Qt.UserRole, fp)
                        self.addItem(it)
                        added_files = True
            
            if added_files:
                self.files_dropped.emit()
        else:
            event.ignore()
    
    def is_already_added(self, file_path):
        for i in range(self.count()):
            if self.item(i).data(Qt.UserRole) == file_path:
                return True
        return False


class ZoomPanPreview(QWidget):
    """
    Кадр предпросмотра: без начального увеличения (вписан в область), масштаб только Ctrl + колёсико,
    сдвиг левой кнопкой мыши при увеличении или если кадр больше окна.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._drag_origin = None
        self._pan_at_drag = QPointF(0.0, 0.0)
        self._message = "Выберите видео и нажмите 'Обновить'"
        self.setMinimumHeight(220)
        self.setStyleSheet('background: #1a1a1e; border: 1px solid #555;')
        self.setMouseTracking(True)
        self.setToolTip(
            'Ctrl + колёсико мыши — масштаб. Зажмите левую кнопку и перетащите — сдвиг кадра.'
        )

    def set_message(self, text: str):
        self._message = text
        self._pixmap = QPixmap()
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def set_image(self, pixmap: QPixmap):
        self._message = ''
        if pixmap is not None and not pixmap.isNull():
            self._pixmap = pixmap.copy()
        else:
            self._pixmap = QPixmap()
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def _fit_scale(self) -> float:
        if self._pixmap.isNull():
            return 1.0
        iw, ih = self._pixmap.width(), self._pixmap.height()
        vw, vh = max(1, self.width()), max(1, self.height())
        return min(vw / iw, vh / ih)

    def _draw_geometry(self):
        """Возвращает (x, y, dw, dh) для отрисовки в координатах виджета."""
        if self._pixmap.isNull():
            return 0.0, 0.0, 0.0, 0.0
        fs = self._fit_scale() * self._zoom
        dw = self._pixmap.width() * fs
        dh = self._pixmap.height() * fs
        vw, vh = float(self.width()), float(self.height())
        x = (vw - dw) / 2.0 + self._pan.x()
        y = (vh - dh) / 2.0 + self._pan.y()
        return x, y, dw, dh

    def _clamp_pan(self):
        if self._pixmap.isNull():
            return
        x, y, dw, dh = self._draw_geometry()
        vw, vh = float(self.width()), float(self.height())
        fs = self._fit_scale() * self._zoom
        base_x = (vw - self._pixmap.width() * fs) / 2.0
        base_y = (vh - self._pixmap.height() * fs) / 2.0
        nx = x
        ny = y
        if dw > vw:
            nx = max(vw - dw, min(0.0, x))
        else:
            nx = base_x
        if dh > vh:
            ny = max(vh - dh, min(0.0, y))
        else:
            ny = base_y
        self._pan.setX(self._pan.x() + (nx - x))
        self._pan.setY(self._pan.y() + (ny - y))

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(26, 26, 30))
        if self._pixmap.isNull():
            p.setPen(QColor(180, 180, 185))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self._message)
            return
        x, y, dw, dh = self._draw_geometry()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawPixmap(QRectF(x, y, dw, dh), self._pixmap, QRectF(self._pixmap.rect()))

    def wheelEvent(self, event):
        if not (event.modifiers() & Qt.ControlModifier):
            return super().wheelEvent(event)
        if self._pixmap.isNull():
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        steps = delta / 120.0
        factor = 1.1 ** steps
        self._zoom = max(0.2, min(10.0, self._zoom * factor))
        self._clamp_pan()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._pixmap.isNull():
            self._drag_origin = event.pos()
            self._pan_at_drag = QPointF(self._pan)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._drag_origin is not None
            and event.buttons() & Qt.LeftButton
            and not self._pixmap.isNull()
        ):
            delta = QPointF(event.pos() - self._drag_origin)
            self._pan = self._pan_at_drag + delta
            self._clamp_pan()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_origin = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._pixmap.isNull():
            self._clamp_pan()
            self.update()


class ProcessingWidgetContent(QWidget):
    video_processed = pyqtSignal(str)
    
    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.parent_window = parent_window
        self.preview_thread = None
        self.processing_thread = None
        self.last_output_path = None
        self._preview_signature = None
        self._preview_signature_pending = None
        self._preview_frame_pixmap = None
        self._preview_auto_no_message = False
        self._clip_last_path = None
        self._preview_refresh_timer = QTimer(self)
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.setInterval(450)
        self._preview_refresh_timer.timeout.connect(self._run_deferred_effects_preview)
        self.init_ui()
    
    def init_ui(self):
        # Основной layout
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # Сплиттер для разделения левой и правой панели
        main_splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(main_splitter)
        
        # Левая панель: вертикальный сплиттер ~75% предпросмотр / ~25% список и кнопки
        left_widget = QWidget()
        self.left_panel = QVBoxLayout(left_widget)
        self.left_panel.setContentsMargins(0, 0, 0, 0)
        self.left_panel.setSpacing(0)

        self._left_vert_splitter = QSplitter(Qt.Vertical)
        self._left_vert_splitter.setChildrenCollapsible(False)

        preview_group = QGroupBox('Предпросмотр')
        preview_layout = QVBoxLayout(preview_group)
        self.preview_stack = QStackedWidget()
        self.preview_video_host = QWidget()
        pvh_layout = QVBoxLayout(self.preview_video_host)
        pvh_layout.setContentsMargins(0, 0, 0, 0)
        if PREVIEW_CLIP_AVAILABLE and QVideoWidget is not None:
            self.preview_video = QVideoWidget()
            self.preview_video.setStyleSheet('background: #000000;')
            self.preview_video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.preview_video.setMinimumHeight(200)
            pvh_layout.addWidget(self.preview_video)
            self._clip_player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
            self._clip_player.setVideoOutput(self.preview_video)
            self._clip_player.mediaStatusChanged.connect(self._on_clip_media_status)
        else:
            self.preview_video = None
            self._clip_player = None
            _no_vid = QLabel('Видео-превью недоступно (PyQt5.QtMultimedia). Статичный кадр — ниже.')
            _no_vid.setWordWrap(True)
            _no_vid.setStyleSheet('color: #888;')
            pvh_layout.addWidget(_no_vid)
        self.main_preview = ZoomPanPreview(self)
        self.main_preview.setObjectName('previewLabel')
        self.preview_stack.addWidget(self.preview_video_host)
        self.preview_stack.addWidget(self.main_preview)
        self.preview_stack.setMinimumHeight(220)
        self.preview_stack.setCurrentIndex(1)
        preview_layout.addWidget(self.preview_stack)
        mode_preview_row = QHBoxLayout()
        self.btn_preview_video_mode = QPushButton('Видео (первые ~8 с)')
        self.btn_preview_still_mode = QPushButton('Кадр + субтитры')
        self.btn_preview_video_mode.setCheckable(True)
        self.btn_preview_still_mode.setCheckable(True)
        self.btn_preview_still_mode.setChecked(True)
        self.btn_preview_video_mode.setEnabled(False)
        self._preview_mode_group = QButtonGroup(self)
        self._preview_mode_group.setExclusive(True)
        self._preview_mode_group.addButton(self.btn_preview_video_mode, 0)
        self._preview_mode_group.addButton(self.btn_preview_still_mode, 1)
        self.btn_preview_video_mode.clicked.connect(lambda: self._set_preview_stack_visual(0))
        self.btn_preview_still_mode.clicked.connect(lambda: self._set_preview_stack_visual(1))
        mode_preview_row.addWidget(self.btn_preview_video_mode)
        mode_preview_row.addWidget(self.btn_preview_still_mode)
        mode_preview_row.addStretch()
        preview_layout.addLayout(mode_preview_row)
        self.preview_button = QPushButton('Обновить предпросмотр')
        preview_layout.addWidget(self.preview_button)
        self._left_vert_splitter.addWidget(preview_group)
        self._left_vert_splitter.setStretchFactor(0, 3)

        bottom_left = QWidget()
        bottom_left_layout = QVBoxLayout(bottom_left)
        bottom_left_layout.setContentsMargins(0, 0, 0, 0)
        bottom_left_layout.setSpacing(10)
        top_left_container = QWidget()
        top_left_layout = QVBoxLayout(top_left_container)
        top_left_layout.setContentsMargins(0, 0, 0, 0)
        self.video_list_widget = DropListWidget(parent=self)
        self.video_list_widget.customContextMenuRequested.connect(self.on_list_menu)
        top_left_layout.addWidget(self.video_list_widget)
        dnd_label = QLabel('Перетащите файлы или папки сюда')
        dnd_label.setAlignment(Qt.AlignCenter)
        dnd_label.setStyleSheet('color: gray; font-style: italic;')
        top_left_layout.addWidget(dnd_label)
        bottom_left_layout.addWidget(top_left_container, 1)
        add_buttons_layout = QHBoxLayout()
        btn_add = QPushButton('Добавить видео/GIF')
        btn_folder = QPushButton('Добавить папку')
        btn_clear = QPushButton('Очистить список')
        add_buttons_layout.addWidget(btn_add)
        add_buttons_layout.addWidget(btn_folder)
        add_buttons_layout.addWidget(btn_clear)
        bottom_left_layout.addLayout(add_buttons_layout)
        self._left_vert_splitter.addWidget(bottom_left)
        self._left_vert_splitter.setStretchFactor(1, 1)
        self._left_vert_splitter.setSizes([600, 200])

        self.left_panel.addWidget(self._left_vert_splitter)

        # Правая панель
        right_widget = QWidget()
        self.right_panel = QVBoxLayout(right_widget)
        self.right_panel.setSpacing(10)
        
        main_splitter.addWidget(left_widget)
        main_splitter.addWidget(right_widget)
        main_splitter.setSizes([350, 750])
        
        # Вкладки настроек
        tab_widget = QTabWidget()
        self.right_panel.addWidget(tab_widget)
        
        # Создание вкладок
        main_tab = QWidget()
        transform_tab = QWidget()
        effects_tab = QScrollArea()
        effects_tab.setWidgetResizable(True)
        effects_tab.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        effects_tab.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        effects_tab_content = QWidget()
        effects_tab.setWidget(effects_tab_content)
        audio_tab = QWidget()
        censor_tab = QScrollArea()
        censor_tab.setWidgetResizable(True)
        censor_tab.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        censor_tab.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        censor_tab_content = QWidget()
        censor_tab.setWidget(censor_tab_content)
        
        tab_widget.addTab(main_tab, 'Меню')
        tab_widget.addTab(transform_tab, 'Трансформация')
        tab_widget.addTab(effects_tab, 'Наложение')
        tab_widget.addTab(audio_tab, 'Аудио')
        tab_widget.addTab(censor_tab, 'Цензура')
        
        # Layouts для вкладок
        main_tab_layout = QVBoxLayout(main_tab)
        transform_tab_layout = QVBoxLayout(transform_tab)
        effects_tab_layout = QVBoxLayout(effects_tab_content)
        audio_tab_layout = QVBoxLayout(audio_tab)
        censor_tab_layout = QVBoxLayout(censor_tab_content)
        
        # === ГЛАВНАЯ ВКЛАДКА ===

        # Группа пресетов
        self.presets_group = QGroupBox('Пресеты обработки')
        presets_layout = QVBoxLayout(self.presets_group)
        presets_row = QHBoxLayout()
        self.presets_combo = QComboBox()
        self.presets_combo.setPlaceholderText('Выберите пресет')
        btn_preset_apply = QPushButton('Применить')
        btn_preset_save = QPushButton('Сохранить текущий')
        btn_preset_delete = QPushButton('Удалить')
        presets_row.addWidget(self.presets_combo, 1)
        presets_row.addWidget(btn_preset_apply)
        presets_row.addWidget(btn_preset_save)
        presets_row.addWidget(btn_preset_delete)
        presets_layout.addLayout(presets_row)
        main_tab_layout.addWidget(self.presets_group)
        
        # Группа формата вывода
        self.output_format_group = QGroupBox('Формат и кодирование')
        ofg_layout = QVBoxLayout(self.output_format_group)
        
        ofg_layout.addWidget(QLabel('Формат вывода:'))
        self.output_format_combo = QComboBox()
        self.output_format_combo.addItems(OUTPUT_FORMATS)
        self.output_format_combo.currentTextChanged.connect(self.on_output_format_changed)
        ofg_layout.addWidget(self.output_format_combo)
        
        self.blur_background_checkbox = QCheckBox('Размыть фон')
        self.blur_background_checkbox.setToolTip('Заполняет черные полосы размытой версией видео (только для Reels)')
        self.blur_background_checkbox.setEnabled(False)
        ofg_layout.addWidget(self.blur_background_checkbox)
        
        ofg_layout.addWidget(QLabel('Видеокодек:'))
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(CODECS.keys())
        self.codec_combo.setToolTip('Аппаратные кодеки (NVIDIA, Intel, AMD) могут значительно ускорить обработку')
        ofg_layout.addWidget(self.codec_combo)
        
        main_tab_layout.addWidget(self.output_format_group)
        main_tab_layout.addStretch()
        
        # === ВКЛАДКА ТРАНСФОРМАЦИИ ===
        
        # Группа обрезки
        self.crop_group = QGroupBox('Обрезка')
        crop_layout = QVBoxLayout(self.crop_group)
        
        self.auto_crop_checkbox = QCheckBox('Обрезать черные полосы (интеллектуально)')
        self.auto_crop_checkbox.setToolTip('Автоматически определяет и обрезает киношные черные полосы в видео')
        crop_layout.addWidget(self.auto_crop_checkbox)
        
        transform_tab_layout.addWidget(self.crop_group)
        
        # Группа фильтров
        self.filter_group = QGroupBox('Фильтры')
        f_lay = QVBoxLayout(self.filter_group)
        
        self.filter_list = QListWidget()
        self.filter_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for fn in FILTERS:
            self.filter_list.addItem(fn)
        self.filter_list.setFixedHeight(150)
        f_lay.addWidget(self.filter_list)
        
        transform_tab_layout.addWidget(self.filter_group)
        
        # Группа Zoom
        self.zoom_group = QGroupBox('Zoom (приближение)')
        zg_lay = QVBoxLayout(self.zoom_group)
        
        # Радио кнопки для режима zoom
        z_mode = QHBoxLayout()
        self.zoom_static_radio = QRadioButton('Статическое (%):')
        self.zoom_dynamic_radio = QRadioButton('Диапазон (%):')
        self.zoom_static_radio.setChecked(True)
        
        self.zoom_button_group = QButtonGroup()
        self.zoom_button_group.addButton(self.zoom_static_radio)
        self.zoom_button_group.addButton(self.zoom_dynamic_radio)
        self.zoom_button_group.buttonClicked.connect(self.on_zoom_mode_changed)
        
        z_mode.addWidget(self.zoom_static_radio)
        z_mode.addWidget(self.zoom_dynamic_radio)
        zg_lay.addLayout(z_mode)
        
        # Статический zoom
        self.zoom_static_widget = QWidget()
        zsw_lay = QHBoxLayout(self.zoom_static_widget)
        zsw_lay.setContentsMargins(0, 0, 0, 0)
        
        self.zoom_static_spin = QSpinBox()
        self.zoom_static_spin.setRange(50, 300)
        self.zoom_static_spin.setValue(100)
        self.zoom_static_spin.setFixedWidth(80)
        zsw_lay.addWidget(self.zoom_static_spin)
        zsw_lay.addStretch()
        
        zg_lay.addWidget(self.zoom_static_widget)
        
        # Динамический zoom
        self.zoom_dynamic_widget = QWidget()
        zdd_lay = QHBoxLayout(self.zoom_dynamic_widget)
        zdd_lay.setContentsMargins(0, 0, 0, 0)
        
        self.zoom_min_spin = QSpinBox()
        self.zoom_min_spin.setRange(50, 300)
        self.zoom_min_spin.setValue(80)
        
        self.zoom_max_spin = QSpinBox()
        self.zoom_max_spin.setRange(50, 300)
        self.zoom_max_spin.setValue(120)
        
        zdd_lay.addWidget(QLabel('Мин:'))
        zdd_lay.addWidget(self.zoom_min_spin)
        zdd_lay.addWidget(QLabel('Макс:'))
        zdd_lay.addWidget(self.zoom_max_spin)
        zdd_lay.addStretch()
        
        zg_lay.addWidget(self.zoom_dynamic_widget)
        self.zoom_dynamic_widget.setVisible(False)
        
        transform_tab_layout.addWidget(self.zoom_group)
        
        # Группа скорости
        self.speed_group = QGroupBox('Скорость')
        sp_lay = QVBoxLayout(self.speed_group)
        
        # Радио кнопки для режима скорости
        sp_mode = QHBoxLayout()
        self.speed_static_radio = QRadioButton('Статическое (%):')
        self.speed_dynamic_radio = QRadioButton('Диапазон (%):')
        self.speed_static_radio.setChecked(True)
        
        self.speed_button_group = QButtonGroup()
        self.speed_button_group.addButton(self.speed_static_radio)
        self.speed_button_group.addButton(self.speed_dynamic_radio)
        self.speed_button_group.buttonClicked.connect(self.on_speed_mode_changed)
        
        sp_mode.addWidget(self.speed_static_radio)
        sp_mode.addWidget(self.speed_dynamic_radio)
        sp_lay.addLayout(sp_mode)
        
        # Статическая скорость
        self.speed_static_widget = QWidget()
        ssw2 = QHBoxLayout(self.speed_static_widget)
        ssw2.setContentsMargins(0, 0, 0, 0)
        
        self.speed_static_spin = QSpinBox()
        self.speed_static_spin.setRange(50, 200)
        self.speed_static_spin.setValue(100)
        self.speed_static_spin.setFixedWidth(80)
        ssw2.addWidget(self.speed_static_spin)
        ssw2.addStretch()
        
        sp_lay.addWidget(self.speed_static_widget)
        
        # Динамическая скорость
        self.speed_dynamic_widget = QWidget()
        sdy2 = QHBoxLayout(self.speed_dynamic_widget)
        sdy2.setContentsMargins(0, 0, 0, 0)
        
        self.speed_min_spin = QSpinBox()
        self.speed_min_spin.setRange(50, 200)
        self.speed_min_spin.setValue(90)
        
        self.speed_max_spin = QSpinBox()
        self.speed_max_spin.setRange(50, 200)
        self.speed_max_spin.setValue(110)
        
        sdy2.addWidget(QLabel('Мин:'))
        sdy2.addWidget(self.speed_min_spin)
        sdy2.addWidget(QLabel('Макс:'))
        sdy2.addWidget(self.speed_max_spin)
        sdy2.addStretch()
        
        sp_lay.addWidget(self.speed_dynamic_widget)
        self.speed_dynamic_widget.setVisible(False)
        
        transform_tab_layout.addWidget(self.speed_group)

        # Группа авто-нарезки виральных моментов
        self.viral_group = QGroupBox('Виральные моменты')
        viral_layout = QVBoxLayout(self.viral_group)

        self.viral_enable_checkbox = QCheckBox('Авто-нарезка самых динамичных моментов')
        self.viral_enable_checkbox.setToolTip(
            'Анализирует видео по динамике сцен и громкости, затем сохраняет лучшие фрагменты'
        )
        viral_layout.addWidget(self.viral_enable_checkbox)

        viral_params_layout = QHBoxLayout()
        viral_params_layout.addWidget(QLabel('Длительность (сек):'))
        self.viral_duration_spin = QSpinBox()
        self.viral_duration_spin.setRange(5, 60)
        self.viral_duration_spin.setValue(15)
        viral_params_layout.addWidget(self.viral_duration_spin)

        viral_params_layout.addWidget(QLabel('Кол-во клипов:'))
        self.viral_count_spin = QSpinBox()
        self.viral_count_spin.setRange(1, 10)
        self.viral_count_spin.setValue(3)
        viral_params_layout.addWidget(self.viral_count_spin)
        viral_params_layout.addStretch()
        viral_layout.addLayout(viral_params_layout)

        transform_tab_layout.addWidget(self.viral_group)
        transform_tab_layout.addStretch()
        
        # === ВКЛАДКА НАЛОЖЕНИЙ ===
        
        # Группа наложения баннера
        self.overlay_group = QGroupBox('Наложение (баннер)')
        ov_lay = QVBoxLayout(self.overlay_group)
        
        # Строка с файлом
        row_ol = QHBoxLayout()
        self.overlay_path = QLineEdit()
        self.overlay_path.setPlaceholderText('PNG, JPG, GIF, MP4, MOV, WebM… (видео зацикливается под длину ролика)')
        
        btn_ol = QPushButton('Обзор...')
        btn_clear_ol = QPushButton('X')
        btn_clear_ol.setFixedWidth(30)
        btn_clear_ol.setToolTip('Очистить поле наложения')
        
        row_ol.addWidget(QLabel('Файл:'))
        row_ol.addWidget(self.overlay_path)
        row_ol.addWidget(btn_ol)
        row_ol.addWidget(btn_clear_ol)
        ov_lay.addLayout(row_ol)
        
        row_pos = QHBoxLayout()
        row_pos.addWidget(QLabel('Положение:'))
        self.overlay_position_combo = QComboBox()
        _ov_pos_pairs = [
            ('Внизу слева', 1),
            ('Внизу по центру', 2),
            ('Внизу справа', 3),
            ('По центру слева', 4),
            ('По центру', 5),
            ('По центру справа', 6),
            ('Вверху слева', 7),
            ('Вверху по центру', 8),
            ('Вверху справа', 9),
        ]
        for _lbl, _aid in _ov_pos_pairs:
            self.overlay_position_combo.addItem(_lbl, _aid)
        self.overlay_position_combo.setCurrentIndex(4)
        self.overlay_position_combo.setToolTip(
            'Сетка как у субтитров. При выбранном видео предпросмотр обновится при смене.'
        )
        row_pos.addWidget(self.overlay_position_combo, 1)
        ov_lay.addLayout(row_pos)

        row_ov_margins = QHBoxLayout()
        row_ov_margins.addWidget(QLabel('Отступ сверху/снизу:'))
        self.overlay_margin_v_spin = QSpinBox()
        self.overlay_margin_v_spin.setRange(-600, 1200)
        self.overlay_margin_v_spin.setValue(0)
        self.overlay_margin_v_spin.setToolTip(
            'Для «Внизу…» — отступ от низа кадра; для «Вверху…» — от верха; для середины — сдвиг от центра.'
        )
        row_ov_margins.addWidget(self.overlay_margin_v_spin)
        row_ov_margins.addSpacing(16)
        row_ov_margins.addWidget(QLabel('Слева/справа:'))
        self.overlay_margin_lr_spin = QSpinBox()
        self.overlay_margin_lr_spin.setRange(0, 350)
        self.overlay_margin_lr_spin.setValue(0)
        row_ov_margins.addWidget(self.overlay_margin_lr_spin)
        row_ov_margins.addStretch()
        ov_lay.addLayout(row_ov_margins)

        row_overlay_scale = QHBoxLayout()
        self.overlay_scale_slider = QSlider(Qt.Horizontal)
        self.overlay_scale_slider.setRange(10, 300)
        self.overlay_scale_slider.setValue(100)
        self.overlay_scale_slider.setToolTip(
            'Масштаб баннера на кадре предпросмотра обновляется автоматически (если выбрано видео).'
        )
        self.overlay_scale_label = QLabel('Масштаб баннера: 100%')
        self.overlay_scale_slider.valueChanged.connect(
            lambda v: self.overlay_scale_label.setText(f'Масштаб баннера: {v}%')
        )
        row_overlay_scale.addWidget(self.overlay_scale_label)
        row_overlay_scale.addWidget(self.overlay_scale_slider, 1)
        ov_lay.addLayout(row_overlay_scale)

        self.overlay_chroma_check = QCheckBox('Хромакей (убрать цвет фона, напр. зелёный экран)')
        self.overlay_chroma_check.setToolTip(
            'Для видео/картинки с однотонным фоном: выберите цвет ключа и при необходимости подстройте чувствительность.'
        )
        ov_lay.addWidget(self.overlay_chroma_check)
        row_chroma = QHBoxLayout()
        self.overlay_chroma_color_btn = QPushButton('Цвет ключа')
        self.overlay_chroma_color_preview = QLabel('   ')
        self.overlay_chroma_color_preview.setFixedWidth(28)
        self.overlay_chroma_color_hex = '#00FF00'
        self.overlay_chroma_color_preview.setStyleSheet('background: #00FF00; border: 1px solid #666;')
        row_chroma.addWidget(self.overlay_chroma_color_btn)
        row_chroma.addWidget(self.overlay_chroma_color_preview)
        row_chroma.addWidget(QLabel('Чувствит.:'))
        self.overlay_chroma_sim_spin = QSpinBox()
        self.overlay_chroma_sim_spin.setRange(5, 60)
        self.overlay_chroma_sim_spin.setValue(15)
        self.overlay_chroma_sim_spin.setToolTip('Насколько агрессивно вырезать цвет (0.05–0.60). Выше — шире диапазон.')
        row_chroma.addWidget(self.overlay_chroma_sim_spin)
        row_chroma.addWidget(QLabel('Край:'))
        self.overlay_chroma_blend_spin = QSpinBox()
        self.overlay_chroma_blend_spin.setRange(0, 40)
        self.overlay_chroma_blend_spin.setValue(8)
        self.overlay_chroma_blend_spin.setToolTip('Сглаживание края ключа (0–0.40).')
        row_chroma.addWidget(self.overlay_chroma_blend_spin)
        row_chroma.addStretch()
        ov_lay.addLayout(row_chroma)
        
        effects_tab_layout.addWidget(self.overlay_group)
        
        # Группа субтитров
        self.subs_group = QGroupBox('Субтитры')
        subs_main_layout = QVBoxLayout(self.subs_group)
        
        # Режим субтитров
        self.subs_mode_group = QButtonGroup()
        subs_mode_layout = QHBoxLayout()
        
        self.subs_off_radio = QRadioButton('Выключены')
        self.subs_from_file_radio = QRadioButton('Из файла SRT')
        self.subs_generate_radio = QRadioButton('Анимированные слова (Whisper)')
        self.subs_off_radio.setChecked(True)
        
        self.subs_mode_group.addButton(self.subs_off_radio)
        self.subs_mode_group.addButton(self.subs_from_file_radio)
        self.subs_mode_group.addButton(self.subs_generate_radio)
        
        subs_mode_layout.addWidget(self.subs_off_radio)
        subs_mode_layout.addWidget(self.subs_from_file_radio)
        subs_mode_layout.addWidget(self.subs_generate_radio)
        subs_main_layout.addLayout(subs_mode_layout)
        
        # Виджет для файла SRT
        self.subs_file_widget = QWidget()
        subs_file_layout = QHBoxLayout(self.subs_file_widget)
        subs_file_layout.setContentsMargins(0, 5, 0, 0)
        
        self.subs_srt_path = QLineEdit()
        self.subs_srt_path.setPlaceholderText('Путь к файлу .srt')
        btn_browse_srt = QPushButton('Обзор...')
        
        subs_file_layout.addWidget(QLabel('Файл:'))
        subs_file_layout.addWidget(self.subs_srt_path)
        subs_file_layout.addWidget(btn_browse_srt)
        
        subs_main_layout.addWidget(self.subs_file_widget)
        
        # Виджет для Whisper настроек
        self.subs_whisper_widget = QWidget()
        subs_whisper_layout = QVBoxLayout(self.subs_whisper_widget)
        subs_whisper_layout.setContentsMargins(0, 5, 0, 5)
        subs_whisper_layout.setSpacing(10)
        
        # Модель
        whisper_row1 = QHBoxLayout()
        whisper_row1.addWidget(QLabel('Модель:'))
        self.subs_model_combo = QComboBox()
        self.subs_model_combo.addItems(WHISPER_MODELS)
        self.subs_model_combo.setCurrentText('distil-large-v3')
        whisper_row1.addWidget(self.subs_model_combo)
        subs_whisper_layout.addLayout(whisper_row1)
        
        # Язык
        whisper_row2 = QHBoxLayout()
        whisper_row2.addWidget(QLabel('Язык:'))
        self.subs_lang_combo = QComboBox()
        self.subs_lang_combo.addItems(WHISPER_LANGUAGES)
        self.subs_lang_combo.setCurrentText('Russian')
        whisper_row2.addWidget(self.subs_lang_combo)
        subs_whisper_layout.addLayout(whisper_row2)
        
        # Слов за одно появление субтитра
        whisper_row3 = QHBoxLayout()
        whisper_row3.addWidget(QLabel('Слов за раз:'))
        self.subs_words_spin = QSpinBox()
        self.subs_words_spin.setRange(1, 14)
        self.subs_words_spin.setValue(2)
        self.subs_words_spin.setToolTip(
            'Сколько слов показывать одновременно. 1–2 = поочередное появление, как вы просили.'
        )
        whisper_row3.addWidget(self.subs_words_spin)
        whisper_row3.addStretch()
        subs_whisper_layout.addLayout(whisper_row3)

        # Настройки анимации одного слова
        whisper_row4 = QHBoxLayout()
        whisper_row4.addWidget(QLabel('Base Font Size:'))
        self.subs_word_anim_base_size_spin = QSpinBox()
        self.subs_word_anim_base_size_spin.setRange(10, 200)
        self.subs_word_anim_base_size_spin.setValue(60)
        whisper_row4.addWidget(self.subs_word_anim_base_size_spin)
        whisper_row4.addSpacing(20)

        whisper_row4.addWidget(QLabel('Zoom Font Size:'))
        self.subs_word_anim_zoom_size_spin = QSpinBox()
        self.subs_word_anim_zoom_size_spin.setRange(10, 300)
        self.subs_word_anim_zoom_size_spin.setValue(80)
        whisper_row4.addWidget(self.subs_word_anim_zoom_size_spin)
        whisper_row4.addStretch()
        subs_whisper_layout.addLayout(whisper_row4)

        whisper_row5 = QHBoxLayout()
        whisper_row5.addWidget(QLabel('Vertical Offset:'))
        self.subs_word_anim_offset_spin = QSpinBox()
        self.subs_word_anim_offset_spin.setRange(-300, 300)
        self.subs_word_anim_offset_spin.setValue(200)
        whisper_row5.addWidget(self.subs_word_anim_offset_spin)
        whisper_row5.addStretch()
        subs_whisper_layout.addLayout(whisper_row5)

        subs_main_layout.addWidget(self.subs_whisper_widget)
        
        # Общие настройки стиля
        common_style_layout = QHBoxLayout()
        common_style_layout.addWidget(QLabel('Шрифт (системный):'))
        self.subs_font_combo = QComboBox()
        self.populate_subtitle_fonts()
        common_style_layout.addWidget(self.subs_font_combo, 1)

        common_style_layout.addWidget(QLabel('Начертание:'))
        self.subs_font_style_combo = QComboBox()
        self.subs_font_style_combo.addItems([
            'Обычный',
            'Курсив',
            'Полужирный',
            'Полужирный курсив',
            'Подчеркнутый',
            'Подчеркнутый курсив'
        ])
        common_style_layout.addWidget(self.subs_font_style_combo)

        common_style_layout.addWidget(QLabel('Размер (pt):'))
        self.subs_size_spin = QSpinBox()
        self.subs_size_spin.setRange(10, 100)
        self.subs_size_spin.setValue(36)
        common_style_layout.addWidget(self.subs_size_spin)
        common_style_layout.addWidget(QLabel('Обводка:'))
        self.subs_outline_spin = QSpinBox()
        self.subs_outline_spin.setRange(0, 8)
        self.subs_outline_spin.setValue(2)
        common_style_layout.addWidget(self.subs_outline_spin)
        common_style_layout.addWidget(QLabel('Контур:'))
        self.subs_outline_mode_combo = QComboBox()
        self.subs_outline_mode_combo.addItems(['Снаружи', 'Внутри'])
        common_style_layout.addWidget(self.subs_outline_mode_combo)
        common_style_layout.addStretch(1)
        subs_main_layout.addLayout(common_style_layout)

        subs_color_layout = QHBoxLayout()
        self.subs_text_color_btn = QPushButton('Цвет текста')
        self.subs_text_color_preview = QLabel('   ')
        self.subs_text_color_preview.setFixedWidth(28)
        self.subs_text_color_preview.setStyleSheet('background: #FFFFFF; border: 1px solid #666;')
        self.subs_text_color_hex = '#FFFFFF'

        self.subs_outline_color_btn = QPushButton('Цвет обводки')
        self.subs_outline_color_preview = QLabel('   ')
        self.subs_outline_color_preview.setFixedWidth(28)
        self.subs_outline_color_preview.setStyleSheet('background: #000000; border: 1px solid #666;')
        self.subs_outline_color_hex = '#000000'

        subs_color_layout.addWidget(self.subs_text_color_btn)
        subs_color_layout.addWidget(self.subs_text_color_preview)
        subs_color_layout.addSpacing(8)
        subs_color_layout.addWidget(self.subs_outline_color_btn)
        subs_color_layout.addWidget(self.subs_outline_color_preview)
        subs_color_layout.addStretch()
        subs_main_layout.addLayout(subs_color_layout)

        self.subs_position_combo = QComboBox()
        _subs_pos_pairs = [
            ('Внизу слева', 1),
            ('Внизу по центру', 2),
            ('Внизу справа', 3),
            ('По центру слева', 4),
            ('По центру', 5),
            ('По центру справа', 6),
            ('Вверху слева', 7),
            ('Вверху по центру', 8),
            ('Вверху справа', 9),
        ]
        for _lbl, _aid in _subs_pos_pairs:
            self.subs_position_combo.addItem(_lbl, _aid)
        self.subs_position_combo.setCurrentIndex(1)
        self.subs_position_combo.setToolTip(
            'Для рилсов чаще выбирайте «Внизу…», чтобы не закрывать лица и центр кадра.'
        )
        self.subs_margin_v_spin = QSpinBox()
        self.subs_margin_v_spin.setRange(-600, 1200)
        self.subs_margin_v_spin.setValue(110)
        self.subs_margin_v_spin.setToolTip(
            'ASS MarginV: для «Внизу…» положительные — выше от низа; отрицательные — ближе к самому низу/в полосу размытия. '
            'Для «Вверху…» наоборот: минус — ближе к верху кадра.'
        )
        self.subs_margin_lr_spin = QSpinBox()
        self.subs_margin_lr_spin.setRange(0, 350)
        self.subs_margin_lr_spin.setValue(28)
        self.subs_margin_lr_spin.setToolTip('Поля MarginL и MarginR')

        # Превью стиля субтитров
        preview_group = QGroupBox('Превью субтитров')
        preview_layout = QVBoxLayout(preview_group)
        self.subs_style_preview = QLabel('Это пример того, как будут выглядеть субтитры')
        self.subs_style_preview.setAlignment(Qt.AlignCenter)
        self.subs_style_preview.setWordWrap(True)
        self.subs_style_preview.setMinimumHeight(120)
        self.subs_style_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.subs_style_preview.setStyleSheet('background: #FFFFFF; color: #000000; border: 1px solid #999;')
        preview_layout.addWidget(self.subs_style_preview)
        subs_main_layout.addWidget(preview_group)

        frame_preview_group = QGroupBox('Положение субтитров на кадре')
        frame_preview_layout = QVBoxLayout(frame_preview_group)

        frame_pos_row1 = QHBoxLayout()
        frame_pos_row1.addWidget(QLabel('Положение:'))
        frame_pos_row1.addWidget(self.subs_position_combo, 1)
        frame_preview_layout.addLayout(frame_pos_row1)
        frame_pos_row2 = QHBoxLayout()
        frame_pos_row2.addWidget(QLabel('Отступ сверху/снизу:'))
        frame_pos_row2.addWidget(self.subs_margin_v_spin)
        frame_pos_row2.addSpacing(16)
        frame_pos_row2.addWidget(QLabel('Слева/справа:'))
        frame_pos_row2.addWidget(self.subs_margin_lr_spin)
        frame_pos_row2.addStretch()
        frame_preview_layout.addLayout(frame_pos_row2)

        self.subs_on_frame_hint = QLabel(
            'Итоговый кадр с баннером и субтитрами — в окне «Предпросмотр» слева '
            '(Ctrl + колёсико — масштаб, перетаскивание — сдвиг).'
        )
        self.subs_on_frame_hint.setWordWrap(True)
        self.subs_on_frame_hint.setStyleSheet('color: #888; font-size: 11px;')
        frame_preview_layout.addWidget(self.subs_on_frame_hint)
        subs_main_layout.addWidget(frame_preview_group)
        
        effects_tab_layout.addWidget(self.subs_group)
        effects_tab_layout.addStretch()
        
        # === ВКЛАДКА АУДИО ===
        
        # Группа управления звуком
        self.mute_group = QGroupBox('Управление звуком')
        mute_layout = QVBoxLayout(self.mute_group)
        
        self.mute_checkbox = QCheckBox('Удалить оригинальный звук из видео')
        mute_layout.addWidget(self.mute_checkbox)
        
        # Громкость оригинала
        orig_vol_layout = QHBoxLayout()
        self.orig_vol_slider = QSlider(Qt.Horizontal)
        self.orig_vol_slider.setRange(0, 150)
        self.orig_vol_slider.setValue(100)
        
        self.orig_vol_label = QLabel('Громкость оригинала: 100%')
        self.orig_vol_slider.valueChanged.connect(
            lambda v: self.orig_vol_label.setText(f'Громкость оригинала: {v}%')
        )
        self.mute_checkbox.toggled.connect(
            lambda c: self.orig_vol_slider.setDisabled(c)
        )
        
        orig_vol_layout.addWidget(self.orig_vol_label)
        orig_vol_layout.addWidget(self.orig_vol_slider)
        mute_layout.addLayout(orig_vol_layout)
        
        audio_tab_layout.addWidget(self.mute_group)
        
        # Группа наложения аудио
        self.overlay_audio_group = QGroupBox('Наложение аудио')
        overlay_audio_layout = QVBoxLayout(self.overlay_audio_group)
        
        # Путь к аудиофайлу
        ol_audio_path_layout = QHBoxLayout()
        self.overlay_audio_path_edit = QLineEdit()
        self.overlay_audio_path_edit.setPlaceholderText('Путь к аудиофайлу (MP3, WAV...)')
        
        browse_ol_audio_btn = QPushButton('Обзор...')
        clear_ol_audio_btn = QPushButton('X')
        clear_ol_audio_btn.setFixedWidth(30)
        
        ol_audio_path_layout.addWidget(QLabel('Файл:'))
        ol_audio_path_layout.addWidget(self.overlay_audio_path_edit)
        ol_audio_path_layout.addWidget(browse_ol_audio_btn)
        ol_audio_path_layout.addWidget(clear_ol_audio_btn)
        overlay_audio_layout.addLayout(ol_audio_path_layout)
        
        # Громкость наложения
        over_vol_layout = QHBoxLayout()
        self.over_vol_slider = QSlider(Qt.Horizontal)
        self.over_vol_slider.setRange(0, 150)
        self.over_vol_slider.setValue(100)
        
        self.over_vol_label = QLabel('Громкость наложения: 100%')
        self.over_vol_slider.valueChanged.connect(
            lambda v: self.over_vol_label.setText(f'Громкость наложения: {v}%')
        )
        
        over_vol_layout.addWidget(self.over_vol_label)
        over_vol_layout.addWidget(self.over_vol_slider)
        overlay_audio_layout.addLayout(over_vol_layout)
        
        # Управление активностью слайдера
        self.overlay_audio_path_edit.textChanged.connect(
            lambda t: self.over_vol_slider.setDisabled(not t)
        )
        self.over_vol_slider.setDisabled(True)
        
        audio_tab_layout.addWidget(self.overlay_audio_group)
        
        # Группа Jump Cut
        self.jumpcut_group = QGroupBox('Jump Cut (удаление тишины)')
        jumpcut_layout = QVBoxLayout(self.jumpcut_group)
        
        self.jumpcut_enable_checkbox = QCheckBox('Включить Jump Cut')
        self.jumpcut_enable_checkbox.setChecked(True)
        jumpcut_layout.addWidget(self.jumpcut_enable_checkbox)
        
        jumpcut_params_layout = QHBoxLayout()
        jumpcut_params_layout.addWidget(QLabel('Агрессивность:'))
        self.jumpcut_aggressiveness_combo = QComboBox()
        self.jumpcut_aggressiveness_combo.addItems(['Не сильно', 'Средне', 'Сильно'])
        self.jumpcut_aggressiveness_combo.setCurrentIndex(1)  # Средне
        jumpcut_params_layout.addWidget(self.jumpcut_aggressiveness_combo)
        
        jumpcut_params_layout.addSpacing(20)
        jumpcut_params_layout.addWidget(QLabel('Затухание перехода (сек):'))
        self.jumpcut_fade_spin = QDoubleSpinBox()
        self.jumpcut_fade_spin.setRange(0.0, 1.0)
        self.jumpcut_fade_spin.setValue(0.3)
        self.jumpcut_fade_spin.setSingleStep(0.1)
        jumpcut_params_layout.addWidget(self.jumpcut_fade_spin)
        jumpcut_params_layout.addStretch()
        jumpcut_layout.addLayout(jumpcut_params_layout)
        
        audio_tab_layout.addWidget(self.jumpcut_group)
        audio_tab_layout.addStretch()
        
        # === ВКЛАДКА ЦЕНЗУРА ===
        
        # Группа опций цензуры
        self.censor_options_group = QGroupBox('Опции цензуры')
        censor_options_layout = QVBoxLayout(self.censor_options_group)
        
        self.censor_subtitles_check = QCheckBox('Цензурировать субтитры')
        self.censor_subtitles_check.setToolTip('Применить цензуру к генерируемым субтитрам')
        censor_options_layout.addWidget(self.censor_subtitles_check)
        
        self.censor_metadata_check = QCheckBox('Очищать метаданные')
        self.censor_metadata_check.setToolTip('Автоматически очищать и оптимизировать название, описание и теги видео')
        censor_options_layout.addWidget(self.censor_metadata_check)
        
        censor_tab_layout.addWidget(self.censor_options_group)
        
        # Группа управления черным списком
        self.censor_list_group = QGroupBox('Черный список слов')
        censor_list_layout = QVBoxLayout(self.censor_list_group)
        
        # Список слов
        self.censor_list_widget = QListWidget()
        self.censor_list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.censor_list_widget.setMinimumHeight(200)
        censor_list_layout.addWidget(QLabel('Слова для цензуры:'))
        censor_list_layout.addWidget(self.censor_list_widget)
        
        # Добавление новых слов
        input_row = QHBoxLayout()
        self.censor_word_input = QLineEdit()
        self.censor_word_input.setPlaceholderText('Введите слово для добавления в черный список...')
        self.censor_word_input.returnPressed.connect(self.on_censor_add_word)
        
        btn_add_word = QPushButton('Добавить')
        btn_add_word.setFixedWidth(100)
        btn_add_word.clicked.connect(self.on_censor_add_word)
        
        btn_remove_word = QPushButton('Удалить')
        btn_remove_word.setFixedWidth(100)
        btn_remove_word.clicked.connect(self.on_censor_remove_word)
        
        input_row.addWidget(self.censor_word_input, 1)
        input_row.addWidget(btn_add_word)
        input_row.addWidget(btn_remove_word)
        
        censor_list_layout.addLayout(input_row)
        
        # Кнопки для управления списком
        buttons_row = QHBoxLayout()
        btn_load_from_file = QPushButton('Загрузить из файла')
        btn_load_from_file.clicked.connect(self.on_censor_load_from_file)
        
        btn_save_to_file = QPushButton('Сохранить в файл')
        btn_save_to_file.clicked.connect(self.on_censor_save_to_file)
        
        btn_clear_list = QPushButton('Очистить список')
        btn_clear_list.clicked.connect(self.on_censor_clear_list)
        
        buttons_row.addWidget(btn_load_from_file)
        buttons_row.addWidget(btn_save_to_file)
        buttons_row.addWidget(btn_clear_list)
        buttons_row.addStretch()
        
        censor_list_layout.addLayout(buttons_row)
        
        censor_tab_layout.addWidget(self.censor_list_group)
        censor_tab_layout.addStretch()
        
        # === НИЖНИЕ ЭЛЕМЕНТЫ УПРАВЛЕНИЯ ===
        
        # Кнопка обработки
        self.process_button = QPushButton('🚀 Обработать')
        self.process_button.setObjectName('process_button')
        self.process_button.setFixedHeight(40)
        
        # Прогресс бар и лейблы
        self.progress_label = QLabel('')
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        
        self.status_label = QLabel('')
        self.status_label.setStyleSheet('color: gray;')
        
        # Layout для прогресса
        progress_layout = QHBoxLayout()
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar, 1)
        
        # Watermark label
        self.watermark_label = QLabel()
        self.watermark_label.setText('Декомпиляцию последней версии программы выполнил llimonix.<br>Мой Telegram канал: '
        '<a href="https://t.me/findllimonix" style="color:#df4f44; text-decoration:none;">@findllimonix</a>')
        self.watermark_label.setTextFormat(Qt.RichText)
        self.watermark_label.setOpenExternalLinks(True)
        self.watermark_label.setAlignment(Qt.AlignCenter)
        
        # Нижний layout
        bottom_controls_layout = QVBoxLayout()
        bottom_controls_layout.addWidget(self.process_button)
        bottom_controls_layout.addLayout(progress_layout)
        bottom_controls_layout.addWidget(self.status_label)
        bottom_controls_layout.addWidget(self.watermark_label)
        
        self.right_panel.addLayout(bottom_controls_layout)
        
        # === ПОДКЛЮЧЕНИЕ СИГНАЛОВ ===
        
        # Кнопки
        btn_add.clicked.connect(self.on_add_files)
        btn_folder.clicked.connect(self.on_add_folder)
        btn_clear.clicked.connect(self.on_clear_list)
        btn_ol.clicked.connect(self.on_select_overlay)
        btn_clear_ol.clicked.connect(lambda: self.overlay_path.clear())
        self.overlay_chroma_color_btn.clicked.connect(self.on_pick_overlay_chroma_color)
        self.overlay_chroma_check.stateChanged.connect(self.update_subtitle_on_video_preview)
        self.overlay_chroma_sim_spin.valueChanged.connect(self.update_subtitle_on_video_preview)
        self.overlay_chroma_blend_spin.valueChanged.connect(self.update_subtitle_on_video_preview)
        self.overlay_chroma_check.stateChanged.connect(
            lambda _s: self.schedule_effects_preview_refresh(350)
        )
        self.overlay_chroma_sim_spin.valueChanged.connect(
            lambda _v: self.schedule_effects_preview_refresh(400)
        )
        self.overlay_chroma_blend_spin.valueChanged.connect(
            lambda _v: self.schedule_effects_preview_refresh(400)
        )
        self.preview_button.clicked.connect(self.on_update_preview)
        btn_browse_srt.clicked.connect(self.on_browse_srt)
        self.subs_mode_group.buttonClicked.connect(self.on_subs_mode_changed)
        self.subs_text_color_btn.clicked.connect(self.on_pick_subs_text_color)
        self.subs_outline_color_btn.clicked.connect(self.on_pick_subs_outline_color)
        self.subs_font_combo.currentTextChanged.connect(self.update_subtitle_style_preview)
        self.subs_font_style_combo.currentTextChanged.connect(self.update_subtitle_style_preview)
        self.subs_size_spin.valueChanged.connect(self.update_subtitle_style_preview)
        self.subs_outline_spin.valueChanged.connect(self.update_subtitle_style_preview)
        self.subs_outline_mode_combo.currentTextChanged.connect(self.update_subtitle_style_preview)
        self.subs_position_combo.currentIndexChanged.connect(self.update_subtitle_style_preview)
        self.subs_margin_v_spin.valueChanged.connect(self.update_subtitle_style_preview)
        self.subs_margin_lr_spin.valueChanged.connect(self.update_subtitle_style_preview)
        self.video_list_widget.itemSelectionChanged.connect(self.update_subtitle_on_video_preview)
        self.auto_crop_checkbox.stateChanged.connect(self.update_subtitle_on_video_preview)
        self.zoom_static_spin.valueChanged.connect(self.update_subtitle_on_video_preview)
        self.zoom_min_spin.valueChanged.connect(self.update_subtitle_on_video_preview)
        self.zoom_max_spin.valueChanged.connect(self.update_subtitle_on_video_preview)
        browse_ol_audio_btn.clicked.connect(self.on_browse_overlay_audio)
        clear_ol_audio_btn.clicked.connect(self.overlay_audio_path_edit.clear)
        self.process_button.clicked.connect(self.start_processing)
        btn_preset_save.clicked.connect(self.on_save_preset)
        btn_preset_apply.clicked.connect(self.on_apply_preset)
        btn_preset_delete.clicked.connect(self.on_delete_preset)
        self.filter_list.itemSelectionChanged.connect(self.update_subtitle_on_video_preview)
        self.blur_background_checkbox.stateChanged.connect(self.update_subtitle_on_video_preview)
        self.overlay_path.textChanged.connect(self.update_subtitle_on_video_preview)
        self.overlay_path.textChanged.connect(lambda _t: self.schedule_effects_preview_refresh(600))
        self.overlay_position_combo.currentIndexChanged.connect(
            lambda _i: self._on_overlay_geometry_changed(0)
        )
        self.overlay_margin_v_spin.valueChanged.connect(
            lambda _v: self._on_overlay_geometry_changed(250)
        )
        self.overlay_margin_lr_spin.valueChanged.connect(
            lambda _v: self._on_overlay_geometry_changed(250)
        )
        self.overlay_scale_slider.valueChanged.connect(self.update_subtitle_on_video_preview)
        self.overlay_scale_slider.valueChanged.connect(
            lambda _v: self.schedule_effects_preview_refresh(450)
        )
        
        # Инициализация состояний
        self.on_subs_mode_changed()
        self.on_output_format_changed(self.output_format_combo.currentText())
        self.on_zoom_mode_changed()
        self.on_speed_mode_changed()
        self.on_viral_mode_changed()
        self.update_subtitle_style_preview()
        self.load_presets_from_config()
        
        # Drag & Drop
        self.video_list_widget.files_dropped.connect(self.refresh_video_list_display)
        self.viral_enable_checkbox.toggled.connect(self.on_viral_mode_changed)
    
    def on_subs_mode_changed(self):
        is_from_file = self.subs_from_file_radio.isChecked()
        is_generate = self.subs_generate_radio.isChecked()
        
        self.subs_file_widget.setVisible(is_from_file)
        self.subs_whisper_widget.setVisible(is_generate)
    
    def on_browse_srt(self):
        fs, _ = QFileDialog.getOpenFileName(
            self, 'Выберите файл субтитров', '',
            'SRT Files (*.srt)'
        )
        if fs:
            self.subs_srt_path.setText(fs)
    
    def on_browse_overlay_audio(self):
        fs, _ = QFileDialog.getOpenFileName(
            self, 'Выберите аудиофайл', '',
            'Audio Files (*.mp3 *.wav *.m4a *.aac)'
        )
        if fs:
            self.overlay_audio_path_edit.setText(fs)

    def on_pick_subs_text_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.subs_text_color_hex = color.name().upper()
            self.subs_text_color_preview.setStyleSheet(
                f'background: {self.subs_text_color_hex}; border: 1px solid #666;'
            )
            self.update_subtitle_style_preview()

    def on_pick_subs_outline_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.subs_outline_color_hex = color.name().upper()
            self.subs_outline_color_preview.setStyleSheet(
                f'background: {self.subs_outline_color_hex}; border: 1px solid #666;'
            )
            self.update_subtitle_style_preview()

    def populate_subtitle_fonts(self):
        db = QFontDatabase()
        families = sorted(db.families())
        self.subs_font_combo.clear()
        self.subs_font_combo.addItems(families)

        default_family = self.font().family()
        default_idx = self.subs_font_combo.findText(default_family)
        if default_idx >= 0:
            self.subs_font_combo.setCurrentIndex(default_idx)
        elif families:
            self.subs_font_combo.setCurrentIndex(0)

    def _subtitle_font_flags(self):
        style = self.subs_font_style_combo.currentText().lower()
        return {
            'bold': 'полужирный' in style,
            'italic': 'курсив' in style,
            'underline': 'подчеркнутый' in style
        }

    def _paint_subtitle_sample(
        self,
        painter: QPainter,
        x: float,
        y: float,
        font: QFont,
        text: str,
        text_color: str,
        outline_color: str,
        outline: int,
        outline_mode: str,
    ):
        painter.setFont(font)
        path = QPainterPath()
        path.addText(x, y, font, text)
        if outline > 0:
            pen = QPen(QColor(outline_color))
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setWidth(max(1, outline * 2))
            if outline_mode == 'Снаружи':
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(path)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(text_color))
                painter.drawPath(path)
            else:
                painter.setPen(pen)
                painter.setBrush(QColor(text_color))
                painter.drawPath(path)
        else:
            painter.setPen(QColor(text_color))
            painter.drawText(int(x), int(y), text)

    def _subtitle_preview_xy(
        self,
        alignment: int,
        margin_lr: int,
        margin_v: int,
        pixmap_w: int,
        pixmap_h: int,
        text_w: int,
        fm,
    ):
        """Координаты текста для превью (как в ASS: 1–9)."""
        ascent = fm.ascent()
        descent = fm.descent()
        if alignment in (1, 2, 3):
            y = float(pixmap_h - margin_v - descent)
        elif alignment in (7, 8, 9):
            y = float(margin_v + ascent)
        else:
            y = float(pixmap_h / 2.0 + (ascent - descent) / 2.0)

        if alignment in (1, 4, 7):
            x = float(margin_lr)
        elif alignment in (2, 5, 8):
            x = (pixmap_w - text_w) / 2.0
        else:
            x = float(pixmap_w - text_w - margin_lr)

        max_x = float(max(0, pixmap_w - text_w - margin_lr))
        x = max(float(margin_lr), min(x, max_x))
        return x, y

    def update_subtitle_style_preview(self):
        font_family = self.subs_font_combo.currentText().strip() or 'Arial'
        font_size = self.subs_size_spin.value()
        text_color = self.subs_text_color_hex
        outline_color = self.subs_outline_color_hex
        outline = self.subs_outline_spin.value()
        outline_mode = self.subs_outline_mode_combo.currentText()
        al_raw = self.subs_position_combo.currentData()
        try:
            alignment = int(al_raw) if al_raw is not None else 2
        except (TypeError, ValueError):
            alignment = 2
        margin_v = self.subs_margin_v_spin.value()
        margin_lr = self.subs_margin_lr_spin.value()
        font_flags = self._subtitle_font_flags()

        self.subs_style_preview.setStyleSheet('background: #FFFFFF; border: 1px solid #999;')
        self.subs_style_preview.setText('')

        preview_text = 'Это пример того, как будут выглядеть субтитры'
        H_ref = 1920
        ass_px_ref = int(_ass_font_size_for_video(font_size, H_ref))

        widget_w = self.subs_style_preview.width()
        widget_h = self.subs_style_preview.height()
        if widget_w < 80:
            widget_w = 420
        if widget_h < 80:
            widget_h = 140

        pixmap_h_est = max(90, widget_h - 4)
        scale_est = max(0.12, min(1.0, pixmap_h_est / float(H_ref)))
        margin_v_est = int(round(margin_v * scale_est))
        f_est = QFont(font_family)
        f_est.setPixelSize(max(6, int(round(ass_px_ref * scale_est))))
        f_est.setBold(font_flags['bold'])
        f_est.setItalic(font_flags['italic'])
        line_h_est = QFontMetrics(f_est).height()
        if alignment in (1, 2, 3, 7, 8, 9):
            min_h_needed = margin_v_est + line_h_est + 24
        else:
            min_h_needed = line_h_est + 80
        pixmap_h = max(90, min_h_needed, widget_h - 4)
        scale_preview = max(0.12, min(1.0, pixmap_h / float(H_ref)))
        margin_v_s = int(round(margin_v * scale_preview))
        margin_lr_s = int(round(margin_lr * scale_preview))

        font = QFont(font_family)
        font.setPixelSize(max(6, int(round(ass_px_ref * scale_preview))))
        font.setBold(font_flags['bold'])
        font.setItalic(font_flags['italic'])
        font.setUnderline(font_flags['underline'])
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(preview_text)
        min_w_needed = max(280, text_w + 2 * margin_lr_s + 24)
        pixmap_w = max(min_w_needed, widget_w - 4, 280)

        pixmap = QPixmap(pixmap_w, pixmap_h)
        pixmap.fill(Qt.white)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(font)

        x, y = self._subtitle_preview_xy(
            alignment, margin_lr_s, margin_v_s, pixmap_w, pixmap_h, text_w, fm
        )
        eff_outline = _subtitle_effective_outline({
            'outline': outline,
            'outline_mode': outline_mode.lower(),
        })
        eff_outline_draw = max(1, int(round(eff_outline * scale_preview))) if eff_outline > 0 else eff_outline
        self._paint_subtitle_sample(
            painter, x, y, font, preview_text,
            text_color, outline_color, eff_outline_draw, outline_mode
        )
        painter.end()
        self.subs_style_preview.setPixmap(pixmap)
        self.update_subtitle_on_video_preview()

    def _subs_preview_zoom_p(self) -> int:
        if self.zoom_dynamic_radio.isChecked():
            return (self.zoom_min_spin.value() + self.zoom_max_spin.value()) // 2
        return self.zoom_static_spin.value()

    def _subs_preview_video_path(self):
        selected = self.video_list_widget.selectedItems()
        if selected:
            p = selected[0].data(Qt.UserRole)
            if p and os.path.isfile(p):
                return p
        if self.video_list_widget.count() > 0:
            p = self.video_list_widget.item(0).data(Qt.UserRole)
            if p and os.path.isfile(p):
                return p
        return None

    def _overlay_alignment_value(self) -> int:
        raw = self.overlay_position_combo.currentData()
        try:
            return int(raw) if raw is not None else 5
        except (TypeError, ValueError):
            return 5

    def _collect_preview_signature(
        self, video_path: Optional[str], crop_filter: Optional[str]
    ) -> Optional[Tuple]:
        if not video_path or not os.path.isfile(video_path):
            return None
        vp = os.path.normcase(os.path.normpath(os.path.abspath(video_path)))
        cf = (crop_filter or '').replace(' ', '')
        filters = tuple(sorted(it.text() for it in self.filter_list.selectedItems()))
        ov_raw = self.overlay_path.text().strip()
        if ov_raw and os.path.isfile(ov_raw):
            ov = os.path.normcase(os.path.normpath(os.path.abspath(ov_raw)))
        else:
            ov = ''
        _ch = self._overlay_chroma_params()
        return (
            vp,
            cf,
            filters,
            int(self._subs_preview_zoom_p()),
            ov,
            self._overlay_alignment_value(),
            int(self.overlay_margin_v_spin.value()),
            int(self.overlay_margin_lr_spin.value()),
            int(self.overlay_scale_slider.value()),
            self.output_format_combo.currentText(),
            bool(self.blur_background_checkbox.isChecked()),
            bool(self.auto_crop_checkbox.isChecked()),
            bool(_ch['enabled']),
            str(_ch['color']),
            int(self.overlay_chroma_sim_spin.value()),
            int(self.overlay_chroma_blend_spin.value()),
        )

    def update_subtitle_on_video_preview(self):
        """Тот же кадр, что после «Обновить предпросмотр», плюс субтитры в пикселях как при вшивании."""
        if not hasattr(self, 'main_preview'):
            return

        W_def, H_def = 1080, 1920
        path = self._subs_preview_video_path()
        cached = getattr(self, '_subs_frame_dim_cache', None)
        if cached is None or cached[0] != path:
            if path:
                w, h = get_video_dimensions(path)
                wh = (w, h) if w > 0 and h > 0 else (1920, 1080)
            else:
                wh = (1920, 1080)
            self._subs_frame_dim_cache = (path, wh)
        sw, sh = self._subs_frame_dim_cache[1]

        crop_filter = None
        if self.auto_crop_checkbox.isChecked() and path:
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = 0.0
            ck = (path, mt)
            if getattr(self, '_subs_preview_crop_key', None) != ck:
                try:
                    self._subs_preview_crop_val = detect_crop_dimensions(path)
                except Exception:
                    self._subs_preview_crop_val = None
                self._subs_preview_crop_key = ck
            crop_filter = getattr(self, '_subs_preview_crop_val', None)

        zoom_p = self._subs_preview_zoom_p()
        is_reels = self.output_format_combo.currentText() == REELS_FORMAT_NAME
        letterbox_inset = 0
        _, bar = float(H_def), 0.0
        if is_reels:
            _, bar, letterbox_inset = reels_preview_bars_heights(sw, sh, crop_filter, zoom_p)

        al_raw = self.subs_position_combo.currentData()
        try:
            alignment = int(al_raw) if al_raw is not None else 2
        except (TypeError, ValueError):
            alignment = 2
        margin_v_base = self.subs_margin_v_spin.value()
        margin_lr = self.subs_margin_lr_spin.value()
        margin_v = margin_v_base
        if is_reels and letterbox_inset > 0 and alignment in (1, 2, 3, 7, 8, 9):
            margin_v = margin_v_base + letterbox_inset

        font_family = self.subs_font_combo.currentText().strip() or 'Arial'
        font_flags = self._subtitle_font_flags()
        preview_text = 'Пример субтитров на кадре'

        current_sig = self._collect_preview_signature(path, crop_filter) if path else None
        use_real_frame = (
            current_sig is not None
            and self._preview_signature == current_sig
            and self._preview_frame_pixmap is not None
            and not self._preview_frame_pixmap.isNull()
        )

        if use_real_frame:
            canvas = QPixmap(self._preview_frame_pixmap)
            W0, H0 = canvas.width(), canvas.height()
        else:
            canvas = QPixmap(W_def, H_def)
            canvas.fill(QColor(26, 26, 30))
            W0, H0 = W_def, H_def
            p0 = QPainter(canvas)
            p0.setRenderHint(QPainter.Antialiasing, True)
            if is_reels and bar > 0.5:
                bh = int(max(1, round(bar)))
                fh = max(1, H0 - 2 * bh)
                p0.fillRect(0, 0, W0, bh, QColor(38, 40, 48))
                p0.fillRect(0, bh, W0, fh, QColor(72, 78, 98))
                p0.fillRect(0, bh + fh, W0, H0 - bh - fh, QColor(38, 40, 48))
            else:
                p0.fillRect(0, 0, W0, H0, QColor(55, 62, 80))
            p0.end()

        ass_px = int(_ass_font_size_for_video(self.subs_size_spin.value(), H0))
        font = QFont(font_family)
        font.setPixelSize(max(6, ass_px))
        font.setBold(font_flags['bold'])
        font.setItalic(font_flags['italic'])
        font.setUnderline(font_flags['underline'])
        f_meas = QFont(font_family)
        f_meas.setPixelSize(max(8, ass_px))
        f_meas.setBold(font_flags['bold'])
        f_meas.setItalic(font_flags['italic'])
        f_meas.setUnderline(font_flags['underline'])
        fm_log = QFontMetrics(f_meas)
        text_w_log = fm_log.horizontalAdvance(preview_text)
        x0, y0 = self._subtitle_preview_xy(
            alignment, margin_lr, margin_v, W0, H0, text_w_log, fm_log
        )

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        eff_ol = _subtitle_effective_outline({
            'outline': self.subs_outline_spin.value(),
            'outline_mode': self.subs_outline_mode_combo.currentText().lower(),
        })
        self._paint_subtitle_sample(
            painter, x0, y0, font, preview_text,
            self.subs_text_color_hex, self.subs_outline_color_hex,
            eff_ol, self.subs_outline_mode_combo.currentText(),
        )
        painter.end()

        self.main_preview.set_image(canvas)

        if use_real_frame:
            self.subs_on_frame_hint.setText(
                'Кадр слева совпадает с последним «Обновить предпросмотр» (формат, зум, размытие, фильтры, баннер, автообрезка). '
                'Размер и отступы субтитров — как при вшивании в пикселях кадра. Ctrl + колёсико — масштаб, перетаскивание — сдвиг.'
            )
        else:
            self.subs_on_frame_hint.setText(
                'Нажмите «Обновить предпросмотр» слева с выбранным видео — в окне предпросмотра будет тот же кадр с эффектами и субтитрами. '
                'Пока схема полос Reels или условный фон; разметка как при кодировании.'
            )

    def showEvent(self, event):
        super().showEvent(event)
        self.update_subtitle_style_preview()

    def _on_overlay_geometry_changed(self, delay_ms: int = 0):
        """
        Баннер рисуется в generate_preview/process_single (FFmpeg). Смена положения/отступов
        меняет подпись кадра — без пересборки use_real_frame ложен и показывается заглушка.
        """
        if self.video_list_widget.selectedItems():
            self.schedule_effects_preview_refresh(delay_ms)
        else:
            self.update_subtitle_on_video_preview()

    def schedule_effects_preview_refresh(self, delay_ms: int = 0):
        """
        Пересобрать кадр предпросмотра (баннер: позиция, масштаб, хромакей, файл).
        delay_ms > 0 — отложить (ползунок масштаба), чтобы не дергать FFmpeg на каждый шаг.
        """
        if delay_ms > 0:
            self._preview_refresh_timer.stop()
            self._preview_refresh_timer.setInterval(delay_ms)
            self._preview_refresh_timer.start()
        else:
            self._run_deferred_effects_preview()

    def _run_deferred_effects_preview(self):
        if self.preview_thread is not None and self.preview_thread.isRunning():
            return
        if not self.video_list_widget.selectedItems():
            return
        self._preview_auto_no_message = True
        try:
            self.on_update_preview()
        finally:
            self._preview_auto_no_message = False

    def on_update_preview(self):
        selected_items = self.video_list_widget.selectedItems()
        if not selected_items:
            if not self._preview_auto_no_message:
                QMessageBox.warning(self, 'Видео не выбрано', 'Пожалуйста, выберите видео из списка для предпросмотра.')
            return
        
        if self._clip_player is not None:
            self._clip_player.stop()
        self._clip_last_path = None
        self.btn_preview_video_mode.setEnabled(False)
        self.preview_stack.setCurrentIndex(1)
        self.btn_preview_still_mode.setChecked(True)

        in_path = selected_items[0].data(Qt.UserRole)
        temp_preview_path = os.path.join(
            self.parent_window.temp_dir,
            f'preview_{uuid.uuid4()}.png'
        )
        temp_clip_path = ''
        if PREVIEW_CLIP_AVAILABLE and self._clip_player is not None:
            temp_clip_path = os.path.join(
                self.parent_window.temp_dir,
                f'preview_clip_{uuid.uuid4()}.mp4'
            )
        
        crop_filter = None
        if self.auto_crop_checkbox.isChecked():
            self.main_preview.set_message('Анализ кадра для обрезки...')
            QApplication.processEvents()
            try:
                crop_filter = detect_crop_dimensions(in_path)
            except Exception as e:
                self.on_preview_error(f'Не удалось определить размеры обрезки: {e}')
                return
        
        self._preview_signature_pending = self._collect_preview_signature(in_path, crop_filter)

        _ch = self._overlay_chroma_params()
        params = {
            'in_path': in_path,
            'out_path_png': temp_preview_path,
            'out_path_clip': temp_clip_path,
            'clip_seconds': 8.0,
            'enable_video_clip': bool(PREVIEW_CLIP_AVAILABLE and self._clip_player is not None),
            'filters': [item.text() for item in self.filter_list.selectedItems()],
            'zoom_p': self._subs_preview_zoom_p(),
            'overlay_file': self.overlay_path.text().strip() or None,
            'overlay_alignment': self._overlay_alignment_value(),
            'overlay_margin_v': self.overlay_margin_v_spin.value(),
            'overlay_margin_lr': self.overlay_margin_lr_spin.value(),
            'overlay_scale_p': self.overlay_scale_slider.value(),
            'output_format': self.output_format_combo.currentText(),
            'blur_background': self.blur_background_checkbox.isChecked(),
            'crop_filter': crop_filter,
            'overlay_chromakey': _ch['enabled'],
            'overlay_chromakey_color': _ch['color'],
            'overlay_chromakey_similarity': _ch['similarity'],
            'overlay_chromakey_blend': _ch['blend'],
        }
        
        self.set_controls_enabled(False)
        self.main_preview.set_message('Генерация предпросмотра...')
        
        self.parent_window.temp_files.append(temp_preview_path)
        if temp_clip_path:
            self.parent_window.temp_files.append(temp_clip_path)
        self.preview_thread = PreviewWorker(params)
        self.preview_thread.finished_signal.connect(self.on_preview_finished)
        self.preview_thread.error_signal.connect(self.on_preview_error)
        self.preview_thread.start()
    
    def on_preview_finished(self, png_path, clip_path):
        if os.path.exists(png_path):
            pixmap = QPixmap(png_path)
            if pixmap.isNull():
                self.main_preview.set_message('Ошибка: файл предпросмотра повреждён')
                self._preview_frame_pixmap = None
                self._preview_signature_pending = None
            else:
                if self._preview_signature_pending is not None:
                    self._preview_signature = self._preview_signature_pending
                self._preview_signature_pending = None
                self._preview_frame_pixmap = pixmap
        else:
            self.main_preview.set_message('Ошибка: файл предпросмотра не найден')
            self._preview_signature_pending = None
        
        self.update_subtitle_on_video_preview()

        self._clip_last_path = None
        clip_abs = os.path.abspath(clip_path) if clip_path else ''
        if (
            clip_abs
            and os.path.isfile(clip_abs)
            and os.path.getsize(clip_abs) > 0
            and self._clip_player is not None
        ):
            self._clip_last_path = clip_abs
            self.btn_preview_video_mode.setEnabled(True)
            self._clip_player.setMedia(QMediaContent(QUrl.fromLocalFile(clip_abs)))
            self._clip_player.play()
            self.preview_stack.setCurrentIndex(0)
            self.btn_preview_video_mode.setChecked(True)
        else:
            self.btn_preview_video_mode.setEnabled(False)
            self.preview_stack.setCurrentIndex(1)
            self.btn_preview_still_mode.setChecked(True)

        self.set_controls_enabled(True)

    def _on_clip_media_status(self, status):
        if not self._clip_player:
            return
        if not PREVIEW_CLIP_AVAILABLE:
            return
        if status == QMediaPlayer.EndOfMedia:
            self._clip_player.setPosition(0)
            self._clip_player.play()

    def _set_preview_stack_visual(self, page: int):
        if page == 0 and not self._clip_last_path:
            self.preview_stack.setCurrentIndex(1)
            self.btn_preview_still_mode.setChecked(True)
            return
        self.preview_stack.setCurrentIndex(page)
        if page == 0 and self._clip_player and self._clip_last_path:
            self._clip_player.play()
        elif self._clip_player:
            self._clip_player.pause()
        if page == 0:
            self.btn_preview_video_mode.setChecked(True)
        else:
            self.btn_preview_still_mode.setChecked(True)
    
    def on_preview_error(self, error_msg):
        if self._clip_player is not None:
            self._clip_player.stop()
        self._clip_last_path = None
        self.btn_preview_video_mode.setEnabled(False)
        self.preview_stack.setCurrentIndex(1)
        self.btn_preview_still_mode.setChecked(True)
        self.main_preview.set_message('Ошибка генерации предпросмотра')
        self._preview_frame_pixmap = None
        self._preview_signature_pending = None
        self.update_subtitle_on_video_preview()
        QMessageBox.critical(self, 'Ошибка предпросмотра', f'Не удалось создать предпросмотр:\n\n{error_msg}')
        self.set_controls_enabled(True)
    
    def set_controls_enabled(self, enabled):
        self.process_button.setEnabled(enabled)
        self.preview_button.setEnabled(enabled)
        self.video_list_widget.setEnabled(enabled)
    
    def on_output_format_changed(self, format_text):
        is_reels = format_text == REELS_FORMAT_NAME
        self.blur_background_checkbox.setEnabled(is_reels)
        if not is_reels:
            self.blur_background_checkbox.setChecked(False)
        self.update_subtitle_on_video_preview()
    
    def on_list_menu(self, pos: QPoint):
        menu = QMenu()
        act_del = menu.addAction('Удалить выделенное')
        act_clear = menu.addAction('Очистить список')
        
        chosen = menu.exec_(self.video_list_widget.viewport().mapToGlobal(pos))
        
        if chosen == act_del:
            selected_items = self.video_list_widget.selectedItems()
            if selected_items:
                for it in reversed(selected_items):
                    self.video_list_widget.takeItem(self.video_list_widget.row(it))
                self.refresh_video_list_display()
        elif chosen == act_clear:
            self.on_clear_list()
    
    def on_clear_list(self):
        self.video_list_widget.clear()
        self.refresh_video_list_display()
    
    def on_select_overlay(self):
        overlay_filter = (
            'Баннеры (*.png *.jpg *.jpeg *.bmp *.gif *.mp4 *.mov *.webm *.mkv *.m4v);;'
            'Изображения (*.png *.jpg *.jpeg *.bmp *.gif);;'
            'Видео (*.mp4 *.mov *.webm *.mkv *.m4v);;'
            'Все файлы (*)'
        )
        fs, _ = QFileDialog.getOpenFileNames(
            self, 'Файл баннера (картинка, GIF или видео MP4/MOV)', '',
            overlay_filter
        )
        if fs:
            self.overlay_path.setText(fs[0])

    def on_pick_overlay_chroma_color(self):
        c = QColorDialog.getColor(QColor(self.overlay_chroma_color_hex), self, 'Цвет хромакея')
        if c.isValid():
            self.overlay_chroma_color_hex = c.name().upper()
            self.overlay_chroma_color_preview.setStyleSheet(
                f'background: {self.overlay_chroma_color_hex}; border: 1px solid #666;'
            )
            self.update_subtitle_on_video_preview()
            self.schedule_effects_preview_refresh(0)

    def _overlay_chroma_params(self):
        """Параметры chromakey для FFmpeg (float similarity/blend)."""
        return {
            'enabled': self.overlay_chroma_check.isChecked(),
            'color': self.overlay_chroma_color_hex,
            'similarity': max(0.01, min(0.8, self.overlay_chroma_sim_spin.value() * 0.01)),
            'blend': max(0.0, min(0.5, self.overlay_chroma_blend_spin.value() * 0.01)),
        }
    
    def on_add_files(self):
        file_filter = 'Видео и GIF (*.mp4 *.mov *.avi *.mkv *.flv *.wmv *.gif);;Все файлы (*)'
        fs, _ = QFileDialog.getOpenFileNames(
            self, 'Выберите видео или GIF', '', file_filter
        )
        if not fs:
            return
        
        added = False
        for f in fs:
            if (is_video_file(f) or f.lower().endswith('.gif')) and not self.video_list_widget.is_already_added(f):
                it = QListWidgetItem(f)
                it.setData(Qt.UserRole, f)
                self.video_list_widget.addItem(it)
                added = True
        
        if added:
            self.refresh_video_list_display()
    
    def on_add_folder(self):
        fol = QFileDialog.getExistingDirectory(self, 'Выберите папку', '')
        if not fol:
            return
        
        vs = find_videos_in_folder(fol, include_gifs=True)
        added = False
        for v in vs:
            if not self.video_list_widget.is_already_added(v):
                it = QListWidgetItem(v)
                it.setData(Qt.UserRole, v)
                self.video_list_widget.addItem(it)
                added = True
        
        if added:
            self.refresh_video_list_display()
    
    def refresh_video_list_display(self):
        self._subs_frame_dim_cache = None
        for i in range(self.video_list_widget.count()):
            it = self.video_list_widget.item(i)
            if not it.text().startswith('[YT]'):
                f = it.data(Qt.UserRole)
                base_name = os.path.basename(f)
                it.setText(f'{i + 1}. {base_name}')
        self.update_subtitle_on_video_preview()
    
    def on_zoom_mode_changed(self):
        is_dynamic = self.zoom_dynamic_radio.isChecked()
        self.zoom_static_widget.setVisible(not is_dynamic)
        self.zoom_dynamic_widget.setVisible(is_dynamic)
        self.update_subtitle_on_video_preview()
    
    def on_speed_mode_changed(self):
        is_dynamic = self.speed_dynamic_radio.isChecked()
        self.speed_static_widget.setVisible(not is_dynamic)
        self.speed_dynamic_widget.setVisible(is_dynamic)

    def on_viral_mode_changed(self):
        enabled = self.viral_enable_checkbox.isChecked()
        self.viral_duration_spin.setEnabled(enabled)
        self.viral_count_spin.setEnabled(enabled)

    def load_presets_from_config(self):
        stored = self.parent_window.config_manager.get_setting('processing_presets', {})
        self.processing_presets = stored if isinstance(stored, dict) else {}
        self.refresh_presets_combo()

    def refresh_presets_combo(self):
        self.presets_combo.clear()
        self.presets_combo.addItems(sorted(self.processing_presets.keys()))

    def _collect_current_processing_settings(self):
        selected_filters = [self.filter_list.item(i).text() for i in range(self.filter_list.count())
                            if self.filter_list.item(i).isSelected()]

        if self.subs_from_file_radio.isChecked():
            subs_mode = 'srt_file'
        elif self.subs_generate_radio.isChecked():
            subs_mode = 'whisper'
        else:
            subs_mode = 'none'

        font_flags = self._subtitle_font_flags()
        return {
            'output_format': self.output_format_combo.currentText(),
            'blur_background': self.blur_background_checkbox.isChecked(),
            'codec_label': self.codec_combo.currentText(),
            'auto_crop': self.auto_crop_checkbox.isChecked(),
            'filters': selected_filters,
            'zoom_mode': 'dynamic' if self.zoom_dynamic_radio.isChecked() else 'static',
            'zoom_static': self.zoom_static_spin.value(),
            'zoom_min': self.zoom_min_spin.value(),
            'zoom_max': self.zoom_max_spin.value(),
            'speed_mode': 'dynamic' if self.speed_dynamic_radio.isChecked() else 'static',
            'speed_static': self.speed_static_spin.value(),
            'speed_min': self.speed_min_spin.value(),
            'speed_max': self.speed_max_spin.value(),
            'viral_enabled': self.viral_enable_checkbox.isChecked(),
            'viral_duration': self.viral_duration_spin.value(),
            'viral_count': self.viral_count_spin.value(),
            'overlay_file': self.overlay_path.text().strip(),
            'overlay_alignment': self._overlay_alignment_value(),
            'overlay_margin_v': self.overlay_margin_v_spin.value(),
            'overlay_margin_lr': self.overlay_margin_lr_spin.value(),
            'overlay_scale': self.overlay_scale_slider.value(),
            'overlay_chroma_enabled': self.overlay_chroma_check.isChecked(),
            'overlay_chroma_color': self.overlay_chroma_color_hex,
            'overlay_chroma_sim': self.overlay_chroma_sim_spin.value(),
            'overlay_chroma_blend': self.overlay_chroma_blend_spin.value(),
            'subtitle_mode': subs_mode,
            'subtitle_srt_path': self.subs_srt_path.text().strip(),
            'subtitle_model': self.subs_model_combo.currentText(),
            'subtitle_language': self.subs_lang_combo.currentText(),
            'subtitle_words_per_line': self.subs_words_spin.value(),
            'subtitle_word_anim_base_size': self.subs_word_anim_base_size_spin.value(),
            'subtitle_word_anim_zoom_size': self.subs_word_anim_zoom_size_spin.value(),
            'subtitle_word_anim_vertical_offset': self.subs_word_anim_offset_spin.value(),
            'subtitle_font': self.subs_font_combo.currentText().strip(),
            'subtitle_font_style': self.subs_font_style_combo.currentText(),
            'subtitle_font_size': self.subs_size_spin.value(),
            'subtitle_outline': self.subs_outline_spin.value(),
            'subtitle_outline_mode': self.subs_outline_mode_combo.currentText(),
            'subtitle_alignment': int(self.subs_position_combo.currentData() or 2),
            'subtitle_margin_v': self.subs_margin_v_spin.value(),
            'subtitle_margin_lr': self.subs_margin_lr_spin.value(),
            'subtitle_text_color': self.subs_text_color_hex,
            'subtitle_outline_color': self.subs_outline_color_hex,
            'subtitle_bold': font_flags['bold'],
            'subtitle_italic': font_flags['italic'],
            'subtitle_underline': font_flags['underline'],
            'mute_audio': self.mute_checkbox.isChecked(),
            'orig_volume': self.orig_vol_slider.value(),
            'overlay_audio_file': self.overlay_audio_path_edit.text().strip(),
            'overlay_volume': self.over_vol_slider.value(),
            'strip_metadata': self.parent_window.settings_widget.strip_meta_checkbox.isChecked(),
            'jumpcut_enabled': self.jumpcut_enable_checkbox.isChecked(),
            'jumpcut_aggressiveness': self.jumpcut_aggressiveness_combo.currentIndex(),
            'jumpcut_fade_duration': self.jumpcut_fade_spin.value(),
            'censor_subtitles': self.censor_subtitles_check.isChecked(),
            'censor_metadata': self.censor_metadata_check.isChecked(),
            'censor_list': self.get_censor_list()
        }

    def _apply_processing_settings(self, preset):
        # Меню и трансформация
        output_format = preset.get('output_format', self.output_format_combo.currentText())
        if self.output_format_combo.findText(output_format) >= 0:
            self.output_format_combo.setCurrentText(output_format)
        self.blur_background_checkbox.setChecked(bool(preset.get('blur_background', False)))

        codec_label = preset.get('codec_label', self.codec_combo.currentText())
        if self.codec_combo.findText(codec_label) >= 0:
            self.codec_combo.setCurrentText(codec_label)

        self.auto_crop_checkbox.setChecked(bool(preset.get('auto_crop', False)))

        wanted_filters = set(preset.get('filters', []))
        for i in range(self.filter_list.count()):
            item = self.filter_list.item(i)
            item.setSelected(item.text() in wanted_filters)

        if preset.get('zoom_mode') == 'dynamic':
            self.zoom_dynamic_radio.setChecked(True)
        else:
            self.zoom_static_radio.setChecked(True)
        self.zoom_static_spin.setValue(int(preset.get('zoom_static', 100)))
        self.zoom_min_spin.setValue(int(preset.get('zoom_min', 80)))
        self.zoom_max_spin.setValue(int(preset.get('zoom_max', 120)))
        self.on_zoom_mode_changed()

        if preset.get('speed_mode') == 'dynamic':
            self.speed_dynamic_radio.setChecked(True)
        else:
            self.speed_static_radio.setChecked(True)
        self.speed_static_spin.setValue(int(preset.get('speed_static', 100)))
        self.speed_min_spin.setValue(int(preset.get('speed_min', 90)))
        self.speed_max_spin.setValue(int(preset.get('speed_max', 110)))
        self.on_speed_mode_changed()

        self.viral_enable_checkbox.setChecked(bool(preset.get('viral_enabled', False)))
        self.viral_duration_spin.setValue(int(preset.get('viral_duration', 15)))
        self.viral_count_spin.setValue(int(preset.get('viral_count', 3)))
        self.on_viral_mode_changed()

        # Наложения и субтитры
        self.overlay_path.setText(preset.get('overlay_file', ''))
        try:
            o_al = int(preset.get('overlay_alignment', 0))
        except (TypeError, ValueError):
            o_al = 0
        if o_al < 1 or o_al > 9:
            legacy = preset.get('overlay_pos')
            if isinstance(legacy, str) and legacy in OVERLAY_LEGACY_POS_TO_ALIGNMENT:
                o_al = OVERLAY_LEGACY_POS_TO_ALIGNMENT[legacy]
            else:
                o_al = 5
        _idx_ov = -1
        for i in range(self.overlay_position_combo.count()):
            raw = self.overlay_position_combo.itemData(i)
            try:
                if int(raw) == o_al:
                    _idx_ov = i
                    break
            except (TypeError, ValueError):
                continue
        if _idx_ov >= 0:
            self.overlay_position_combo.setCurrentIndex(_idx_ov)
        self.overlay_margin_v_spin.setValue(int(preset.get('overlay_margin_v', 0)))
        self.overlay_margin_lr_spin.setValue(int(preset.get('overlay_margin_lr', 0)))
        self.overlay_scale_slider.setValue(int(preset.get('overlay_scale', 100)))
        self.overlay_chroma_check.setChecked(bool(preset.get('overlay_chroma_enabled', False)))
        _och = preset.get('overlay_chroma_color', '#00FF00')
        if isinstance(_och, str) and _och.startswith('#') and len(_och) == 7:
            self.overlay_chroma_color_hex = _och.upper()
        self.overlay_chroma_color_preview.setStyleSheet(
            f'background: {self.overlay_chroma_color_hex}; border: 1px solid #666;'
        )
        self.overlay_chroma_sim_spin.setValue(int(preset.get('overlay_chroma_sim', 15)))
        self.overlay_chroma_blend_spin.setValue(int(preset.get('overlay_chroma_blend', 8)))

        subs_mode = preset.get('subtitle_mode', 'none')
        self.subs_off_radio.setChecked(subs_mode == 'none')
        self.subs_from_file_radio.setChecked(subs_mode == 'srt_file')
        self.subs_generate_radio.setChecked(subs_mode == 'whisper')
        self.on_subs_mode_changed()

        self.subs_srt_path.setText(preset.get('subtitle_srt_path', ''))
        model = preset.get('subtitle_model', self.subs_model_combo.currentText())
        if self.subs_model_combo.findText(model) >= 0:
            self.subs_model_combo.setCurrentText(model)
        lang = preset.get('subtitle_language', self.subs_lang_combo.currentText())
        if self.subs_lang_combo.findText(lang) >= 0:
            self.subs_lang_combo.setCurrentText(lang)
        self.subs_words_spin.setValue(int(preset.get('subtitle_words_per_line', 2)))
        self.subs_word_anim_base_size_spin.setValue(int(preset.get('subtitle_word_anim_base_size', 60)))
        self.subs_word_anim_zoom_size_spin.setValue(int(preset.get('subtitle_word_anim_zoom_size', 80)))
        self.subs_word_anim_offset_spin.setValue(int(preset.get('subtitle_word_anim_vertical_offset', 200)))

        font_name = preset.get('subtitle_font', self.subs_font_combo.currentText())
        if self.subs_font_combo.findText(font_name) >= 0:
            self.subs_font_combo.setCurrentText(font_name)

        style_name = preset.get('subtitle_font_style', self.subs_font_style_combo.currentText())
        if self.subs_font_style_combo.findText(style_name) >= 0:
            self.subs_font_style_combo.setCurrentText(style_name)

        self.subs_size_spin.setValue(int(preset.get('subtitle_font_size', 36)))
        self.subs_outline_spin.setValue(int(preset.get('subtitle_outline', 2)))
        outline_mode = preset.get('subtitle_outline_mode', self.subs_outline_mode_combo.currentText())
        if self.subs_outline_mode_combo.findText(outline_mode) >= 0:
            self.subs_outline_mode_combo.setCurrentText(outline_mode)

        al = int(preset.get('subtitle_alignment', 2))
        _idx = -1
        for i in range(self.subs_position_combo.count()):
            raw = self.subs_position_combo.itemData(i)
            try:
                if int(raw) == al:
                    _idx = i
                    break
            except (TypeError, ValueError):
                continue
        if _idx >= 0:
            self.subs_position_combo.setCurrentIndex(_idx)
        self.subs_margin_v_spin.setValue(int(preset.get('subtitle_margin_v', 110)))
        self.subs_margin_lr_spin.setValue(int(preset.get('subtitle_margin_lr', 28)))

        self.subs_text_color_hex = preset.get('subtitle_text_color', '#FFFFFF')
        self.subs_outline_color_hex = preset.get('subtitle_outline_color', '#000000')
        self.subs_text_color_preview.setStyleSheet(
            f'background: {self.subs_text_color_hex}; border: 1px solid #666;'
        )
        self.subs_outline_color_preview.setStyleSheet(
            f'background: {self.subs_outline_color_hex}; border: 1px solid #666;'
        )
        self.update_subtitle_style_preview()

        # Аудио + глобальные настройки
        self.mute_checkbox.setChecked(bool(preset.get('mute_audio', False)))
        self.orig_vol_slider.setValue(int(preset.get('orig_volume', 100)))
        self.overlay_audio_path_edit.setText(preset.get('overlay_audio_file', ''))
        self.over_vol_slider.setValue(int(preset.get('overlay_volume', 100)))
        self.parent_window.settings_widget.strip_meta_checkbox.setChecked(
            bool(preset.get('strip_metadata', True))
        )
        
        # Jump Cut настройки
        self.jumpcut_enable_checkbox.setChecked(bool(preset.get('jumpcut_enabled', True)))
        self.jumpcut_aggressiveness_combo.setCurrentIndex(int(preset.get('jumpcut_aggressiveness', 1)))
        self.jumpcut_fade_spin.setValue(float(preset.get('jumpcut_fade_duration', 0.3)))
        
        # Загружаем опции цензуры
        self.load_censor_settings_from_preset(preset)

    def on_save_preset(self):
        preset_name, ok = QInputDialog.getText(self, 'Сохранить пресет', 'Название пресета:')
        preset_name = (preset_name or '').strip()
        if not ok or not preset_name:
            return

        self.processing_presets[preset_name] = copy.deepcopy(self._collect_current_processing_settings())
        self.parent_window.config_manager.set_setting('processing_presets', self.processing_presets)
        self.refresh_presets_combo()
        self.presets_combo.setCurrentText(preset_name)
        self.status_label.setText(f'Пресет "{preset_name}" сохранен')

    def on_apply_preset(self):
        preset_name = self.presets_combo.currentText().strip()
        if not preset_name:
            QMessageBox.warning(self, 'Пресет не выбран', 'Выберите пресет для применения.')
            return
        preset = self.processing_presets.get(preset_name)
        if not preset:
            QMessageBox.warning(self, 'Ошибка', f'Пресет "{preset_name}" не найден.')
            return
        self._apply_processing_settings(preset)
        self.status_label.setText(f'Пресет "{preset_name}" применен')

    def on_delete_preset(self):
        preset_name = self.presets_combo.currentText().strip()
        if not preset_name:
            QMessageBox.warning(self, 'Пресет не выбран', 'Выберите пресет для удаления.')
            return
        reply = QMessageBox.question(
            self, 'Удаление пресета',
            f'Удалить пресет "{preset_name}"?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        self.processing_presets.pop(preset_name, None)
        self.parent_window.config_manager.set_setting('processing_presets', self.processing_presets)
        self.refresh_presets_combo()
        self.status_label.setText(f'Пресет "{preset_name}" удален')
    
    def start_processing(self):
        video_files = [
            self.video_list_widget.item(i).data(Qt.UserRole)
            for i in range(self.video_list_widget.count())
        ]
        
        if not video_files:
            QMessageBox.warning(self, 'Нет файлов', 'Добавьте хотя бы один видео или GIF файл.')
            return
        
        out_dir = QFileDialog.getExistingDirectory(self, 'Выберите папку для сохранения результатов')
        if not out_dir:
            return
        
        # Настройки субтитров
        subtitle_settings = {'mode': 'none'}
        
        if self.subs_from_file_radio.isChecked():
            subtitle_settings['mode'] = 'srt_file'
            subtitle_settings['srt_path'] = self.subs_srt_path.text()
        elif self.subs_generate_radio.isChecked():
            subtitle_settings['mode'] = 'whisper'
            subtitle_settings['model'] = self.subs_model_combo.currentText()
            subtitle_settings['language'] = self.subs_lang_combo.currentText()
            subtitle_settings['words_per_line'] = self.subs_words_spin.value()
            subtitle_settings['word_anim_base_size'] = self.subs_word_anim_base_size_spin.value()
            subtitle_settings['word_anim_zoom_size'] = self.subs_word_anim_zoom_size_spin.value()
            subtitle_settings['word_anim_vertical_offset'] = self.subs_word_anim_offset_spin.value()
        
        _al_raw = self.subs_position_combo.currentData()
        try:
            _alignment = int(_al_raw) if _al_raw is not None else 2
        except (TypeError, ValueError):
            _alignment = 2
        subtitle_settings['style'] = {
            'font_size': self.subs_size_spin.value(),
            'font_name': self.subs_font_combo.currentText().strip() or 'Arial',
            'font_bold': self._subtitle_font_flags()['bold'],
            'font_italic': self._subtitle_font_flags()['italic'],
            'font_underline': self._subtitle_font_flags()['underline'],
            'text_color': self.subs_text_color_hex,
            'outline_color': self.subs_outline_color_hex,
            'outline': self.subs_outline_spin.value(),
            'outline_mode': self.subs_outline_mode_combo.currentText().lower(),
            'alignment': _alignment,
            'margin_v': self.subs_margin_v_spin.value(),
            'margin_lr': self.subs_margin_lr_spin.value(),
        }
        
        # Создание worker'а
        _och = self._overlay_chroma_params()
        self.processing_thread = Worker(
            files=video_files,
            filters=[item.text() for item in self.filter_list.selectedItems()],
            zoom_mode='dynamic' if self.zoom_dynamic_radio.isChecked() else 'static',
            zoom_static=self.zoom_static_spin.value(),
            zoom_min=self.zoom_min_spin.value(),
            zoom_max=self.zoom_max_spin.value(),
            speed_mode='dynamic' if self.speed_dynamic_radio.isChecked() else 'static',
            speed_static=self.speed_static_spin.value(),
            speed_min=self.speed_min_spin.value(),
            speed_max=self.speed_max_spin.value(),
            overlay_file=self.overlay_path.text().strip() or None,
            overlay_alignment=self._overlay_alignment_value(),
            overlay_margin_v=self.overlay_margin_v_spin.value(),
            overlay_margin_lr=self.overlay_margin_lr_spin.value(),
            overlay_scale_p=self.overlay_scale_slider.value(),
            overlay_chromakey=_och['enabled'],
            overlay_chromakey_color=_och['color'],
            overlay_chromakey_similarity=_och['similarity'],
            overlay_chromakey_blend=_och['blend'],
            out_dir=out_dir,
            mute_audio=self.mute_checkbox.isChecked(),
            output_format=self.output_format_combo.currentText(),
            blur_background=self.blur_background_checkbox.isChecked(),
            strip_metadata=self.parent_window.settings_widget.strip_meta_checkbox.isChecked(),
            codec=CODECS.get(self.codec_combo.currentText(), 'libx264'),
            subtitle_settings=subtitle_settings,
            auto_crop=self.auto_crop_checkbox.isChecked(),
            overlay_audio=self.overlay_audio_path_edit.text().strip() or None,
            original_volume=self.orig_vol_slider.value(),
            overlay_volume=self.over_vol_slider.value(),
            viral_clips_enabled=self.viral_enable_checkbox.isChecked(),
            viral_clip_duration=self.viral_duration_spin.value(),
            viral_clip_count=self.viral_count_spin.value(),
            censor_words=self.get_censor_list(),
            jumpcut_enabled=self.jumpcut_enable_checkbox.isChecked(),
            jumpcut_aggressiveness=self.jumpcut_aggressiveness_combo.currentIndex(),
            jumpcut_fade_duration=self.jumpcut_fade_spin.value()
        )
        
        # Подключение сигналов
        self.processing_thread.progress.connect(self.on_prog)
        self.processing_thread.file_progress.connect(self.on_file_prog)
        self.processing_thread.finished.connect(self.on_done)
        self.processing_thread.error.connect(self.on_err)
        self.processing_thread.file_processing.connect(self.on_file_processing)
        self.processing_thread.status_update.connect(self.on_status_update)
        
        # Начальное состояние
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat('%p%')
        self.progress_label.setText(f'0 / {len(video_files)}')
        self.status_label.setText('Подготовка...')
        self.set_controls_enabled(False)
        
        self.processing_thread.start()
    
    def on_prog(self, done, total):
        self.progress_label.setText(f'{done} / {total}')
        self.progress_bar.setValue(100)
    
    def on_file_prog(self, percentage):
        self.progress_bar.setValue(percentage)
    
    def on_file_processing(self, fname):
        try:
            fm = QFontMetrics(self.status_label.font())
            elided_text = fm.elidedText(
                f'Обрабатываю: {fname}',
                Qt.ElideMiddle,
                self.status_label.width() - 20
            )
            self.status_label.setText(elided_text)
            self.progress_bar.setValue(0)
        except Exception:
            self.status_label.setText(f'Обрабатываю: ...{fname[-30:]}')
            self.progress_bar.setValue(0)
    
    def on_status_update(self, message: str):
        self.status_label.setText(message)
    
    def on_done(self):
        if self.processing_thread and not self.processing_thread.isRunning():
            output_paths = self.processing_thread.output_paths
            QMessageBox.information(self, 'Готово', 'Обработка успешно завершена!')
            
            if len(output_paths) == 1:
                self.video_processed.emit(output_paths[0])
        
        self.set_controls_enabled(True)
        self.status_label.setText('Готово')
        self.processing_thread = None
    
    def on_err(self, msg):
        QMessageBox.critical(self, 'Ошибка обработки', f'Произошла ошибка:\n\n{msg}')
        self.set_controls_enabled(True)
        self.status_label.setText('Ошибка')
        self.processing_thread = None

    # ==================== МЕТОДЫ ДЛЯ УПРАВЛЕНИЯ ЦЕНЗУРОЙ ====================
    
    def on_censor_add_word(self):
        """Добавить слово в черный список"""
        word = self.censor_word_input.text().strip()
        if not word:
            QMessageBox.warning(self, 'Ошибка', 'Введите слово для добавления')
            return
        
        # Проверяем, что слова нет в списке
        for i in range(self.censor_list_widget.count()):
            if self.censor_list_widget.item(i).text().lower() == word.lower():
                QMessageBox.warning(self, 'Ошибка', 'Это слово уже в списке')
                return
        
        self.censor_list_widget.addItem(word)
        self.censor_word_input.clear()
        self.save_censor_list_to_config()
    
    def on_censor_remove_word(self):
        """Удалить выбранные слова из черного списка"""
        selected = self.censor_list_widget.selectedItems()
        if not selected:
            QMessageBox.warning(self, 'Ошибка', 'Выберите слова для удаления')
            return
        
        for item in selected:
            self.censor_list_widget.takeItem(self.censor_list_widget.row(item))
        
        self.save_censor_list_to_config()
    
    def on_censor_clear_list(self):
        """Очистить весь черный список"""
        reply = QMessageBox.question(
            self, 'Подтверждение', 
            'Очистить весь черный список?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.censor_list_widget.clear()
            self.save_censor_list_to_config()
    
    def on_censor_load_from_file(self):
        """Загрузить черный список из файла"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, 'Выберите файл черного списка', '',
            'Text Files (*.txt);;All Files (*.*)'
        )
        
        if not file_path:
            return
        
        try:
            from utils.subtitle_utils import load_censor_list_from_file
            words = load_censor_list_from_file(file_path)
            
            # Очищаем текущий список или добавляем в существующий
            reply = QMessageBox.question(
                self, 'Загрузка', 
                'Заменить текущий список или добавить слова?',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            
            if reply == QMessageBox.Yes:
                self.censor_list_widget.clear()
            
            # Добавляем слова из файла
            for word in words:
                # Проверяем дубликаты
                found = False
                for i in range(self.censor_list_widget.count()):
                    if self.censor_list_widget.item(i).text().lower() == word.lower():
                        found = True
                        break
                
                if not found:
                    self.censor_list_widget.addItem(word)
            
            self.save_censor_list_to_config()
            QMessageBox.information(self, 'Успех', f'Загружено {len(words)} слов из файла')
            
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка', f'Ошибка загрузки файла:\n{str(e)}')
    
    def on_censor_save_to_file(self):
        """Сохранить черный список в файл"""
        if self.censor_list_widget.count() == 0:
            QMessageBox.warning(self, 'Пусто', 'Нечего сохранять - список пуст')
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, 'Сохранить черный список', '',
            'Text Files (*.txt);;All Files (*.*)'
        )
        
        if not file_path:
            return
        
        try:
            words = [self.censor_list_widget.item(i).text() for i in range(self.censor_list_widget.count())]
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(words))
            
            QMessageBox.information(self, 'Успех', f'Сохранено {len(words)} слов')
            
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка', f'Ошибка сохранения файла:\n{str(e)}')
    
    def get_censor_list(self):
        """Получить текущий черный список"""
        words = []
        for i in range(self.censor_list_widget.count()):
            words.append(self.censor_list_widget.item(i).text())
        return words
    
    def save_censor_list_to_config(self):
        """Сохранить черный список в конфиг"""
        preset_name = self.presets_combo.currentText().strip()
        if preset_name and preset_name in self.processing_presets:
            self.processing_presets[preset_name]['censor_list'] = self.get_censor_list()
            self.processing_presets[preset_name]['censor_subtitles'] = self.censor_subtitles_check.isChecked()
            self.processing_presets[preset_name]['censor_metadata'] = self.censor_metadata_check.isChecked()
            self.parent_window.config_manager.set_setting('processing_presets', self.processing_presets)
    
    def load_censor_settings_from_preset(self, preset):
        """Загрузить опции цензуры из пресета"""
        # Загружаем опции
        self.censor_subtitles_check.setChecked(bool(preset.get('censor_subtitles', False)))
        self.censor_metadata_check.setChecked(bool(preset.get('censor_metadata', False)))
        
        # Загружаем черный список
        self.censor_list_widget.clear()
        censor_list = preset.get('censor_list', [])
        for word in censor_list:
            self.censor_list_widget.addItem(word)


class SettingsWidget(QWidget):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Основные настройки
        main_group = QGroupBox('Основные настройки')
        main_layout = QVBoxLayout(main_group)
        
        # FFmpeg путь
        ffmpeg_layout = QHBoxLayout()
        self.ffmpeg_path_edit = QLineEdit()
        self.ffmpeg_path_edit.setPlaceholderText('Укажите путь к ffmpeg.exe (необязательно)')
        browse_ffmpeg_btn = QPushButton('Выбрать')
        
        ffmpeg_layout.addWidget(QLabel('Путь к FFmpeg:'))
        ffmpeg_layout.addWidget(self.ffmpeg_path_edit)
        ffmpeg_layout.addWidget(browse_ffmpeg_btn)
        main_layout.addLayout(ffmpeg_layout)
        
        # Очистка метаданных
        self.strip_meta_checkbox = QCheckBox('Очистить метаданные при обработке')
        self.strip_meta_checkbox.setChecked(True)
        main_layout.addWidget(self.strip_meta_checkbox)
        
        layout.addWidget(main_group)
        
        # Внешний вид
        style_group = QGroupBox('Внешний вид')
        style_layout = QHBoxLayout(style_group)
        
        self.style_combo = QComboBox()
        self.style_combo.addItem('Dark [mod by llimonix]', 'styles_dark')
        self.style_combo.addItem('Light [mod by llimonix]', 'styles_light')
        self.style_combo.addItem('Dark [Original]', 'original_dark')
        self.style_combo.addItem('Light [Original]', 'original_light')
        
        style_layout.addWidget(QLabel('Тема оформления:'))
        style_layout.addWidget(self.style_combo)
        style_layout.addStretch()
        
        layout.addWidget(style_group)
        layout.addStretch()
        
        # Подключение сигналов
        browse_ffmpeg_btn.clicked.connect(self.browse_ffmpeg)
    
    def browse_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Выберите ffmpeg.exe', '',
            'Executable Files (*.exe)'
        )
        if path:
            self.ffmpeg_path_edit.setText(path)


class VideoUnicApp(QMainWindow):
    
    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self.temp_dir = tempfile.mkdtemp(prefix='reels_maker_')
        self.temp_files = []
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle(f'{APP_NAME} v{APP_VERSION}')
        self.setGeometry(100, 100, 1200, 850)
        
        # Иконка приложения
        icon_path = resource_path(os.path.join('resources', 'icon.png'))
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        # Центральный виджет
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        
        # Основной layout
        self.main_layout = QHBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        # Левое меню
        self.left_menu = QFrame()
        self.left_menu.setObjectName('left_menu')
        self.left_menu.setFixedWidth(200)
        
        self.left_menu_layout = QVBoxLayout(self.left_menu)
        self.left_menu_layout.setContentsMargins(0, 0, 0, 0)
        self.left_menu_layout.setSpacing(0)
        
        # Группа кнопок меню
        self.button_group = QButtonGroup()
        self.button_group.setExclusive(True)
        
        # Кнопки меню
        self.processing_btn = QPushButton(qta.icon('fa5s.cogs', color='white', color_active='white'), ' Обработка')
        self.processing_btn.setObjectName('menu_button')
        self.processing_btn.setCheckable(True)
        self.button_group.addButton(self.processing_btn)
        
        self.upload_btn = QPushButton(qta.icon('fa5s.upload', color='white', color_active='white'), ' Загрузка на YouTube')
        self.upload_btn.setObjectName('menu_button')
        self.upload_btn.setCheckable(True)
        self.button_group.addButton(self.upload_btn)
        
        self.settings_btn = QPushButton(qta.icon('fa5s.sliders-h', color='white', color_active='white'), ' Настройки')
        self.settings_btn.setObjectName('menu_button')
        self.settings_btn.setCheckable(True)
        self.button_group.addButton(self.settings_btn)
        
        # Добавление кнопок в layout
        self.left_menu_layout.addWidget(self.processing_btn)
        self.left_menu_layout.addWidget(self.upload_btn)
        self.left_menu_layout.addWidget(self.settings_btn)
        self.left_menu_layout.addStretch()
        
        # Кнопка выхода
        self.exit_btn = QPushButton(qta.icon('fa5s.sign-out-alt', color='white', color_active='white'), ' Выход')
        self.exit_btn.setObjectName('menu_button')
        self.left_menu_layout.addWidget(self.exit_btn)
        
        # Основной контент
        self.main_content = QFrame()
        
        self.main_layout.addWidget(self.left_menu)
        self.main_layout.addWidget(self.main_content)
        
        self.main_content_layout = QVBoxLayout(self.main_content)
        
        # Создание виджетов содержимого
        self.processing_widget = ProcessingWidgetContent(self)
        self.settings_widget = SettingsWidget(self)
        self.uploader_widget = UploaderWidget(self)
        
        # Стек виджетов
        self.stacked_widget = QStackedWidget()
        self.stacked_widget.addWidget(self.processing_widget)
        self.stacked_widget.addWidget(self.uploader_widget)
        self.stacked_widget.addWidget(self.settings_widget)
        
        self.main_content_layout.addWidget(self.stacked_widget)
        
        # Подключение сигналов
        self.processing_btn.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
        self.upload_btn.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(1))
        self.settings_btn.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(2))
        self.exit_btn.clicked.connect(self.close)
        
        # Настройки темы
        self.settings_widget.style_combo.currentIndexChanged[int].connect(self.on_style_changed)
        
        # Установка начального состояния
        self.processing_btn.setChecked(True)
        # Установка начального стиля
        style_default = self.config_manager.get_setting('style', 'styles_dark')
        self.apply_stylesheet(style_default)

        # Установка стиля в комбобоксе
        self.settings_widget.style_combo.setCurrentIndex(self.settings_widget.style_combo.findData(style_default))
        
        # Подключение сигнала обработки видео
        self.processing_widget.video_processed.connect(self.prepare_for_upload)

    def on_style_changed(self, index):
        # Получение выбранного стиля
        style_key = self.settings_widget.style_combo.itemData(index)
        # Сохранение выбранного стиля в конфигурации
        self.config_manager.set_setting('style', style_key)
        # Применение стиля
        self.apply_stylesheet(style_key)

    def apply_stylesheet(self, mode):
        style_filename = f'{mode}.qss'
        path = resource_path(os.path.join('resources', style_filename))
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                style = f.read()
                self.setStyleSheet(style)

                icon_color = 'black' if 'light' in style_filename else 'white'
                
                self.processing_btn.setIcon(qta.icon('fa5s.cogs', color=icon_color, color_active='white'))
                self.upload_btn.setIcon(qta.icon('fa5s.upload', color=icon_color, color_active='white'))
                self.settings_btn.setIcon(qta.icon('fa5s.sliders-h', color=icon_color, color_active='white'))
                self.exit_btn.setIcon(qta.icon('fa5s.sign-out-alt', color=icon_color, color_active='white'))
                self.uploader_widget.add_account_btn.setIcon(qta.icon('fa5s.user-plus', color=icon_color, color_active='white'))
                for i in range(self.uploader_widget.tabs.count()):
                    icon = qta.icon('fa5s.user-circle', color=icon_color, color_active='white')
                    self.uploader_widget.tabs.setTabIcon(i, icon)

        except FileNotFoundError:
            print(f'Stylesheet not found at {path}')
            self.setStyleSheet('')
    
    def prepare_for_upload(self, video_path):
        # Получение списка аккаунтов
        accounts = self.uploader_widget.get_account_names()
        if not accounts:
            QMessageBox.warning(self, 'Нет аккаунтов', "Сначала добавьте аккаунт в разделе 'Загрузка на YouTube'.")
            return
        
        # Подтверждение загрузки
        reply = QMessageBox.question(
            self, 'Загрузка видео',
            'Видео успешно обработано. Хотите отправить его на загрузку?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        
        if reply == QMessageBox.No:
            return
        
        # Выбор аккаунта
        account_name, ok = QInputDialog.getItem(
            self, 'Выбор аккаунта',
            'Выберите аккаунт для загрузки:',
            accounts, 0, False
        )
        
        if ok and account_name:
            # Переключение на вкладку загрузки
            self.stacked_widget.setCurrentWidget(self.uploader_widget)
            self.upload_btn.setChecked(True)
            
            # Передача видео для загрузки
            self.uploader_widget.receive_video_for_upload(video_path, account_name)
    
    def _cleanup_temp_files(self):
        print('Cleaning up temporary files...')
        
        import time
        def safe_remove(file_path):
            if os.path.exists(file_path):
                for _ in range(3):
                    try:
                        os.remove(file_path)
                        break
                    except OSError as e:
                        if _ == 2:
                            print(f'Error removing temp file {file_path}: {e}')
                        time.sleep(0.5)
        
        def safe_rmtree(dir_path):
            if os.path.exists(dir_path):
                for _ in range(3):
                    try:
                        shutil.rmtree(dir_path)
                        break
                    except OSError as e:
                        if _ == 2:
                            print(f'Error removing temp directory {dir_path}: {e}')
                        time.sleep(0.5)
        
        # Удаление временных файлов
        for f in self.temp_files:
            safe_remove(f)
        
        self.temp_files.clear()
        
        # Удаление временной директории
        safe_rmtree(self.temp_dir)
    
    def closeEvent(self, event):
        # Проверка на работающие потоки
        proc_thread = self.processing_widget.processing_thread
        is_running = proc_thread and proc_thread.isRunning()
        
        reply = QMessageBox.Yes
        if is_running:
            reply = QMessageBox.question(
                self, 'Подтверждение',
                'Идет обработка видео. Вы уверены, что хотите выйти?',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
        
        if reply == QMessageBox.Yes:
            if is_running:
                try:
                    proc_thread.stop()
                    proc_thread.wait(1000)
                except Exception as e:
                    print(f'Error stopping worker thread: {e}')
            
            self._cleanup_temp_files()
            event.accept()
        else:
            event.ignore()