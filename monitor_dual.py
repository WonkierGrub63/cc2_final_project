#!/usr/bin/env python3
"""
Dual Serial Monitor for ESP32 Nodes
Monitors both /dev/cu.usbmodem* ports concurrently with color-coded tags.
"""

import sys
import os
import glob
import time
import threading

# Auto re-exec with PlatformIO's python if pyserial isn't in system python
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    pio_py = os.path.expanduser("~/.platformio/penv/bin/python")
    if os.path.exists(pio_py) and sys.executable != pio_py:
        os.execv(pio_py, [pio_py] + sys.argv)
    else:
        print("Error: 'pyserial' is required. Run: pip install pyserial")
        sys.exit(1)

# ANSI Colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

COLORS = [CYAN, YELLOW, GREEN]

print_lock = threading.Lock()
running = True


def reader_thread(port_path, node_name, color):
    """Continuously read and format lines from a single serial port."""
    global running
    while running:
        try:
            with serial.Serial(port_path, 115200, timeout=1) as ser:
                try:
                    ser.dtr = True
                    ser.rts = True
                except Exception:
                    pass
                with print_lock:
                    print(f"{color}{BOLD}[Connected]{RESET} {node_name} on {port_path}")
                
                while running:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue

                    # Highlight alerts, alarms, and acknowledgments
                    extra_style = ""
                    if "ALERT" in line or "BUTTON PRESSED" in line or "ALARM TRIGGERED" in line:
                        extra_style = f"{BOLD}{RED}"
                    elif "ACK" in line or "ACKNOWLEDGED" in line or "CANCELLED" in line or "CONFIRMED" in line:
                        extra_style = f"{BOLD}{GREEN}"

                    with print_lock:
                        tag = f"{color}{BOLD}[{node_name}]{RESET}"
                        if extra_style:
                            print(f"{tag} {extra_style}{line}{RESET}")
                        else:
                            print(f"{tag} {line}")

        except serial.SerialException as e:
            if not running:
                break
            with print_lock:
                print(f"{RED}[Waiting]{RESET} {node_name} ({port_path}) is busy or disconnected. Retrying...")
            time.sleep(2)
        except Exception as e:
            if not running:
                break
            with print_lock:
                print(f"{RED}[Error]{RESET} {node_name}: {e}")
            time.sleep(2)


def load_platformio_ports():
    """Load node_1 and node_2 ports directly from platformio.ini if present."""
    ini_path = os.path.join(os.path.dirname(__file__), "platformio.ini")
    mapped = {}
    if os.path.exists(ini_path):
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read(ini_path)
            for section in config.sections():
                if "node_1" in section:
                    port = config.get(section, "upload_port", fallback=None)
                    if port:
                        mapped["Node 1"] = port
                elif "node_2" in section:
                    port = config.get(section, "upload_port", fallback=None)
                    if port:
                        mapped["Node 2"] = port
        except Exception:
            pass
    return mapped


def main():
    global running

    pio_ports = load_platformio_ports()

    # Detect usbmodem ports or use CLI arguments if provided
    if len(sys.argv) > 1:
        ports = sys.argv[1:]
        port_assignments = [(f"Node {i+1} ({os.path.basename(p)})", p) for i, p in enumerate(ports)]
    elif pio_ports and all(os.path.exists(p) for p in pio_ports.values()):
        # Use exact mapping from platformio.ini
        port_assignments = [(f"{name} ({os.path.basename(p)})", p) for name, p in pio_ports.items()]
    else:
        detected = sorted(glob.glob("/dev/cu.usbmodem*"))
        port_assignments = []
        # If any match platformio.ini, preserve their Node 1 / Node 2 names
        for p in detected:
            matched_name = None
            for name, ini_p in pio_ports.items():
                if os.path.basename(p) == os.path.basename(ini_p):
                    matched_name = name
                    break
            if not matched_name:
                matched_name = f"Node {len(port_assignments)+1}"
            port_assignments.append((f"{matched_name} ({os.path.basename(p)})", p))

    if not port_assignments:
        print(f"{RED}No /dev/cu.usbmodem* devices found!{RESET}")
        print("Please check USB connections and try again.")
        sys.exit(1)

    print(f"{BOLD}===================================================={RESET}")
    print(f"{BOLD}        Dual ESP32 Node Serial Monitor             {RESET}")
    print(f"{BOLD}===================================================={RESET}")
    print(f"Monitoring {len(port_assignments)} port(s):")
    for name, p in port_assignments:
        print(f"  • {name} -> {p}")
    print("Press Ctrl+C to stop.\n")

    threads = []
    for i, (node_name, port) in enumerate(port_assignments):
        color = COLORS[i % len(COLORS)]
        t = threading.Thread(target=reader_thread, args=(port, node_name, color), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{BOLD}Exiting monitor...{RESET}")
        running = False


if __name__ == "__main__":
    main()

