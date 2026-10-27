"""Local app: pick or scan a curtain, build a QR label, print it directly."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import labeler
from data_store import Store, parse_label, parse_table

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("PRINTQR_PORT", "8765"))
store = Store(ROOT)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("printqr")

app = FastAPI(title="In tem QR", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def no_cache(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Có lỗi"
    return JSONResponse({"message": message}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unexpected_error(_request, exc: Exception):
    log.exception("request failed")
    if isinstance(exc, (RuntimeError, ValueError, OSError)):
        message = str(exc)
    else:
        message = "Lỗi không xác định"
    return JSONResponse({"message": message}, status_code=500)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/master")
def get_master():
    return store.load_master()


@app.get("/api/lookup")
def lookup(sku: str = ""):
    product = store.lookup(sku)
    if not product:
        return {"found": False}
    return {"found": True, "product": product}


@app.post("/api/import")
async def import_master(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 8_000_000:
        raise HTTPException(400, "File lớn hơn 8 MB")
    try:
        products = parse_table(file.filename or "", raw)
        return store.import_products(products, replace=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/template")
def template_file():
    content = "SKU,Tên SP,QR code\nSP001,Rèm vải voan 200x250,SP001\n"
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=master-data.csv"},
    )


@app.post("/api/products")
def save_product(body: dict):
    try:
        product = store.upsert_product(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"product": product}


@app.delete("/api/products")
def delete_product(body: dict):
    try:
        store.delete_product(str(body.get("sku", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return store.load_settings()


@app.put("/api/settings")
def put_settings(body: dict):
    try:
        return store.save_settings(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/printers")
async def get_printers(refresh: int = 0):
    remembered = store.load_settings().get("printer", "")
    return await asyncio.to_thread(labeler.list_printers, bool(refresh), remembered)


@app.get("/api/printer-info")
async def get_printer_info(name: str = ""):
    printer = name.strip()
    if not printer:
        raise HTTPException(400, "Chọn máy in")
    if printer.startswith("lan:"):
        ip = labeler.private_ipv4(printer.removeprefix("lan:"))
        if not ip:
            raise HTTPException(400, "IP máy in không hợp lệ")
        return {"width_mm": 35, "height_mm": 22, "dpi_x": 203, "dpi_y": 203}
    known = {item["id"] for item in labeler.list_printers()["printers"]}
    if printer not in known:
        raise HTTPException(400, "Máy in không tồn tại")
    try:
        return await asyncio.to_thread(labeler.printer_metrics, printer)
    except Exception as exc:
        raise HTTPException(400, f"Không đọc được khổ giấy của máy in: {exc}") from exc


@app.post("/api/preview")
async def preview(body: dict):
    try:
        item, width_mm, height_mm = parse_label(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    image = await asyncio.to_thread(labeler.render_sheet, item, 220, 220)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")


@app.post("/api/print")
async def do_print(body: dict):
    try:
        item, width_mm, height_mm = parse_label(body)
        printer = str(body.get("printer", "")).strip()
        copies = int(body.get("copies", 2))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (TypeError, KeyError) as exc:
        raise HTTPException(400, "Thiếu thông tin in") from exc
    if not printer:
        raise HTTPException(400, "Chọn máy in")
    return await asyncio.to_thread(labeler.print_label, item, width_mm, height_mm, printer, copies)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((HOST, port)) == 0


def our_app_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=0.4) as response:
            return b'"ok"' in response.read()
    except Exception:
        return False


def choose_port() -> tuple[int, str]:
    env_port = os.environ.get("PRINTQR_PORT")
    candidates = [int(env_port)] if env_port else list(range(8765, 8776))
    for port in candidates:
        if our_app_running(port):
            return port, "already"
        if not port_in_use(port):
            return port, "start"
    if env_port:
        raise SystemExit(f"Cổng {env_port} đang được dùng bởi chương trình khác.")
    raise SystemExit("Không tìm được cổng trống từ 8765 đến 8775.")


def open_app_window(port: int) -> None:
    url = f"http://{HOST}:{port}"
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            subprocess.Popen([path, f"--app={url}", "--window-size=1280,900"])
            return
    webbrowser.open(url)


def wait_and_open(port: int) -> None:
    for _ in range(50):
        if our_app_running(port):
            open_app_window(port)
            return
        time.sleep(0.1)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    port, mode = choose_port()
    if mode == "already":
        print("Ứng dụng đang chạy. Mở lại cửa sổ.", flush=True)
        if os.environ.get("PRINTQR_NO_BROWSER") != "1":
            open_app_window(port)
        return
    print(f"In tem QR: http://{HOST}:{port}", flush=True)
    print("Đóng cửa sổ này để tắt ứng dụng.", flush=True)
    if os.environ.get("PRINTQR_NO_BROWSER") != "1":
        threading.Thread(target=wait_and_open, args=(port,), daemon=True).start()
    import uvicorn

    uvicorn.run(app, host=HOST, port=port, log_level="info")


if __name__ == "__main__":
    main()
