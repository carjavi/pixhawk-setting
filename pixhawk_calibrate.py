#!/usr/bin/env python3
# Calibracion (acelerometro/compass) y actualizacion de firmware ArduPilot
# estable para Pixhawk, con TUI Textual -- Windows 10/11 y Linux / Raspberry Pi.
# @author: Carlos Briceño <carjavi@hotmail.com>
# @date: 24-09-2026
# @copyright: Copyright (c) 2026 www.carjavi.com
# @version: V3.2
# @library:
# - pip install pymavlink pyserial textual
"""
pixhawk_calibrate.py  v3.2
===========================
Calibracion y actualizacion de firmware para Pixhawk/ArduPilot
TUI con Textual -- Windows 10/11 y Linux / Raspberry Pi

Dependencias:
    pip install pymavlink pyserial textual        (Windows)
    pip3 install pymavlink pyserial textual       (Linux / RPi)

Uso Windows:
    python pixhawk_calibrate.py
    python pixhawk_calibrate.py --port COM3
    python pixhawk_calibrate.py --port COM3 --baud 57600
    python pixhawk_calibrate.py --check-firmware   # valida URLs de todos los firmwares

Uso Linux / Raspberry Pi:
    sudo usermod -aG dialout $USER                 # solo la primera vez, luego re-login
    python3 pixhawk_calibrate.py
    python3 pixhawk_calibrate.py --port /dev/ttyACM0
    python3 pixhawk_calibrate.py --check-firmware

Ctrl+C : cierra la aplicacion limpiamente.

Cambios v3.2:
    - Multiplataforma: la actualizacion de firmware (repos corregidos en v3.1) y las
      calibraciones funcionan tambien en Linux / Raspberry Pi.
    - Reinicio a bootloader por MAVLink2 desde el script: la Pixhawk ignoraba el
      paquete MAVLink1 fijo de uploader.py.
    - El bootloader enumera en OTRO puerto (Pixhawk 2.4.8: app COM5, bootloader COM3);
      se detecta al reiniciar y se leen los COM recordados por Windows.
    - Si uploader.py rechaza el firmware, se corta de inmediato y la placa vuelve
      al firmware instalado (antes quedaba retenida en el bootloader).

Cambios v3.1:
    - Firmware: nombre de .apj correcto por vehiculo (ardurover.apj, arducopter.apj...);
      antes se pedia 'ardupilot.apj', que no existe en firmware.ardupilot.org (HTTP 404).
    - Validacion del .apj descargado (magic, board_id, imagen, git hash == stable).
    - Validacion del board_id real del Pixhawk (AUTOPILOT_VERSION) antes de flashear.
    - uploader.py descargado del mismo commit que el firmware estable.
    - Descargas con timeout + reintentos; watchdog real para el flasheo.
"""

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Optional

# ── Plataforma ────────────────────────────────────────────────────────────────
#: True en Windows; en Linux / Raspberry Pi se usan /dev/tty* y /proc.
IS_WINDOWS = sys.platform == "win32"
#: Comando pip sugerido en los mensajes de error.
PIP_CMD = "pip" if IS_WINDOWS else "pip3"

if IS_WINDOWS:
    # Activar ANSI en consola Windows (necesario para Textual)
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        for handle in (-10, -11, -12):
            k32.SetConsoleMode(k32.GetStdHandle(handle), 7)
    except Exception:
        pass
elif not sys.platform.startswith("linux"):
    print("\n[ERROR] Plataforma no soportada: solo Windows y Linux / Raspberry Pi.\n")
    sys.exit(1)

# ── Verificar dependencias ────────────────────────────────────────────────────
def _check_dep(import_name: str, pkg: str):
    try:
        __import__(import_name)
    except ImportError:
        print(f"\n[ERROR] Falta '{pkg}':\n        {PIP_CMD} install {pkg}\n")
        sys.exit(1)

_check_dep("textual",   "textual")
_check_dep("pymavlink", "pymavlink")
_check_dep("serial",    "pyserial")

from textual.app import App, ComposeResult                     # noqa: E402
from textual.screen import Screen                              # noqa: E402
from textual.widgets import (                                  # noqa: E402
    Header, Footer, Button, Static, Label,
    RichLog, ProgressBar, Rule, Select,
)
from textual.containers import Vertical, Horizontal, Center, VerticalScroll  # noqa: E402
from textual import on                                         # noqa: E402
from pymavlink import mavutil                                  # noqa: E402


# ── Constantes MAVLink ────────────────────────────────────────────────────────
MAV_CMD_PREFLIGHT_CALIBRATION          = 241
MAV_CMD_ACCELCAL_VEHICLE_POS           = 42429
MAV_CMD_DO_START_MAG_CAL               = 42424
MAV_CMD_DO_CANCEL_MAG_CAL              = 42426
MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES = 520
MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN      = 246
MAV_RESULT_ACCEPTED                    = 0

# ── Firmware ArduPilot ────────────────────────────────────────────────────────
#: Servidor oficial de firmwares ArduPilot (mismo que usan Mission Planner / QGC).
FW_BASE = "https://firmware.ardupilot.org"
#: Canal de release. Solo se flashean versiones estables.
FW_CHANNEL = "stable"
#: uploader.py oficial; {ref} = git hash del firmware estable (fallback: master).
UPLOADER_URL_TMPL = ("https://raw.githubusercontent.com/ArduPilot/ardupilot/"
                     "{ref}/Tools/scripts/uploader.py")
#: Timeout (s) por intento de descarga HTTP.
HTTP_TIMEOUT_S = 20
#: Reintentos de descarga ante errores de red / 5xx.
HTTP_RETRIES = 3
#: Tiempo maximo (s) para que uploader.py termine (bootloader + borrado + escritura).
FLASH_TIMEOUT_S = 240
#: Mensajes de uploader.py que indican un error definitivo (no se arregla reintentando).
#: Todos ocurren ANTES de borrar la flash, por lo que el firmware instalado sigue intacto.
UPLOADER_FATAL_PATTERNS = (
    "Firmware not suitable for this board",
    "Firmware image is too large",
)
#: Magic obligatorio del formato .apj de ArduPilot.
APJ_MAGIC = "APJFWv1"

#: Vehiculo -> (directorio en FW_BASE, sufijo del dir. de board, archivo .apj,
#: prefijo APMVERSION esperado). URL: FW_BASE/<dir>/stable/<board><sufijo>/<apj>.
#: El .apj NO se llama 'ardupilot.apj': cada vehiculo tiene su propio nombre.
FIRMWARE_TARGETS = {
    "Copter":         ("Copter",         "",      "arducopter.apj",      "ArduCopter"),
    "Heli":           ("Copter",         "-heli", "arducopter-heli.apj", "ArduCopter"),
    "Plane":          ("Plane",          "",      "arduplane.apj",       "ArduPlane"),
    "Rover":          ("Rover",          "",      "ardurover.apj",       "ArduRover"),
    "Sub":            ("Sub",            "",      "ardusub.apj",         "ArduSub"),
    "AntennaTracker": ("AntennaTracker", "",      "antennatracker.apj",  "AntennaTracker"),
}

VEHICLE_OPTIONS = [
    ("Copter (multirrotor)",      "Copter"),
    ("Copter Heli (helicoptero)", "Heli"),
    ("Plane (ala fija)",          "Plane"),
    ("Rover (terrestre / bote)",  "Rover"),
    ("Sub (submarino)",           "Sub"),
    ("AntennaTracker",            "AntennaTracker"),
]

#: Board -> APJ board_id (verificado contra firmware.ardupilot.org/manifest.json.gz).
#: Varios targets comparten board_id 9 (familia Pixhawk1/FMUv2/v3/CubeBlack).
BOARD_IDS = {
    "fmuv3":          9,
    "Pixhawk1-1M":    9,
    "fmuv2":          9,
    "Pixhawk1":       9,
    "CubeBlack":      9,
    "Pixhawk4":       50,
    "Pixhawk6C":      56,
    "Pixhawk6X":      53,
    "CubeOrange":     140,
    "CubeOrangePlus": 1063,
    "MatekH743":      1013,
}

BOARD_OPTIONS = [
    ("fmuv3 -- Pixhawk 2.4.8 / clones (2MB flash)", "fmuv3"),
    ("Pixhawk1-1M -- Pixhawk 2.4.8 con limite 1MB", "Pixhawk1-1M"),
    ("fmuv2 -- Pixhawk 1 v2 (original 3DR)",        "fmuv2"),
    ("Pixhawk1 -- Pixhawk 1 (mRo / 3DR)",           "Pixhawk1"),
    ("CubeBlack",                                   "CubeBlack"),
    ("Pixhawk4",                                    "Pixhawk4"),
    ("Pixhawk6C",                                   "Pixhawk6C"),
    ("Pixhawk6X",                                   "Pixhawk6X"),
    ("CubeOrange",                                  "CubeOrange"),
    ("CubeOrangePlus",                              "CubeOrangePlus"),
    ("MatekH743",                                   "MatekH743"),
]

#: MAV_TYPE (HEARTBEAT.type) -> clave de FIRMWARE_TARGETS, para preseleccionar vehiculo.
MAV_TYPE_TO_VEHICLE = {
    1: "Plane", 2: "Copter", 3: "Copter", 4: "Heli", 5: "AntennaTracker",
    10: "Rover", 11: "Rover", 12: "Sub", 13: "Copter", 14: "Copter", 15: "Copter",
    19: "Plane", 20: "Plane", 21: "Plane", 22: "Plane", 29: "Copter",
}

#: USB VIDs de autopilotos/bootloaders; el bootloader puede enumerar en otro COM.
AUTOPILOT_USB_VIDS = {0x1209, 0x26AC, 0x2DAE, 0x3162, 0x0483, 0x35A7}


@dataclass
class StableFirmware:
    """Firmware estable resuelto y validado para un vehiculo/board.

    Attributes:
        vehicle: Clave de FIRMWARE_TARGETS (ej. "Rover").
        board: Target de ArduPilot (ej. "fmuv3").
        url: URL del .apj.
        version: Texto APMVERSION (ej. "ArduRover V4.7.1").
        git_hash: Commit completo del release estable.
    """
    vehicle: str
    board: str
    url: str
    version: str
    git_hash: str


def http_get(url: str, timeout: float = HTTP_TIMEOUT_S, retries: int = HTTP_RETRIES,
             progress=None) -> bytes:
    """Descarga una URL con timeout y reintentos (backoff lineal).

    Args:
        url: URL HTTP(S) a descargar.
        timeout: Timeout en segundos por intento.
        retries: Numero de intentos totales.
        progress: Callback opcional ``progress(bytes_leidos, total)``.

    Returns:
        Contenido descargado.

    Raises:
        RuntimeError: Si todos los intentos fallan. Un HTTP 4xx no se reintenta
            (la URL no existe: reintentar no sirve).
    """
    last_err: Exception = RuntimeError("sin intentos")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pixhawk_calibrate/3.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                total = int(r.headers.get("Content-Length") or 0)
                chunks, read = [], 0
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    read += len(chunk)
                    if progress:
                        progress(read, total)
                data = b"".join(chunks)
                if total and len(data) != total:
                    raise IOError(f"descarga incompleta ({len(data)}/{total} bytes)")
                return data
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise RuntimeError(f"HTTP {e.code} en {url}") from e
            last_err = e
        except Exception as e:  # red, timeout, descarga incompleta
            last_err = e
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError(f"No se pudo descargar {url} tras {retries} intentos: {last_err}")


def resolve_stable_firmware(vehicle: str, board: str) -> StableFirmware:
    """Consulta git-version.txt del canal estable y valida que corresponda al vehiculo.

    Args:
        vehicle: Clave de FIRMWARE_TARGETS.
        board: Target de ArduPilot (clave de BOARD_IDS).

    Returns:
        StableFirmware con URL, version y git hash.

    Raises:
        RuntimeError: Vehiculo/board desconocido, URL inexistente o version inconsistente.

    Example:
        >>> fw = resolve_stable_firmware("Rover", "fmuv3")
        >>> fw.url
        'https://firmware.ardupilot.org/Rover/stable/fmuv3/ardurover.apj'
    """
    if vehicle not in FIRMWARE_TARGETS:
        raise RuntimeError(f"Vehiculo desconocido: {vehicle}")
    if board not in BOARD_IDS:
        raise RuntimeError(f"Board desconocido: {board}")
    fw_dir, board_suffix, apj_name, apm_prefix = FIRMWARE_TARGETS[vehicle]
    base = f"{FW_BASE}/{fw_dir}/{FW_CHANNEL}/{board}{board_suffix}"

    text = http_get(f"{base}/git-version.txt").decode("utf-8", errors="replace")
    m_hash = re.search(r"^commit\s+([0-9a-f]{40})", text, re.MULTILINE)
    m_ver = re.search(r"^APMVERSION:\s*(.+)$", text, re.MULTILINE)
    if not m_hash or not m_ver:
        raise RuntimeError(f"git-version.txt con formato inesperado en {base}")
    version = m_ver.group(1).strip()
    if not version.startswith(apm_prefix):
        raise RuntimeError(f"Version '{version}' no corresponde a {apm_prefix}")
    if re.search(r"(dev|beta|rc)\b", version, re.IGNORECASE):
        raise RuntimeError(f"El canal stable reporta una version no estable: {version}")
    return StableFirmware(vehicle, board, f"{base}/{apj_name}", version, m_hash.group(1))


def validate_apj(data: bytes, fw: StableFirmware) -> dict:
    """Valida un archivo .apj descargado antes de flashearlo.

    Comprueba: JSON valido, magic APJFWv1, board_id del board elegido, imagen
    presente y descomprimible con el tamano declarado, y git hash == release estable.

    Args:
        data: Contenido del .apj.
        fw: Firmware estable esperado.

    Returns:
        Diccionario con los metadatos del .apj (sin la imagen).

    Raises:
        RuntimeError: Si alguna validacion falla.
    """
    try:
        apj = json.loads(data)
    except ValueError as e:
        raise RuntimeError(f".apj no es JSON valido: {e}") from e
    if apj.get("magic") != APJ_MAGIC:
        raise RuntimeError(f".apj con magic invalido: {apj.get('magic')!r}")
    expected_id = BOARD_IDS[fw.board]
    if apj.get("board_id") != expected_id:
        raise RuntimeError(f".apj para board_id {apj.get('board_id')}, se esperaba "
                           f"{expected_id} ({fw.board})")
    try:
        image = zlib.decompress(base64.b64decode(apj["image"]))
    except Exception as e:
        raise RuntimeError(f"Imagen del .apj corrupta: {e}") from e
    if len(image) != apj.get("image_size"):
        raise RuntimeError(f"Tamano de imagen {len(image)} != image_size {apj.get('image_size')}")
    git_id = str(apj.get("git_identity", ""))
    if not git_id or not fw.git_hash.startswith(git_id[:7]):
        raise RuntimeError(f"git_identity {git_id!r} no coincide con el release "
                           f"estable {fw.git_hash[:8]}")
    return {k: v for k, v in apj.items() if k not in ("image", "extf_image")}


def download_uploader(git_hash: str) -> bytes:
    """Descarga uploader.py oficial del mismo commit del firmware (fallback: master).

    Args:
        git_hash: Commit del release estable.

    Returns:
        Codigo fuente de uploader.py.

    Raises:
        RuntimeError: Si no se pudo descargar un uploader.py valido.
    """
    errors = []
    for ref in (git_hash, "master"):
        try:
            src = http_get(UPLOADER_URL_TMPL.format(ref=ref))
            if b"Firmware uploader for the PX autopilot system" not in src:
                raise RuntimeError("contenido inesperado")
            return src
        except Exception as e:
            errors.append(f"{ref[:8]}: {e}")
    raise RuntimeError("uploader.py no disponible (" + "; ".join(errors) + ")")


def boards_for_id(board_id: int) -> list:
    """Devuelve los targets de BOARD_OPTIONS compatibles con un board_id."""
    return [val for _, val in BOARD_OPTIONS if BOARD_IDS[val] == board_id]


#: Linux: nombres estables de udev para autopilotos/bootloaders (uploader.py expande globs).
LINUX_BY_ID_GLOBS = [
    "/dev/serial/by-id/usb-ArduPilot*",
    "/dev/serial/by-id/usb-3D_Robotics*",
    "/dev/serial/by-id/usb-Hex_ProfiCNC*",
    "/dev/serial/by-id/usb-Holybro*",
    "/dev/serial/by-id/usb-mRo*",
    "/dev/serial/by-id/*PX4*",
]


def windows_known_autopilot_ports() -> list:
    """Windows: puertos COM que el registro recuerda para VIDs de autopilotos.

    El bootloader (ej. "PX4 BL FMU v2.x", VID 0x26AC PID 0x0011 en Pixhawk 2.4.8)
    enumera con otro VID/PID que la aplicacion y por eso recibe OTRO numero de COM
    (ej. app COM5, bootloader COM3) que solo existe unos segundos tras el reinicio.
    Windows guarda esa asignacion en HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB.

    Returns:
        Lista de "COMn" (vacia fuera de Windows o si no hay registros).
    """
    if not IS_WINDOWS:
        return []
    import winreg
    ports = []
    base = r"SYSTEM\CurrentControlSet\Enum\USB"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            dev = winreg.EnumKey(root, i)
            m = re.match(r"VID_([0-9A-F]{4})&PID_[0-9A-F]{4}$", dev, re.IGNORECASE)
            if not m or int(m.group(1), 16) not in AUTOPILOT_USB_VIDS:
                continue
            try:
                with winreg.OpenKey(root, dev) as dk:
                    for j in range(winreg.QueryInfoKey(dk)[0]):
                        inst = winreg.EnumKey(dk, j)
                        try:
                            with winreg.OpenKey(dk, inst + r"\Device Parameters") as pk:
                                name = winreg.QueryValueEx(pk, "PortName")[0]
                            if name.upper().startswith("COM") and name not in ports:
                                ports.append(name)
                        except OSError:
                            continue
            except OSError:
                continue
    return ports


def autopilot_ports(primary: str, extra: Optional[list] = None) -> str:
    """Lista de puertos para uploader.py: el puerto actual primero y luego otros
    puertos donde puede aparecer el bootloader (COM5 -> COM3 en Windows,
    /dev/ttyACM0 -> /dev/ttyACM1 en Linux).

    Args:
        primary: Puerto en uso (ej. "COM5" o "/dev/ttyACM0").
        extra: Puertos detectados al reiniciar a bootloader (van primero).

    Returns:
        Lista separada por comas, ej. "COM3,COM5" o "/dev/ttyACM0,/dev/serial/by-id/usb-ArduPilot*".
    """
    ports = list(extra or [])
    if primary not in ports:
        ports.append(primary)
    try:
        import serial.tools.list_ports as lp
        for p in lp.comports():
            if p.vid in AUTOPILOT_USB_VIDS and p.device not in ports:
                ports.append(p.device)
    except Exception:
        pass
    ports += [p for p in windows_known_autopilot_ports() if p not in ports]
    if not IS_WINDOWS:
        # Los globs cubren el bootloader aunque aun no exista al momento de lanzar el uploader
        ports += [g for g in LINUX_BY_ID_GLOBS if g not in ports]
    return ",".join(ports)


def exit_bootloader(ports: list) -> list:
    """Saca la placa del bootloader y arranca el firmware instalado (no escribe flash).

    Envia PROTO_BOOT (0x30) + PROTO_EOC (0x20) del protocolo de bootloader PX4/ArduPilot.
    Se usa cuando el flasheo falla tras reiniciar con param1=3, que deja la placa
    retenida en el bootloader.

    Args:
        ports: Puertos donde se detecto el bootloader.

    Returns:
        Puertos a los que se envio el comando.
    """
    import serial
    sent = []
    for dev in ports:
        try:
            with serial.Serial(dev, 115200, timeout=1, write_timeout=1) as s:
                s.write(b"\x30\x20")
                s.flush()
            sent.append(dev)
        except Exception:
            continue
    return sent


def reboot_to_bootloader(conn, primary: str, timeout: float = 8.0) -> list:
    """Reinicia el Pixhawk al bootloader por MAVLink2 y detecta su puerto.

    uploader.py intenta reiniciar la placa con un paquete MAVLink1 fijo que algunos
    firmwares ignoran (verificado en Pixhawk 2.4.8 con ArduSub 4.5.7); por eso el
    reinicio se hace con la conexion MAVLink ya abierta, igual que Mission Planner/QGC.
    El bootloader de ArduPilot/PX4 solo espera unos segundos, asi que el uploader
    debe lanzarse inmediatamente despues.

    Args:
        conn: Conexion pymavlink abierta (se cierra aqui).
        primary: Puerto de la aplicacion (ej. "COM5").
        timeout: Espera maxima (s) a que aparezca el puerto del bootloader.

    Returns:
        Puertos de autopiloto que aparecieron tras el reinicio (vacio si no se detecto).
    """
    import serial.tools.list_ports as lp

    def _autopilot_devices() -> set:
        return {p.device for p in lp.comports() if p.vid in AUTOPILOT_USB_VIDS}

    conn.mav.command_long_send(
        conn.target_system, 1, MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN, 0,
        3,  # param1=3: reiniciar y quedarse en el bootloader
        0, 0, 0, 0, 0, 0,
    )
    time.sleep(0.2)
    try:
        conn.close()
    except Exception:
        pass

    deadline = time.time() + 2.0            # esperar que desaparezca la app
    while time.time() < deadline and primary in _autopilot_devices():
        time.sleep(0.05)
    deadline = time.time() + timeout        # esperar que enumere el bootloader
    while time.time() < deadline:
        found = sorted(_autopilot_devices())
        if found:
            return found
        time.sleep(0.05)
    return []


def check_all_firmware_urls() -> bool:
    """Valida (sin hardware) que existan todos los firmwares estables vehiculo x board.

    Descarga git-version.txt de cada combinacion y hace un HEAD al .apj.

    Returns:
        True si todas las combinaciones estan disponibles.
    """
    all_ok = True
    for _, vehicle in VEHICLE_OPTIONS:
        for _, board in BOARD_OPTIONS:
            try:
                fw = resolve_stable_firmware(vehicle, board)
                req = urllib.request.Request(fw.url, method="HEAD")
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                    size = int(r.headers.get("Content-Length") or 0)
                print(f"  [OK]    {vehicle:15s} {board:15s} {fw.version:24s} "
                      f"{size // 1024:5d} KB  {fw.url}")
            except Exception as e:
                all_ok = False
                print(f"  [FALLO] {vehicle:15s} {board:15s} {e}")
    return all_ok

# ── Posiciones acelerometro ───────────────────────────────────────────────────
ACCEL_POSITIONS = [
    {
        "id": 0, "name": "NIVEL (cara arriba)", "short": "NIVEL",
        "instruction": "Coloque el Pixhawk PLANO con la cara superior hacia arriba.",
        "diagram": (
            "       /\\ NARIZ\n"
            "  +----+----+\n"
            "  | /= [O] =\\ |  <- cara ARRIBA\n"
            "  +----+----+\n"
            "  apoya plano sobre superficie"
        ),
    },
    {
        "id": 1, "name": "IZQUIERDA ABAJO", "short": "IZQ ABAJO",
        "instruction": "Gire 90 grados: lado IZQUIERDO hacia el suelo, nariz al frente.",
        "diagram": (
            "       /\\ NARIZ\n"
            "  +=========+\n"
            "  | [O]      |  lado IZQ al PISO\n"
            "  +=========+\n"
            "  IZQ abajo     DER arriba"
        ),
    },
    {
        "id": 2, "name": "DERECHA ABAJO", "short": "DER ABAJO",
        "instruction": "Gire 90 grados: lado DERECHO hacia el suelo, nariz al frente.",
        "diagram": (
            "       /\\ NARIZ\n"
            "  +=========+\n"
            "  |      [O] |  lado DER al PISO\n"
            "  +=========+\n"
            "  IZQ arriba    DER abajo"
        ),
    },
    {
        "id": 3, "name": "NARIZ ABAJO", "short": "NARIZ abajo",
        "instruction": "Incline hacia adelante: NARIZ apunta al suelo, cola arriba.",
        "diagram": (
            "  COLA arriba\n"
            "  +=========+\n"
            "  |   [O]    |\n"
            "  +=========+\n"
            "  NARIZ abajo"
        ),
    },
    {
        "id": 4, "name": "NARIZ ARRIBA", "short": "NARIZ arriba",
        "instruction": "Incline hacia atras: NARIZ apunta al cielo, cola al suelo.",
        "diagram": (
            "  NARIZ arriba\n"
            "  +=========+\n"
            "  |   [O]    |\n"
            "  +=========+\n"
            "  COLA abajo"
        ),
    },
    {
        "id": 5, "name": "BOCA ABAJO", "short": "INVERTIDO",
        "instruction": "Voltee 180 grados: cara superior mirando al suelo.",
        "diagram": (
            "       /\\ NARIZ\n"
            "  +----+----+\n"
            "  | \\= [O] =/ |  <- cara ABAJO\n"
            "  +----+----+\n"
            "  boca abajo (invertido)"
        ),
    },
]

COMPASS_DIAGRAM = (
    "  Rote el Pixhawk en TODAS las orientaciones:\n"
    "  +==============+\n"
    "  |  +--------+  |   Yaw  : giro horizontal\n"
    "  |  |  [O]   |  |   Pitch: nariz arriba/abajo\n"
    "  |  +--------+  |   Roll : costado izq/der\n"
    "  +==============+\n"
    "  Haga 2-3 rotaciones lentas en cada eje."
)

# ── Deteccion de puerto serie ─────────────────────────────────────────────────
KNOWN_VID_PID = {
    (0x26AC, None), (0x0483, 0x5740), (0x0483, 0x374B),
    (0x27AC, None), (0x1209, 0x5741), (0x0403, 0x6001),
    (0x0403, 0x6015), (0x10C4, 0xEA60), (0x1A86, 0x7523),
    (0x1A86, 0x55D4), (0x2341, None),
}
KNOWN_KEYWORDS = [
    "pixhawk", "ardupilot", "px4", "cube", "holybro",
    "mro", "fmuv", "ftdi", "ch340", "cp210", "stm32",
]

#: Linux: prioridad de puertos tipicos (USB CDC del Pixhawk primero, UART GPIO al final).
LINUX_PORT_PRIORITY = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0",
                       "/dev/ttyUSB1", "/dev/serial0", "/dev/ttyAMA0"]


def _port_bonus(device: str) -> int:
    """Puntaje extra por nombre de puerto segun la plataforma."""
    if IS_WINDOWS:
        try:
            return max(0, 5 - int(device[3:]) // 4)
        except ValueError:
            return 0
    try:
        return max(0, 8 - LINUX_PORT_PRIORITY.index(device))
    except ValueError:
        return 0


def _is_candidate_port(device: str) -> bool:
    """COMx en Windows; ttyACM/ttyUSB/ttyAMA/serial0 en Linux (descarta ttyS* virtuales)."""
    if IS_WINDOWS:
        return device.upper().startswith("COM")
    return device.startswith(("/dev/ttyACM", "/dev/ttyUSB", "/dev/ttyAMA", "/dev/serial"))


def detect_port() -> Optional[str]:
    """Devuelve el puerto serie con mayor probabilidad de ser un Pixhawk, o None.

    Puntua por palabras clave del descriptor USB, VID/PID conocidos y nombre del puerto.
    """
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return None
    best_score, best_port = -1, None
    for p in lp.comports():
        if not _is_candidate_port(p.device):
            continue
        score = 0
        combined = f"{p.description or ''} {p.manufacturer or ''} {p.product or ''}".lower()
        for kw in KNOWN_KEYWORDS:
            if kw in combined:
                score += 10
                break
        if p.vid is not None:
            for kvid, kpid in KNOWN_VID_PID:
                if p.vid == kvid and (kpid is None or p.pid == kpid):
                    score += 20
                    break
        score += _port_bonus(p.device)
        if score > best_score:
            best_score, best_port = score, p.device
    return best_port


#: Programas GCS que suelen tener abierto el puerto del Pixhawk.
PORT_BLOCKERS_WIN = ["MissionPlanner.exe", "QGroundControl.exe", "mavproxy.exe", "ArduPilot.exe"]
PORT_BLOCKERS_LINUX = ["QGroundControl", "mavproxy.py", "MAVProxy", "MissionPlanner"]


def _linux_port_users(port: str) -> list:
    """Linux: (pid, nombre, cmdline) de los procesos con `port` abierto, via /proc/*/fd."""
    target = os.path.realpath(port)
    users = []
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = int(fd_dir.split("/")[2])
        if pid == os.getpid():
            continue
        try:
            if not any(os.path.realpath(os.path.join(fd_dir, fd)) == target
                       for fd in os.listdir(fd_dir)):
                continue
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(errors="replace")
            users.append((pid, comm, cmdline))
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
    return users


def release_serial_port(port: str) -> tuple:
    """Cierra GCS conocidos que bloquean el puerto del Pixhawk.

    Windows: tasklist/taskkill por nombre. Linux: busca los procesos que tienen
    el puerto abierto y termina (SIGTERM) solo los GCS conocidos; el resto se reporta.

    Args:
        port: Puerto serie ("COM5" o "/dev/ttyACM0").

    Returns:
        (cerrados, otros): nombres de procesos cerrados y de procesos desconocidos
        que siguen usando el puerto (ej. ModemManager).
    """
    killed, others = [], []
    if IS_WINDOWS:
        try:
            result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                    capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                name = line.split(",")[0].strip('"')
                if any(b.lower() == name.lower() for b in PORT_BLOCKERS_WIN):
                    subprocess.run(["taskkill", "/F", "/IM", name],
                                   capture_output=True, timeout=5)
                    killed.append(name)
        except Exception:
            pass
        return killed, others

    import signal
    for pid, comm, cmdline in _linux_port_users(port):
        if any(b.lower() in f"{comm} {cmdline}".lower() for b in PORT_BLOCKERS_LINUX):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(f"{comm} ({pid})")
            except OSError:
                others.append(f"{comm} ({pid})")
        else:
            others.append(f"{comm} ({pid})")
    return killed, others


def serial_help() -> str:
    """Texto de ayuda para problemas de acceso al puerto segun la plataforma."""
    if IS_WINDOWS:
        return ("Cierre Mission Planner, QGroundControl, MAVProxy o Arduino IDE\n"
                "y verifique el driver en Administrador de dispositivos.")
    return ("Agregue su usuario al grupo dialout y vuelva a iniciar sesion:\n"
            "  sudo usermod -aG dialout $USER\n"
            "Cierre QGroundControl / MAVProxy. Si existe ModemManager:\n"
            "  sudo systemctl stop ModemManager")


# ── CSS ───────────────────────────────────────────────────────────────────────
APP_CSS = """
Screen { background: #1a1a1a; }
Header { background: #FF5100; color: white; height: 1; }
Footer { height: 1; }

/* Menu */
#menu_wrap { align: center middle; height: 100%; }
.menu_box {
    width: 60; height: auto;
    border: solid #FF5100;
    padding: 1 3; background: #222222;
}
.menu_title { text-align: center; color: #FF5100; text-style: bold; }
.menu_sub   { text-align: center; color: #555555; }
.menu_conn  { text-align: center; color: #888888; margin-bottom: 1; }

/* Botones */
Button           { width: 100%; margin-top: 1; }
Button.primary   { background: #FF5100; color: white; }
Button.secondary { background: #2a4a6a; color: #ccddee; }
Button.confirm   { background: #1a6a2a; color: white; }
Button.danger    { background: #6a1a1a; color: #ffcccc; }
Button.warning   { background: #6a4a00; color: #ffeeaa; }
/* En la barra de botones, dividir ancho equitativamente */
.btn_bar Button  { width: 1fr; margin-top: 0; }

/* Cal screens -- VerticalScroll debe ser 1fr para que btn_bar quede visible */
AccelCalScreen VerticalScroll,
CompassCalScreen VerticalScroll,
FirmwareScreen VerticalScroll { height: 1fr; }
.cal_scroll { padding: 0 1; }
.cal_title { color: #FF5100; text-style: bold; }
.pos_label { color: #FF5100; text-style: bold; text-align: right; }

.diagram_box {
    border: solid #444444; background: #111111;
    padding: 0 1; height: 7;
}
.instruction_box {
    border: solid #333333; background: #1a1a1a;
    padding: 0 1; height: 4; color: #dddddd;
}
RichLog {
    height: 5; border: solid #2a2a2a; background: #111111;
}
ProgressBar { margin: 0; }
.btn_bar { height: 3; dock: bottom; }

/* Firmware */
.fw_info {
    border: solid #444444; background: #111111;
    padding: 0 1; height: 5;
}
.sel_row { height: 3; margin-bottom: 1; }
Select { width: 1fr; }
"""


class CalScroll(VerticalScroll):
    """VerticalScroll con height:1fr forzado para que los botones siempre sean visibles."""
    DEFAULT_CSS = "CalScroll { height: 1fr; padding: 0 1; }"


# ─────────────────────────────────────────────────────────────────────────────
# Menu Principal
# ─────────────────────────────────────────────────────────────────────────────
class MainMenuScreen(Screen):
    BINDINGS = [
        ("escape", "app.exit", "Salir"),
        ("ctrl+c", "app.exit", "Salir"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center(id="menu_wrap"):
            with Vertical(classes="menu_box"):
                yield Label("Calibracion Pixhawk  v3.0", classes="menu_title")
                yield Label(f"ArduPilot  |  MAVLink v2  |  {'Windows' if IS_WINDOWS else 'Linux'}", classes="menu_sub")
                yield Label("Buscando Pixhawk...", id="conn_label", classes="menu_conn")
                yield Rule(line_style="heavy")
                yield Button("  Calibrar Acelerometro  (6 posiciones)", id="btn_accel",   classes="primary")
                yield Button("  Calibrar Compass  (rotacion libre)",    id="btn_compass", classes="secondary")
                yield Button("  Actualizar Firmware",                   id="btn_fw",      classes="warning")
                yield Button("  Reset / Reboot Pixhawk",                id="btn_reset",   classes="secondary")
                yield Rule()
                yield Button("  Salir  [Esc]",                          id="btn_exit",    classes="danger")
        yield Footer()

    def set_conn_label(self, text: str):
        try:
            self.query_one("#conn_label", Label).update(text)
        except Exception:
            pass

    def _need_conn(self) -> bool:
        if self.app.mavconn is None:
            self.app.notify("Pixhawk no conectado. Verifique el cable USB.", severity="error")
            return False
        return True

    @on(Button.Pressed, "#btn_accel")
    def go_accel(self):
        if self._need_conn():
            self.app.push_screen(AccelCalScreen())

    @on(Button.Pressed, "#btn_compass")
    def go_compass(self):
        if self._need_conn():
            self.app.push_screen(CompassCalScreen())

    @on(Button.Pressed, "#btn_fw")
    def go_firmware(self):
        self.app.push_screen(FirmwareScreen())

    @on(Button.Pressed, "#btn_reset")
    def do_reset(self):
        if self._need_conn():
            self.app.reboot_pixhawk()

    @on(Button.Pressed, "#btn_exit")
    def do_exit(self):
        self.app.exit()


# ─────────────────────────────────────────────────────────────────────────────
# Calibracion Acelerometro
# ─────────────────────────────────────────────────────────────────────────────
class AccelCalScreen(Screen):
    BINDINGS = [
        ("escape", "cancel_or_back", "Cancelar"),
        ("ctrl+c", "app.exit",       "Salir"),
        ("enter",  "confirm_pos",    "Confirmar"),
    ]
    _cal_active = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CalScroll():
            with Horizontal():
                yield Label("CALIBRACION ACELEROMETRO -- 6 POSICIONES", classes="cal_title")
                yield Label("Posicion: --", id="pos_label", classes="pos_label")
            yield Static(id="diagram", classes="diagram_box")
            yield Static(
                "Presione [INICIAR] para comenzar.",
                id="instruction", classes="instruction_box",
            )
            yield Label("Progreso:")
            yield ProgressBar(total=6, show_eta=False, id="accel_bar")
            yield RichLog(id="accel_log", highlight=True, markup=True)
        with Horizontal(classes="btn_bar"):
            yield Button("INICIAR",           id="btn_start",   classes="primary")
            yield Button("CONFIRMAR [Enter]", id="btn_confirm", classes="confirm",  disabled=True)
            yield Button("CANCELAR  [Esc]",   id="btn_cancel",  classes="danger")
        yield Footer()

    def on_mount(self):
        self._confirm_evt = threading.Event()
        self._cancel_evt  = threading.Event()
        self.query_one("#diagram", Static).update("Esperando inicio...")


    # ── helpers UI (llamados siempre desde hilo principal) ─────
    def _log(self, msg: str, style: str = ""):
        ts = time.strftime("%H:%M:%S")
        self.query_one("#accel_log", RichLog).write(
            f"[{ts}] [{style}]{msg}[/{style}]" if style else f"[{ts}] {msg}"
        )

    def _ui_diagram(self, idx: Optional[int]):
        self.query_one("#diagram", Static).update(
            ACCEL_POSITIONS[idx]["diagram"] if idx is not None else "Esperando inicio..."
        )

    def _ui_instruction(self, text: str):
        self.query_one("#instruction", Static).update(text)

    def _ui_pos_label(self, text: str):
        self.query_one("#pos_label", Label).update(text)

    def _ui_confirm_btn(self, enabled: bool):
        self.query_one("#btn_confirm", Button).disabled = not enabled

    def _ui_start_btn(self, enabled: bool):
        self.query_one("#btn_start", Button).disabled = not enabled

    def _ui_advance_bar(self):
        self.query_one("#accel_bar", ProgressBar).advance(1)

    def _ui_reset_bar(self):
        self.query_one("#accel_bar", ProgressBar).update(progress=0)

    # ── acciones ───────────────────────────────────────────────
    @on(Button.Pressed, "#btn_start")
    def start_cal(self):
        self._cal_active = True
        self._cancel_evt.clear()
        self._confirm_evt.clear()
        threading.Thread(target=self._run_accel_cal, daemon=True).start()

    @on(Button.Pressed, "#btn_confirm")
    def btn_confirm(self):
        self._confirm_evt.set()

    @on(Button.Pressed, "#btn_cancel")
    def btn_cancel(self):
        self._do_cancel()

    def action_cancel_or_back(self):
        self._do_cancel()

    def action_confirm_pos(self):
        if not self.query_one("#btn_confirm", Button).disabled:
            self._confirm_evt.set()

    def _do_cancel(self):
        if self._cal_active:
            self._cancel_evt.set()
            self._confirm_evt.set()   # desbloquea .wait() si el hilo espera
        else:
            self.app.pop_screen()

    # ── logica MAVLink en hilo separado ────────────────────────
    def _run_accel_cal(self):
        conn = self.app.mavconn
        self.app.call_from_thread(self._ui_start_btn, False)
        self.app.call_from_thread(self._log, "Iniciando calibracion de acelerometro...")

        conn.mav.command_long_send(
            conn.target_system, conn.target_component,
            MAV_CMD_PREFLIGHT_CALIBRATION, 0,
            1, 0, 0, 0, 0, 0, 0,
        )
        ack = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=6)
        if ack is None or ack.result != MAV_RESULT_ACCEPTED:
            self.app.call_from_thread(self._log,
                f"Rechazado (result={ack.result if ack else 'TIMEOUT'}). Armado?", "bold red")
            self.app.call_from_thread(self._finish_accel, False)
            return

        self.app.call_from_thread(self._log, "Calibracion aceptada.", "green")

        for idx, pos in enumerate(ACCEL_POSITIONS):
            if self._cancel_evt.is_set():
                break

            self.app.call_from_thread(self._ui_diagram,      idx)
            self.app.call_from_thread(self._ui_instruction,  pos["instruction"])
            self.app.call_from_thread(self._ui_pos_label,    f"Pos {idx+1}/6  {pos['name']}")
            self.app.call_from_thread(self._log,             f">> Posicion {idx+1}/6: {pos['name']}")
            self.app.call_from_thread(self._ui_confirm_btn,  True)

            self._confirm_evt.clear()
            self._confirm_evt.wait()          # espera Enter/boton o cancelacion
            self.app.call_from_thread(self._ui_confirm_btn, False)

            if self._cancel_evt.is_set():
                break

            self.app.call_from_thread(self._log, f"Capturando {pos['short']}...", "yellow")
            conn.mav.command_long_send(
                conn.target_system, conn.target_component,
                MAV_CMD_ACCELCAL_VEHICLE_POS, 0,
                float(pos["id"]), 0, 0, 0, 0, 0, 0,
            )

            deadline = time.time() + 15
            while time.time() < deadline and not self._cancel_evt.is_set():
                msg = conn.recv_match(type=["STATUSTEXT"], blocking=True, timeout=0.3)
                if msg:
                    text = msg.text.strip()
                    self.app.call_from_thread(self._log, f"  {text}", "cyan")
                    tl = text.lower()
                    if any(kw in tl for kw in ["place", "level", "left", "right", "nose", "back", "done"]):
                        break
                    if "fail" in tl or "error" in tl:
                        self._cancel_evt.set()
                        break

            if not self._cancel_evt.is_set():
                self.app.call_from_thread(self._ui_advance_bar)
                self.app.call_from_thread(self._log, f"  [ok] {pos['short']} capturado.", "green")

        if self._cancel_evt.is_set():
            conn.mav.command_long_send(
                conn.target_system, conn.target_component,
                MAV_CMD_PREFLIGHT_CALIBRATION, 0, 0, 0, 0, 0, 0, 0, 0,
            )
            self.app.call_from_thread(self._log, "Cancelado.", "yellow")
            self.app.call_from_thread(self._finish_accel, False)
            return

        self.app.call_from_thread(self._ui_instruction, "Procesando datos -- espere...")
        success = False
        deadline = time.time() + 25
        while time.time() < deadline:
            msg = conn.recv_match(type="STATUSTEXT", blocking=True, timeout=0.3)
            if msg:
                text = msg.text.strip()
                self.app.call_from_thread(self._log, f"  {text}", "cyan")
                if "success" in text.lower() or "complete" in text.lower():
                    success = True
                    break
                if "fail" in text.lower():
                    break

        self.app.call_from_thread(self._finish_accel, success)

    def _finish_accel(self, success: bool):
        self._cal_active = False
        self._ui_start_btn(True)
        self._ui_confirm_btn(False)
        if success:
            self._ui_pos_label("[ok] COMPLETADO -- reiniciando...")
            self._log("Calibracion OK. Enviando reboot...", "green")
            self.app.reboot_pixhawk()
            self._ui_instruction("[ok] Acelerometro calibrado. Pixhawk reiniciado. [CANCELAR] para volver.")
            self._ui_pos_label("[ok] COMPLETADO")
        else:
            self._ui_instruction("[x] Cancelado o fallido. [INICIAR] para reintentar.")
            self._ui_pos_label("[x] CANCELADO")
            self._ui_reset_bar()
            self._ui_diagram(None)


# ─────────────────────────────────────────────────────────────────────────────
# Calibracion Compass
# ─────────────────────────────────────────────────────────────────────────────
class CompassCalScreen(Screen):
    BINDINGS = [
        ("escape", "cancel_or_back", "Cancelar"),
        ("ctrl+c", "app.exit",       "Salir"),
    ]
    _cal_active = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CalScroll():
            yield Label("CALIBRACION DE COMPASS -- ROTACION LIBRE", classes="cal_title")
            yield Static(COMPASS_DIAGRAM, id="compass_diagram", classes="diagram_box")
            yield Static(
                "Presione [INICIAR] para comenzar.\n"
                "Rote el Pixhawk en todas las orientaciones.",
                id="compass_instr", classes="instruction_box",
            )
            yield Label("Progreso:")
            yield ProgressBar(total=100, show_eta=False, id="compass_bar")
            yield RichLog(id="compass_log", highlight=True, markup=True)
        with Horizontal(classes="btn_bar"):
            yield Button("INICIAR",         id="btn_cmp_start",  classes="primary")
            yield Button("CANCELAR  [Esc]", id="btn_cmp_cancel", classes="danger")
        yield Footer()

    def on_mount(self):
        self._cancel_evt = threading.Event()


    def _log(self, msg: str, style: str = ""):
        ts = time.strftime("%H:%M:%S")
        self.query_one("#compass_log", RichLog).write(
            f"[{ts}] [{style}]{msg}[/{style}]" if style else f"[{ts}] {msg}"
        )

    def _ui_instr(self, text: str):
        self.query_one("#compass_instr", Static).update(text)

    def _ui_progress(self, pct: int):
        self.query_one("#compass_bar", ProgressBar).update(progress=pct)

    def _ui_start_btn(self, enabled: bool):
        self.query_one("#btn_cmp_start", Button).disabled = not enabled

    @on(Button.Pressed, "#btn_cmp_start")
    def start_compass(self):
        self._cal_active = True
        self._cancel_evt.clear()
        self._ui_progress(0)
        threading.Thread(target=self._run_compass_cal, daemon=True).start()

    @on(Button.Pressed, "#btn_cmp_cancel")
    def cancel_btn(self):
        self._do_cancel()

    def action_cancel_or_back(self):
        self._do_cancel()

    def _do_cancel(self):
        if self._cal_active:
            self._cancel_evt.set()
        else:
            self.app.pop_screen()

    def _run_compass_cal(self):
        conn = self.app.mavconn
        self.app.call_from_thread(self._ui_start_btn, False)
        self.app.call_from_thread(self._log, "Enviando comando calibracion compass...")

        conn.mav.command_long_send(
            conn.target_system, conn.target_component,
            MAV_CMD_DO_START_MAG_CAL, 0,
            0, 1, 1, 0, 0, 0, 0,
        )
        ack = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
        accepted = ack is not None and ack.result == MAV_RESULT_ACCEPTED

        if not accepted:
            self.app.call_from_thread(self._log,
                f"DO_START_MAG_CAL result={ack.result if ack else 'TIMEOUT'}, probando PREFLIGHT...",
                "yellow")
            conn.mav.command_long_send(
                conn.target_system, conn.target_component,
                MAV_CMD_PREFLIGHT_CALIBRATION, 0,
                0, 1, 0, 0, 0, 0, 0,
            )
            ack2 = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
            accepted = ack2 is not None and ack2.result == MAV_RESULT_ACCEPTED
            if not accepted:
                self.app.call_from_thread(self._log, "No se pudo iniciar. Verifique conexion.", "bold red")
                self.app.call_from_thread(self._finish_compass, False, 0.0)
                return

        self.app.call_from_thread(self._log, "Calibracion iniciada.", "green")
        self.app.call_from_thread(self._ui_instr,
            "GIRANDO -- Rote en todas las orientaciones:\n"
            "  Yaw: rotaciones horizontales | Pitch: nariz up/down | Roll: costados")

        last_pct = -1
        deadline = time.time() + 180
        while time.time() < deadline and not self._cancel_evt.is_set():
            msg = conn.recv_match(
                type=["MAG_CAL_PROGRESS", "MAG_CAL_REPORT", "STATUSTEXT"],
                blocking=True, timeout=0.4,
            )
            if msg is None:
                continue
            mt = msg.get_type()
            if mt == "MAG_CAL_PROGRESS":
                pct = int(getattr(msg, "completion_pct", 0))
                if pct != last_pct:
                    last_pct = pct
                    self.app.call_from_thread(self._ui_progress, pct)
                    self.app.call_from_thread(self._log, f"Compass: {pct}%")
            elif mt == "MAG_CAL_REPORT":
                success = getattr(msg, "cal_status", 1) == 0
                fitness = float(getattr(msg, "fitness", 0.0))
                self.app.call_from_thread(self._finish_compass, success, fitness)
                return
            elif mt == "STATUSTEXT":
                self.app.call_from_thread(self._log, f"  {msg.text.strip()}", "cyan")

        if self._cancel_evt.is_set():
            conn.mav.command_long_send(
                conn.target_system, conn.target_component,
                MAV_CMD_DO_CANCEL_MAG_CAL, 0, 0, 0, 0, 0, 0, 0, 0,
            )
            self.app.call_from_thread(self._log, "Cancelado.", "yellow")
        else:
            self.app.call_from_thread(self._log, "Timeout (3 min). No completado.", "bold red")

        self.app.call_from_thread(self._finish_compass, False, 0.0)

    def _finish_compass(self, success: bool, fitness: float):
        self._cal_active = False
        self._ui_start_btn(True)
        if success:
            self._ui_progress(100)
            self._log("Calibracion OK. Enviando reboot...", "green")
            self.app.reboot_pixhawk()
            self._ui_instr(
                f"[ok] Compass calibrado. Fitness: {fitness:.4f} (menor = mejor)\n"
                "Pixhawk reiniciado. [CANCELAR] para volver."
            )
            self.app.notify(f"Compass calibrado. Fitness={fitness:.4f}", severity="information")
        else:
            self._ui_instr("[x] Cancelado o fallido. [INICIAR] para reintentar.")


# ─────────────────────────────────────────────────────────────────────────────
# Actualizacion Firmware
# ─────────────────────────────────────────────────────────────────────────────
class FirmwareScreen(Screen):
    """Pantalla de actualizacion de firmware ArduPilot estable.

    Flujo: detectar board_id/vehiculo del Pixhawk -> VERIFICAR (resuelve la
    version estable del vehiculo/board elegido) -> DESCARGAR Y FLASHEAR (descarga,
    valida el .apj contra el board y el release, y lo sube con uploader.py).
    """
    BINDINGS = [
        ("escape", "go_back", "Volver"),
        ("ctrl+c", "app.exit", "Salir"),
    ]
    _busy = False
    #: board_id reportado por el Pixhawk conectado (None = desconocido).
    _hw_board_id: Optional[int] = None
    #: Firmware resuelto por el ultimo VERIFICAR; se invalida al cambiar la seleccion.
    _resolved: Optional[StableFirmware] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CalScroll():
            yield Label("ACTUALIZACION DE FIRMWARE ARDUPILOT (STABLE)", classes="cal_title")
            yield Static("Version actual: --\nBoard: --",
                         id="fw_current", classes="fw_info")
            with Horizontal(classes="sel_row"):
                yield Select(
                    [(lbl, val) for lbl, val in VEHICLE_OPTIONS],
                    prompt="Vehiculo", id="sel_vehicle", value="Copter",
                    allow_blank=False,
                )
                yield Select(
                    [(lbl, val) for lbl, val in BOARD_OPTIONS],
                    prompt="Board", id="sel_board", value="fmuv3",
                    allow_blank=False,
                )
            yield Static("Ultima version: --", id="fw_latest", classes="fw_info")
            yield ProgressBar(total=100, show_eta=False, id="fw_bar")
            yield RichLog(id="fw_log", highlight=True, markup=True)
        with Horizontal(classes="btn_bar"):
            yield Button("VERIFICAR VERSION",    id="btn_fw_check", classes="secondary")
            yield Button("DESCARGAR Y FLASHEAR", id="btn_fw_flash", classes="primary", disabled=True)
            yield Button("VOLVER  [Esc]",        id="btn_fw_back",  classes="danger")
        yield Footer()

    def on_mount(self):
        if self.app.mavconn is not None:
            threading.Thread(target=self._detect_version, daemon=True).start()

    def _log(self, msg: str, style: str = ""):
        ts = time.strftime("%H:%M:%S")
        self.query_one("#fw_log", RichLog).write(
            f"[{ts}] [{style}]{msg}[/{style}]" if style else f"[{ts}] {msg}"
        )

    def _ui_current(self, text: str):
        self.query_one("#fw_current", Static).update(text)

    def _ui_latest(self, text: str):
        self.query_one("#fw_latest", Static).update(text)

    def _ui_progress(self, pct: int):
        self.query_one("#fw_bar", ProgressBar).update(progress=pct)

    def _ui_flash_btn(self, enabled: bool):
        self.query_one("#btn_fw_flash", Button).disabled = not enabled

    def _ui_check_btn(self, enabled: bool):
        self.query_one("#btn_fw_check", Button).disabled = not enabled

    def _ui_select(self, sel_id: str, value: str):
        self.query_one(sel_id, Select).value = value

    def _selection(self) -> tuple:
        """Devuelve (vehiculo, board) seleccionados. Llamar desde el hilo de UI."""
        vehicle = self.query_one("#sel_vehicle", Select).value
        board = self.query_one("#sel_board", Select).value
        vehicle = vehicle if vehicle in FIRMWARE_TARGETS else "Copter"
        board = board if board in BOARD_IDS else "fmuv3"
        return vehicle, board

    @on(Select.Changed)
    def _selection_changed(self, _event: Select.Changed):
        """Cambiar vehiculo/board invalida la verificacion previa."""
        self._resolved = None
        self._ui_flash_btn(False)
        self._ui_latest("Ultima version: --  (pulse VERIFICAR VERSION)")

    def _detect_version(self):
        """Lee AUTOPILOT_VERSION (version + board_id) y preselecciona board/vehiculo."""
        conn = self.app.mavconn
        self.app.call_from_thread(self._log, "Leyendo version firmware actual...")
        try:
            conn.mav.command_long_send(
                conn.target_system, conn.target_component,
                MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES, 0,
                1, 0, 0, 0, 0, 0, 0,
            )
            msg = conn.recv_match(type="AUTOPILOT_VERSION", blocking=True, timeout=5)
        except Exception as e:
            self.app.call_from_thread(self._log, f"Error leyendo version: {e}", "yellow")
            msg = None
        if not msg:
            self.app.call_from_thread(self._ui_current,
                "No se pudo leer la version.\nVerifique manualmente el board elegido.")
            return

        v = msg.flight_sw_version
        major, minor, patch = (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF
        vt = {0: "dev", 64: "alpha", 128: "beta", 192: "rc", 255: "stable"}.get(v & 0xFF, "?")
        gh = bytes(msg.flight_custom_version[:8]).decode("ascii", errors="replace").strip("\x00")
        # ArduPilot envia board_version = APJ_BOARD_ID << 16 (GCS_Common.cpp).
        board_id = (msg.board_version >> 16) & 0xFFFF
        compatible = boards_for_id(board_id) if board_id else []
        self._hw_board_id = board_id or None
        self.app.call_from_thread(self._ui_current,
            f"Version actual:  v{major}.{minor}.{patch} ({vt})   Git: {gh}\n"
            f"Board ID:        {board_id or '?'}   "
            f"Targets compatibles: {', '.join(compatible) or 'desconocido'}")
        self.app.call_from_thread(self._log,
            f"Firmware: v{major}.{minor}.{patch}  board_id={board_id}", "cyan")

        if compatible:
            self.app.call_from_thread(self._preselect_board, compatible)
        hb = conn.messages.get("HEARTBEAT") if hasattr(conn, "messages") else None
        vehicle = MAV_TYPE_TO_VEHICLE.get(getattr(hb, "type", None))
        if vehicle:
            self.app.call_from_thread(self._ui_select, "#sel_vehicle", vehicle)
            self.app.call_from_thread(self._log, f"Vehiculo detectado: {vehicle}", "cyan")

    def _preselect_board(self, compatible: list):
        """Si el board elegido no es compatible con el hardware, elige el primero que si."""
        _, board = self._selection()
        if board not in compatible:
            self._ui_select("#sel_board", compatible[0])
            self._log(f"Board ajustado a {compatible[0]} segun el hardware.", "yellow")

    @on(Button.Pressed, "#btn_fw_check")
    def check_latest(self):
        if self._busy:
            return
        self._busy = True
        self._ui_flash_btn(False)
        vehicle, board = self._selection()
        threading.Thread(target=self._do_check, args=(vehicle, board), daemon=True).start()

    def _hw_mismatch(self, board: str) -> Optional[str]:
        """Mensaje de error si el board elegido no coincide con el hardware, o None."""
        if self._hw_board_id is None or BOARD_IDS[board] == self._hw_board_id:
            return None
        return (f"El Pixhawk reporta board_id {self._hw_board_id} pero '{board}' es "
                f"board_id {BOARD_IDS[board]}. Compatibles: "
                f"{', '.join(boards_for_id(self._hw_board_id)) or 'ninguno de la lista'}")

    def _do_check(self, vehicle: str, board: str):
        self.app.call_from_thread(self._log, f"Consultando {vehicle}/{FW_CHANNEL}/{board}...")
        try:
            mismatch = self._hw_mismatch(board)
            if mismatch:
                raise RuntimeError(mismatch)
            fw = resolve_stable_firmware(vehicle, board)
        except Exception as e:
            self.app.call_from_thread(self._log, f"Error: {e}", "bold red")
            self.app.call_from_thread(self._ui_latest, f"Ultima version: ERROR\n  {e}")
            self._busy = False
            return

        self._resolved = fw
        self.app.call_from_thread(self._ui_latest,
            f"Ultima version estable ({vehicle}/{board}):\n  {fw.version}  "
            f"(git {fw.git_hash[:8]})\n  URL: {fw.url}")
        self.app.call_from_thread(self._log, f"Disponible: {fw.version}", "green")
        self.app.call_from_thread(self._ui_flash_btn, True)
        self._busy = False

    @on(Button.Pressed, "#btn_fw_flash")
    def flash_fw(self):
        if self._busy or self._resolved is None:
            return
        self._busy = True
        self._ui_flash_btn(False)
        self._ui_check_btn(False)
        threading.Thread(target=self._do_flash, args=(self._resolved,), daemon=True).start()

    def _fail(self, msg: str):
        """Registra un error y termina el flasheo como fallido (hilo worker)."""
        self.app.call_from_thread(self._log, msg, "bold red")
        self.app.call_from_thread(self._finish_flash, False)

    def _do_flash(self, fw: StableFirmware):
        port = self.app._port or self.app._detected_port
        if not port:
            self._fail("No hay puerto serie detectado.")
            return
        mismatch = self._hw_mismatch(fw.board)
        if mismatch:
            self._fail(mismatch)
            return

        fw_path = os.path.join(tempfile.gettempdir(), f"ardupilot_{fw.vehicle}_{fw.board}.apj")
        up_path = os.path.join(tempfile.gettempdir(), "ardupilot_uploader.py")

        # 1. uploader.py del mismo commit que el firmware
        self.app.call_from_thread(self._log, "Descargando uploader.py oficial...")
        self.app.call_from_thread(self._ui_progress, 5)
        try:
            with open(up_path, "wb") as f:
                f.write(download_uploader(fw.git_hash))
        except Exception as e:
            self._fail(f"Error: {e}")
            return

        # 2. Firmware + validacion
        self.app.call_from_thread(self._log, f"Descargando {fw.url} ...")
        try:
            def _progress(read, total):
                if total > 0:
                    self.app.call_from_thread(self._ui_progress,
                        min(55, 10 + int(45 * read / total)))
            data = http_get(fw.url, progress=_progress)
            meta = validate_apj(data, fw)
            with open(fw_path, "wb") as f:
                f.write(data)
        except Exception as e:
            self._fail(f"Error: {e}")
            return
        self.app.call_from_thread(self._log,
            f"Firmware valido: {fw.version}  board_id={meta['board_id']}  "
            f"git={meta['git_identity']}  imagen={meta['image_size'] // 1024} KB", "green")
        self.app.call_from_thread(self._ui_progress, 60)

        # 3. Liberar puerto: cerrar otros GCS y reiniciar a bootloader por MAVLink2
        killed, others = release_serial_port(port)
        if killed:
            self.app.call_from_thread(self._log,
                f"Procesos cerrados: {', '.join(killed)}", "yellow")
        if others:
            self.app.call_from_thread(self._log,
                f"Aviso: {port} tambien abierto por {', '.join(others)}", "bold yellow")

        bl_ports = []
        conn, self.app.mavconn = self.app.mavconn, None
        if conn is not None:
            self.app.call_from_thread(self._log, "Reiniciando Pixhawk en modo bootloader...")
            try:
                bl_ports = reboot_to_bootloader(conn, port)
            except Exception as e:
                self.app.call_from_thread(self._log, f"Reinicio MAVLink fallo: {e}", "yellow")
            if bl_ports:
                self.app.call_from_thread(self._log,
                    f"Bootloader detectado en {', '.join(bl_ports)}", "cyan")
        else:
            self.app.call_from_thread(self._log,
                "Sin conexion MAVLink: uploader.py intentara reiniciar la placa.", "yellow")

        # 4. Flasheo (uploader.py tambien verifica board_id contra el bootloader)
        ports = autopilot_ports(port, bl_ports)
        self.app.call_from_thread(self._ui_progress, 65)
        self.app.call_from_thread(self._log,
            f"Flasheando via {ports}... (no desconecte el USB)", "yellow")

        output = []
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", up_path, "--port", ports, fw_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as e:
            self._fail(f"Error: {e}")
            return

        # Watchdog: readline() bloquea si uploader.py queda buscando el bootloader,
        # por eso el timeout se aplica matando el proceso desde otro hilo.
        timed_out = threading.Event()
        def _kill():
            timed_out.set()
            proc.kill()
        watchdog = threading.Timer(FLASH_TIMEOUT_S, _kill)
        watchdog.start()
        fatal = False
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if line:
                    output.append(line)
                    self.app.call_from_thread(self._log, line, "cyan")
                    # uploader.py reintenta en bucle ante errores definitivos (board
                    # incorrecto, imagen grande): cortar en vez de esperar el watchdog.
                    if any(p in line for p in UPLOADER_FATAL_PATTERNS):
                        fatal = True
                        proc.kill()
                        break
            proc.wait()
        finally:
            watchdog.cancel()

        if timed_out.is_set() or fatal or proc.returncode != 0:
            if timed_out.is_set():
                self.app.call_from_thread(self._log,
                    f"Timeout ({FLASH_TIMEOUT_S} s): no se encontro el bootloader. "
                    "Desconecte y reconecte el USB y reintente.", "bold red")
            else:
                self._log_flash_hint("\n".join(output), fw)
            # La placa pudo quedar retenida en el bootloader: volver al firmware instalado
            sent = exit_bootloader(bl_ports)
            if sent:
                self.app.call_from_thread(self._log,
                    f"Pixhawk devuelto al firmware instalado ({', '.join(sent)}).", "yellow")
            self.app.call_from_thread(self._finish_flash, False)
            return
        self.app.call_from_thread(self._ui_progress, 100)
        self.app.call_from_thread(self._finish_flash, True)

    def _log_flash_hint(self, output: str, fw: StableFirmware):
        """Traduce errores conocidos de uploader.py a una accion concreta."""
        low = output.lower()
        if "too large" in low and BOARD_IDS[fw.board] == 9:
            hint = ("El chip tiene el limite de 1MB (errata STM32F427 rev.3). "
                    "Elija el board 'Pixhawk1-1M' y reintente.")
        elif "not suitable for this board" in low:
            hint = "El bootloader rechazo el firmware: el board elegido no coincide con el hardware."
        elif "access is denied" in low or "acceso denegado" in low or "permission" in low:
            hint = "Sin acceso al puerto serie. " + serial_help().replace("\n", " ")
        elif "modemmanager" in low or "brltty" in low:
            hint = ("ModemManager/brltty interfiere con el bootloader: "
                    "sudo systemctl stop ModemManager y reintente.")
        else:
            hint = "Revise el log de uploader.py arriba."
        self.app.call_from_thread(self._log, hint, "bold yellow")

    def _finish_flash(self, success: bool):
        """Cierra el flasheo (hilo de UI). uploader.py ya reinicia el Pixhawk al terminar.

        Si la conexion MAVLink se cerro para flashear (exito o fallo), reconecta en 8 s.
        """
        self._busy = False
        self._ui_check_btn(True)
        if success:
            self._log("[ok] Firmware actualizado. Reconectando en 8 s...", "bold green")
            self.app.notify("Firmware actualizado. Pixhawk reiniciando...", severity="information")
        else:
            self._log("[x] Flasheo fallido.", "bold red")
            self._ui_flash_btn(self._resolved is not None)
        if self.app.mavconn is None:
            def _reconnect():
                time.sleep(8)
                self.app._connect_mavlink()
            threading.Thread(target=_reconnect, daemon=True).start()

    @on(Button.Pressed, "#btn_fw_back")
    def go_back_btn(self):
        if not self._busy:
            self.app.pop_screen()

    def action_go_back(self):
        if not self._busy:
            self.app.pop_screen()


# ─────────────────────────────────────────────────────────────────────────────
# App Principal
# ─────────────────────────────────────────────────────────────────────────────
class PixhawkCalApp(App):
    TITLE = "Calibracion Pixhawk  v3.2"
    CSS   = APP_CSS
    BINDINGS = [("ctrl+c", "app.exit", "Salir")]

    def __init__(self, port: Optional[str], baud: int):
        super().__init__()
        self._port          = port
        self._baud          = baud
        self._detected_port: Optional[str] = None
        self.mavconn: Optional[mavutil.mavfile] = None

    def on_mount(self):
        self.push_screen(MainMenuScreen())
        threading.Thread(target=self._connect_mavlink, daemon=True).start()

    def reboot_pixhawk(self):
        """Envia MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN para reiniciar el Pixhawk."""
        conn = self.mavconn
        if conn is None:
            return
        try:
            conn.mav.command_long_send(
                conn.target_system, conn.target_component,
                MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
                0,
                1,   # param1=1 reboot autopilot
                0, 0, 0, 0, 0, 0,
            )
            self.notify("Pixhawk reiniciando...", severity="information")
        except Exception as exc:
            self.notify(f"Error al reiniciar: {exc}", severity="warning")

    def on_exit(self):
        """Limpieza al cerrar: terminar conexion MAVLink."""
        if self.mavconn:
            try:
                self.mavconn.close()
            except Exception:
                pass

    def _connect_mavlink(self):
        def _lbl(text: str):
            if isinstance(self.screen, MainMenuScreen):
                self.screen.set_conn_label(text)

        self.app.call_from_thread(_lbl, "Buscando Pixhawk en puertos serie...")

        port = self._port or detect_port()
        self._detected_port = port

        if port is None:
            self.app.call_from_thread(_lbl, "x No se encontro puerto serie con Pixhawk.")
            self.app.call_from_thread(self.notify,
                "No se encontro Pixhawk.\nVerifique el cable USB.\n" + serial_help(),
                title="Sin dispositivo", severity="error")
            return

        self.app.call_from_thread(_lbl, f"Conectando a {port} @ {self._baud} baud...")
        try:
            conn = mavutil.mavlink_connection(
                port, baud=self._baud,
                autoreconnect=True,
                source_system=255, source_component=190,
            )
            hb = conn.wait_heartbeat(timeout=15)
            if hb is None:
                self.app.call_from_thread(_lbl, f"x Sin heartbeat en {port}. Baud incorrecto?")
                return

            conn.mav.request_data_stream_send(
                conn.target_system, conn.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1,
            )
            self.mavconn = conn
            self._detected_port = port
            self.app.call_from_thread(_lbl,
                f"[ok] {port}  SysID:{conn.target_system}  AP:{hb.autopilot}")
            self.app.call_from_thread(self.notify,
                f"Pixhawk conectado en {port}",
                title="Conectado", severity="information")

        except PermissionError:
            self.app.call_from_thread(_lbl, f"x {port}: sin acceso (en uso o sin permisos).")
            self.app.call_from_thread(self.notify,
                f"{port} esta bloqueado.\n" + serial_help(),
                title="Puerto ocupado", severity="error")
        except Exception as exc:
            msg = str(exc).lower()
            # Linux: pyserial reporta permisos como "could not open port ...: [Errno 13]",
            # por eso se evalua antes que el caso "could not open" (puerto inexistente).
            if ("access is denied" in msg or "acceso denegado" in msg
                    or "permission denied" in msg or "errno 13" in msg):
                self.app.call_from_thread(_lbl, f"x {port}: acceso denegado.")
                self.app.call_from_thread(self.notify,
                    f"Acceso denegado a {port}.\n" + serial_help(),
                    title="Acceso denegado", severity="error")
            elif "could not open" in msg or "no such file" in msg:
                self.app.call_from_thread(_lbl, f"x {port} no encontrado.")
                self.app.call_from_thread(self.notify,
                    f"Puerto {port} no existe.\n"
                    + ("Verifique Administrador de dispositivos." if IS_WINDOWS
                       else "Verifique con: ls -l /dev/ttyACM* /dev/ttyUSB*"),
                    title="Puerto no encontrado", severity="error")
            else:
                self.app.call_from_thread(_lbl, f"x Error: {exc}")
                self.app.call_from_thread(self.notify,
                    str(exc), title="Error de conexion", severity="error")


# ─────────────────────────────────────────────────────────────────────────────
# Test de botones (headless)
# ─────────────────────────────────────────────────────────────────────────────
async def _run_button_tests():
    """Prueba todos los botones usando el piloto de Textual (sin hardware)."""
    import unittest.mock as mock

    # Mock de mavutil para no necesitar puerto real
    mock_conn = mock.MagicMock()
    mock_conn.target_system = 1
    mock_conn.target_component = 0
    mock_conn.mav = mock.MagicMock()
    mock_hb = mock.MagicMock()
    mock_hb.autopilot = 3
    mock_conn.wait_heartbeat.return_value = mock_hb

    app = PixhawkCalApp(port="COM_TEST", baud=115200)
    app.mavconn = mock_conn   # inyectar conexion mock

    results = {}

    P = 1.0   # pausa entre acciones (pop_screen necesita tick de event loop)

    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause(P)

        # 1. Boton Acelerometro -> AccelCalScreen
        try:
            await pilot.click("#btn_accel")
            await pilot.pause(P)
            results["btn_accel -> AccelCalScreen"] = isinstance(app.screen, AccelCalScreen)
        except Exception as e:
            results["btn_accel -> AccelCalScreen"] = f"ERROR: {e}"

        # 2. Boton CANCELAR -> vuelve al menu
        try:
            await pilot.click("#btn_cancel")
            await pilot.pause(P)
            results["btn_cancel -> MainMenuScreen"] = isinstance(app.screen, MainMenuScreen)
        except Exception as e:
            results["btn_cancel -> MainMenuScreen"] = f"ERROR: {e}"

        # 3. Boton Compass -> CompassCalScreen
        try:
            await pilot.click("#btn_compass")
            await pilot.pause(P)
            results["btn_compass -> CompassCalScreen"] = isinstance(app.screen, CompassCalScreen)
        except Exception as e:
            results["btn_compass -> CompassCalScreen"] = f"ERROR: {e}"

        # 4. ESC en CompassCalScreen -> vuelve al menu
        try:
            await pilot.press("escape")
            await pilot.pause(P)
            results["ESC compass -> MainMenuScreen"] = isinstance(app.screen, MainMenuScreen)
        except Exception as e:
            results["ESC compass -> MainMenuScreen"] = f"ERROR: {e}"

        # 5. Boton Firmware -> FirmwareScreen
        try:
            await pilot.click("#btn_fw")
            await pilot.pause(P)
            results["btn_fw -> FirmwareScreen"] = isinstance(app.screen, FirmwareScreen)
        except Exception as e:
            results["btn_fw -> FirmwareScreen"] = f"ERROR: {e}"

        # 6. ESC en FirmwareScreen -> vuelve al menu
        try:
            await pilot.press("escape")
            await pilot.pause(P)
            results["ESC firmware -> MainMenuScreen"] = isinstance(app.screen, MainMenuScreen)
        except Exception as e:
            results["ESC firmware -> MainMenuScreen"] = f"ERROR: {e}"

        # 7. Boton RESET -> llama reboot_pixhawk (verifica que no lanza excepcion y manda el comando)
        try:
            cmd_calls_before = mock_conn.mav.command_long_send.call_count
            await pilot.click("#btn_reset")
            await pilot.pause(P)
            called = mock_conn.mav.command_long_send.call_count > cmd_calls_before
            # Verificar que el argumento del ultimo comando fue MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN (246)
            if called:
                last_call_args = mock_conn.mav.command_long_send.call_args[0]
                cmd_id = last_call_args[2]
                results["btn_reset -> MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN"] = (
                    cmd_id == MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN
                )
            else:
                results["btn_reset -> MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN"] = "NO SE LLAMO command_long_send"
        except Exception as e:
            results["btn_reset -> MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN"] = f"ERROR: {e}"

        # 8. Boton SALIR cierra la app
        try:
            await pilot.click("#btn_exit")
            await pilot.pause(P)
            results["btn_exit -> app cerrada"] = "OK"
        except Exception as e:
            results["btn_exit -> app cerrada"] = f"ERROR: {e}"

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Calibracion y actualizacion de firmware Pixhawk -- Windows / Linux"
    )
    parser.add_argument("--port", "-p", default=None,
                        help="Puerto serie (ej: COM3 o /dev/ttyACM0). Omitir para auto-detectar.")
    parser.add_argument("--baud", "-b", type=int, default=115200,
                        help="Baud rate (default: 115200)")
    parser.add_argument("--test-buttons", action="store_true",
                        help="Ejecutar prueba automatica de botones (headless)")
    parser.add_argument("--check-firmware", action="store_true",
                        help="Validar URLs de todos los firmwares estables (sin hardware)")
    args = parser.parse_args()

    if args.check_firmware:
        print(f"Validando firmwares {FW_CHANNEL} en {FW_BASE} ...")
        ok = check_all_firmware_urls()
        print("\nTodos los firmwares disponibles" if ok else "\nHAY FALLOS -- revisar arriba")
        sys.exit(0 if ok else 1)

    if args.test_buttons:
        import asyncio
        print("Ejecutando prueba de botones...")
        results = asyncio.run(_run_button_tests())
        print("\n=== RESULTADO PRUEBA DE BOTONES ===")
        all_ok = True
        for test, result in results.items():
            ok = result is True or result == "OK"
            status = "[OK]" if ok else "[FALLO]"
            print(f"  {status}  {test}: {result}")
            if not ok:
                all_ok = False
        print(f"\n{'Todos los botones OK' if all_ok else 'HAY FALLOS -- revisar arriba'}")
        return

    PixhawkCalApp(port=args.port, baud=args.baud).run()


if __name__ == "__main__":
    main()
