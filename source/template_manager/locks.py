"""Shared/exclusive lock for the template library.

Reads (listing, downloads, backups, matching) run side by side; changes run one
at a time and wait for running reads to finish. A waiting change blocks new
reads, so a steady stream of reads cannot starve it. Not re-entrant: never take
either side while already holding one.
"""
from contextlib import contextmanager
import threading


class LibraryLock:
    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def shared(self):
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if not self._readers:
                    self._cond.notify_all()

    @contextmanager
    def exclusive(self):
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()
