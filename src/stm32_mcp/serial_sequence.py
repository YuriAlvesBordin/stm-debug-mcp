"""serial_sequence — run multi-step serial command sequences in a single tool call."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .board_map import resolve_probe_full
from .debug_tools import _do_read_memory, _do_write_memory
from .serial_bridge import _get_send_lock
from .serial_tools import LINE_ENDINGS, _connections, _read_with_polling

_executor = ThreadPoolExecutor(max_workers=2)

CAPTURE_DIR = Path("/tmp/stm32-captures")

_camera = None
_camera_device = None

def _get_camera(device_index: int = 0):
    """Get or open a camera connection. Reuses across steps."""
    global _camera, _camera_device
    if _camera is not None and _camera_device == device_index and _camera.isOpened():
        return _camera

    if _camera is not None:
        _camera.release()
    import cv2
    _camera = cv2.VideoCapture(device_index)
    _camera_device = device_index
    if not _camera.isOpened():
        _camera = None
        _camera_device = None
        return None

    _camera.read()
    return _camera

def _release_camera():
    """Release the camera after a sequence completes."""
    global _camera, _camera_device
    if _camera is not None:
        _camera.release()
        _camera = None
        _camera_device = None

def _do_capture(step: dict, step_num: int) -> str:
    """Capture a frame and save to disk. Returns report line."""
    import cv2

    device_index = int(step.get("device_index", 0))
    label = step.get("label", f"step{step_num}")

    cam = _get_camera(device_index)
    if cam is None:
        return f"Step {step_num} CAPTURE: ERROR — could not open camera {device_index}"

    ret, frame = cam.read()
    if not ret or frame is None:
        return f"Step {step_num} CAPTURE: ERROR — frame grab failed"

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"seq_step{step_num}_{label}.png"
    filepath = CAPTURE_DIR / filename
    cv2.imwrite(str(filepath), frame)

    return f"Step {step_num} CAPTURE: {filepath}"

def _do_serial_sequence(
    steps_json: str,
    on_failure: str,
    filter_responses: bool,
) -> str:
    """Run a sequence of send/delay steps synchronously. Returns formatted report."""
    try:
        steps = json.loads(steps_json)
    except json.JSONDecodeError as e:
        return f"ERROR: Invalid JSON in steps: {e}"

    if not isinstance(steps, list):
        return "ERROR: steps must be a JSON array."

    lines: list[str] = []
    send_count = 0
    send_ok = 0
    assert_count = 0
    assert_pass = 0
    capture_count = 0
    mem_write_count = 0
    mem_write_ok = 0
    mem_read_count = 0
    mem_read_ok = 0
    stopped = False

    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            lines.append(f"Step {i} ERROR: expected object, got {type(step).__name__}")
            continue

        if "delay_ms" in step:
            ms = int(step["delay_ms"])
            lines.append(f"Step {i} DELAY: {ms}ms")
            time.sleep(ms / 1000.0)
            continue

        if "capture" in step:
            capture_count += 1
            result = _do_capture(step, i)
            lines.append(result)
            lines.append("")
            continue

        if step.get("mem_write") is True:
            mem_write_count += 1
            address = step.get("address", "")
            symbol = step.get("symbol", "")
            elf_path = step.get("elf_path", "")
            value = step.get("value", "")
            width = int(step.get("width", 32))
            probe = step.get("probe", "")

            sn, target_cfg, chipid = resolve_probe_full(probe)
            result = _do_write_memory(
                address, symbol, elf_path, value, width, sn, target_cfg, chipid
            )
            probe_tag = f"[{probe}] " if probe else ""
            lines.append(f"Step {i} {probe_tag}MEM_WRITE: {result}")
            if result.startswith("ERROR"):
                if on_failure == "stop":
                    stopped = True
                    break
            else:
                mem_write_ok += 1
            continue

        if step.get("mem_read") is True:
            mem_read_count += 1
            address = step.get("address", "")
            symbol = step.get("symbol", "")
            elf_path = step.get("elf_path", "")
            count = int(step.get("count", 1))
            width = int(step.get("width", 32))
            probe = step.get("probe", "")
            label = step.get("label", "")

            sn, target_cfg, chipid = resolve_probe_full(probe)
            result = _do_read_memory(
                address, symbol, elf_path, count, width, sn, target_cfg, chipid
            )
            probe_tag = f"[{probe}] " if probe else ""
            label_tag = f"{label} " if label else ""
            lines.append(f"Step {i} {probe_tag}MEM_READ: {label_tag}{result}")
            if result.startswith("ERROR"):
                if on_failure == "stop":
                    stopped = True
                    break
            else:
                mem_read_ok += 1
            continue

        if "send" not in step or "to" not in step:
            lines.append(f"Step {i} ERROR: send step requires 'send' and 'to' fields")
            continue

        data = step["send"]
        cid = step["to"]
        expect = step.get("expect")
        read_timeout = float(step.get("read_timeout", 2.0))
        line_ending = step.get("line_ending", "lf")

        send_count += 1

        ser = _connections.get(cid)
        if ser is None or not ser.is_open:
            lines.append(f"Step {i} [{cid}] SEND: {data}")
            lines.append(f"  ERROR: No active connection '{cid}'. Call serial_connect first.")
            if on_failure == "stop":
                stopped = True
                break
            continue

        ending = LINE_ENDINGS.get(line_ending, "\n")
        payload = (data + ending).encode("utf-8")

        lock = _get_send_lock(cid)
        with lock:
            try:
                ser.reset_input_buffer()
                ser.write(payload)
                ser.flush()
            except Exception as e:
                lines.append(f"Step {i} [{cid}] SEND: {data}")
                lines.append(f"  ERROR: Write failed: {e}")
                if on_failure == "stop":
                    stopped = True
                    break
                continue

            raw = _read_with_polling(ser, timeout=read_timeout)

        if raw:
            response_text = raw.decode("utf-8", errors="replace").strip()
        else:
            response_text = "(no data received)"

        lines.append(f"Step {i} [{cid}] SEND: {data}")
        lines.append(f"  Response: {response_text}")
        send_ok += 1

        if expect is not None:
            assert_count += 1
            if filter_responses and response_text:

                check_lines = [
                    ln for ln in response_text.splitlines() if ln.startswith(">")
                ]
                check_text = "\n".join(check_lines)
            else:
                check_text = response_text

            if expect in check_text:
                assert_pass += 1
                lines.append(f'  Expect "{expect}": PASS')
            else:
                lines.append(f'  Expect "{expect}": FAIL')
                if on_failure == "stop":
                    stopped = True
                    break

        lines.append("")

    if capture_count > 0:
        _release_camera()

    summary_parts = []
    if send_count > 0:
        summary_parts.append(f"{send_ok}/{send_count} sends OK")
    if assert_count > 0:
        summary_parts.append(f"{assert_pass}/{assert_count} assertions PASS")
    if capture_count > 0:
        summary_parts.append(f"{capture_count} captures saved to {CAPTURE_DIR}")
    if mem_write_count > 0:
        summary_parts.append(f"{mem_write_ok}/{mem_write_count} mem_writes OK")
    if mem_read_count > 0:
        summary_parts.append(f"{mem_read_ok}/{mem_read_count} mem_reads OK")
    if not summary_parts:
        summary_parts.append("0 steps executed")
    if stopped:
        summary_parts.append("STOPPED on failure")
    lines.append(f"Summary: {', '.join(summary_parts)}")

    return "\n".join(lines)

async def serial_sequence(
    steps: str,
    on_failure: str = "continue",
    filter_responses: bool = False,
) -> str:
    """Run a multi-step hardware sequence (serial + SWD memory) in one tool call.

    Executes a list of send, delay, capture, mem_write, and mem_read steps
    sequentially with real timing (no tool-call overhead between steps).
    Useful for hardware test sequences that are timing-sensitive — blinking
    GPIOs over SWD, bit-banging registers with deterministic delays, mixed
    serial/memory flows.

    Timing note: mem ops reuse the probe's pooled OpenOCD connection (no
    process startup per op), but a TCL round-trip per op still means
    sub-10ms delays between memory ops are approximate. Delay steps
    themselves are accurate.

    Args:
        steps: JSON array of step objects. Step types:
            Send:      {"send": "CMD", "to": "/dev/ttyACM0 (Linux) or /dev/cu.usbmodemXXXX (macOS)", "expect": "OK", "read_timeout": 2.0, "line_ending": "lf"}
            Delay:     {"delay_ms": 500}
            Capture:   {"capture": true, "label": "my_label", "device_index": 0}
            MemWrite:  {"mem_write": true, "address": "0x48000418", "value": "0x40", "probe": "yellow", "width": 32}
                       or {"mem_write": true, "symbol": "blink", "elf_path": "/path/to.elf", "value": "1", "probe": "yellow"}
            MemRead:   {"mem_read": true, "address": "0x48000400", "count": 2, "probe": "yellow", "width": 32, "label": "gpio_pre"}
                       or {"mem_read": true, "symbol": "blink", "elf_path": "/path/to.elf", "probe": "yellow"}

            Serial: "to" is the connection_id from serial_connect. "expect",
                "read_timeout", "line_ending" optional on send steps.
            Capture: "label" and "device_index" optional. PNGs saved to /tmp/stm32-captures/.
            Memory: "probe" accepts board nickname, probe nickname, or ST-Link SN.
                "address" is hex ("0x48000418"); or use "symbol" + "elf_path".
                "width" is 8/16/32 bits, defaults to 32 (auto-detected from symbol size).
                "count" (read only) defaults to 1. "label" (read only) prefixes the result line.
        on_failure: "continue" (default) to run all steps, or "stop" to abort on first failure.
        filter_responses: If true, expect patterns match only >-prefixed VCP response lines.

    Returns:
        Formatted report with per-step results and a summary line.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        lambda: _do_serial_sequence(steps, on_failure, filter_responses),
    )
