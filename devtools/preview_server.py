"""Serve the real Kata dashboard bundle and API outside Hermes, for development and screenshots.

    python scripts/demo.py --db /tmp/kata-demo.db
    KATA_DB_PATH=/tmp/kata-demo.db python devtools/preview_server.py   # http://127.0.0.1:8765

The page provides a minimal ``window.__HERMES_PLUGIN_SDK__`` (React and fetchJSON), the
same globals the Hermes dashboard gives plugins. The API is the plugin's own router.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "kata" / "dashboard"

if not os.environ.get("KATA_DB_PATH"):
    sys.exit("set KATA_DB_PATH to a Kata database (run scripts/demo.py --db PATH first)")

spec = importlib.util.spec_from_file_location("kata_plugin_api", DASH / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

app = FastAPI()
app.include_router(api.router, prefix="/api/plugins/kata")

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kata dashboard preview</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Newsreader:opsz,wght@6..72,500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/dist/style.css">
<style>
 body{margin:0;font-family:"IBM Plex Sans",system-ui,sans-serif;background:#14130f;color:#ebe5d8}
 body.light{background:#f4f0e6;color:#1a1814}
 .host-bar{display:flex;gap:18px;align-items:center;padding:12px 24px;border-bottom:1px solid rgba(235,229,216,.12);font-size:13px;opacity:.85}
 body.light .host-bar{border-color:rgba(26,24,20,.12)}
 .host-bar b{font-weight:600}.host-bar span{opacity:.6}.host-bar .on{opacity:1;border-bottom:2px solid #c8361d;padding-bottom:2px}
</style></head><body class="__THEME__">
<div class="host-bar"><b>Hermes</b><span>Sessions</span><span>Analytics</span><span>Skills</span><span class="on">Kata</span><span>Config</span></div>
<div id="root"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"></script>
<script>
 window.__HERMES_PLUGIN_SDK__ = { React: React, hooks: React, components: {}, utils: {},
   fetchJSON: function (url, opts) { return fetch(url, opts).then(function (r) {
     return r.json().then(function (j) { return r.ok ? j : Promise.reject(j); }); }); } };
 window.__HERMES_PLUGINS__ = { register: function (name, C) {
   ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(C)); },
   registerSlot: function () {} };
</script>
<script src="/dist/index.js"></script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def page(theme: str = "dark") -> str:
    return PAGE.replace("__THEME__", "light" if theme == "light" else "dark")


@app.get("/dist/{name}")
def dist(name: str):
    path = (DASH / "dist" / name).resolve()
    if path.parent != (DASH / "dist").resolve() or not path.is_file():
        return HTMLResponse(status_code=404)
    return FileResponse(path)


@app.get("/favicon.svg")
def favicon():
    return FileResponse(ROOT / "docs" / "assets" / "kata-mark.svg", media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))
