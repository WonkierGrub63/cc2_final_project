-- SQLite Schema for Distributed Alarm & Infrastructure Telemetry System
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. SCHEDULED TASKS CONFIGURATION
CREATE TABLE IF NOT EXISTS schedule_tasks (
    schedule_task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name        TEXT NOT NULL,
    task_type        TEXT NOT NULL CHECK(task_type IN ('MEDICATION_REMINDER', 'REPORT_TRANSMIT', 'SYSTEM_HEALTH_CHECK')),
    frequency        TEXT NOT NULL CHECK(frequency IN ('HOURLY', 'DAILY', 'WEEKLY')),
    task_time        TEXT NOT NULL, -- Format 'HH:MM:SS' (e.g. '08:30:00')
    day_of_week      TEXT CHECK(day_of_week IN ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')),
    failure_action   TEXT NOT NULL CHECK(failure_action IN ('ALERT_CALL_CENTRE', 'LOG', 'ALARM')),
    is_active        INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 2. SCHEDULE TASK EXECUTION & ALARM LOG
CREATE TABLE IF NOT EXISTS schedule_log (
    schedule_log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_task_id     INTEGER NOT NULL,
    action_timestamp     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    schedule_action_type TEXT NOT NULL CHECK(schedule_action_type IN ('MEDICATION_ALERT_SENT', 'REPORT_RAN', 'CALL_CENTRE_NOTIFIED')),
    alarm_status         TEXT NOT NULL DEFAULT 'TRIGGERED' CHECK(alarm_status IN ('TRIGGERED', 'ACKNOWLEDGED', 'TIMED_OUT', 'FAILED')),
    acknowledged_at      TEXT,     -- When remote cancel button was pressed
    response_time_sec    INTEGER,  -- Latency between fire and cancel
    error_message        TEXT,
    FOREIGN KEY (schedule_task_id) REFERENCES schedule_tasks(schedule_task_id) ON DELETE CASCADE
);

-- 3. HARDWARE & INFRASTRUCTURE EVENT LOG
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
    rssi                        INTEGER, -- Signal strength in dBm
    rtt_ms                      INTEGER, -- Round-trip latency in milliseconds
    details                     TEXT,    -- Contextual notes, error message, or payload
    event_timestamp             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (schedule_log_id) REFERENCES schedule_log(schedule_log_id) ON DELETE SET NULL
);

-- 4. INDEXES
CREATE INDEX IF NOT EXISTS idx_tasks_active_time ON schedule_tasks(is_active, task_time);
CREATE INDEX IF NOT EXISTS idx_sched_log_task_id ON schedule_log(schedule_task_id);
CREATE INDEX IF NOT EXISTS idx_sched_log_status  ON schedule_log(alarm_status, action_timestamp);
CREATE INDEX IF NOT EXISTS idx_infra_timestamp   ON infrastructure_event_log(event_timestamp);
CREATE INDEX IF NOT EXISTS idx_infra_event_type  ON infrastructure_event_log(event_type);
CREATE INDEX IF NOT EXISTS idx_infra_node        ON infrastructure_event_log(node_id);

