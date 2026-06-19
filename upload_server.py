#!/usr/bin/env python3
"""Simple browser workflow for noScribe: upload, transcribe, download.

This is the lightweight alternative to the full noVNC desktop (see webgui.sh).
It serves a small page where you can:

  * drag-and-drop audio files onto the server's working folder,
  * pick a language and press "Transcribe" to run the noScribe CLI headlessly
    (no GUI / no noVNC), and
  * download the finished transcript.

Transcription shells out to `python -m noScribe <audio> <out> --no-gui ...`,
inheriting this process's environment — so when launched with the cloud
credentials loaded (see serve.sh), it transcribes via your OpenAI-compatible
endpoint. Standard library only; intended to be reached over your tailnet.

    python3 upload_server.py --dir ~/transcribe/data --bind 100.x.y.z --transcribe
"""

import argparse
import html
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

WORK_DIR = os.path.expanduser("~/transcribe/data")
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB cap per upload request

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
              ".wma", ".aiff", ".aif", ".mp4", ".mov", ".mkv", ".avi", ".webm",
              ".m4v", ".3gp", ".amr"}
TRANSCRIPT_EXTS = {".html", ".htm", ".txt", ".vtt"}

# (label, code). Empty code = auto-detect (no --language passed).
LANGUAGES = [
    ("Auto-detect", ""), ("English", "en"), ("German", "de"), ("French", "fr"),
    ("Spanish", "es"), ("Italian", "it"), ("Dutch", "nl"), ("Portuguese", "pt"),
    ("Russian", "ru"), ("Polish", "pl"), ("Czech", "cs"), ("Danish", "da"),
    ("Swedish", "sv"), ("Norwegian", "no"), ("Finnish", "fi"), ("Greek", "el"),
    ("Turkish", "tr"), ("Arabic", "ar"), ("Hebrew", "he"), ("Hindi", "hi"),
    ("Japanese", "ja"), ("Korean", "ko"), ("Chinese", "zh"), ("Ukrainian", "uk"),
    ("Romanian", "ro"), ("Hungarian", "hu"), ("Catalan", "ca"),
]
LANG_CODES = {code for _, code in LANGUAGES if code}

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>noScribe</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 820px; margin: 2rem auto;
         padding: 0 1rem; color: #e7e9ee; background: #1b1d23; }
  h1 { font-size: 1.3rem; } h2 { font-size: 1rem; }
  #drop { border: 2px dashed #4b5161; border-radius: 12px; padding: 2rem 1rem;
          text-align: center; color: #aab; transition: .15s; cursor: pointer; }
  #drop.hover { border-color: #6ea8fe; color: #cfe0ff; background: #232634; }
  .bar { height: 8px; background: #2a2e3a; border-radius: 6px; overflow: hidden;
         margin-top: 1rem; display: none; }
  .bar > div { height: 100%; width: 0; background: #6ea8fe; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  th, td { text-align: left; padding: .45rem .5rem; border-bottom: 1px solid #2a2e3a;
           vertical-align: middle; }
  a { color: #6ea8fe; }
  .muted { color: #8b91a1; font-size: .9rem; }
  select, button { font: inherit; padding: .35rem .5rem; border-radius: 8px;
                   border: 1px solid #3a3f4d; background: #232634; color: #e7e9ee; }
  button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
  .toolbar { display: flex; gap: .6rem; align-items: center; margin: 1rem 0; flex-wrap: wrap; }
  .st-running { color: #ffd479; } .st-done { color: #7ddc8a; } .st-error { color: #ff8a8a; }
</style>
</head>
<body>
  <h1>noScribe</h1>
  <p class="muted">Working folder: <code>__WORKDIR__</code></p>

  <div id="drop">Drop audio files here, or click to choose</div>
  <input id="file" type="file" multiple hidden>
  <div class="bar"><div id="prog"></div></div>

  <div class="toolbar" __TOOLBAR_HIDDEN__>
    <label for="lang">Language</label>
    <select id="lang">__LANGS__</select>
    <label><input type="checkbox" id="spk" checked> Identify speakers</label>
    <span class="muted">applied when you press Transcribe</span>
  </div>
  <p id="status" class="muted"></p>

  <h2>Files</h2>
  <table>
    <thead><tr><th>Name</th><th>Size</th><th>Action</th></tr></thead>
    <tbody>__ROWS__</tbody>
  </table>

<script>
const TRANSCRIBE = __TRANSCRIBE_JS__;
const drop = document.getElementById('drop');
const file = document.getElementById('file');
const bar  = document.querySelector('.bar');
const prog = document.getElementById('prog');
const status = document.getElementById('status');

drop.addEventListener('click', () => file.click());
file.addEventListener('change', () => upload(file.files));
['dragenter','dragover'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.add('hover'); }));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.remove('hover'); }));
drop.addEventListener('drop', ev => upload(ev.dataTransfer.files));

function upload(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files[]', f, f.name);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', 'upload', true);
  bar.style.display = 'block';
  xhr.upload.onprogress = e => {
    if (e.lengthComputable) prog.style.width = (e.loaded/e.total*100) + '%'; };
  xhr.onload = () => {
    status.textContent = xhr.status === 200 ? 'Uploaded. Reloading…'
                                            : ('Upload failed: ' + xhr.status);
    if (xhr.status === 200) setTimeout(() => location.reload(), 500); };
  xhr.onerror = () => status.textContent = 'Upload failed (network error).';
  status.textContent = 'Uploading ' + files.length + ' file(s)…';
  xhr.send(fd);
}

function transcribe(name, btn) {
  const lang = document.getElementById('lang').value;
  const spk = document.getElementById('spk').checked ? 'auto' : 'none';
  btn.disabled = true;
  const cell = document.getElementById('st-' + cssid(name));
  if (cell) { cell.textContent = 'queued…'; cell.className = 'st-running'; }
  const body = 'file=' + encodeURIComponent(name) + '&language=' + encodeURIComponent(lang)
             + '&speaker=' + encodeURIComponent(spk);
  fetch('transcribe', { method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'}, body })
    .then(r => r.json())
    .then(j => { if (!j.ok) { status.textContent = 'Error: ' + (j.error||'failed'); btn.disabled = false; }
                 else poll(); })
    .catch(() => { status.textContent = 'Network error.'; btn.disabled = false; });
}

function cssid(s){ return s.replace(/[^a-zA-Z0-9_-]/g,'_'); }

let polling = false;
function poll() {
  if (polling) return; polling = true;
  const tick = () => fetch('status').then(r => r.json()).then(j => {
    let anyRunning = false, anyDone = false;
    (j.jobs||[]).forEach(job => {
      const cell = document.getElementById('st-' + cssid(job.audio));
      if (cell) {
        if (job.status === 'running' || job.status === 'queued') {
          cell.textContent = 'transcribing…'; cell.className = 'st-running'; anyRunning = true;
        } else if (job.status === 'done') { cell.textContent = 'done'; cell.className = 'st-done'; anyDone = true;
        } else if (job.status === 'error') { cell.textContent = 'error'; cell.className = 'st-error';
          status.textContent = (job.error||'Transcription failed'); }
      }
    });
    if (anyRunning) setTimeout(tick, 1500);
    else { polling = false; if (anyDone) setTimeout(() => location.reload(), 700); }
  }).catch(() => { polling = false; });
  tick();
}
// Resume polling if a job is already running when the page loads.
if (TRANSCRIBE) poll();
</script>
</body>
</html>
"""


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def safe_name(name):
    name = name.replace("\\", "/").split("/")[-1]
    name = name.strip().lstrip(".") or "file"
    return re.sub(r'[\x00-\x1f"]+', "_", name)


def unique_path(directory, name):
    base = safe_name(name)
    candidate = os.path.join(directory, base)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(base)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{i}{ext}")
        i += 1
    return candidate


def parse_multipart(body, boundary):
    delim = b"--" + boundary
    for part in body.split(delim):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        m = re.search(r'filename="([^"]*)"', header_blob.decode("utf-8", "replace"))
        if m and m.group(1):
            yield m.group(1), data


class Transcriber:
    """Runs the noScribe CLI in background threads and tracks job state."""

    def __init__(self, work_dir, model=None, speaker_detection="auto"):
        self.work_dir = work_dir
        self.model = model
        self.speaker_detection = speaker_detection  # default when none requested
        self.jobs = {}          # audio_name -> dict(status, output, error)
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return [dict(audio=a, **v) for a, v in self.jobs.items()]

    def start(self, audio_name, language_code, speaker_detection=None):
        audio_name = safe_name(audio_name)
        audio_path = os.path.join(self.work_dir, audio_name)
        if not os.path.isfile(audio_path):
            return False, "File not found"
        if language_code and language_code not in LANG_CODES:
            return False, "Unknown language"
        speaker = speaker_detection or self.speaker_detection
        with self.lock:
            cur = self.jobs.get(audio_name)
            if cur and cur["status"] in ("queued", "running"):
                return True, "already running"
            stem = os.path.splitext(audio_name)[0]
            out_name = os.path.basename(unique_path(self.work_dir, stem + ".html"))
            self.jobs[audio_name] = {"status": "queued", "output": out_name, "error": ""}
        out_path = os.path.join(self.work_dir, out_name)
        threading.Thread(target=self._run,
                         args=(audio_name, audio_path, out_path, out_name, language_code, speaker),
                         daemon=True).start()
        return True, "started"

    def _run(self, audio_name, audio_path, out_path, out_name, language_code, speaker):
        cmd = [sys.executable, "-m", "noScribe", audio_path, out_path,
               "--no-gui", "--speaker-detection", speaker]
        if language_code:
            cmd += ["--language", language_code]
        if self.model:
            cmd += ["--model", self.model]
        with self.lock:
            self.jobs[audio_name]["status"] = "running"
        try:
            proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True,
                                  text=True, timeout=60 * 60 * 6)
            ok = proc.returncode == 0 and os.path.isfile(out_path)
            err = ""
            if not ok:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                err = " ".join(tail[-3:]) if tail else f"exit code {proc.returncode}"
        except Exception as e:
            ok, err = False, str(e)
        with self.lock:
            self.jobs[audio_name]["status"] = "done" if ok else "error"
            self.jobs[audio_name]["error"] = "" if ok else err


class Handler(BaseHTTPRequestHandler):
    work_dir = WORK_DIR
    transcriber = None  # set in main() when --transcribe

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), ctype="application/json")

    # ---- routing -------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if "/dl/" in path:
            return self._download(path.split("/dl/", 1)[1])
        if path.endswith("/status"):
            return self._json(200, {"jobs": self.transcriber.snapshot() if self.transcriber else []})
        return self._page()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/transcribe"):
            return self._transcribe()
        return self._upload()

    # ---- handlers ------------------------------------------------------
    def _upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._send(400, "Expected multipart/form-data")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self._send(400, "Missing multipart boundary")
        boundary = m.group(1).strip().strip('"').encode("utf-8")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(400, "Empty body")
        if length > MAX_BYTES:
            return self._send(413, "Upload too large")
        body = self.rfile.read(length)
        os.makedirs(self.work_dir, exist_ok=True)
        saved = []
        for filename, data in parse_multipart(body, boundary):
            dest = unique_path(self.work_dir, filename)
            with open(dest, "wb") as f:
                f.write(data)
            saved.append(os.path.basename(dest))
        if not saved:
            return self._send(400, "No files found in request")
        return self._send(200, "Saved: " + ", ".join(saved),
                          ctype="text/plain; charset=utf-8")

    def _transcribe(self):
        if not self.transcriber:
            return self._json(400, {"ok": False, "error": "Transcription is disabled"})
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        form = urllib.parse.parse_qs(data)
        name = (form.get("file") or [""])[0]
        lang = (form.get("language") or [""])[0]
        spk = (form.get("speaker") or [""])[0]
        speaker = "auto" if spk == "auto" else ("none" if spk == "none" else None)
        if not name:
            return self._json(400, {"ok": False, "error": "No file specified"})
        ok, msg = self.transcriber.start(name, lang, speaker_detection=speaker)
        return self._json(200 if ok else 400, {"ok": ok, "error": "" if ok else msg})

    def _page(self):
        os.makedirs(self.work_dir, exist_ok=True)
        try:
            entries = sorted(os.listdir(self.work_dir))
        except OSError:
            entries = []
        enabled = self.transcriber is not None
        rows = []
        for name in entries:
            full = os.path.join(self.work_dir, name)
            if not os.path.isfile(full):
                continue
            ext = os.path.splitext(name)[1].lower()
            size = human_size(os.path.getsize(full))
            esc = html.escape(name)
            cssid = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
            if ext in AUDIO_EXTS:
                if enabled:
                    btn = (f"<button onclick=\"transcribe('{esc}', this)\">Transcribe</button>"
                           f" <span id='st-{cssid}' class='muted'></span>")
                else:
                    btn = "<span class='muted'>audio</span>"
                action = btn
            elif ext in TRANSCRIPT_EXTS:
                action = f"<a href='dl/{urllib.parse.quote(name)}'>download</a>"
            else:
                action = f"<a href='dl/{urllib.parse.quote(name)}'>download</a>"
            rows.append(f"<tr><td>{esc}</td><td>{size}</td><td>{action}</td></tr>")
        if not rows:
            rows.append("<tr><td colspan='3' class='muted'>empty — upload an audio file</td></tr>")

        langs = "".join(f"<option value='{c}'>{html.escape(l)}</option>" for l, c in LANGUAGES)
        page = (PAGE
                .replace("__WORKDIR__", html.escape(self.work_dir))
                .replace("__ROWS__", "".join(rows))
                .replace("__LANGS__", langs)
                .replace("__TOOLBAR_HIDDEN__", "" if enabled else "style='display:none'")
                .replace("__TRANSCRIBE_JS__", "true" if enabled else "false"))
        self._send(200, page)

    def _download(self, raw):
        name = safe_name(urllib.parse.unquote(raw))
        full = os.path.join(self.work_dir, name)
        if not os.path.isfile(full):
            return self._send(404, "Not found")
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype="application/octet-stream",
                   extra={"Content-Disposition": f'attachment; filename="{name}"'})


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="Upload / transcribe / download page for noScribe.")
    ap.add_argument("--dir", default=WORK_DIR, help="Shared working folder (default: ~/transcribe/data)")
    ap.add_argument("--bind", default="127.0.0.1", help="Address to bind (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=6080, help="Port (default: 6080)")
    ap.add_argument("--transcribe", action="store_true",
                    help="Enable the in-browser Transcribe button (runs the noScribe CLI)")
    ap.add_argument("--model", default=None, help="Pass a specific --model to the CLI (cloud mode ignores it)")
    ap.add_argument("--speaker-detection", default="auto",
                    help="Default speaker detection for CLI transcription (default: auto)")
    args = ap.parse_args()

    Handler.work_dir = os.path.abspath(os.path.expanduser(args.dir))
    os.makedirs(Handler.work_dir, exist_ok=True)
    if args.transcribe:
        Handler.transcriber = Transcriber(Handler.work_dir, model=args.model,
                                          speaker_detection=args.speaker_detection)

    httpd = ThreadingServer((args.bind, args.port), Handler)
    mode = "upload + transcribe" if args.transcribe else "upload/download only"
    print(f"noScribe web ({mode}): http://{args.bind}:{args.port}/  ->  {Handler.work_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
