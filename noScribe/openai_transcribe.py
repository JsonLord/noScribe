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

import json
import os
import uuid
import urllib.error
import urllib.request
from pathlib import Path

# Environment variable names (app specific) and their conventional fallbacks.
ENV_BASE_URL = "NOSCRIBE_OPENAI_BASE_URL"
ENV_API_KEY = "NOSCRIBE_OPENAI_API_KEY"
ENV_MODEL = "NOSCRIBE_OPENAI_MODEL"

_BASE_URL_FALLBACKS = ("OPENAI_BASE_URL", "OPENAI_API_BASE")
_API_KEY_FALLBACKS = ("OPENAI_API_KEY",)
_MODEL_FALLBACKS = ("OPENAI_MODEL",)

DEFAULT_MODEL = "whisper-1"


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


def transcribe_file(audio_path, cfg, language=None, prompt=None,
                    fallback_duration=None, timeout=900):
    """Transcribe ``audio_path`` through the configured cloud endpoint.

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

    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {cfg['api_key']}")
    request.add_header("Content-Type", content_type)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError(
            f"Transcription endpoint returned HTTP {e.code} {e.reason}. {detail}".strip()
        ) from e
    except urllib.error.URLError as e:
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
