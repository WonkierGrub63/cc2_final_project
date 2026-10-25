#!/usr/bin/env python3
"""
Raspberry Pi Alarm Controller for ESP32-S3 over USB Serial UART with SQLite Persistence.

Capabilities:
  1. Automated schedule runner: monitors SQLite schedule_tasks and triggers alarms at specified times.
  2. Live serial event listener: captures ESP32 buzzer activations, cancellations, and mesh telemetry.
  3. SQLite audit logging: stores all infrastructure events, error states, and response latencies.
  4. Interactive CLI: manual triggering, timers, status queries, and real-time log inspection.
"""

import sys
import os
import glob
import time
import threading
import argparse
import sqlite3
import re
from datetime import datetime

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
MAGENTA = "\033[95m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Global runtime state
running = True
alarm_active = False
active_alarm_log_id = None
alarm_start_timestamp = None
db_manager = None
ser_instance = None
state_lock = threading.Lock()


# ============================================================================
# DATABASE MANAGER (SQLite)
# ============================================================================
class DatabaseManager:
    """Thread-safe SQLite manager for scheduled tasks and infrastructure logging."""

    def __init__(self, db_path="alarm_system.db"):
        self.db_path = os.path.abspath(db_path)
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def init_db(self):
        """Creates tables, indexes, and initial seed tasks if not present."""
        schema = """
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = WAL;

        CREATE TABLE IF NOT EXISTS schedule_tasks (
            schedule_task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name        TEXT NOT NULL,
            task_type        TEXT NOT NULL CHECK(task_type IN ('MEDICATION_REMINDER', 'REPORT_TRANSMIT', 'SYSTEM_HEALTH_CHECK')),
            frequency        TEXT NOT NULL CHECK(frequency IN ('HOURLY', 'DAILY', 'WEEKLY')),
            task_time        TEXT NOT NULL,
            day_of_week      TEXT CHECK(day_of_week IN ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')),
            failure_action   TEXT NOT NULL CHECK(failure_action IN ('ALERT_CALL_CENTRE', 'LOG', 'ALARM')),
            is_active        INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS schedule_log (
            schedule_log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_task_id     INTEGER,
            action_timestamp     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            schedule_action_type TEXT NOT NULL CHECK(schedule_action_type IN ('MEDICATION_ALERT_SENT', 'REPORT_RAN', 'CALL_CENTRE_NOTIFIED')),
            alarm_status         TEXT NOT NULL DEFAULT 'TRIGGERED' CHECK(alarm_status IN ('TRIGGERED', 'ACKNOWLEDGED', 'TIMED_OUT', 'FAILED')),
            acknowledged_at      TEXT,
            response_time_sec    REAL,
            error_message        TEXT,
            FOREIGN KEY (schedule_task_id) REFERENCES schedule_tasks(schedule_task_id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS infrastructure_event_log (
            infrastructure_event_log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_log_id             INTEGER,
            node_id                     TEXT NOT NULL CHECK(node_id IN ('RPI_HOST', 'NODE_1', 'NODE_2', 'MESH')),
            event_type                  TEXT NOT NULL CHECK(event_type IN (
                                            'ESP_GATEWAY_CONNECTED', 
                                            'ESP_GATEWAY_DISCONNECTED', 
                                            'ESP_GATEWAY_ERROR', 
                                            'ESP_NOW_MESH_CONNECTED', 
                                            'ESP_NOW_MESH_DISCONNECTED', 
                                            'ESP_NOW_MESH_ERROR', 
                                            'BUZZER_ACTIVATED', 
                                            'BUZZER_CANCELLED', 
                                            'BUZZER_ACK', 
                                            'BUTTON_PRESSED'
                                        )),
            rssi                        INTEGER,
            rtt_ms                      INTEGER,
            details                     TEXT,
            event_timestamp             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (schedule_log_id) REFERENCES schedule_log(schedule_log_id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_active_time ON schedule_tasks(is_active, task_time);
        CREATE INDEX IF NOT EXISTS idx_sched_log_task_id ON schedule_log(schedule_task_id);
        CREATE INDEX IF NOT EXISTS idx_sched_log_status  ON schedule_log(alarm_status, action_timestamp);
        CREATE INDEX IF NOT EXISTS idx_infra_timestamp   ON infrastructure_event_log(event_timestamp);
        CREATE INDEX IF NOT EXISTS idx_infra_event_type  ON infrastructure_event_log(event_type);
        CREATE INDEX IF NOT EXISTS idx_infra_node        ON infrastructure_event_log(node_id);
        """
        with self.get_connection() as conn:
            conn.executescript(schema)
            # Insert default demo tasks if empty
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM schedule_tasks")
            if cur.fetchone()[0] == 0:
                conn.execute("""
                    INSERT INTO schedule_tasks (task_name, task_type, frequency, task_time, failure_action)
                    VALUES 
                      ('Morning Medication Reminder', 'MEDICATION_REMINDER', 'DAILY', '08:30:00', 'ALERT_CALL_CENTRE'),
                      ('Evening Medication Reminder', 'MEDICATION_REMINDER', 'DAILY', '20:00:00', 'ALERT_CALL_CENTRE'),
                      ('Daily Connectivity Health Check', 'SYSTEM_HEALTH_CHECK', 'DAILY', '12:00:00', 'LOG');
                """)
                conn.commit()

    def log_infra_event(self, node_id, event_type, rssi=None, rtt_ms=None, details=None, schedule_log_id=None):
        """Inserts a hardware or infrastructure event into infrastructure_event_log."""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO infrastructure_event_log (
                        schedule_log_id, node_id, event_type, rssi, rtt_ms, details
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (schedule_log_id, node_id, event_type, rssi, rtt_ms, details))
                conn.commit()
        except Exception as e:
            print(f"{RED}[DB ERROR] Failed to log infra event: {e}{RESET}")

    def create_schedule_log(self, schedule_task_id=None, action_type="MEDICATION_ALERT_SENT"):
        """Creates a new active alarm log entry and returns its ID."""
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO schedule_log (
                        schedule_task_id, schedule_action_type, alarm_status
                    ) VALUES (?, ?, 'TRIGGERED')
                """, (schedule_task_id, action_type))
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            print(f"{RED}[DB ERROR] Failed to create schedule log: {e}{RESET}")
            return None

    def acknowledge_schedule_log(self, log_id, response_time_sec=None):
        """Marks an active alarm log entry as ACKNOWLEDGED with response time."""
        if not log_id:
            return
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    UPDATE schedule_log
                    SET alarm_status = 'ACKNOWLEDGED',
                        acknowledged_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                        response_time_sec = ?
                    WHERE schedule_log_id = ?
                """, (response_time_sec, log_id))
                conn.commit()
        except Exception as e:
            print(f"{RED}[DB ERROR] Failed to acknowledge schedule log: {e}{RESET}")

    def get_recent_infra_events(self, limit=10):
        """Returns the most recent infrastructure events."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT infrastructure_event_log_id, node_id, event_type, rssi, rtt_ms, details, event_timestamp
                FROM infrastructure_event_log
                ORDER BY infrastructure_event_log_id DESC
                LIMIT ?
            """, (limit,))
            return cur.fetchall()

    def get_recent_schedule_logs(self, limit=10):
        """Returns recent schedule execution history with task details."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT l.schedule_log_id, t.task_name, l.schedule_action_type, 
                       l.alarm_status, l.action_timestamp, l.acknowledged_at, l.response_time_sec
                FROM schedule_log l
                LEFT JOIN schedule_tasks t ON l.schedule_task_id = t.schedule_task_id
                ORDER BY l.schedule_log_id DESC
                LIMIT ?
            """, (limit,))
            return cur.fetchall()

    def get_active_tasks(self):
        """Returns all currently active scheduled tasks."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT schedule_task_id, task_name, task_type, frequency, task_time, day_of_week, failure_action
                FROM schedule_tasks
                WHERE is_active = 1
                ORDER BY task_time ASC
            """)
            return cur.fetchall()

    def add_task(self, name, task_type, frequency, task_time, failure_action="ALERT_CALL_CENTRE", day_of_week=None):
        """Inserts a new scheduled task."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO schedule_tasks (task_name, task_type, frequency, task_time, failure_action, day_of_week)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (name, task_type, frequency, task_time, failure_action, day_of_week))
            conn.commit()


# ============================================================================
# SERIAL COMMUNICATION & EVENT LISTENER
# ============================================================================
def detect_serial_port():
    """Auto-detects USB serial port on Raspberry Pi or macOS."""
    # 1. Linux / Raspberry Pi
    pi_ports = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    if pi_ports:
        return sorted(pi_ports)[0]

    # 2. macOS development fallback
    mac_ports = glob.glob("/dev/cu.usbmodem*")
    if mac_ports:
        return sorted(mac_ports)[0]

    return None


def send_command(ser, cmd):
    """Sends a newline-terminated command to the ESP32."""
    if not ser or not ser.is_open:
        print(f"{RED}Serial port is not open.{RESET}")
        return
    try:
        ser.write((cmd.strip() + "\n").encode("utf-8"))
        ser.flush()
    except Exception as e:
        print(f"{RED}Failed to send command: {e}{RESET}")


def serial_listener(ser, db, verbose=False):
    """Background thread reading and parsing notifications from the ESP32."""
    global running, alarm_active, active_alarm_log_id, alarm_start_timestamp

    while running:
        try:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # 1. Alarm Started Notification
            if line == "EVENT:ALARM_STARTED":
                with state_lock:
                    alarm_active = True
                    if alarm_start_timestamp is None:
                        alarm_start_timestamp = time.time()
                    if active_alarm_log_id is None:
                        active_alarm_log_id = db.create_schedule_log(None, "MEDICATION_ALERT_SENT")
                    db.log_infra_event("NODE_1", "BUZZER_ACTIVATED", details="Buzzer sounding locally on Node 1", schedule_log_id=active_alarm_log_id)
                print(f"\n{RED}{BOLD}[RPI EVENT] 🚨 Buzzer is sounding on ESP32! Waiting for remote cancel...{RESET}")
                print("> ", end="", flush=True)

            # 2. Alarm Cancelled Remotely via ESP-NOW Button Press
            elif line == "EVENT:ALARM_CANCELLED":
                with state_lock:
                    alarm_active = False
                    elapsed = None
                    if alarm_start_timestamp:
                        elapsed = round(time.time() - alarm_start_timestamp, 2)
                        alarm_start_timestamp = None

                    db.acknowledge_schedule_log(active_alarm_log_id, elapsed)
                    db.log_infra_event("NODE_2", "BUTTON_PRESSED", details="Remote cancel button (GPIO 4) pressed", schedule_log_id=active_alarm_log_id)
                    db.log_infra_event("NODE_1", "BUZZER_CANCELLED", details=f"Alarm silenced via ESP-NOW (Response time: {elapsed}s)", schedule_log_id=active_alarm_log_id)
                    active_alarm_log_id = None

                resp_str = f" in {elapsed}s" if elapsed is not None else ""
                print(f"\n{GREEN}{BOLD}[RPI EVENT] ✅ ALARM CANCELLED! Remote Node 2 disarmed buzzer{resp_str}. Saved to SQLite.{RESET}")
                print(f"{CYAN}Ready for next command.{RESET}\n> ", end="", flush=True)

            # 3. Alarm Cancelled Locally
            elif line == "EVENT:ALARM_CANCELLED_LOCAL":
                with state_lock:
                    alarm_active = False
                    elapsed = round(time.time() - alarm_start_timestamp, 2) if alarm_start_timestamp else None
                    alarm_start_timestamp = None
                    db.acknowledge_schedule_log(active_alarm_log_id, elapsed)
                    db.log_infra_event("NODE_1", "BUTTON_PRESSED", details="Local cancel button pressed", schedule_log_id=active_alarm_log_id)
                    db.log_infra_event("NODE_1", "BUZZER_CANCELLED", details="Local alarm silenced", schedule_log_id=active_alarm_log_id)
                    active_alarm_log_id = None

                print(f"\n{YELLOW}{BOLD}[RPI EVENT] 🔇 Alarm silenced locally at Node 1. Saved to SQLite.{RESET}")
                print(f"{CYAN}Ready for next command.{RESET}\n> ", end="", flush=True)

            # 4. Status response
            elif line.startswith("STATUS:"):
                print(f"\n{CYAN}{BOLD}[ESP32 STATUS]{RESET} {line}")
                print("> ", end="", flush=True)

            # 5. Heartbeat ping
            elif line.startswith("PONG:"):
                print(f"\n{GREEN}[ESP32 PING]{RESET} {line}")
                print("> ", end="", flush=True)

            # 6. Parse RF Telemetry logs (e.g. RSSI, RTT)
            elif "RSSI:" in line or "RTT:" in line or "DATA:" in line:
                # Extract numbers if present for telemetry tracking
                rssi_match = re.search(r"RSSI:\s*(-?\d+)", line)
                rtt_match = re.search(r"RTT:\s*(\d+)", line)
                rssi_val = int(rssi_match.group(1)) if rssi_match else None
                rtt_val = int(rtt_match.group(1)) if rtt_match else None
                if rssi_val is not None:
                    db.log_infra_event("MESH", "ESP_NOW_MESH_CONNECTED", rssi=rssi_val, rtt_ms=rtt_val, details=line)

                if verbose:
                    print(f"\n\033[90m[ESP32 LOG] {line}{RESET}")
                    print("> ", end="", flush=True)

            elif verbose:
                print(f"\n\033[90m[ESP32 LOG] {line}{RESET}")
                print("> ", end="", flush=True)

        except serial.SerialException:
            if running:
                print(f"\n{RED}Serial connection lost.{RESET}")
                db.log_infra_event("RPI_HOST", "ESP_GATEWAY_DISCONNECTED", details="Serial port closed unexpectedly")
            break
        except Exception as e:
            if running:
                print(f"\n{RED}Serial listener error: {e}{RESET}")
                db.log_infra_event("RPI_HOST", "ESP_GATEWAY_ERROR", details=str(e))
            break


# ============================================================================
# AUTOMATED SCHEDULE DAEMON
# ============================================================================
def scheduler_daemon(ser, db):
    """Background worker that executes daily/hourly scheduled tasks from SQLite."""
    global running
    last_checked_minute = None

    while running:
        now = datetime.now()
        current_time_str = now.strftime("%H:%M:00")
        current_day_str = now.strftime("%a").upper()

        if now.minute != last_checked_minute:
            last_checked_minute = now.minute

            tasks = db.get_active_tasks()
            for task in tasks:
                # Match time (HH:MM)
                task_hh_mm = task["task_time"][:5]
                now_hh_mm = current_time_str[:5]

                if task_hh_mm == now_hh_mm:
                    # Check frequency conditions
                    match = False
                    if task["frequency"] == "DAILY":
                        match = True
                    elif task["frequency"] == "WEEKLY" and task["day_of_week"] == current_day_str:
                        match = True
                    elif task["frequency"] == "HOURLY":
                        match = True

                    if match:
                        trigger_scheduled_alarm(ser, db, task)

        time.sleep(5.0)


def trigger_scheduled_alarm(ser, db, task):
    """Triggers an alarm associated with a scheduled task."""
    global active_alarm_log_id, alarm_start_timestamp, alarm_active

    with state_lock:
        active_alarm_log_id = db.create_schedule_log(task["schedule_task_id"], "MEDICATION_ALERT_SENT")
        alarm_start_timestamp = time.time()
        alarm_active = True
        db.log_infra_event(
            "RPI_HOST", "BUZZER_ACTIVATED", 
            details=f"Scheduled task triggered: {task['task_name']}", 
            schedule_log_id=active_alarm_log_id
        )

    print(f"\n{MAGENTA}{BOLD}⏰ [SCHEDULED TASK FIRED] '{task['task_name']}' at {task['task_time']}!{RESET}")
    print(f"{YELLOW}Sending ALARM_ON to ESP32...{RESET}")
    send_command(ser, "ALARM_ON")
    print("> ", end="", flush=True)


# ============================================================================
# CLI DISPLAY HELPERS
# ============================================================================
def display_recent_logs(db):
    """Prints recent infrastructure and schedule history formatted as tables."""
    print(f"\n{BOLD}==================== RECENT SCHEDULE LOGS ===================={RESET}")
    sched_logs = db.get_recent_schedule_logs(8)
    if not sched_logs:
        print("No schedule logs found in database.")
    else:
        print(f"{'ID':<4} {'Task Name':<28} {'Status':<14} {'Response(s)':<12} {'Triggered At'}")
        print("-" * 75)
        for row in sched_logs:
            task_name = row["task_name"] or "Manual Trigger"
            status_color = GREEN if row["alarm_status"] == "ACKNOWLEDGED" else (RED if row["alarm_status"] == "TRIGGERED" else YELLOW)
            resp = f"{row['response_time_sec']:.1f}s" if row["response_time_sec"] is not None else "-"
            print(f"{row['schedule_log_id']:<4} {task_name:<28} {status_color}{row['alarm_status']:<14}{RESET} {resp:<12} {row['action_timestamp']}")

    print(f"\n{BOLD}================ RECENT INFRASTRUCTURE EVENTS ================{RESET}")
    infra_logs = db.get_recent_infra_events(8)
    if not infra_logs:
        print("No infrastructure events recorded.")
    else:
        print(f"{'ID':<4} {'Node':<10} {'Event Type':<26} {'Details':<25} {'Timestamp'}")
        print("-" * 80)
        for row in infra_logs:
            details = (row["details"] or "")[:24]
            print(f"{row['infrastructure_event_log_id']:<4} {row['node_id']:<10} {row['event_type']:<26} {details:<25} {row['event_timestamp']}")
    print()


def display_scheduled_tasks(db):
    """Prints all configured tasks in schedule_tasks."""
    print(f"\n{BOLD}==================== CONFIGURED SCHEDULES ===================={RESET}")
    tasks = db.get_active_tasks()
    if not tasks:
        print("No active schedules found.")
    else:
        print(f"{'ID':<4} {'Task Name':<30} {'Type':<20} {'Freq':<8} {'Time':<10} {'Action'}")
        print("-" * 85)
        for t in tasks:
            print(f"{t['schedule_task_id']:<4} {t['task_name']:<30} {t['task_type']:<20} {t['frequency']:<8} {t['task_time']:<10} {t['failure_action']}")
    print()


# ============================================================================
# MAIN APPLICATION
# ============================================================================
def main():
    global running, ser_instance, db_manager, active_alarm_log_id, alarm_start_timestamp

    parser = argparse.ArgumentParser(description="Raspberry Pi Alarm Controller with SQLite for ESP32-S3")
    parser.add_argument("-p", "--port", help="Serial port (e.g. /dev/ttyACM0 or /dev/cu.usbmodem11301)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("-d", "--db", default="alarm_system.db", help="Path to SQLite database (default: alarm_system.db)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print all raw ESP32 debug traffic")
    parser.add_argument("--trigger-after", type=int, help="Optional: trigger alarm automatically after N seconds")
    args = parser.parse_args()

    # Initialize SQLite Database
    db_manager = DatabaseManager(args.db)
    print(f"{GREEN}SQLite Database initialized at: {BOLD}{db_manager.db_path}{RESET}")

    # Detect Serial Port
    port = args.port or detect_serial_port()
    if not port:
        print(f"{RED}Error: No ESP32 serial port found!{RESET}")
        print("Please connect the ESP32-S3 via USB and check /dev/ttyACM* or /dev/ttyUSB*.")
        db_manager.log_infra_event("RPI_HOST", "ESP_GATEWAY_ERROR", details="No serial port detected on startup")
        sys.exit(1)

    print(f"{BOLD}===================================================={RESET}")
    print(f"{BOLD}   Raspberry Pi Alarm Controller & SQLite Logger    {RESET}")
    print(f"{BOLD}===================================================={RESET}")
    print(f"Connecting to {BOLD}{port}{RESET} at {args.baud} baud...")

    try:
        ser_instance = serial.Serial(port, args.baud, timeout=1)
        try:
            ser_instance.dtr = True
            ser_instance.rts = True
        except Exception:
            pass
        time.sleep(1.0)
    except Exception as e:
        print(f"{RED}Failed to open port {port}: {e}{RESET}")
        db_manager.log_infra_event("RPI_HOST", "ESP_GATEWAY_ERROR", details=f"Failed to open port {port}: {e}")
        sys.exit(1)

    print(f"{GREEN}Connected to ESP32 Gateway successfully!{RESET}\n")
    db_manager.log_infra_event("RPI_HOST", "ESP_GATEWAY_CONNECTED", details=f"Port: {port} at {args.baud} baud")

    # Start background threads
    listener_thread = threading.Thread(target=serial_listener, args=(ser_instance, db_manager, args.verbose), daemon=True)
    listener_thread.start()

    scheduler_thread = threading.Thread(target=scheduler_daemon, args=(ser_instance, db_manager), daemon=True)
    scheduler_thread.start()

    # Optional one-shot automated trigger
    if args.trigger_after is not None:
        print(f"{YELLOW}Automated trigger armed: firing in {args.trigger_after} seconds...{RESET}")
        time.sleep(args.trigger_after)
        print(f"{RED}Firing alarm now!{RESET}")
        with state_lock:
            active_alarm_log_id = db_manager.create_schedule_log(None, "MEDICATION_ALERT_SENT")
            alarm_start_timestamp = time.time()
            db_manager.log_infra_event("RPI_HOST", "BUZZER_ACTIVATED", details="Automated CLI timer trigger", schedule_log_id=active_alarm_log_id)
        send_command(ser_instance, "ALARM_ON")

    print("Menu options:")
    print("  [1] Trigger Alarm Now (turns on buzzer)")
    print("  [2] Set Countdown Timer (e.g. simulate 10s reminder)")
    print("  [3] Query ESP32 Status")
    print("  [4] Cancel Alarm from Pi")
    print("  [5] View SQLite Logs (Schedule History & Infra Events)")
    print("  [6] View Active Scheduled Tasks")
    print("  [7] Add New Scheduled Task (e.g. HH:MM:SS)")
    print("  [q] Quit\n")

    try:
        while running:
            try:
                cmd = input("> ").strip().lower()
            except EOFError:
                break

            if cmd == "1":
                print(f"{YELLOW}Triggering alarm and recording in SQLite...{RESET}")
                with state_lock:
                    active_alarm_log_id = db_manager.create_schedule_log(None, "MEDICATION_ALERT_SENT")
                    alarm_start_timestamp = time.time()
                    db_manager.log_infra_event("RPI_HOST", "BUZZER_ACTIVATED", details="Manual trigger from CLI", schedule_log_id=active_alarm_log_id)
                send_command(ser_instance, "ALARM_ON")

            elif cmd == "2":
                try:
                    sec_str = input("Enter seconds to delay before triggering alarm: ").strip()
                    sec = int(sec_str)
                    print(f"{YELLOW}Timer armed for {sec} seconds...{RESET}")
                    time.sleep(sec)
                    print(f"{RED}{BOLD}⏰ Timer elapsed! Triggering alarm now!{RESET}")
                    with state_lock:
                        active_alarm_log_id = db_manager.create_schedule_log(None, "MEDICATION_ALERT_SENT")
                        alarm_start_timestamp = time.time()
                        db_manager.log_infra_event("RPI_HOST", "BUZZER_ACTIVATED", details=f"Timer elapsed ({sec}s)", schedule_log_id=active_alarm_log_id)
                    send_command(ser_instance, "ALARM_ON")
                except ValueError:
                    print("Invalid number of seconds.")

            elif cmd == "3":
                send_command(ser_instance, "STATUS")

            elif cmd == "4":
                print(f"{YELLOW}Sending ALARM_OFF to silence buzzer...{RESET}")
                with state_lock:
                    if active_alarm_log_id:
                        db_manager.log_infra_event("RPI_HOST", "BUZZER_CANCELLED", details="Forced cancel from Raspberry Pi", schedule_log_id=active_alarm_log_id)
                        active_alarm_log_id = None
                        alarm_start_timestamp = None
                send_command(ser_instance, "ALARM_OFF")

            elif cmd == "5":
                display_recent_logs(db_manager)

            elif cmd == "6":
                display_scheduled_tasks(db_manager)

            elif cmd == "7":
                try:
                    name = input("Enter Task Name (e.g. Noon Pill Reminder): ").strip()
                    task_time = input("Enter Time in HH:MM:SS format (e.g. 14:30:00): ").strip()
                    if name and task_time:
                        db_manager.add_task(name, "MEDICATION_REMINDER", "DAILY", task_time)
                        print(f"{GREEN}Task '{name}' added successfully for {task_time}!{RESET}")
                except Exception as e:
                    print(f"{RED}Failed to add task: {e}{RESET}")

            elif cmd in ("q", "quit", "exit"):
                break

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        running = False
        if ser_instance and ser_instance.is_open:
            db_manager.log_infra_event("RPI_HOST", "ESP_GATEWAY_DISCONNECTED", details="Normal clean shutdown")
            ser_instance.close()
        print("Disconnected.")


if __name__ == "__main__":
    main()
