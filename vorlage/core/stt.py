"""
Speech-to-Text engines for MARK XL.

Whisper  – offline transcription via faster-whisper (VAD-buffered)
Vosk     – offline streaming transcription (lighter)
"""
import json
import numpy as np


def _enable_hf_online() -> None:
    """Re-enable HuggingFace network access at runtime.

    main.py sets HF_HUB_OFFLINE=1 *before* huggingface_hub is imported, so the
    library computes its module-level `constants.HF_HUB_OFFLINE = True` once at
    import time.  Simply popping the env var afterwards is too late — the cached
    constant still reads True and every download raises OfflineModeIsEnabled.
    We must overwrite the already-computed constant as well as the env vars so
    the one-time model download can proceed.
    """
    import os
    os.environ["HF_HUB_OFFLINE"]       = "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "0"
    os.environ.pop("HF_DATASETS_OFFLINE", None)
    try:
        import huggingface_hub.constants as _hf_const
        _hf_const.HF_HUB_OFFLINE = False
    except Exception:
        pass


def _add_cuda_dll_dirs() -> None:
    """Make pip-installed NVIDIA CUDA libraries loadable by CTranslate2 on Windows.

    `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` drops the DLLs under
    site-packages/nvidia/<lib>/bin.  CTranslate2's native loader searches the
    PATH environment variable (not the os.add_dll_directory user dirs), so we
    must prepend those bin folders to PATH — that is what actually lets it find
    cublas64_12.dll / cudnn64_9.dll.  We also call os.add_dll_directory for good
    measure.  No-op on non-Windows or if the packages aren't installed.
    """
    import os
    import sys
    if not hasattr(os, "add_dll_directory"):       # non-Windows
        return
    bin_dirs: list[str] = []
    for base in sys.path:
        nvidia_dir = os.path.join(base, "nvidia")
        if not os.path.isdir(nvidia_dir):
            continue
        for lib in os.listdir(nvidia_dir):
            bin_dir = os.path.join(nvidia_dir, lib, "bin")
            if os.path.isdir(bin_dir):
                bin_dirs.append(bin_dir)
                try:
                    os.add_dll_directory(bin_dir)
                except (OSError, FileNotFoundError):
                    pass
    if bin_dirs:
        # Prepend to PATH (deduplicated) — this is the search path CTranslate2 uses.
        existing = os.environ.get("PATH", "")
        new = [d for d in bin_dirs if d not in existing]
        if new:
            os.environ["PATH"] = os.pathsep.join(new) + os.pathsep + existing


def _is_cuda_lib_error(err: Exception) -> bool:
    """True if *err* looks like a missing/unloadable CUDA runtime library."""
    msg = str(err).lower()
    markers = (
        "cublas", "cudnn", "cuda", "cublas64", "cudnn64",
        "is not found or cannot be loaded", ".dll", "libcudnn", "libcublas",
    )
    return any(m in msg for m in markers)


class WhisperSTT:
    """Offline transcription using faster-whisper."""

    def __init__(self, model_name: str = "base", language: str | None = None):
        from faster_whisper import WhisperModel
        print(f"[STT] Loading Whisper '{model_name}'…")
        try:
            import torch
            device  = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
        except Exception:
            device, compute = "cpu", "int8"

        if device == "cuda":
            _add_cuda_dll_dirs()   # let CTranslate2 find pip-installed CUDA DLLs (Windows)

        def _build(dev: str, comp: str):
            try:
                return WhisperModel(model_name, device=dev, compute_type=comp)
            except Exception as _first_err:
                # Offline flag set but model not cached yet → clear flags and download once.
                # Keywords cover multiple huggingface_hub error message variants across versions.
                _e = str(_first_err).lower()
                _offline_keywords = (
                    "offline", "not found", "cache", "localentry",
                    "does not exist", "outgoing", "local_files_only",
                )
                if any(k in _e for k in _offline_keywords):
                    print(f"[STT] Whisper '{model_name}' not in local cache — downloading (one-time, internet required)…")
                    _enable_hf_online()
                    try:
                        return WhisperModel(model_name, device=dev, compute_type=comp)
                    except Exception as _dl_err:
                        raise RuntimeError(
                            f"Whisper '{model_name}' model download failed.\n"
                            f"Internet access is required the first time to download the speech model (~75–290 MB).\n"
                            f"After the first download it runs fully offline.\n"
                            f"Details: {_dl_err}"
                        ) from _dl_err
                # Gated / access-restricted HuggingFace repo → needs login + approval.
                if any(k in _e for k in ("gated", "restricted", "401", "must be authenticated", "access to")):
                    raise RuntimeError(
                        f"Whisper model '{model_name}' is a gated HuggingFace repo (login required).\n"
                        f"Pick a freely accessible German model in Configure instead, e.g.:\n"
                        f"  jimmymeister/whisper-large-v3-turbo-german-ct2\n"
                        f"Details: {_first_err}"
                    ) from _first_err
                raise

        self._model = _build(device, compute)
        self._language = None if (not language or language.strip().lower() == "auto") else language.strip().lower()

        # CTranslate2 loads the CUDA libraries (cuBLAS / cuDNN) lazily on the
        # first inference, not at model creation — so a missing cublas64_12.dll
        # only surfaces when the user first speaks.  Force the load now via a
        # silent warmup; if the GPU libraries are missing, fall back to CPU so
        # speech recognition still works (just slower) instead of crashing later.
        if device == "cuda":
            try:
                self._warmup()
            except Exception as e:
                if _is_cuda_lib_error(e):
                    print(
                        "[STT] CUDA libraries (cuBLAS/cuDNN) not found — falling back to CPU.\n"
                        "      For GPU speed install them once:\n"
                        "        pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
                    )
                    device, compute = "cpu", "int8"
                    self._model = _build(device, compute)
                else:
                    raise

        print(f"[STT] Whisper '{model_name}' ready ({device})")

    def _warmup(self) -> None:
        """Run one tiny inference to force CTranslate2 to load its CUDA libs."""
        silence = np.zeros(16000, dtype=np.float32)   # 1 s of silence @ 16 kHz
        segments, _ = self._model.transcribe(silence, language=self._language, beam_size=1)
        for _ in segments:                            # generator is lazy — drain it
            pass

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz numpy array. Returns transcript string."""
        try:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1,                       # greedy — 2-3x faster
                best_of=1,
                condition_on_previous_text=False,  # no hallucinations, faster
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            return " ".join(s.text for s in segments).strip()
        except Exception as e:
            print(f"[STT] Transcription error: {e}")
            raise


class RemoteWhisperSTT:
    """Transcription via a remote OpenAI-compatible STT server.

    Sends the VAD-buffered audio to ``{base_url}/v1/audio/transcriptions`` and
    returns the transcript — analogous to ``OpenAISpeechTTSEngine`` for TTS.
    This is what moves the (GPU-heavy) Whisper inference off the local machine
    onto the SH-Mark-XL inference tier (see deploy/gpu/docker-compose.gpu.yml,
    service ``stt``).  No torch/CUDA is needed on the client.

    Exposes the same ``transcribe(np.ndarray) -> str`` interface as WhisperSTT,
    so the existing ``_listen_whisper`` loop drives it unchanged.
    """

    def __init__(
        self,
        base_url: str,
        model:    str = "Systran/faster-whisper-large-v3",
        language: str | None = None,
        api_key:  str = "",
        timeout:  int = 60,
    ):
        self.base_url = (base_url or "http://localhost:8001").rstrip("/")
        self.model    = model or "Systran/faster-whisper-large-v3"
        self.language = None if (not language or language.strip().lower() == "auto") else language.strip().lower()
        self.api_key  = api_key or ""
        self.timeout  = timeout
        print(f"[STT] Remote Whisper → {self.base_url} (model='{self.model}', lang='{self.language or 'auto'}')")

    def transcribe(self, audio: np.ndarray) -> str:
        """Encode a float32 mono 16 kHz array as WAV and POST it for transcription."""
        import io
        import requests
        import soundfile as sf

        buf = io.BytesIO()
        sf.write(buf, audio, 16000, format="WAV", subtype="PCM_16")
        buf.seek(0)

        files = {"file": ("audio.wav", buf, "audio/wav")}
        data  = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

        try:
            resp = requests.post(
                f"{self.base_url}/v1/audio/transcriptions",
                files=files, data=data, headers=headers, timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"STT server not reachable at {self.base_url}.\n"
                "Is the GPU inference tier running?  On the GPU server:\n"
                "    docker compose -f deploy/gpu/docker-compose.gpu.yml up -d\n"
                "Or set 'stt_engine' back to 'whisper' in Configure to run locally.\n"
                f"Details: {e}"
            ) from e

        try:
            return (resp.json().get("text") or "").strip()
        except ValueError:
            return resp.text.strip()


class VoskSTT:
    """Streaming transcription using Vosk."""

    def __init__(self, model_path: str | None = None, language: str = "en-us"):
        from vosk import Model, KaldiRecognizer
        print("[STT] Loading Vosk model…")
        if model_path:
            model = Model(model_path)
        else:
            lang  = language.strip().lower() if language and language.strip().lower() != "auto" else "en-us"
            model = Model(lang=lang)
        self._rec = KaldiRecognizer(model, 16000)
        print("[STT] Vosk ready.")

    def process_chunk(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Feed raw int16 LE PCM bytes. Returns (text, is_final)."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False
