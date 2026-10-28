"""Serial tools — list, connect, send, read, disconnect."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import serial
import serial.tools.list_ports

from .board_map import get_board_nickname_for_probe_sn, get_probe_nickname

_executor = ThreadPoolExecutor(max_workers=4)

_connections: dict[str, serial.Serial] = {}

ST_VID = 0x0483
ST_PIDS = {0x374B, 0x374E, 0x3752, 0x3754}

INTER_BYTE_SLEEP = 0.050
SILENCE_BREAK = 0.200
DEFAULT_TIMEOUT = 2.0
DEFAULT_MAX_BYTES = 4096

LINE_ENDINGS = {
    "lf": "\n",
    "cr": "\r",
    "crlf": "\r\n",
    "none": "",
}

def _read_with_polling(
    ser: serial.Serial,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bytes:
    """Read serial data with polling. 50ms inter-byte, 200ms silence break."""
    data = bytearray()
    start = time.monotonic()
    last_data_time = None

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            break
        if len(data) >= max_bytes:
            break

        waiting = ser.in_waiting
        if waiting > 0:
            chunk = ser.read(min(waiting, max_bytes - len(data)))
            data.extend(chunk)
            last_data_time = time.monotonic()
        else:

            if last_data_time is not None:
                silence = time.monotonic() - last_data_time
                if silence >= SILENCE_BREAK:
                    break
            time.sleep(INTER_BYTE_SLEEP)

    return bytes(data)

def _do_list_ports() -> str:
    """List available serial ports."""
    ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)

    if not ports:
        return "No serial ports found."

    lines = []
    for port in ports:
        is_stlink = port.vid == ST_VID if port.vid else False

        if is_stlink:

            nick_parts = []
            if port.serial_number:
                probe_nick = get_probe_nickname(port.serial_number)
                board_nick = get_board_nickname_for_probe_sn(port.serial_number)
                if board_nick:
                    nick_parts.append(f'"{board_nick}"')
                if probe_nick:
                    nick_parts.append(f'via "{probe_nick}"')
            if nick_parts:
                marker = f" [ST-Link VCP \u2014 {' '.join(nick_parts)}]"
            else:
                marker = " [ST-Link VCP]"
        else:
            marker = ""

        line = f"{port.device}{marker}"
        details = []
        if port.description and port.description != "n/a":
            details.append(port.description)
        if port.vid is not None:
            details.append(f"VID:PID={port.vid:04X}:{port.pid:04X}")
        if port.serial_number:
            details.append(f"SN={port.serial_number}")

        if details:
            line += f"  ({', '.join(details)})"
        lines.append(line)

    return "\n".join(lines)

def _do_connect(port: str, baudrate: int) -> str:
    """Open serial connection. Pool is keyed by port path only."""

    if port in _connections:
        existing = _connections[port]
        if existing.is_open:
            return f"Already connected: {port} @ {baudrate}"

        del _connections[port]

    try:
        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=0.1,
            write_timeout=1.0,
        )
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except serial.SerialException as e:
        return f"ERROR: Could not open {port}: {e}"

    _connections[port] = ser
    return f"Connected: {port} @ {baudrate}"

def _do_send(
    connection_id: str,
    data: str,
    read_response: bool,
    read_timeout: float,
    line_ending: str,
) -> str:
    """Send data and optionally read response."""
    ser = _connections.get(connection_id)
    if ser is None or not ser.is_open:
        return f"ERROR: No active connection '{connection_id}'. Call serial_connect first."

    ending = LINE_ENDINGS.get(line_ending, "\n")
    payload = (data + ending).encode("utf-8")

    try:
        ser.reset_input_buffer()
        ser.write(payload)
        ser.flush()
    except serial.SerialException as e:
        return f"ERROR: Write failed: {e}"

    parts = [f"Sent: {data!r}"]

    if read_response:
        try:
            response = _read_with_polling(ser, timeout=read_timeout)
        except serial.SerialException as e:
            parts.append(f"Response: ERROR reading: {e}")
            return "\n".join(parts)
        if response:
            try:
                text = response.decode("utf-8", errors="replace").strip()
            except Exception:
                text = repr(response)
            parts.append(f"Response: {text}")
        else:
            parts.append("Response: (no data received)")

    return "\n".join(parts)

def _do_read(connection_id: str, timeout: float, max_bytes: int) -> str:
    """Read available data."""
    ser = _connections.get(connection_id)
    if ser is None or not ser.is_open:
        return f"ERROR: No active connection '{connection_id}'. Call serial_connect first."

    data = _read_with_polling(ser, timeout=timeout, max_bytes=max_bytes)
    if data:
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            text = repr(data)
        return text
    return "(no data received)"

def _do_disconnect(connection_id: str) -> str:
    """Close serial connection."""
    ser = _connections.pop(connection_id, None)
    if ser is None:
        return f"No connection '{connection_id}' found."

    try:
        if ser.is_open:
            ser.close()
    except serial.SerialException:
        pass

    return f"Disconnected: {connection_id}"

async def serial_list_ports() -> str:
    """List available serial ports.

    Shows all serial ports on this machine with device path, description,
    VID:PID, and serial number. ST-Link VCP ports are marked with [ST-Link VCP].

    Returns:
        List of serial ports with details.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _do_list_ports)

async def serial_connect(port: str, baudrate: int = 115200) -> str:
    """Open a serial connection to an STM32 board.

    Opens the specified serial port and stores the connection for use
    with serial_send, serial_read, and serial_disconnect. The connection
    persists across tool calls until explicitly disconnected.

    Args:
        port: Serial port path (e.g., "/dev/cu.usbmodem1234"). Use serial_list_ports to find available ports.
        baudrate: Baud rate (default 115200).

    Returns:
        Connection status message. The connection_id for subsequent calls is the port path
        (e.g., "/dev/cu.usbmodem1234") — the same value you passed as the port argument.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: _do_connect(port, baudrate))

async def serial_send(
    connection_id: str,
    data: str,
    read_response: bool = True,
    read_timeout: float = 2.0,
    line_ending: str = "lf",
) -> str:
    """Send data over serial and optionally read the response.

    Sends the data string (plus line ending) to the connected board. If
    read_response is true, waits for and returns the response using a
    polling loop (50ms inter-byte, 200ms silence break).

    Args:
        connection_id: Connection ID from serial_connect.
        data: The string to send.
        read_response: If true, wait for and return response data.
        read_timeout: Max seconds to wait for response (default 2.0).
        line_ending: Line ending to append — "lf" (default), "cr", "crlf", or "none".

    Returns:
        What was sent, and the response if read_response is true.
    """
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            lambda: _do_send(connection_id, data, read_response, read_timeout, line_ending),
        ),
        timeout=read_timeout + 5,
    )

async def serial_read(
    connection_id: str,
    timeout: float = 2.0,
    max_bytes: int = 4096,
) -> str:
    """Read data from the serial port buffer.

    Reads whatever data is available or arrives within the timeout period.
    Useful for catching async output like boot messages or periodic prints.
    Uses polling loop (50ms inter-byte, 200ms silence break).

    Args:
        connection_id: Connection ID from serial_connect.
        timeout: Max seconds to wait for data (default 2.0).
        max_bytes: Max bytes to read (default 4096).

    Returns:
        Received data as text, or "(no data received)".
    """
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            lambda: _do_read(connection_id, timeout, max_bytes),
        ),
        timeout=timeout + 5,
    )

async def serial_disconnect(connection_id: str) -> str:
    """Close a serial connection.

    Closes the serial port and removes it from the connection pool.

    Args:
        connection_id: Connection ID from serial_connect.

    Returns:
        Confirmation message.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, lambda: _do_disconnect(connection_id)
    )
