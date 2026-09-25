#!/usr/bin/env python3
# Monitor de telemetria Pixhawk via MAVLink/USB: actitud, compass, GPS y bateria.
# @author: Carlos Briceño <carjavi@hotmail.com>
# @date: 24-09-2026
# @copyright: Copyright (c) 2026 www.carjavi.com
# @version: V2.2
# @library:
# - pip install pymavlink pyserial
"""
pixhawk_monitor.py  v2.2
========================
Monitor de telemetría Pixhawk via MAVLink/USB

Captura y muestra en loop infinito:
  • Giroscopio : Pitch / Roll / Yaw  (grados)
  • Compass    : Heading (N/E/S/W + grados) y origen (interno / GPS externo)
  • GPS        : Fix, satélites, lat/lon, altitud, HDOP, velocidad
  • Voltaje    : Voltios
  • Corriente  : Amperes

Dependencias:
    pip install pymavlink pyserial

Uso:
    python pixhawk_monitor.py                      # auto-detecta puerto


Install:
    pip install pymavlink pyserial
"""

import argparse
import glob
import math
import sys
import time

# ──────────────────────────────────────────────
# Forzar salida UTF-8 (Git Bash/MinTTY no usa la consola
# nativa de Windows y cae a cp1252, rompiendo los caracteres
# especiales de este script)
# ──────────────────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ──────────────────────────────────────────────
# Verificar dependencias antes de continuar
# ──────────────────────────────────────────────
def check_dependencies():
    missing = []
    try:
        from pymavlink import mavutil  # noqa: F401
    except ImportError:
        missing.append("pymavlink")
    try:
        import serial  # noqa: F401
    except ImportError:
        missing.append("pyserial")

    if missing:
        print("\n[ERROR] Faltan dependencias:")
        for pkg in missing:
            print(f"        pip install {pkg}")
        print("\nInstale todo de una vez:")
        print("        pip install pymavlink pyserial\n")
        sys.exit(1)

check_dependencies()
from pymavlink import mavutil  # noqa: E402

# ──────────────────────────────────────────────
# Colores ANSI (Windows 10+ los soporta)
# ──────────────────────────────────────────────
def _enable_windows_ansi():
    """Activa secuencias ANSI en la consola de Windows."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

_enable_windows_ansi()

USE_COLOR = True  # Windows 10+ soporta ANSI en cmd/PowerShell/Terminal
C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_ORANGE = "\033[38;2;255;81;0m"    # #FF5100 Maquintel
C_GRAY   = "\033[38;2;180;180;180m"
C_CYAN   = "\033[96m"
C_GREEN  = "\033[92m"
C_YELLOW = "\033[93m"
C_RED    = "\033[91m"
C_WHITE  = "\033[97m"
C_BLUE   = "\033[94m"

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def heading_to_cardinal(deg: float) -> str:
    """Convierte grados a punto cardinal (8 rumbos)."""
    deg = deg % 360
    idx = int((deg + 22.5) / 45) % 8
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][idx]


def rad_to_deg(rad: float) -> float:
    return math.degrees(rad)


#: GPS_RAW_INT.fix_type -> texto. 0 = ArduPilot no detecta ningun GPS en el UART.
GPS_FIX_NAMES = {
    0: "SIN GPS (no detectado)", 1: "sin fix (buscando satelites)", 2: "2D",
    3: "3D", 4: "3D DGPS", 5: "RTK float", 6: "RTK fixed", 7: "estatico", 8: "PPP",
}

#: Tipos de compass (AP_Compass_Backend::DevTypes) habituales en Pixhawk y GPS.
COMPASS_DEVTYPES = {
    0x01: "HMC5883_OLD", 0x02: "LSM303D", 0x04: "AK8963", 0x05: "BMM150",
    0x06: "LSM9DS1", 0x07: "HMC5883", 0x08: "LIS3MDL", 0x09: "AK09916",
    0x0A: "IST8310", 0x0B: "ICM20948", 0x0C: "MMC3416", 0x0D: "QMC5883L",
    0x0E: "MAG3110", 0x10: "IST8308", 0x11: "RM3100", 0x12: "RM3100",
    0x13: "MMC5883", 0x14: "AK09918", 0x15: "AK09915", 0x16: "QMC5883P",
}

#: Bus I2C interno en Pixhawk1 / 2.4.8 (fmuv2/fmuv3: I2C_ORDER I2C2 I2C1 -> bus 0 = interno).
INTERNAL_I2C_BUS = 0


def decode_compass_dev_id(dev_id: int) -> str:
    """Decodifica un COMPASS_DEV_ID de ArduPilot a texto legible.

    Formato (AP_HAL::Device): bits 0-2 bus_type, 3-7 bus, 8-15 address, 16-23 devtype.

    Args:
        dev_id: Valor del parametro COMPASS_DEV_IDn.

    Returns:
        Descripcion del sensor y si es interno o externo.

    Example:
        >>> decode_compass_dev_id(658945)
        'IST8310 I2C bus 0 addr 0x0E (interno)'
    """
    bus_type = dev_id & 0x07
    bus      = (dev_id >> 3) & 0x1F
    address  = (dev_id >> 8) & 0xFF
    devtype  = (dev_id >> 16) & 0xFF
    name     = COMPASS_DEVTYPES.get(devtype, f"devtype 0x{devtype:02X}")
    if bus_type == 1:
        where = "interno" if bus == INTERNAL_I2C_BUS else "externo/GPS"
        return f"{name} I2C bus {bus} addr 0x{address:02X} ({where})"
    if bus_type == 2:
        return f"{name} SPI (interno)"
    if bus_type == 3:
        return f"{name} DroneCAN (externo)"
    return f"{name} bus_type {bus_type}"


# ──────────────────────────────────────────────
# Detección automática de puerto (Windows mejorado)
# ──────────────────────────────────────────────

# VIDs/PIDs conocidos de controladores Pixhawk/ArduPilot
KNOWN_VID_PID = {
    (0x26AC, None),   # 3DR / ArduPilot genérico
    (0x0483, 0x5740), # STM32 Virtual COM (Pixhawk 4, Cube, etc.)
    (0x0483, 0x374B), # STM32 STLink
    (0x27AC, None),   # Holybro
    (0x1209, 0x5741), # mRo
    (0x0403, 0x6001), # FTDI FT232 (cables telemetría)
    (0x0403, 0x6015), # FTDI FT230X
    (0x10C4, 0xEA60), # Silicon Labs CP210x
    (0x1A86, 0x7523), # CH340 (clones)
    (0x1A86, 0x55D4), # CH9102
    (0x2341, None),   # Arduino (a veces usado como bridge)
}

KNOWN_KEYWORDS = [
    "pixhawk", "ardupilot", "px4", "cube", "holybro",
    "mro", "fmuv", "autopilot", "mavlink",
    "ftdi", "ft232", "ft230",
    "ch340", "ch9102",
    "cp210", "silicon labs",
    "stm32", "virtual com",
]

BAUD_RATES = [115200, 57600, 921600, 460800]  # más comunes primero


def scan_serial_ports() -> list[dict]:
    """
    Escanea todos los puertos serie disponibles y devuelve lista ordenada
    por probabilidad de ser un Pixhawk.
    """
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return []

    results = []
    for p in lp.comports():
        score    = 0
        desc     = (p.description or "").lower()
        mfr      = (p.manufacturer or "").lower()
        prod     = (p.product or "").lower()
        combined = f"{desc} {mfr} {prod}"

        # Puntaje por keyword en descripción
        for kw in KNOWN_KEYWORDS:
            if kw in combined:
                score += 10
                break

        # Puntaje por VID/PID conocido
        vid = p.vid
        pid = p.pid
        if vid is not None:
            for (kvid, kpid) in KNOWN_VID_PID:
                if vid == kvid and (kpid is None or pid == kpid):
                    score += 20
                    break

        # Puntaje base por ser un puerto COM numerado (Windows)
        if sys.platform.startswith("win") and p.device.upper().startswith("COM"):
            try:
                num = int(p.device[3:])
                if 3 <= num <= 30:
                    score += 1
            except ValueError:
                pass

        results.append({
            "device":      p.device,
            "description": p.description or "Sin descripción",
            "vid":         f"0x{vid:04X}" if vid else "—",
            "pid":         f"0x{pid:04X}" if pid else "—",
            "score":       score,
        })

    # Ordenar: primero los de mayor puntaje, luego por nombre de puerto
    results.sort(key=lambda x: (-x["score"], x["device"]))
    return results


def show_port_menu(ports: list[dict]) -> str:
    """Muestra menú interactivo para seleccionar puerto si hay varios."""
    print(f"\n{C_ORANGE}{C_BOLD}  Puertos serie detectados:{C_RESET}")
    print(f"  {'#':<4} {'Puerto':<10} {'Descripción':<40} {'VID':<8} {'PID':<8} Score")
    print(f"  {'─'*4} {'─'*10} {'─'*40} {'─'*8} {'─'*8} {'─'*5}")

    for i, p in enumerate(ports):
        marker = f"{C_GREEN}★{C_RESET}" if p["score"] >= 10 else " "
        print(
            f"  {marker}{i+1:<3} {C_WHITE}{p['device']:<10}{C_RESET} "
            f"{p['description']:<40} {p['vid']:<8} {p['pid']:<8} {p['score']}"
        )

    print(f"\n  {C_GREEN}★{C_RESET} = probable Pixhawk/autopiloto")

    if ports[0]["score"] >= 10:
        # Hay un candidato claro — usar automáticamente con cuenta regresiva
        best = ports[0]
        print(
            f"\n{C_ORANGE}[AUTO]{C_RESET} Seleccionando {C_WHITE}{best['device']}{C_RESET} "
            f"({best['description']}) en 3 segundos..."
        )
        print(f"       Presione {C_YELLOW}Enter{C_RESET} para confirmar o ingrese otro número: ", end="", flush=True)

        import select
        msvcrt = None
        if sys.platform.startswith("win"):
            try:
                import msvcrt as _msvcrt
                msvcrt = _msvcrt
            except ImportError:
                pass

        chosen = None
        deadline = time.time() + 3.0

        if sys.platform.startswith("win"):
            while time.time() < deadline:
                if msvcrt.kbhit():
                    key = msvcrt.getwchar()
                    if key in ("\r", "\n"):
                        chosen = best["device"]
                        break
                    elif key.isdigit():
                        num = int(key)
                        if 1 <= num <= len(ports):
                            chosen = ports[num - 1]["device"]
                            break
                time.sleep(0.05)
        else:
            r, _, _ = select.select([sys.stdin], [], [], 3.0)
            if r:
                line = sys.stdin.readline().strip()
                if line.isdigit() and 1 <= int(line) <= len(ports):
                    chosen = ports[int(line) - 1]["device"]

        if chosen is None:
            chosen = best["device"]
        print(f"\n  → Usando: {C_WHITE}{chosen}{C_RESET}")
        return chosen

    else:
        # Ningún candidato claro — pedir selección manual
        while True:
            try:
                raw = input(f"\n  Ingrese número de puerto [1-{len(ports)}]: ").strip()
                num = int(raw)
                if 1 <= num <= len(ports):
                    return ports[num - 1]["device"]
            except (ValueError, KeyboardInterrupt):
                pass
            print(f"  {C_RED}Número inválido, intente nuevamente.{C_RESET}")


def detect_port_auto() -> tuple[str, int]:
    """
    Devuelve (puerto, baud) detectados automáticamente.
    Si no encuentra nada, muestra error y sale.
    """
    ports = scan_serial_ports()

    if not ports:
        print(f"\n{C_RED}[ERROR]{C_RESET} No se encontró ningún puerto serie en el sistema.")
        print("\n  Verifique que el Pixhawk esté conectado por USB y que el")
        print("  driver esté instalado (STM32 VCP, CP210x, CH340, o FTDI).\n")
        sys.exit(1)

    if len(ports) == 1:
        port = ports[0]["device"]
        print(
            f"\n{C_ORANGE}[AUTO]{C_RESET} Un solo puerto encontrado: "
            f"{C_WHITE}{port}{C_RESET} ({ports[0]['description']})"
        )
    else:
        port = show_port_menu(ports)

    return port, BAUD_RATES[0]  # intentar 115200 primero


# ──────────────────────────────────────────────
# Visualización en pantalla
# ──────────────────────────────────────────────

#: Ancho de las lineas separadoras.
SCREEN_W = 62
#: Separador rojo entre secciones (una sola linea entre dos secciones).
SEP      = f"{C_RED}{'─' * SCREEN_W}{C_RESET}"
#: Separador rojo de inicio y fin de pantalla.
SEP_EDGE = f"{C_RED}{'═' * SCREEN_W}{C_RESET}"
#: Borrar hasta el final de la linea: elimina restos del cuadro anterior.
EOL      = "\033[K"


def _section(lines: list, height: int) -> list:
    """Rellena una seccion con lineas vacias hasta `height` para que el alto sea fijo.

    Con alto fijo, las secciones de abajo no se desplazan cuando cambia el
    contenido (ej. GPS sin fix -> con fix), evitando restos en pantalla.
    """
    return lines + [""] * (height - len(lines))


def _angle_bar(val: float, rng: float = 180) -> str:
    """Barra horizontal con marcador ● centrada en 0 (│)."""
    half = 18
    norm = max(-1.0, min(1.0, val / rng))
    bar  = [" "] * (half * 2 + 1)
    bar[half] = "│"
    bar[max(0, min(len(bar) - 1, int(norm * half) + half))] = "●"
    return "".join(bar)


def _deg_color(v: float) -> str:
    """Verde < 5°, amarillo < 20°, rojo el resto."""
    return C_GREEN if abs(v) < 5 else (C_YELLOW if abs(v) < 20 else C_RED)


def build_frame(state: dict, port: str, baud: int, loop: int, fps: float) -> list:
    """Construye todas las lineas de un cuadro del monitor.

    Cada seccion tiene alto fijo y va separada por una unica linea roja.

    Args:
        state: Ultimos valores recibidos (actitud, rumbo, GPS, bateria).
        port: Puerto serie en uso.
        baud: Baud rate.
        loop: Numero de cuadro.
        fps: Frecuencia de refresco suavizada.

    Returns:
        Lista de lineas (con codigos ANSI) listas para imprimir.
    """
    out = [
        SEP_EDGE,
        f"{C_ORANGE}{C_BOLD}   Monitor Pixhawk  v2.2  (MAVLink){C_RESET}",
        SEP_EDGE,
        f"{C_GRAY}   Puerto: {C_WHITE}{port}{C_GRAY}  |  Baud: {C_WHITE}{baud}"
        f"{C_GRAY}  |  #{loop}  |  {fps:.1f} Hz{C_RESET}",
        SEP,
    ]

    # ── Giroscopio / Actitud ─────────────────────────────────────
    pitch, roll, yaw = state.get("pitch"), state.get("roll"), state.get("yaw")
    sec = [f"{C_CYAN}{C_BOLD}   🔄  GIROSCOPIO / ACTITUD{C_RESET}"]
    if pitch is not None:
        sec += [
            f"   Pitch   : {_deg_color(pitch)}{pitch:+8.2f}°{C_RESET}  [{_angle_bar(pitch)}]",
            f"   Roll    : {_deg_color(roll)}{roll:+8.2f}°{C_RESET}  [{_angle_bar(roll)}]",
            f"   Yaw     : {C_WHITE}{yaw:+8.2f}°{C_RESET}  [{_angle_bar(yaw)}]",
        ]
    else:
        sec.append(f"   {C_YELLOW}Esperando mensaje ATTITUDE...{C_RESET}")
    out += _section(sec, 4) + [SEP]

    # ── Compass / Heading ────────────────────────────────────────
    hdg = state.get("heading")
    sec = [f"{C_GREEN}{C_BOLD}   🧭  COMPASS / RUMBO{C_RESET}"]
    if hdg is not None:
        bar = ["·"] * 36
        bar[int((hdg / 360) * 36) % 36] = "▲"
        sec += [
            f"   Heading : {C_WHITE}{hdg:6.1f}°  {C_GREEN}{C_BOLD}"
            f"{heading_to_cardinal(hdg)}{C_RESET}",
            f"   N {C_GREEN}[{''.join(bar)}]{C_RESET} N",
            f"   {C_GRAY}  0°      90° E     180° S    270° W    360°{C_RESET}",
        ]
    else:
        sec.append(f"   {C_YELLOW}Esperando mensaje VFR_HUD...{C_RESET}")
    sec = _section(sec, 4)
    sec += [f"   {C_GRAY}Sensor  : {desc}{C_RESET}" for desc in state.get("compasses", [])]
    out += sec + [SEP]

    # ── GPS ──────────────────────────────────────────────────────
    gps = state.get("gps")
    sec = [f"{C_BLUE}{C_BOLD}   📡  GPS{C_RESET}"]
    if gps is not None:
        fix   = gps["fix"]
        f_col = C_GREEN if fix >= 3 else (C_YELLOW if fix >= 1 else C_RED)
        sec.append(f"   Fix     : {f_col}{GPS_FIX_NAMES.get(fix, fix)}{C_RESET}"
                   f"   Satélites: {C_WHITE}{gps['sats']}{C_RESET}"
                   f"   HDOP: {C_WHITE}{gps['hdop']}{C_RESET}")
        if fix >= 2:
            sec += [
                f"   Lat/Lon : {C_WHITE}{gps['lat']:.7f}, {gps['lon']:.7f}{C_RESET}",
                f"   Altitud : {C_WHITE}{gps['alt']:.1f} m{C_RESET}"
                f"   Velocidad: {C_WHITE}{gps['speed']:.2f} m/s{C_RESET}",
            ]
        elif fix == 1:
            sec.append(f"   {C_YELLOW}Coloque el GPS con vista al cielo (1-3 min).{C_RESET}")
        else:
            sec.append(f"   {C_RED}Revise cableado TX/RX, GPS_TYPE=1 y SERIAL3_PROTOCOL=5.{C_RESET}")
    else:
        sec.append(f"   {C_YELLOW}Esperando mensaje GPS_RAW_INT...{C_RESET}")
    sec = _section(sec, 4)
    if state.get("gps_info"):
        sec.append(f"   {C_GRAY}{state['gps_info']}{C_RESET}")
    out += sec + [SEP]

    # ── Batería ──────────────────────────────────────────────────
    volts, amps = state.get("voltage"), state.get("current")
    pct = state.get("battery_pct", -1)
    sec = [f"{C_YELLOW}{C_BOLD}   🔋  BATERÍA{C_RESET}"]
    if volts is not None:
        # Rango típico LiPo 3S–6S: 9.0 V vacía → 25.2 V llena
        v_norm   = max(0.0, min(1.0, (volts - 9.0) / (25.2 - 9.0)))
        v_filled = int(v_norm * 24)
        v_color  = C_GREEN if v_norm > 0.5 else (C_YELLOW if v_norm > 0.25 else C_RED)
        v_bar    = v_color + "█" * v_filled + C_GRAY + "░" * (24 - v_filled) + C_RESET
        pct_str  = f"  ({pct}%)" if pct >= 0 else ""
        amps_str = (f"{C_WHITE}{amps:6.2f} A{C_RESET}   Potencia: "
                    f"{C_WHITE}{volts * amps:6.1f} W{C_RESET}"
                    if amps is not None else f"{C_YELLOW}sin datos{C_RESET}")
        sec += [
            f"   Voltaje  : {C_WHITE}{volts:6.3f} V{C_RESET}{pct_str}",
            f"   Carga    : [{v_bar}] {C_GRAY}9 - 25 V{C_RESET}",
            f"   Corriente: {amps_str}",
        ]
    else:
        sec.append(f"   {C_YELLOW}Esperando SYS_STATUS / BATTERY_STATUS...{C_RESET}")
    out += _section(sec, 4)

    out += [
        SEP_EDGE,
        f"{C_GRAY}   {time.strftime('%Y-%m-%d  %H:%M:%S')}   "
        f"{C_YELLOW}[Ctrl+C para salir]{C_RESET}",
    ]
    return out


def print_data(state: dict, port: str, baud: int, loop: int, fps: float):
    """Redibuja el cuadro en una sola escritura: cursor al origen, cada linea
    terminada en EOL (borra restos) y borrado del resto de pantalla al final."""
    frame = build_frame(state, port, baud, loop, fps)
    sys.stdout.write("\033[H" + "\n".join(line + EOL for line in frame) + "\n\033[J")
    sys.stdout.flush()


# ──────────────────────────────────────────────
# Conexión MAVLink
# ──────────────────────────────────────────────

def connect(port: str, baud: int) -> mavutil.mavfile:
    print(f"\n{C_ORANGE}[MAQUINTEL]{C_RESET} Conectando a {C_WHITE}{port}{C_RESET} @ {C_WHITE}{baud}{C_RESET} baud...")

    conn = mavutil.mavlink_connection(
        port,
        baud=baud,
        autoreconnect=True,
        source_system=255,    # GCS system id
        source_component=190,
    )

    print(f"{C_GRAY}   Esperando heartbeat del autopiloto...{C_RESET}", end="", flush=True)
    hb = conn.wait_heartbeat(timeout=15)
    if hb is None:
        print(f" {C_RED}TIMEOUT{C_RESET}")
        print(f"\n{C_RED}[ERROR]{C_RESET} No se recibió heartbeat.")
        print("   Verifique conexión USB y que el Pixhawk esté encendido.")
        sys.exit(1)

    sys_id  = conn.target_system
    comp_id = conn.target_component
    ap_type = hb.autopilot
    print(f" {C_GREEN}OK{C_RESET}  SysID:{sys_id}  CompID:{comp_id}  AP:{ap_type}")

    # Solicitar streams MAVLink (ArduPilot + PX4)
    conn.mav.request_data_stream_send(
        sys_id, comp_id,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 10, 1
    )
    for msg_id in [
        mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE,        # 30
        mavutil.mavlink.MAVLINK_MSG_ID_VFR_HUD,         # 74
        mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS,      # 1
        mavutil.mavlink.MAVLINK_MSG_ID_BATTERY_STATUS,  # 147
        mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT,     # 24
    ]:
        try:
            conn.mav.command_long_send(
                sys_id, comp_id,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                0, msg_id, 100_000, 0, 0, 0, 0, 0  # 100 ms = 10 Hz
            )
        except Exception:
            pass

    time.sleep(0.5)
    return conn


def read_compass_ids(conn: mavutil.mavfile, timeout: float = 4.0) -> list:
    """Lee COMPASS_DEV_ID/2/3 y los devuelve decodificados (interno vs GPS externo).

    Args:
        conn: Conexion MAVLink activa.
        timeout: Tiempo maximo de espera en segundos.

    Returns:
        Lista de descripciones, una por compass detectado (vacia si no hay respuesta).
    """
    names = ["COMPASS_DEV_ID", "COMPASS_DEV_ID2", "COMPASS_DEV_ID3"]
    for n in names:
        conn.mav.param_request_read_send(conn.target_system, conn.target_component,
                                         n.encode(), -1)
    values, deadline = {}, time.time() + timeout
    while len(values) < len(names) and time.time() < deadline:
        p = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
        if p and p.param_id in names:
            values[p.param_id] = int(p.param_value)
    return [f"#{i + 1} {decode_compass_dev_id(values[n])}"
            for i, n in enumerate(names) if values.get(n)]


# ──────────────────────────────────────────────
# Loop principal
# ──────────────────────────────────────────────

def run(conn: mavutil.mavfile, port: str, baud: int):
    state = {
        "pitch":       None,
        "roll":        None,
        "yaw":         None,
        "heading":     None,
        "voltage":     None,
        "current":     None,
        "battery_pct": -1,
        "gps":         None,
        "gps_info":    "",
        "compasses":   read_compass_ids(conn),
    }

    loop       = 0
    last_print = 0.0
    last_loop  = time.monotonic()
    fps_smooth = 0.0
    REFRESH    = 1.0 / 8  # 8 refresco/s

    print(f"\n{C_GREEN}   Recibiendo datos — Ctrl+C para salir{C_RESET}\n")
    time.sleep(0.3)

    # Limpieza única antes de empezar a refrescar en el mismo lugar
    print("\033[2J\033[H", end="")

    while True:
        msg = conn.recv_match(
            type=["ATTITUDE", "VFR_HUD", "SYS_STATUS", "BATTERY_STATUS",
                  "GPS_RAW_INT", "STATUSTEXT"],
            blocking=True,
            timeout=0.05,
        )

        if msg is not None:
            mt = msg.get_type()

            if mt == "ATTITUDE":
                state["pitch"] = rad_to_deg(msg.pitch)
                state["roll"]  = rad_to_deg(msg.roll)
                state["yaw"]   = rad_to_deg(msg.yaw)

            elif mt == "VFR_HUD":
                state["heading"] = float(msg.heading)

            elif mt == "SYS_STATUS":
                v = msg.voltage_battery
                i = msg.current_battery
                r = msg.battery_remaining
                if v > 0:
                    state["voltage"]  = v / 1000.0
                if i >= 0:
                    state["current"]  = i / 100.0
                if r >= 0:
                    state["battery_pct"] = r

            elif mt == "GPS_RAW_INT":
                state["gps"] = {
                    "fix":   msg.fix_type,
                    "sats":  msg.satellites_visible if msg.satellites_visible != 255 else 0,
                    "lat":   msg.lat / 1e7,
                    "lon":   msg.lon / 1e7,
                    "alt":   msg.alt / 1000.0,
                    "hdop":  f"{msg.eph / 100:.2f}" if msg.eph != 65535 else "—",
                    "speed": msg.vel / 100.0 if msg.vel != 65535 else 0.0,
                }

            elif mt == "STATUSTEXT":
                # Mensajes del driver GPS; solo aparecen al arrancar el Pixhawk
                if "GPS 1: detected" in msg.text or "u-blox 1 HW" in msg.text:
                    state["gps_info"] = msg.text.strip()

            elif mt == "BATTERY_STATUS":
                voltages = [v for v in msg.voltages if v != 65535]
                if voltages:
                    state["voltage"] = sum(voltages) / 1000.0
                if msg.current_battery != -1:
                    state["current"] = msg.current_battery / 100.0
                if msg.battery_remaining != -1:
                    state["battery_pct"] = msg.battery_remaining

        now = time.monotonic()
        if now - last_print >= REFRESH:
            dt         = now - last_loop
            fps_smooth = 0.8 * fps_smooth + 0.2 * (1.0 / dt if dt > 0 else 0)
            last_loop  = now
            last_print = now
            loop      += 1
            print_data(state, port, baud, loop, fps_smooth)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Maquintel SpA — Monitor Pixhawk (giroscopio, compass, batería)"
    )
    parser.add_argument(
        "--port", "-p",
        help="Puerto serie (ej: COM3, /dev/ttyACM0). Omitir para auto-detectar.",
        default=None,
    )
    parser.add_argument(
        "--baud", "-b",
        type=int,
        default=None,
        help="Baud rate (default: 115200). ArduPilot USB suele ser 115200.",
    )
    args = parser.parse_args()

    # ── Resolución de puerto ─────────────────────────────────────
    if args.port:
        port = args.port
        baud = args.baud or 115200
    else:
        port, baud = detect_port_auto()
        if args.baud:
            baud = args.baud

    # ── Conexión y loop ──────────────────────────────────────────
    try:
        conn = connect(port, baud)
        run(conn, port, baud)
    except KeyboardInterrupt:
        print(f"\n\n{C_ORANGE}[MAQUINTEL]{C_RESET} Monitor detenido. ¡Hasta pronto!\n")
        sys.exit(0)
    except Exception as exc:
        print(f"\n{C_RED}[ERROR]{C_RESET} {exc}")
        print("\n  Verifique:")
        print("  1. El Pixhawk está conectado por USB")
        print("  2. Ningún otro programa usa el puerto (Mission Planner, QGC, etc.)")
        print("  3. El driver USB está instalado:")
        print("     STM32 VCP  → https://www.st.com/en/development-tools/stsw-stm32102.html")
        print("     CP210x     → https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers")
        print("     CH340      → buscar 'CH340 driver Windows'")
        sys.exit(1)


if __name__ == "__main__":
    main()