"""Cooperative task control: pause at safe inference boundaries, never freeze HTTP."""
import threading
import time

ACTIVE = ("queued", "running", "pausing", "paused")


class JobCancelled(Exception):
    pass


class JobControl:
    def __init__(self):
        self.cv = threading.Condition()
        self.paused = False
        self.cancelled = False
        self.paused_seconds = 0.0
        self.pause_started = None

    def clock(self):
        with self.cv:
            now = time.monotonic()
            waiting = now - self.pause_started if self.pause_started is not None else 0
            return now - self.paused_seconds - waiting

    def checkpoint(self, on_pause=lambda: None, on_resume=lambda: None):
        with self.cv:
            if self.cancelled:
                raise JobCancelled()
            if self.paused:
                self.pause_started = time.monotonic()
                on_pause()
                while self.paused and not self.cancelled:
                    self.cv.wait()
                self.paused_seconds += time.monotonic() - self.pause_started
                self.pause_started = None
                if self.cancelled:
                    raise JobCancelled()
                on_resume()
