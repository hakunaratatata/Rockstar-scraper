from PySide6.QtCore import QObject, Signal, Slot

from .engine import MonitorEngine


class CheckWorker(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, engine: MonitorEngine, official_only: bool = False):
        super().__init__()
        self.engine = engine
        self.official_only = official_only

    @Slot()
    def run(self) -> None:
        try:
            self.succeeded.emit(self.engine.run_cycle(self.official_only, progress=self.progress.emit))
        except Exception as exc:
            self.failed.emit(f"Monitoring cycle failed: {exc}")
        finally:
            self.finished.emit()

