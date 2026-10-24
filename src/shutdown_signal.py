"""Cooperative stop notice for independently launched project clients."""
from pathlib import Path
import uuid


class ShutdownNotice:
    def __init__(self, path):
        self.path = Path(path)
        self.initial = self.read()

    def read(self):
        try:
            return self.path.read_text(encoding='ascii')
        except (OSError, UnicodeError):
            return ''

    def changed(self):
        value = self.read()
        return bool(value and value != self.initial)

    @staticmethod
    def broadcast(path):
        Path(path).write_text(uuid.uuid4().hex, encoding='ascii')
