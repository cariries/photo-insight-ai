"""Background batch job: send each photo to the local model, one at a time."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import geo, images
from .config import Config
from .llm import LLMError, Ollama
from .store import Store, signature


@dataclass
class JobState:
    status: str = "idle"          # idle | running | cancelling | cancelled | done | failed
    total: int = 0
    done: int = 0
    skipped: int = 0
    errors: int = 0
    current: str | None = None
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    seconds_per_photo: float | None = None
    last_error: str | None = None
    changed: list[str] = field(default_factory=list)  # paths finished since last poll

    def snapshot(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "changed"}
        remaining = self.total - self.done - self.skipped - self.errors
        d["eta_seconds"] = round(remaining * self.seconds_per_photo) if self.seconds_per_photo and self.status == "running" else None
        return d


def analyze_one(path: Path, cfg: Config, store: Store, client: Ollama, sig: str, force: bool) -> None:
    s = cfg.settings
    jpeg = images.jpeg_bytes(path, s.image_size)
    place = geo.lookup(*(store.ensure_gps(path, refresh=True) or (None, None)))
    result = client.analyze(jpeg, s.model, cfg.categories, s.language, s.chinese_hashtags,
                            place.describe() if place else None)
    store.save_analysis(path, result, s.model, sig, clear_overrides=force)


class Runner:
    def __init__(self, store: Store):
        self.store = store
        self.state = JobState()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, paths: list[Path], cfg: Config, force: bool = False,
              on_progress: Callable[[JobState], None] | None = None) -> None:
        if self.busy:
            raise RuntimeError("A batch is already running")
        self._cancel.clear()
        self.state = JobState(status="running", total=len(paths), started_at=time.time())
        self._thread = threading.Thread(target=self._run, args=(paths, cfg, force, on_progress), daemon=True)
        self._thread.start()

    def wait(self) -> None:
        if self._thread:
            self._thread.join()

    def cancel(self) -> None:
        if self.busy:
            self._cancel.set()
            self.state.status = "cancelling"

    def take_changed(self) -> list[str]:
        with self._lock:
            out, self.state.changed = self.state.changed, []
        return out

    def _run(self, paths, cfg: Config, force: bool, on_progress) -> None:
        st = self.state
        sig = signature(cfg)
        client = Ollama(cfg.settings.ollama_host)
        analysed_time = 0.0
        analysed = 0
        try:
            for path in paths:
                if self._cancel.is_set():
                    st.status = "cancelled"
                    break
                st.current = path.name
                if not force and not self.store.needs_analysis(path, sig):
                    st.skipped += 1
                    continue
                t0 = time.time()
                try:
                    analyze_one(path, cfg, self.store, client, sig, force)
                    st.done += 1
                except LLMError as e:
                    if "Cannot reach" in str(e) or "not found" in str(e):
                        raise  # fatal for the whole batch
                    st.errors += 1
                    st.last_error = f"{path.name}: {e}"
                    self.store.save_error(path, str(e))
                except Exception as e:  # unreadable/corrupt image etc.
                    st.errors += 1
                    st.last_error = f"{path.name}: {e}"
                    self.store.save_error(path, f"{type(e).__name__}: {e}")
                analysed_time += time.time() - t0
                analysed += 1
                st.seconds_per_photo = analysed_time / analysed
                with self._lock:
                    st.changed.append(str(path))
                if on_progress:
                    on_progress(st)
            else:
                st.status = "done"
        except Exception as e:
            st.status = "failed"
            st.message = str(e)
        finally:
            st.current = None
            st.finished_at = time.time()
            if on_progress:
                on_progress(st)
