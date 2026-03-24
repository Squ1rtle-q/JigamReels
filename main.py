import sys
import os
import ctypes
import logging

if not getattr(sys, 'frozen', False):
    _project_root = os.path.dirname(os.path.abspath(__file__))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QIcon

from utils.path_utils import get_ffmpeg_path, get_logs_directory
from ui.main_window import VideoUnicApp
from utils.constants import APP_NAME, APP_VERSION, FFMPEG_EXE_PATH


def set_app_user_model_id(app_id):
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


def main():
    if getattr(sys, 'frozen', False):
        log_file_path = os.path.join(get_logs_directory(), "crash_log.log")
    else:
        log_file_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "crash_log.log",
        )
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] - %(message)s",
        handlers=[
            logging.FileHandler(log_file_path, mode="w", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logging.info("Application started.")

    if sys.platform.startswith("win"):
        try:
            os.system("chcp 65001")
            os.environ["PYTHONIOENCODING"] = "utf-8"
        except Exception as e:
            logging.warning("Warning: Failed to set console encoding: " + str(e))

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    myappid = f"mycompany.{APP_NAME}.{APP_VERSION}"
    set_app_user_model_id(myappid)

    ffpmeg_path = get_ffmpeg_path()

    if sys.platform.startswith("win") and not os.path.exists(ffpmeg_path):
        logging.error(f"Error: ffmpeg.exe not found at {FFMPEG_EXE_PATH}")
        logging.error("Please ensure FFmpeg is in the specified path or in your system's PATH.")

    w = VideoUnicApp()
    w.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    # Windows + PyInstaller: дочерние процессы (multiprocessing в зависимостях) не должны
    # повторно выполнять main() и открывать второе окно Qt.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
    