"""Cloud transcription via an OpenAI-compatible audio endpoint.

This module lets noScribe send the prepared audio to a cloud-hosted model
that exposes the OpenAI `/v1/audio/transcriptions` API (OpenAI itself, but
also compatible servers such as Groq, a self-hosted faster-whisper-server,
LiteLLM, vLLM, ...). It is configured entirely through environment variables
so that no API key ends up in the on-disk config:

    NOSCRIBE_OPENAI_BASE_URL   Base URL of the endpoint, e.g.
                               "https://api.openai.com/v1" or
                               "http://localhost:8000/v1". Setting this
                               (or OPENAI_BASE_URL) is what switches noScribe
                               from the bundled local model to the cloud model.
    NOSCRIBE_OPENAI_API_KEY    Bearer token sent in the Authorization header.
    NOSCRIBE_OPENAI_MODEL      Model name to request (default "whisper-1").

For each variable a conventional fallback (OPENAI_BASE_URL, OPENAI_API_KEY,
OPENAI_MODEL) is also accepted.

The endpoint is asked for `verbose_json` so that we get per-segment (and, when
supported, per-word) timestamps. Segments are returned in the very same dict
shape that ``whisper_mp_worker`` streams, so the rest of the pipeline does not
need to know which backend produced them.
"""

import array
import json
import os
import tempfile
import time
import uuid
import wave
import urllib.error
import urllib.request
from pathlib import Path

# Environment variable names (app specific) and their conventional fallbacks.
ENV_BASE_URL = "NOSCRIBE_OPENAI_BASE_URL"
ENV_API_KEY = "NOSCRIBE_OPENAI_API_KEY"
ENV_MODEL = "NOSCRIBE_OPENAI_MODEL"
ENV_CHUNK_SECONDS = "NOSCRIBE_OPENAI_CHUNK_SECONDS"

_BASE_URL_FALLBACKS = ("OPENAI_BASE_URL", "OPENAI_API_BASE")
_API_KEY_FALLBACKS = ("OPENAI_API_KEY",)
_MODEL_FALLBACKS = ("OPENAI_MODEL",)

DEFAULT_MODEL = "whisper-1"

# Long recordings are split into chunks so a single request does not exceed the
# endpoint's (or its proxy's) time/size limits, which otherwise shows up as a
# 502/504. Each chunk's timestamps are offset back to absolute time.
#
# Kept deliberately small: many hosted proxies (e.g. Blablador) put a short
# gateway timeout in front of the upstream model, so a chunk that takes too long
# to transcribe comes back as a 502 "invalid response from upstream" on every
# retry. Roughly two minutes of 16 kHz mono audio transcribes well within a
# typical 60 s proxy window. Override with NOSCRIBE_OPENAI_CHUNK_SECONDS if your
# endpoint tolerates longer requests (fewer round-trips), or lower it further
# if 502s persist.
DEFAULT_CHUNK_SECONDS = 120


def _first_env(primary, fallbacks):
    """Return the first non-empty value among the given environment variables."""
    for name in (primary, *fallbacks):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def get_endpoint_config():
    """Return the cloud endpoint configuration or ``None``.

    Cloud transcription is considered *enabled* as soon as a base URL is
    configured (this is the explicit opt-in the user controls through the
    environment). When enabled, an API key is required and a missing key is
    reported as an error so the problem is visible instead of silently falling
    back to the local model.
    """
    base_url = _first_env(ENV_BASE_URL, _BASE_URL_FALLBACKS)
    if not base_url:
        return None

    api_key = _first_env(ENV_API_KEY, _API_KEY_FALLBACKS)
    if not api_key:
        raise ValueError(
            f"{ENV_BASE_URL} is set but no API key was found. "
            f"Set {ENV_API_KEY} (or OPENAI_API_KEY) to the Bearer token "
            f"for the endpoint."
        )

    model = _first_env(ENV_MODEL, _MODEL_FALLBACKS) or DEFAULT_MODEL

    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model,
    }


def _encode_multipart(text_fields, repeated_fields, file_path):
    """Build a ``multipart/form-data`` body using only the standard library.

    ``text_fields`` is a dict of single-valued fields, ``repeated_fields`` a
    list of ``(name, value)`` tuples for fields that may appear more than once
    (e.g. ``timestamp_granularities[]``). ``file_path`` is uploaded as ``file``.
    Returns ``(body_bytes, content_type_header)``.
    """
    boundary = f"----noScribe{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts = []

    def add_field(name, value):
        parts.append(b"--" + boundary.encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
        )
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))

    for name, value in text_fields.items():
        add_field(name, value)
    for name, value in repeated_fields:
        add_field(name, value)

    file_path = Path(file_path)
    with open(file_path, "rb") as f:
        file_data = f.read()

    parts.append(b"--" + boundary.encode("utf-8"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{file_path.name}"'
        ).encode("utf-8")
    )
    parts.append(b"Content-Type: application/octet-stream")
    parts.append(b"")

    body = crlf.join(parts) + crlf + file_data + crlf
    body += b"--" + boundary.encode("utf-8") + b"--" + crlf

    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def _parse_response(data, fallback_duration=None):
    """Convert an OpenAI ``verbose_json`` response into noScribe segments."""
    info = {
        "language": data.get("language"),
        "duration": data.get("duration", fallback_duration),
    }

    segments = []
    raw_segments = data.get("segments")
    if raw_segments:
        for s in raw_segments:
            seg = {
                "start": s.get("start"),
                "end": s.get("end"),
                "text": s.get("text"),
            }
            words = s.get("words")
            if words:
                seg["words"] = [
                    {
                        "word": w.get("word"),
                        "start": w.get("start"),
                        "end": w.get("end"),
                        "prob": w.get("probability"),
                    }
                    for w in words
                ]
            segments.append(seg)
    else:
        # Some endpoints/models only return the plain text. Emit a single
        # segment spanning the whole recording so the transcript is not lost.
        segments.append(
            {
                "start": 0.0,
                "end": info.get("duration") or fallback_duration or 0.0,
                "text": data.get("text", ""),
            }
        )

    return segments, info


# HTTP statuses that are typically transient (proxy/upstream hiccups). These
# come and go, so retrying after a short pause usually succeeds.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def transcribe_file(audio_path, cfg, language=None, prompt=None,
                    fallback_duration=None, timeout=900,
                    retries=4, retry_backoff=3, log=None):
    """Transcribe ``audio_path`` through the configured cloud endpoint.

    Transient errors (network blips and proxy/upstream statuses such as 502
    Bad Gateway) are retried up to ``retries`` times with a growing backoff.

    Returns ``(segments, info)`` where ``segments`` is a list of dicts in the
    same shape used by the local whisper worker.
    """
    url = cfg["base_url"] + "/audio/transcriptions"

    text_fields = {
        "model": cfg["model"],
        "response_format": "verbose_json",
    }
    if language:
        text_fields["language"] = language
    if prompt:
        text_fields["prompt"] = prompt

    # Ask for both granularities; servers that ignore them still return segments.
    repeated_fields = [
        ("timestamp_granularities[]", "segment"),
        ("timestamp_granularities[]", "word"),
    ]

    body, content_type = _encode_multipart(text_fields, repeated_fields, audio_path)

    attempt = 0
    while True:
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Authorization", f"Bearer {cfg['api_key']}")
        request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in _RETRYABLE_STATUS and attempt < retries:
                attempt += 1
                wait = retry_backoff * attempt
                if log:
                    log(f"Endpoint returned HTTP {e.code}; retrying in {wait}s "
                        f"(attempt {attempt}/{retries})...")
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"Transcription endpoint returned HTTP {e.code} {e.reason}. {detail}".strip()
            ) from e
        except urllib.error.URLError as e:
            if attempt < retries:
                attempt += 1
                wait = retry_backoff * attempt
                if log:
                    log(f"Could not reach endpoint ({e.reason}); retrying in {wait}s "
                        f"(attempt {attempt}/{retries})...")
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"Could not reach transcription endpoint at {url}: {e.reason}"
            ) from e

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(
            "Transcription endpoint returned a response that is not valid JSON."
        ) from e

    return _parse_response(data, fallback_duration=fallback_duration)


def _resolve_chunk_seconds():
    try:
        val = int(os.environ.get(ENV_CHUNK_SECONDS, "").strip())
        return val if val > 0 else DEFAULT_CHUNK_SECONDS
    except (ValueError, AttributeError):
        return DEFAULT_CHUNK_SECONDS


def _find_quiet_cut(samples, target, search, win):
    """Return a frame index near ``target`` with minimal energy (a likely
    silence), so chunk boundaries fall between words rather than mid-word."""
    n = len(samples)
    lo = max(win, target - search)
    hi = min(n - win, target + search)
    if hi <= lo:
        return min(max(target, 0), n)
    best_i, best_e = target, None
    i = lo
    while i < hi:
        seg = samples[i:i + win]
        e = sum(s * s for s in seg)
        if best_e is None or e < best_e:
            best_e, best_i = e, i
        i += win
    return best_i


# Raw 16 kHz mono PCM is ~1.9 MB/min, which trips proxy body-size limits (a
# 413 from e.g. openresty/nginx, whose default cap is 1 MB) on even a couple of
# minutes. Compressing each chunk to Opus/Vorbis (or MP3) shrinks it ~15-30x and
# uploads faster. Both ``ogg`` and ``mp3`` are in the OpenAI-accepted upload
# formats, so the endpoint decodes them transparently.

def _compress_with_soundfile(wav_path):
    """Compress a WAV to Ogg/Opus (or Ogg/Vorbis) via soundfile. Returns the
    output path or ``None`` if soundfile/the codecs are unavailable."""
    try:
        import soundfile as sf
    except Exception:
        return None
    try:
        data, rate = sf.read(wav_path, dtype="int16")
    except Exception:
        return None
    for subtype in ("OPUS", "VORBIS"):
        out = os.path.join(tempfile.gettempdir(),
                           f"noscribe_chunk_{uuid.uuid4().hex}.ogg")
        try:
            sf.write(out, data, rate, format="OGG", subtype=subtype)
            return out
        except Exception:
            try:
                os.remove(out)
            except OSError:
                pass
    return None


def _compress_with_av(wav_path, bitrate=48000):
    """Transcode a WAV to a small mono-ish MP3 via PyAV. Returns the output
    path or ``None`` if PyAV is unavailable or encoding fails."""
    try:
        import av
    except Exception:
        return None
    out = os.path.join(tempfile.gettempdir(),
                       f"noscribe_chunk_{uuid.uuid4().hex}.mp3")
    in_c = out_c = None
    try:
        in_c = av.open(wav_path)
        in_s = in_c.streams.audio[0]
        out_c = av.open(out, "w", format="mp3")
        out_s = out_c.add_stream("mp3", rate=in_s.codec_context.rate)
        try:
            out_s.bit_rate = bitrate
        except Exception:
            pass
        resampler = av.AudioResampler(
            format=out_s.format, layout=out_s.layout, rate=out_s.rate)

        def _emit(frames):
            if frames is None:
                return
            if not isinstance(frames, (list, tuple)):
                frames = [frames]
            for fr in frames:
                if fr is not None:
                    for pkt in out_s.encode(fr):
                        out_c.mux(pkt)

        for frame in in_c.decode(in_s):
            frame.pts = None
            _emit(resampler.resample(frame))
        _emit(resampler.resample(None))
        for pkt in out_s.encode(None):
            out_c.mux(pkt)
        out_c.close()
        out_c = None
        return out
    except Exception:
        try:
            os.remove(out)
        except OSError:
            pass
        return None
    finally:
        for c in (out_c, in_c):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass


def _compress_audio(wav_path):
    """Return a path to a compressed copy of ``wav_path`` for upload, or
    ``None`` if no encoder is available (caller uploads the raw WAV)."""
    return _compress_with_soundfile(wav_path) or _compress_with_av(wav_path)


def _transcribe_wav_payload(wav_path, cfg, language, prompt,
                            fallback_duration, log):
    """Compress ``wav_path`` if possible, transcribe it, and clean up the
    compressed temp afterwards. Falls back to the raw WAV when no encoder is
    available."""
    upload = _compress_audio(wav_path)
    if upload and log:
        try:
            kb = os.path.getsize(upload) / 1024.0
            ext = os.path.splitext(upload)[1].lstrip(".") or "audio"
            log(f"  uploading {kb:.0f} KB ({ext})")
        except OSError:
            pass
    try:
        return transcribe_file(upload or wav_path, cfg, language=language,
                               prompt=prompt, fallback_duration=fallback_duration,
                               log=log)
    finally:
        if upload:
            try:
                os.remove(upload)
            except OSError:
                pass


def transcribe_audio(audio_path, cfg, language=None, prompt=None,
                     fallback_duration=None, chunk_seconds=None, log=None):
    """Transcribe ``audio_path``, splitting long WAV input into chunks.

    For audio longer than ``chunk_seconds`` the (16 kHz mono) WAV is cut on
    near-silence boundaries; each chunk is transcribed separately and its
    timestamps are offset back to absolute time. Short audio, or non-WAV input,
    is sent in a single request.

    Returns ``(segments, info)``.
    """
    if chunk_seconds is None:
        chunk_seconds = _resolve_chunk_seconds()

    try:
        with wave.open(audio_path, "rb") as w:
            nch = w.getnchannels()
            sw = w.getsampwidth()
            rate = w.getframerate()
            nframes = w.getnframes()
            raw = w.readframes(nframes)
    except (wave.Error, EOFError, OSError):
        # Not a readable WAV — fall back to a single request.
        return transcribe_file(audio_path, cfg, language=language, prompt=prompt,
                               fallback_duration=fallback_duration, log=log)

    duration = nframes / float(rate) if rate else 0.0
    if duration <= chunk_seconds or nframes == 0:
        return _transcribe_wav_payload(audio_path, cfg, language, prompt,
                                       fallback_duration or duration, log)

    # Use 16-bit samples for the silence search when possible.
    samples = None
    if sw == 2:
        samples = array.array("h")
        samples.frombytes(raw)
        if nch > 1:  # take channel 0 only for the energy search
            samples = samples[0::nch]
        frames_total = len(samples)
    else:
        frames_total = nframes

    chunk_frames = int(chunk_seconds * rate)
    search = int(min(3.0, chunk_seconds / 4) * rate)
    win = max(1, int(0.05 * rate))

    # Compute cut points (in frames of the original audio).
    cuts = [0]
    pos = chunk_frames
    while pos < nframes:
        if samples is not None:
            cut = _find_quiet_cut(samples, pos, search, win)
        else:
            cut = pos
        if cut <= cuts[-1]:
            cut = min(cuts[-1] + chunk_frames, nframes)
        cuts.append(cut)
        pos = cut + chunk_frames
    cuts.append(nframes)
    # De-duplicate/clean monotonic boundaries.
    bounds = []
    for c in cuts:
        if not bounds or c > bounds[-1]:
            bounds.append(min(c, nframes))

    all_segments = []
    bytes_per_frame = nch * sw
    total = len(bounds) - 1
    for idx in range(total):
        a, b = bounds[idx], bounds[idx + 1]
        if b <= a:
            continue
        offset = a / float(rate)
        if log:
            log(f"Transcribing chunk {idx + 1}/{total} "
                f"({offset:.0f}s–{b / float(rate):.0f}s)...")
        tmp = os.path.join(tempfile.gettempdir(), f"noscribe_chunk_{uuid.uuid4().hex}.wav")
        try:
            with wave.open(tmp, "wb") as cw:
                cw.setnchannels(nch)
                cw.setsampwidth(sw)
                cw.setframerate(rate)
                cw.writeframes(raw[a * bytes_per_frame:b * bytes_per_frame])
            segs, _ = _transcribe_wav_payload(tmp, cfg, language, prompt,
                                              (b - a) / float(rate), log)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        for s in segs:
            if s.get("start") is not None:
                s["start"] = s["start"] + offset
            if s.get("end") is not None:
                s["end"] = s["end"] + offset
            for wd in s.get("words", []) or []:
                if wd.get("start") is not None:
                    wd["start"] = wd["start"] + offset
                if wd.get("end") is not None:
                    wd["end"] = wd["end"] + offset
            all_segments.append(s)

    return all_segments, {"language": language, "duration": duration}
