from ....helper.ext_utils.status_utils import (
    MirrorStatus,
    EngineStatus,
    get_readable_file_size,
    get_readable_time,
)


class UphosterStatus:
    def __init__(self, listener, obj, gid, status):
        self.listener = listener
        self._obj = obj
        self._size = self.listener.size
        self._gid = gid
        self._status = status
        self.engine = EngineStatus().STATUS_UPHOSTER

    def processed_bytes(self):
        return get_readable_file_size(self._obj.processed_bytes)

    def size(self):
        return get_readable_file_size(self._size)

    def status(self):
        if self._status == "up":
            return MirrorStatus.STATUS_UPLOAD
        return MirrorStatus.STATUS_DOWNLOAD

    def name(self):
        return self.listener.name

    def gid(self) -> str:
        return self._gid

    def progress_raw(self):
        try:
            return self._obj.processed_bytes / self._size * 100
        except ZeroDivisionError:
            return 0

    def progress(self):
        return f"{round(self.progress_raw(), 2)}%"

    def speed(self):
        return f"{get_readable_file_size(self._obj.speed)}/s"

    def status_message(self):
        if hasattr(self._obj, "status_message") and (message := self._obj.status_message()):
            return message
        if self._status == "up" and self.progress_raw() >= 99.9:
            return "Uploading to TeleCloud. Pls wait..."
        return ""

    def eta(self):
        try:
            seconds = (self._size - self._obj.processed_bytes) / self._obj.speed
            return get_readable_time(seconds)
        except Exception:
            return "-"

    def task(self):
        return self._obj
