#!/usr/bin/env python3
"""Diagnose an OpenAI-compatible transcription endpoint for noScribe.

Generates a short valid WAV in pure Python (no ffmpeg needed) and POSTs it to
the configured endpoint the same way noScribe does, then tries response-format
fallbacks. Prints exactly what works so we know how to configure / fix things.

    set -a; . ./.env; set +a        # load NOSCRIBE_OPENAI_* into the env
    python3 diagnose_cloud.py
"""

import json
import math
import os
import struct
import tempfile
import urllib.error
import urllib.request
import uuid
import wave


def env(*names):
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


def make_wav(path, seconds=3, rate=16000, freq=440):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            val = int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", val)
        w.writeframes(bytes(frames))


def encode_multipart(text_fields, repeated_fields, file_path):
    boundary = "----diag" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts = []

    def add(name, value):
        parts.append(b"--" + boundary.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode())

    for k, v in text_fields.items():
        add(k, v)
    for k, v in repeated_fields:
        add(k, v)
    with open(file_path, "rb") as f:
        data = f.read()
    parts.append(b"--" + boundary.encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(file_path)}"'.encode())
    parts.append(b"Content-Type: application/octet-stream")
    parts.append(b"")
    body = crlf.join(parts) + crlf + data + crlf + b"--" + boundary.encode() + b"--" + crlf
    return body, f"multipart/form-data; boundary={boundary}"


def post(url, api_key, fields, repeated, wav):
    body, ctype = encode_multipart(fields, repeated, wav)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def summarize(status, body):
    print(f"  HTTP {status}")
    snippet = body[:500].replace("\n", " ")
    print(f"  body: {snippet}")
    seg = words = False
    if status == 200:
        try:
            d = json.loads(body)
            seg = bool(d.get("segments"))
            words = bool(d.get("words")) or (d.get("segments") and "words" in d["segments"][0])
            print(f"  -> parsed JSON. text={'yes' if d.get('text') is not None else 'no'}, "
                  f"segments={'yes' if seg else 'no'}, words={'yes' if words else 'no'}")
        except Exception:
            print("  -> 200 but body is not JSON (plain text response?)")
    return status == 200, seg


def main():
    base = env("NOSCRIBE_OPENAI_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
    key = env("NOSCRIBE_OPENAI_API_KEY", "OPENAI_API_KEY")
    model = env("NOSCRIBE_OPENAI_MODEL", "OPENAI_MODEL") or "whisper-1"
    if not base or not key:
        print("Set NOSCRIBE_OPENAI_BASE_URL and NOSCRIBE_OPENAI_API_KEY first "
              "(e.g. `set -a; . ./.env; set +a`).")
        return 1
    url = base.rstrip("/") + "/audio/transcriptions"
    print(f"Endpoint: {url}\nModel:    {model}\n")

    wav = os.path.join(tempfile.mkdtemp(), "tone.wav")
    make_wav(wav)
    print(f"Test file: {wav} ({os.path.getsize(wav)} bytes, 3s 16kHz mono)\n")

    print("[1] verbose_json + segment&word timestamps + language=de  (what noScribe sends):")
    ok_v, seg_v = summarize(*post(url, key,
        {"model": model, "response_format": "verbose_json", "language": "de"},
        [("timestamp_granularities[]", "segment"), ("timestamp_granularities[]", "word")], wav))

    print("\n[2] response_format=json:")
    ok_j, _ = summarize(*post(url, key,
        {"model": model, "response_format": "json", "language": "de"}, [], wav))

    print("\n[3] response_format=text:")
    ok_t, _ = summarize(*post(url, key,
        {"model": model, "response_format": "text", "language": "de"}, [], wav))

    print("\n==== verdict ====")
    if ok_v and seg_v:
        print("verbose_json with segments WORKS — noScribe should transcribe fine. "
              "The earlier failure was likely the file (size/duration). Check the file log.")
    elif ok_v and not seg_v:
        print("verbose_json returns 200 but NO segments — noScribe will fall back to a "
              "single block. I should relax parsing to accept that.")
    elif ok_j or ok_t:
        print("verbose_json FAILS but json/text works — I should add a response_format "
              "fallback (verbose_json -> json -> text) in openai_transcribe.py.")
    else:
        print("All formats failed — see the HTTP codes/bodies above (auth, model id, "
              "or the route doesn't accept these params).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
