"""Render a 35×22 mm label and send it straight to a Windows or LAN printer."""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
import winreg
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import qrcode
from PIL import Image, ImageDraw, ImageFont

from data_store import (
    FORM_HEIGHT_MM,
    LABEL_GAP_MM,
    LABEL_HEIGHT_MM,
    LABEL_WIDTH_MM,
    ROW_GAP_MM,
    SHEET_HEIGHT_MM,
    SHEET_WIDTH_MM,
    normalize_copies,
)

log = logging.getLogger(__name__)
BLACK = "#000000"
LEFT_SHIFT_MM = 1.5
_LAN_LOCK = threading.Lock()
_LAN_CACHE = {"at": 0.0, "ips": []}
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def render_label(
    item: dict,
    width_mm: float,
    height_mm: float,
    dpi_x: int = 300,
    dpi_y: int | None = None,
) -> Image.Image:
    dpi_y = dpi_x if dpi_y is None else dpi_y
    width_px = max(40, px(width_mm, dpi_x))
    height_px = max(40, px(height_mm, dpi_y))
    image = Image.new("RGB", (width_px, height_px), "white")

    sku = str(item["sku"]).strip()
    name = str(item["name"]).strip()
    qr_text = str(item.get("qr") or "").strip() or sku
    layout = choose_layout(name, sku, width_px, height_px, dpi_x, dpi_y)
    image.paste(make_qr(qr_text, layout["qr_side"]), (layout["qr_x"], layout["qr_y"]))
    draw_text_block(image, layout["text_rect"], layout["plan"])
    return image


def choose_layout(name: str, sku: str, width_px: int, height_px: int, dpi_x: int, dpi_y: int) -> dict:
    pad_x = px(0.5, dpi_x)
    pad_y = px(0.4, dpi_y)
    gap = px(0.35, dpi_x)
    text_height = height_px - pad_y * 2
    max_qr = text_height
    min_qr = px(6, dpi_x)
    min_text = px(8, dpi_x)
    chosen = None
    for qr_side in range(max_qr, min_qr - 1, -1):
        text_width = width_px - pad_x * 2 - gap - qr_side
        if text_width < min_text:
            continue
        plan = fit_text(name, sku, text_width, text_height, dpi_y)
        if plan is None:
            continue
        chosen = (qr_side, text_width, plan)
        break
    if chosen is None:
        qr_side = min_qr
        text_width = max(min_text, width_px - pad_x * 2 - gap - qr_side)
        plan = fit_text(name, sku, text_width, text_height, dpi_y, force=True)
        chosen = (qr_side, text_width, plan)
    qr_side, text_width, plan = chosen
    qr_x = pad_x
    qr_y = pad_y + max(0, (text_height - qr_side) // 2)
    text_left = qr_x + qr_side + gap
    return {
        "qr_side": qr_side,
        "qr_x": qr_x,
        "qr_y": qr_y,
        "text_rect": (text_left, pad_y, text_left + text_width, pad_y + text_height),
        "plan": plan,
    }


def fit_text(name: str, sku: str, max_width: int, max_height: int, dpi: int, force: bool = False) -> dict | None:
    leading = max(0, px(0.08, dpi) - 1)
    section_gap = max(1, px(0.28, dpi))
    name_px = px(2.7, dpi)
    floor = 6 if force else max(6, px(1.05, dpi))
    best = None
    while name_px >= floor:
        sku_px = max(6, int(round(name_px * 0.86)))
        name_font = font(name_px, bold=True)
        sku_font = font(sku_px, bold=False)
        name_lines = wrap_full(name, name_font, max_width)
        sku_lines = wrap_full(sku, sku_font, max_width)
        if name_lines is None or sku_lines is None:
            name_px -= 1
            continue
        entries: list[tuple[str, ImageFont.FreeTypeFont, bool]] = [(line, name_font, False) for line in name_lines]
        if entries:
            entries[-1] = (entries[-1][0], entries[-1][1], True)
        entries.extend((line, sku_font, False) for line in sku_lines)
        height = block_height(entries, leading, section_gap)
        best = {
            "entries": entries,
            "height": height,
            "leading": leading,
            "section_gap": section_gap,
        }
        if height <= max_height:
            return best
        name_px -= 1
    return best if force else None


def make_qr(data: str, side: int) -> Image.Image:
    border = 2
    level = qrcode.constants.ERROR_CORRECT_M if len(data) > 40 else qrcode.constants.ERROR_CORRECT_Q
    qr = qrcode.QRCode(error_correction=level, box_size=1, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    modules = len(qr.get_matrix()) + border * 2
    qr.box_size = max(1, side // modules)
    rendered = qr.make_image(fill_color="black", back_color="white")
    base = rendered.get_image() if hasattr(rendered, "get_image") else rendered
    base = base.convert("RGB")
    canvas = Image.new("RGB", (side, side), "white")
    if base.width > side or base.height > side:
        base = base.resize((side, side), Image.Resampling.NEAREST)
        canvas.paste(base, (0, 0))
        return canvas
    offset_x = (side - base.width) // 2
    offset_y = (side - base.height) // 2
    canvas.paste(base, (offset_x, offset_y))
    return canvas


def draw_text_block(image: Image.Image, rect: tuple[int, int, int, int], plan: dict | None) -> None:
    left, top, right, bottom = rect
    max_width = max(1, right - left)
    max_height = max(1, bottom - top)
    if not plan or not plan.get("entries"):
        return
    layer = Image.new("RGB", (max_width, max_height), "white")
    draw = ImageDraw.Draw(layer)
    y = max(0, (max_height - plan["height"]) / 2)
    entries = plan["entries"]
    leading = plan["leading"]
    section_gap = plan["section_gap"]
    for index, (text, text_font, section_end) in enumerate(entries):
        draw_line(draw, text, text_font, 0, y, max_width, False)
        y += line_height(text_font)
        if index != len(entries) - 1:
            y += section_gap if section_end else leading
    image.paste(layer, (left, top))


def to_solid_black(image: Image.Image) -> Image.Image:
    black = image.convert("L").point(lambda value: 0 if value < 200 else 255)
    return black.convert("RGB")


def render_sheet(item: dict, dpi_x: int = 203, dpi_y: int | None = None) -> Image.Image:
    dpi_y = dpi_x if dpi_y is None else dpi_y
    label = to_solid_black(render_label(item, LABEL_WIDTH_MM, LABEL_HEIGHT_MM, dpi_x, dpi_y))
    sheet = Image.new("RGB", (px(SHEET_WIDTH_MM, dpi_x), px(SHEET_HEIGHT_MM, dpi_y)), "white")
    gap = px(LABEL_GAP_MM, dpi_x)
    side = max(0, (sheet.width - label.width * 2 - gap) // 2)
    top = max(0, (sheet.height - label.height) // 2)
    sheet.paste(label, (side, top))
    sheet.paste(label, (side + label.width + gap, top))
    return to_solid_black(sheet)


def render_form(item: dict, dpi_x: int = 203, dpi_y: int | None = None) -> Image.Image:
    dpi_y = dpi_x if dpi_y is None else dpi_y
    sheet = render_sheet(item, dpi_x, dpi_y)
    gap = px(ROW_GAP_MM, dpi_y)
    form = Image.new("RGB", (sheet.width, sheet.height + gap), "white")
    form.paste(sheet, (0, 0))
    return form


def render_pair(item: dict, dpi_x: int, dpi_y: int, page_w: int | None = None, page_h: int | None = None) -> Image.Image:
    """Two 35×22 mm labels on the printer's current page. Does not change driver settings."""
    label = to_solid_black(render_label(item, LABEL_WIDTH_MM, LABEL_HEIGHT_MM, dpi_x, dpi_y))
    gap = px(LABEL_GAP_MM, dpi_x)
    if page_w is None:
        page_w = label.width * 2 + gap
    if page_h is None:
        page_h = label.height
    page_w = max(1, int(page_w))
    page_h = max(1, int(page_h))
    scale = min(page_w / max(1, label.width * 2 + gap), page_h / max(1, label.height), 1.0)
    if scale < 0.999:
        label = label.resize(
            (max(1, int(round(label.width * scale))), max(1, int(round(label.height * scale)))),
            Image.Resampling.NEAREST,
        )
        gap = max(0, int(round(gap * scale)))
    pair_w = label.width * 2 + gap
    pair_h = label.height
    image = Image.new("RGB", (page_w, page_h), "white")
    # Keep the pair against the left edge. Centering leaves a blank strip on the left, so the print sits to the right of the sticker.
    left = 0
    top = max(0, (page_h - pair_h) // 2)
    if pair_w > page_w:
        left = page_w - pair_w
    image.paste(label, (left, top))
    image.paste(label, (left + label.width + gap, top))
    return to_solid_black(image)


def block_height(entries: list[tuple[str, ImageFont.FreeTypeFont, bool]], leading: int, section_gap: int) -> int:
    height = 0
    for index, (_, text_font, section_end) in enumerate(entries):
        height += line_height(text_font)
        if index != len(entries) - 1:
            height += section_gap if section_end else leading
    return height


def draw_line(draw, text, text_font, left, top, max_width, center) -> None:
    width = text_width(text, text_font)
    x = left if not center else left + max(0, (max_width - width) / 2)
    draw.text((x, top), text, font=text_font, fill=BLACK, anchor="lt")


def wrap_full(text: str, text_font, max_width: int) -> list[str] | None:
    words = text.split()
    if not words:
        return []
    if text_width(text, text_font) <= max_width:
        return [text]

    def chunks(word: str) -> list[str] | None:
        pieces = []
        current = ""
        for char in word:
            trial = current + char
            if text_width(trial, text_font) <= max_width:
                current = trial
                continue
            if not current or text_width(char, text_font) > max_width:
                return None
            pieces.append(current)
            current = char
        if current:
            pieces.append(current)
        return pieces

    def break_token(word: str) -> list[str] | None:
        bits = [bit for bit in re.split(r"(?<=[-/_])", word) if bit]
        if len(bits) <= 1:
            return chunks(word)
        pieces = []
        for bit in bits:
            if text_width(bit, text_font) <= max_width:
                pieces.append(bit)
                continue
            broken = chunks(bit)
            if broken is None:
                return None
            pieces.extend(broken)
        return pieces

    lines: list[str] = []
    current = ""
    for word in words:
        if text_width(word, text_font) > max_width:
            if current:
                lines.append(current)
                current = ""
            pieces = break_token(word)
            if pieces is None:
                return None
            for piece in pieces:
                trial = piece if not current else current + piece
                if text_width(trial, text_font) <= max_width:
                    current = trial
                else:
                    if current:
                        lines.append(current)
                    if text_width(piece, text_font) > max_width:
                        return None
                    current = piece
            continue
        trial = word if not current else f"{current} {word}"
        if text_width(trial, text_font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def text_width(text: str, text_font) -> float:
    return text_font.getlength(text)


def line_height(text_font) -> int:
    ascent, descent = text_font.getmetrics()
    return ascent + descent


def px(mm: float, dpi: int) -> int:
    return max(1, int(round(float(mm) / 25.4 * dpi)))


def font(size: int, bold: bool = False):
    size = max(6, int(size))
    return _load_font(size, bold)


@lru_cache(maxsize=128)
def _load_font(size: int, bold: bool):
    path = resolve_font(bold)
    return ImageFont.truetype(path, size)


def resolve_font(bold: bool) -> str:
    regular = [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
    ]
    bold_fonts = [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\tahomabd.ttf",
    ]
    for path in bold_fonts if bold else regular:
        if os.path.isfile(path):
            return path
    raise RuntimeError("Không tìm thấy font chữ trên Windows")


def private_ipv4(value: str) -> str:
    text = str(value).strip()
    parts = text.split(".")
    if len(parts) != 4:
        return ""
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return ""
    if any(num < 0 or num > 255 for num in nums):
        return ""
    first, second = nums[0], nums[1]
    if first == 10 or (first == 172 and 16 <= second <= 31) or (first == 192 and second == 168):
        return ".".join(str(num) for num in nums)
    return ""


def local_private_ips() -> list[str]:
    found: list[str] = []

    def add(ip: str) -> None:
        clean = private_ipv4(ip)
        if clean and clean not in found:
            found.append(clean)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return found


def port_open(host: str, timeout: float) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, 9100)) == 0
    except OSError:
        return False


def scan_lan_printers() -> list[str]:
    own = set(local_private_ips())
    hosts = []
    for ip in own:
        prefix = ".".join(ip.split(".")[:3])
        for number in range(1, 255):
            host = f"{prefix}.{number}"
            if host not in own and host not in hosts:
                hosts.append(host)
    if not hosts:
        return []

    def probe(host: str) -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.45)
                if sock.connect_ex((host, 9100)) == 0:
                    return host
        except OSError:
            return ""
        return ""

    found = []
    with ThreadPoolExecutor(max_workers=80) as pool:
        for result in pool.map(probe, hosts):
            if result:
                found.append(result)
    found.sort(key=lambda item: tuple(int(part) for part in item.split(".")))
    log.info("LAN printers: %s", ", ".join(found) if found else "none")
    return found


def discover_lan_printers(refresh: bool = False) -> list[str]:
    with _LAN_LOCK:
        now = time.monotonic()
        if not refresh and _LAN_CACHE["at"] and now - _LAN_CACHE["at"] < 20:
            return list(_LAN_CACHE["ips"])
        try:
            ips = scan_lan_printers()
        except Exception:
            log.exception("lan scan failed")
            ips = []
        _LAN_CACHE["at"] = time.monotonic()
        _LAN_CACHE["ips"] = ips
        return list(ips)


def tcp_port_hosts() -> dict[str, str]:
    hosts = {}
    base = r"SYSTEM\CurrentControlSet\Control\Print\Monitors\Standard TCP/IP Port\Ports"
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return hosts
    try:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            try:
                port = winreg.OpenKey(key, name)
            except OSError:
                continue
            try:
                host = ""
                for value_name in ("IPAddress", "HostName"):
                    try:
                        value, _ = winreg.QueryValueEx(port, value_name)
                    except OSError:
                        continue
                    if str(value).strip():
                        host = str(value).strip()
                        break
                if host:
                    hosts[name] = host
            finally:
                port.Close()
    finally:
        key.Close()
    return hosts


def printer_host(port_name: str, port_hosts: dict[str, str]) -> str:
    raw = port_hosts.get(port_name, "")
    ip = private_ipv4(raw)
    if ip:
        return ip
    match = _IP_RE.search(f"{port_name} {raw}")
    if match:
        return private_ipv4(match.group(0))
    return ""


def list_printers(refresh: bool = False, remembered: str = "") -> dict:
    queues = []
    default_name = ""
    install_message = ""
    try:
        import win32print
    except ImportError:
        win32print = None
        install_message = "Chưa cài thư viện in. Đóng ứng dụng và chạy lại start.bat."
    if win32print is not None:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        port_hosts = tcp_port_hosts()
        seen = set()
        for item in win32print.EnumPrinters(flags, None, 2):
            name = item.get("pPrinterName") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            queues.append({"name": name, "host": printer_host(item.get("pPortName") or "", port_hosts)})
        try:
            default_name = win32print.GetDefaultPrinter()
        except Exception:
            default_name = queues[0]["name"] if queues else ""
    lan_ips = discover_lan_printers(refresh)
    remembered_ip = ""
    remembered_text = str(remembered).strip()
    if remembered_text.startswith("lan:"):
        remembered_ip = private_ipv4(remembered_text.removeprefix("lan:"))
    offline_ips = []
    if remembered_ip and remembered_ip not in lan_ips:
        if port_open(remembered_ip, 1.5):
            lan_ips.append(remembered_ip)
            lan_ips.sort(key=lambda item: tuple(int(part) for part in item.split(".")))
        else:
            offline_ips.append(remembered_ip)
    known_hosts = {item["host"] for item in queues if item["host"]}
    printers = []
    for item in queues:
        host = item["host"]
        printers.append({
            "id": item["name"],
            "label": f"{host} — {item['name']}" if host else item["name"],
            "host": host,
            "kind": "lan" if host else "windows",
            "online": True,
        })
    for ip in lan_ips:
        if ip in known_hosts:
            continue
        printers.append({"id": f"lan:{ip}", "label": ip, "host": ip, "kind": "lan", "online": True})
    for ip in offline_ips:
        if ip in known_hosts or ip in lan_ips:
            continue
        printers.append({
            "id": f"lan:{ip}",
            "label": f"{ip} — không phản hồi",
            "host": ip,
            "kind": "lan",
            "online": False,
        })
    printers.sort(key=lambda item: (0 if item["kind"] == "lan" else 1, item["host"] or "zzz", item["label"].casefold()))
    lan = [item for item in printers if item["kind"] == "lan"]
    if len(lan) == 1:
        default = lan[0]["id"]
    elif any(item["id"] == default_name for item in printers):
        default = default_name
    else:
        default = printers[0]["id"] if printers else ""
    if install_message and not printers:
        message = install_message
    elif not lan:
        message = "Không thấy máy in cổng 9100 trong mạng LAN."
    elif offline_ips and not lan_ips:
        message = f"{offline_ips[0]} không phản hồi. Bật máy in rồi bấm Tải lại."
    else:
        message = ""
    return {"printers": printers, "default": default, "message": message}


def printer_metrics(name: str) -> dict:
    import win32con
    import win32ui

    hdc = win32ui.CreateDC()
    try:
        hdc.CreatePrinterDC(name)
        dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX) or 203
        dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY) or 203
        width_mm = hdc.GetDeviceCaps(win32con.HORZSIZE) or 0
        height_mm = hdc.GetDeviceCaps(win32con.VERTSIZE) or 0
        if not width_mm:
            width_mm = round(hdc.GetDeviceCaps(win32con.HORZRES) / dpi_x * 25.4, 1)
        if not height_mm:
            height_mm = round(hdc.GetDeviceCaps(win32con.VERTRES) / dpi_y * 25.4, 1)
        return {
            "width_mm": width_mm,
            "height_mm": height_mm,
            "dpi_x": dpi_x,
            "dpi_y": dpi_y,
        }
    finally:
        try:
            hdc.DeleteDC()
        except Exception:
            pass


def shift_image_left(image: Image.Image, dots: int) -> Image.Image:
    dots = max(0, int(dots))
    if not dots:
        return image
    moved = Image.new(image.mode, image.size, "white")
    moved.paste(image, (-dots, 0))
    return moved


def tspl_payload(image: Image.Image, copies: int, shift_left_dots: int = 0) -> bytes:
    fitted = shift_image_left(image, shift_left_dots).convert("L")
    if fitted.width % 8:
        padded = Image.new("L", (fitted.width + (8 - fitted.width % 8), fitted.height), 255)
        padded.paste(fitted, (0, 0))
        fitted = padded
    width_bytes = fitted.width // 8
    height_dots = fitted.height
    data = bytearray()
    for y in range(height_dots):
        for x_byte in range(width_bytes):
            byte = 0
            for bit in range(8):
                x = x_byte * 8 + bit
                if fitted.getpixel((x, y)) < 128:
                    byte |= 0x80 >> bit
            data.append(byte)
    # BITMAP stays at 0,0. A negative X makes the HPRT D41 reject the job.
    header = f"CLS\r\nBITMAP 0,0,{width_bytes},{height_dots},0,".encode("ascii")
    footer = f"\r\nPRINT {max(1, int(copies))},1\r\n".encode("ascii")
    return header + bytes(data) + footer


def print_raw_lan(item: dict, printer: str, copies: int) -> dict:
    ip = private_ipv4(str(printer).removeprefix("lan:"))
    if not ip:
        raise ValueError("IP máy in không hợp lệ")
    stickers = normalize_copies(copies)
    image = render_pair(item, 203, 203)
    payload = tspl_payload(image, stickers // 2, px(LEFT_SHIFT_MM, 203))
    last_error: OSError | None = None
    for _ in range(3):
        try:
            with socket.create_connection((ip, 9100), timeout=8) as sock:
                sock.sendall(payload)
            last_error = None
            break
        except OSError as exc:
            last_error = exc
            time.sleep(0.4)
    if last_error is not None:
        raise RuntimeError(f"Không gửi được tới {ip}: {last_error}") from last_error
    log.info("printed %s stickers on %s", stickers, ip)
    return {"message": f"Đã in {stickers} tem tới {ip}", "warning": ""}


def printer_driver_port(printer: str) -> tuple[str, str]:
    import win32print

    handle = win32print.OpenPrinter(printer)
    try:
        info = win32print.GetPrinter(handle, 2)
    finally:
        win32print.ClosePrinter(handle)
    return info.get("pDriverName") or "", info.get("pPortName") or ""


def is_hprt_d41(printer: str, driver: str = "") -> bool:
    return "d41" in f"{printer} {driver}".casefold()


def raw_port_ok(port: str) -> bool:
    text = port.casefold()
    if any(mark in text for mark in ("onenote", "nul", "portprompt", "pdf", "xps", "fax")):
        return False
    return text.startswith("usb") or "tcp" in text or text.startswith("ip_")


def print_raw_spool(printer: str, item: dict, copies: int) -> dict:
    import win32print

    stickers = normalize_copies(copies)
    image = shift_image_left(render_pair(item, 203, 203), px(LEFT_SHIFT_MM, 203))
    payload = tspl_payload(image, stickers // 2)
    handle = win32print.OpenPrinter(printer)
    started = False
    try:
        win32print.StartDocPrinter(handle, 1, (f"Tem {item['sku']}", None, "RAW"))
        started = True
        win32print.StartPagePrinter(handle)
        written = win32print.WritePrinter(handle, payload)
        win32print.EndPagePrinter(handle)
        win32print.EndDocPrinter(handle)
        started = False
    except Exception as exc:
        if started:
            try:
                win32print.AbortPrinter(handle)
            except Exception:
                pass
        raise RuntimeError(f"HPRT D41 không nhận lệnh in: {exc}") from exc
    finally:
        win32print.ClosePrinter(handle)
    if written == 0:
        raise RuntimeError("HPRT D41 không nhận lệnh in.")
    log.info("printed %s stickers raw on %s", stickers, printer)
    return {"message": f"Đã in {stickers} tem tới «{printer}»", "warning": ""}


def print_label(item: dict, width_mm: float, height_mm: float, printer: str, copies: int) -> dict:
    if str(printer).startswith("lan:"):
        return print_raw_lan(item, printer, copies)
    try:
        import win32con
        import win32print
        import win32ui
        from PIL import ImageWin
    except ImportError as exc:
        raise RuntimeError("Chưa cài thư viện in Windows. Chạy lại start.bat để cài đặt.") from exc

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [item_info.get("pPrinterName") for item_info in win32print.EnumPrinters(flags, None, 2)]
    if printer not in names:
        raise ValueError("Máy in không tồn tại")
    driver, port = printer_driver_port(printer)
    if is_hprt_d41(printer, driver):
        if not raw_port_ok(port):
            raise RuntimeError(
                "HPRT D41 không in được vì cổng máy in không phải cổng USB. "
                "Vào Windows → Máy in → Thuộc tính máy in HPRT D41 → Cổng, chọn cổng USB của máy in."
            )
        return print_raw_spool(printer, item, copies)

    hdc = win32ui.CreateDC()
    doc_started = False
    try:
        try:
            hdc.CreatePrinterDC(printer)
        except Exception as exc:
            raise RuntimeError(f"Không mở được máy in: {exc}") from exc

        page_w = int(hdc.GetDeviceCaps(win32con.HORZRES) or 0)
        page_h = int(hdc.GetDeviceCaps(win32con.VERTRES) or 0)
        if page_w < 80 or page_h < 40:
            raise RuntimeError("Máy in không có vùng in hợp lệ. Giữ nguyên khổ tem đang cài trên HPRT.")
        dpi_x = int(hdc.GetDeviceCaps(win32con.LOGPIXELSX) or 203)
        dpi_y = int(hdc.GetDeviceCaps(win32con.LOGPIXELSY) or 203)
        stickers = normalize_copies(copies)
        rows = stickers // 2
        image = render_pair(item, min(dpi_x, 600), min(dpi_y, 600), page_w, page_h)
        # The driver already starts 1.25 mm in from the paper edge. Shift another 1.5 mm left.
        origin_x = int(hdc.GetDeviceCaps(win32con.PHYSICALOFFSETX) or 0) + px(LEFT_SHIFT_MM, dpi_x)
        origin_y = int(hdc.GetDeviceCaps(win32con.PHYSICALOFFSETY) or 0)

        dib = ImageWin.Dib(image)
        hdc.StartDoc(f"Tem {item['sku']}")
        doc_started = True
        for _ in range(rows):
            hdc.StartPage()
            dib.draw(
                hdc.GetHandleOutput(),
                (-origin_x, -origin_y, page_w - origin_x, page_h - origin_y),
            )
            hdc.EndPage()
        hdc.EndDoc()
        doc_started = False
        log.info("printed %s stickers on %s", stickers, printer)
        return {"message": f"Đã in {stickers} tem tới «{printer}»", "warning": ""}
    except ValueError:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        if doc_started:
            try:
                hdc.AbortDoc()
            except Exception:
                pass
            doc_started = False
        log.exception("print failed")
        raise RuntimeError(f"Không gửi được lệnh in: {exc}") from exc
    finally:
        if doc_started:
            try:
                hdc.AbortDoc()
            except Exception:
                pass
        try:
            hdc.DeleteDC()
        except Exception:
            pass


if __name__ == "__main__":
    from pathlib import Path

    item = {"sku": "SP001", "name": "Rèm vải voan 200x250", "qr": "QR-SP001"}
    out = Path("data/samples")
    out.mkdir(parents=True, exist_ok=True)
    render_label(item, LABEL_WIDTH_MM, LABEL_HEIGHT_MM, 300, 300).save(out / "35x22.png")
    print(f"wrote samples to {out}")
