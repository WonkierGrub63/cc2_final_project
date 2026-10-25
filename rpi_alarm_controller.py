#!/usr/bin/env python3
"""
Raspberry Pi Alarm Controller for ESP32-S3 over USB Serial UART.

Allows the Raspberry Pi to:
  1. Trigger the ESP32 buzzer immediately or via scheduled timer.
  2. Listen for the 'EVENT:ALARM_CANCELLED' notification when the remote
     ESP32 presses Button 1 to disarm the alarm over ESP-NOW.
  3. Query status and cancel the alarm directly.
"""

import sys
import os
import glob
import time
import threading
import argparse

# Auto-fallback to PlatformIO python if pyserial isn't installed in system python
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

# ANSI formatting
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

running = True
alarm_active = False


def detect_serial_port():
    """Auto-detects USB serial port on Raspberry Pi (ttyACM*/ttyUSB*) or macOS (cu.usbmodem*)."""
    # 1. Linux / Raspberry Pi
    pi_ports = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    if pi_ports:
        return pi_ports[0]

    # 2. macOS development fallback
    mac_ports = glob.glob("/dev/cu.usbmodem*")
    if mac_ports:
        return mac_ports[0]

    return None


def serial_listener(ser, verbose=False):
    """Background thread reading events and notifications from the ESP32."""
    global running, alarm_active

    while running:
        try:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # Process structured events from ESP32
            if line == "EVENT:ALARM_STARTED":
                alarm_active = True
                print(f"\n{RED}{BOLD}[RPI EVENT] 🚨 Buzzer is sounding on ESP32! Waiting for remote cancel...{RESET}")

            elif line == "EVENT:ALARM_CANCELLED":
                alarm_active = False
                print(f"\n{GREEN}{BOLD}[RPI EVENT] ✅ ALARM CANCELLED! Remote node cancelled via ESP-NOW. Buzzer is OFF.{RESET}")
                print(f"{CYAN}Ready for next command.{RESET}\n> ", end="", flush=True)

            elif line == "EVENT:ALARM_CANCELLED_LOCAL":
                alarm_active = False
                print(f"\n{YELLOW}{BOLD}[RPI EVENT] 🔇 Alarm cancelled locally.{RESET}")
                print(f"{CYAN}Ready for next command.{RESET}\n> ", end="", flush=True)

            elif line.startswith("STATUS:"):
                print(f"\n{CYAN}{BOLD}[ESP32 STATUS]{RESET} {line}")
                print("> ", end="", flush=True)

            elif line.startswith("PONG:"):
                print(f"\n{GREEN}[ESP32 PING]{RESET} {line}")
                print("> ", end="", flush=True)

            elif verbose:
                # Print raw ESP32 debug logs if requested
                print(f"\n\033[90m[ESP32 LOG] {line}{RESET}")
                print("> ", end="", flush=True)

        except serial.SerialException:
            if running:
                print(f"\n{RED}Serial connection lost.{RESET}")
            break
        except Exception as e:
            if running:
                print(f"\n{RED}Serial listener error: {e}{RESET}")
            break


def send_command(ser, cmd):
    """Sends a newline-terminated command to the ESP32."""
    try:
        ser.write((cmd.strip() + "\n").encode("utf-8"))
        ser.flush()
    except Exception as e:
        print(f"{RED}Failed to send command: {e}{RESET}")


def main():
    global running, alarm_active

    parser = argparse.ArgumentParser(description="Raspberry Pi Alarm Controller for ESP32-S3")
    parser.add_argument("-p", "--port", help="Serial port (e.g. /dev/ttyACM0 or /dev/cu.usbmodem11301)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print all raw ESP32 debug traffic")
    parser.add_argument("--trigger-after", type=int, help="Optional: trigger alarm automatically after N seconds")
    args = parser.parse_args()

    port = args.port or detect_serial_port()
    if not port:
        print(f"{RED}Error: No ESP32 serial port found!{RESET}")
        print("Please connect the ESP32-S3 via USB and check /dev/ttyACM* or /dev/ttyUSB*.")
        sys.exit(1)

    print(f"{BOLD}===================================================={RESET}")
    print(f"{BOLD}   Raspberry Pi Alarm Controller for ESP32-S3      {RESET}")
    print(f"{BOLD}===================================================={RESET}")
    print(f"Connecting to {BOLD}{port}{RESET} at {args.baud} baud...")

    try:
        ser = serial.Serial(port, args.baud, timeout=1)
        time.sleep(1.0)  # Allow port to settle
    except Exception as e:
        print(f"{RED}Failed to open port {port}: {e}{RESET}")
        sys.exit(1)

    print(f"{GREEN}Connected successfully!{RESET}\n")

    # Start background listener thread
    listener = threading.Thread(target=serial_listener, args=(ser, args.verbose), daemon=True)
    listener.start()

    # If automated delay was specified
    if args.trigger_after is not None:
        print(f"{YELLOW}Automated trigger armed: firing in {args.trigger_after} seconds...{RESET}")
        time.sleep(args.trigger_after)
        print(f"{RED}Firing alarm now!{RESET}")
        send_command(ser, "ALARM_ON")

    print("Menu options:")
    print("  [1] Trigger Alarm Now (turns on buzzer)")
    print("  [2] Set Countdown Timer (simulate scheduled alarm)")
    print("  [3] Query ESP32 Status")
    print("  [4] Cancel Alarm from Pi")
    print("  [q] Quit\n")

    try:
        while running:
            try:
                cmd = input("> ").strip().lower()
            except EOFError:
                break

            if cmd == "1":
                print(f"{YELLOW}Sending ALARM_ON command...{RESET}")
                send_command(ser, "ALARM_ON")

            elif cmd == "2":
                try:
                    sec_str = input("Enter seconds to delay before triggering alarm: ").strip()
                    sec = int(sec_str)
                    print(f"{YELLOW}Timer set for {sec} seconds...{RESET}")
                    time.sleep(sec)
                    print(f"{RED}{BOLD}⏰ Timer elapsed! Triggering alarm now!{RESET}")
                    send_command(ser, "ALARM_ON")
                except ValueError:
                    print("Invalid number of seconds.")

            elif cmd == "3":
                send_command(ser, "STATUS")

            elif cmd == "4":
                print(f"{YELLOW}Sending ALARM_OFF command...{RESET}")
                send_command(ser, "ALARM_OFF")

            elif cmd in ("q", "quit", "exit"):
                break

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        running = False
        ser.close()
        print("Disconnected.")


if __name__ == "__main__":
    main()

