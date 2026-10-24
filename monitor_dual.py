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
                with print_lock:
                    print(f"{color}{BOLD}[Connected]{RESET} {node_name} on {port_path}")
                
                while running:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue

                    # Highlight alerts and acknowledgments
                    extra_style = ""
                    if "ALERT" in line or "BUTTON PRESSED" in line:
                        extra_style = f"{BOLD}{RED}"
                    elif "ACK" in line or "ACKNOWLEDGED" in line:
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


def main():
    global running

    # Detect usbmodem ports or use CLI arguments if provided
    if len(sys.argv) > 1:
        ports = sys.argv[1:]
    else:
        ports = sorted(glob.glob("/dev/cu.usbmodem*"))

    if not ports:
        print(f"{RED}No /dev/cu.usbmodem* devices found!{RESET}")
        print("Please check USB connections and try again.")
        sys.exit(1)

    print(f"{BOLD}===================================================={RESET}")
    print(f"{BOLD}        Dual ESP32 Node Serial Monitor             {RESET}")
    print(f"{BOLD}===================================================={RESET}")
    print(f"Monitoring {len(ports)} port(s): {', '.join(ports)}")
    print("Press Ctrl+C to stop.\n")

    threads = []
    for i, port in enumerate(ports):
        node_name = f"Node {i+1} ({os.path.basename(port)})"
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

