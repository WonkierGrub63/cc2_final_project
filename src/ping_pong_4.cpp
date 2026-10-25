#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_idf_version.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// =========================================
// Dual Serial Support (Native USB + UART port)
// Ensures debugging & serial commands work
// regardless of which USB-C port is plugged in.
// =========================================
class DualPortSerial : public Stream {
public:
  void begin(unsigned long baud) {
    ::Serial.begin(baud);
    Serial0.begin(baud);
  }
  int available() override {
    return (::Serial ? ::Serial.available() : 0) + Serial0.available();
  }
  int read() override {
    if (::Serial && ::Serial.available()) return ::Serial.read();
    if (Serial0.available()) return Serial0.read();
    return -1;
  }
  int peek() override {
    if (::Serial && ::Serial.available()) return ::Serial.peek();
    if (Serial0.available()) return Serial0.peek();
    return -1;
  }
  void flush() override {
    if (::Serial) ::Serial.flush();
    Serial0.flush();
  }
  size_t write(uint8_t c) override {
    if (::Serial) ::Serial.write(c);
    Serial0.write(c);
    return 1;
  }
  size_t write(const uint8_t *buffer, size_t size) override {
    if (::Serial) ::Serial.write(buffer, size);
    Serial0.write(buffer, size);
    return size;
  }
};

static DualPortSerial DualSerial;
#define Serial DualSerial

// =========================================
// ESP-NOW v4 - Two node reliability test
// Identical firmware on both ESP32-S3s
// Arduino-ESP32 3.3.x
// =========================================

constexpr uint8_t CHANNEL = 1;

constexpr uint32_t HELLO_INTERVAL = 3000;
constexpr uint32_t PING_INTERVAL = 1500;
constexpr uint32_t PONG_TIMEOUT = 1000;
constexpr uint32_t OFFLINE_TIMEOUT = 6000;
constexpr uint32_t TX_GAP = 20;

const uint8_t BROADCAST[6] = {
  0xFF, 0xFF, 0xFF,
  0xFF, 0xFF, 0xFF
};

constexpr uint32_t MAGIC = 0x45535034;

// =========================================
// Hardware Configuration
// =========================================
constexpr uint8_t BUTTON1_PIN = 4; // Button 1: Cancel / Silence Alarm (to GND)
constexpr uint8_t BUZZER_PIN  = 5; // Buzzer: GPIO 5 (+) / Signal
constexpr uint8_t BUTTON2_PIN = 6; // Button 2: Trigger Alarm (to GND)
constexpr uint8_t BOOT_BUTTON_PIN = 0; // Onboard BOOT button (also triggers alarm)
constexpr bool PASSIVE_BUZZER = false; // Set to true if using a passive piezo buzzer (PWM)

// I2C 16x2 / 20x4 LCD Configuration
constexpr uint8_t LCD_SDA_PIN = 8; // I2C SDA -> LCD SDA pin
constexpr uint8_t LCD_SCL_PIN = 9; // I2C SCL -> LCD SCL pin
constexpr uint8_t LCD_COLS = 16;
constexpr uint8_t LCD_ROWS = 2;

enum MessageType : uint8_t {
  HELLO = 1,
  HELLO_ACK = 2,
  PING = 3,
  PONG = 4,
  ALARM_TRIGGER = 5,
  ALARM_CANCEL = 6,
  ALARM_CANCEL_ACK = 7
};

struct __attribute__((packed)) Message {
  uint32_t magic;
  uint8_t type;
  uint32_t bootId;
  uint32_t sequence;
  uint32_t replyBootId;
};

struct RxEvent {
  uint8_t mac[6];
  Message msg;
  int8_t rssi;
};

struct TxRequest {
  uint8_t mac[6];
  Message msg;
};

// =========================================
// FreeRTOS queues
// =========================================

QueueHandle_t rxQueue;
QueueHandle_t txQueue;
QueueHandle_t urgentQueue;
QueueHandle_t txResultQueue;

// =========================================
// Node state
// =========================================

uint8_t myMac[6] = {};
uint8_t peerMac[6] = {};

uint32_t myBootId;
uint32_t peerBootId = 0;

bool peerKnown = false;
bool peerHealthy = false;

uint32_t lastPong = 0;
uint32_t lastHello = 0;
uint32_t lastPing = 0;

uint32_t nextSequence = 0;
uint32_t pendingSequence = 0;
uint32_t pendingSince = 0;

bool awaitingPong = false;

uint32_t pingOK = 0;
uint32_t pingLost = 0;
uint32_t macFailures = 0;

// TX scheduling
bool txBusy = false;
uint32_t lastTxFinished = 0;

// Alarm & Buzzer state
bool alarmActive = false;

void buzzerOn() {
  if (PASSIVE_BUZZER) {
    tone(BUZZER_PIN, 2000); // 2 kHz tone for passive piezo
  } else {
    digitalWrite(BUZZER_PIN, HIGH);
  }
}

void buzzerOff() {
  if (PASSIVE_BUZZER) {
    noTone(BUZZER_PIN);
  } else {
    digitalWrite(BUZZER_PIN, LOW);
  }
}

// LCD & Mesh Telemetry state
LiquidCrystal_I2C lcd(0x27, LCD_COLS, LCD_ROWS);
bool lcdAvailable = false;
int8_t lastRssi = 0;
uint32_t lastRtt = 0;
bool remoteAlarmActive = false;

void updateLcdStatus(bool force = false) {
  if (!lcdAvailable) return;

  static uint32_t lastUpdate = 0;
  uint32_t now = millis();
  if (!force && (now - lastUpdate < 500)) return;
  lastUpdate = now;

  char line1[17];
  char line2[17];

  if (alarmActive) {
    snprintf(line1, sizeof(line1), "** ALARM ON! ** ");
    snprintf(line2, sizeof(line2), "BUZZER! B1:Stop ");
  } else if (remoteAlarmActive) {
    snprintf(line1, sizeof(line1), "!REMOTE ALARM!  ");
    snprintf(line2, sizeof(line2), "Press B1:Cancel ");
  } else if (!peerKnown) {
    snprintf(line1, sizeof(line1), "MESH: SEARCHING ");
    snprintf(line2, sizeof(line2), "CH:%u ID:%02X%02X%02X ",
      CHANNEL, myMac[3], myMac[4], myMac[5]);
  } else if (!peerHealthy) {
    snprintf(line1, sizeof(line1), "MESH: OFFLINE!  ");
    snprintf(line2, sizeof(line2), "PEER UNHEALTHY  ");
  } else {
    // Normal healthy mesh status
    snprintf(line1, sizeof(line1), "MESH:OK %+3ddBm ", lastRssi);
    snprintf(line2, sizeof(line2), "RTT:%lums P:%-5lu",
      (unsigned long)lastRtt, (unsigned long)pingOK);
  }

  line1[16] = '\0';
  line2[16] = '\0';

  lcd.setCursor(0, 0);
  lcd.print(line1);
  lcd.setCursor(0, 1);
  lcd.print(line2);
}

// =========================================
// Utilities
// =========================================

String macText(const uint8_t *mac) {
  char buffer[18];

  snprintf(buffer, sizeof(buffer),
    "%02X:%02X:%02X:%02X:%02X:%02X",
    mac[0], mac[1], mac[2],
    mac[3], mac[4], mac[5]);

  return String(buffer);
}

const char *typeName(uint8_t type) {
  switch (type) {
    case HELLO: return "HELLO";
    case HELLO_ACK: return "HELLO_ACK";
    case PING: return "DING";
    case PONG: return "DONG";
    case ALARM_TRIGGER: return "ALARM_TRIGGER";
    case ALARM_CANCEL: return "ALARM_CANCEL";
    case ALARM_CANCEL_ACK: return "ALARM_CANCEL_ACK";
    default: return "UNKNOWN";
  }
}

bool registerPeer(const uint8_t *mac) {

  if (peerKnown) {
    return memcmp(peerMac, mac, 6) == 0;
  }

  esp_now_peer_info_t peer = {};

  memcpy(peer.peer_addr, mac, 6);
  peer.channel = CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;

  if (!esp_now_is_peer_exist(mac)) {
    esp_err_t err = esp_now_add_peer(&peer);

    if (err != ESP_OK) {
      Serial.printf(
        "ERROR: add peer failed: %d\n", err
      );
      return false;
    }
  }

  memcpy(peerMac, mac, 6);
  peerKnown = true;

  Serial.printf(
    "DISCOVERED peer %s\n",
    macText(peerMac).c_str()
  );

  return true;
}

// =========================================
// Queue outgoing messages
// =========================================

bool queueMessage(
  const uint8_t *mac,
  MessageType type,
  uint32_t seq = 0,
  bool urgent = false
) {

  TxRequest request = {};

  memcpy(request.mac, mac, 6);

  request.msg.magic = MAGIC;
  request.msg.type = type;
  request.msg.bootId = myBootId;
  request.msg.sequence = seq;
  request.msg.replyBootId = peerBootId;

  QueueHandle_t queue =
    urgent ? urgentQueue : txQueue;

  if (xQueueSend(queue, &request, 0) != pdTRUE) {
    Serial.println("WARNING: TX queue full");
    return false;
  }

  return true;
}

// =========================================
// Alarm Management & Serial (Raspberry Pi) API
// =========================================

void triggerLocalAlarm(const char *source) {
  if (!alarmActive) {
    alarmActive = true;
    buzzerOn();
    updateLcdStatus(true);

    Serial.println();
    Serial.println("EVENT:ALARM_STARTED");
    Serial.println("*************************************************");
    Serial.printf(" 🚨 LOCAL ALARM TRIGGERED (%s)! Buzzer ON!\n", source);
    Serial.println(" 👉 Press Button 1 (GPIO 4) on remote node to cancel.");
    Serial.println("*************************************************");

    if (peerKnown) {
      nextSequence++;
      queueMessage(peerMac, ALARM_TRIGGER, nextSequence, true);
    } else {
      Serial.println("Notice: Alarm active locally (peer not yet discovered).");
    }
  }
}

void cancelLocalAlarm(const char *source) {
  if (alarmActive) {
    alarmActive = false;
    buzzerOff();
    updateLcdStatus(true);

    Serial.println();
    Serial.println("EVENT:ALARM_CANCELLED_LOCAL");
    Serial.println("*************************************************");
    Serial.printf(" 🔇 Local alarm silenced (%s).\n", source);
    Serial.println("*************************************************");
  }
}

void handleSerialCommand(const char *cmd) {
  if (strcmp(cmd, "ALARM_ON") == 0 || strcmp(cmd, "START_ALARM") == 0 || strcmp(cmd, "TRIGGER") == 0) {
    triggerLocalAlarm("Raspberry Pi USB command");
  } else if (strcmp(cmd, "ALARM_OFF") == 0 || strcmp(cmd, "STOP_ALARM") == 0 || strcmp(cmd, "CANCEL") == 0) {
    cancelLocalAlarm("Raspberry Pi USB command");
    if (peerKnown) {
      nextSequence++;
      queueMessage(peerMac, ALARM_CANCEL, nextSequence, true);
    }
  } else if (strcmp(cmd, "PING") == 0) {
    Serial.println("PONG:ESP32_OK");
  } else if (strcmp(cmd, "STATUS") == 0) {
    Serial.printf("STATUS:ALARM=%s,PEER=%s\n",
      alarmActive ? "ACTIVE" : "IDLE",
      peerKnown ? "CONNECTED" : "DISCONNECTED"
    );
  }
}

void processSerialInput() {
  static char serialBuf[64];
  static uint8_t bufIdx = 0;

  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (bufIdx > 0) {
        serialBuf[bufIdx] = '\0';
        handleSerialCommand(serialBuf);
        bufIdx = 0;
      }
    } else if (bufIdx < sizeof(serialBuf) - 1) {
      serialBuf[bufIdx++] = c;
    }
  }
}

// =========================================
// ESP-NOW receive callback
// =========================================

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5,0,0)
void onReceive(
  const esp_now_recv_info_t *info,
  const uint8_t *data,
  int len
) {
  if (!info || len != sizeof(Message)) {
    return;
  }

  RxEvent event = {};

  memcpy(&event.msg, data, sizeof(Message));

  if (event.msg.magic != MAGIC) {
    return;
  }

  memcpy(event.mac, info->src_addr, 6);

  event.rssi = info->rx_ctrl
    ? info->rx_ctrl->rssi : 0;

  xQueueSend(rxQueue, &event, 0);
}
#else
void onReceive(
  const uint8_t *mac,
  const uint8_t *data,
  int len
) {
  if (!mac || len != sizeof(Message)) {
    return;
  }

  RxEvent event = {};

  memcpy(&event.msg, data, sizeof(Message));

  if (event.msg.magic != MAGIC) {
    return;
  }

  memcpy(event.mac, mac, 6);

  event.rssi = 0;

  xQueueSend(rxQueue, &event, 0);
}
#endif

// =========================================
// Send completion callback
// Supports older/newer ESP-IDF API
// =========================================

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5,5,0)
void onSent(
  const esp_now_send_info_t *info,
  esp_now_send_status_t status
) {
#else
void onSent(
  const uint8_t *mac,
  esp_now_send_status_t status
) {
#endif
  uint8_t result =
    status == ESP_NOW_SEND_SUCCESS ? 1 : 0;

  xQueueSend(txResultQueue, &result, 0);
}

// =========================================
// Incoming message processing
// =========================================

void processReceived(const RxEvent &event) {

  const Message &msg = event.msg;

  // Ignore ourselves, if loopback is seen.
  if (memcmp(event.mac, myMac, 6) == 0) {
    return;
  }

  // Only one remote peer in this version.
  if (!registerPeer(event.mac)) {
    return;
  }

  // Detect remote board restart.
  if (peerBootId != msg.bootId) {

    if (peerBootId != 0) {
      Serial.println(
        "*** PEER RESTART DETECTED ***"
      );
    }

    peerBootId = msg.bootId;
    peerHealthy = false;
    awaitingPong = false;

    Serial.printf(
      "Peer boot ID: %08lX\n",
      (unsigned long)peerBootId
    );
  }

  switch (msg.type) {

    case HELLO:
      // Reply immediately through priority queue.
      queueMessage(
        peerMac, HELLO_ACK, 0, true
      );
      break;

    case HELLO_ACK:
      // Discovery alone does not mean healthy.
      break;

    case PING:
      lastRssi = event.rssi;
      Serial.printf(
        "RX PING #%lu | RSSI %d dBm\n",
        (unsigned long)msg.sequence,
        event.rssi
      );

      // PONG has priority over normal traffic.
      queueMessage(
        peerMac, PONG,
        msg.sequence, true
      );
      break;

    case PONG:
      // A valid response must match our
      // current outstanding PING.
      if (awaitingPong &&
          msg.sequence == pendingSequence &&
          msg.replyBootId == myBootId) {

        uint32_t rtt =
          millis() - pendingSince;

        awaitingPong = false;
        lastPong = millis();
        pingOK++;
        lastRtt = rtt;
        lastRssi = event.rssi;

        if (!peerHealthy) {
          peerHealthy = true;
          Serial.println(
            "*** PEER HEALTHY ***"
          );
        }

        Serial.printf(
          "PONG #%lu | RTT %lu ms | "
          "RSSI %d dBm | OK %lu | Lost %lu\n",
          (unsigned long)msg.sequence,
          (unsigned long)rtt,
          event.rssi,
          (unsigned long)pingOK,
          (unsigned long)pingLost
        );
      }
      break;

    case ALARM_TRIGGER:
      remoteAlarmActive = true;
      updateLcdStatus(true);
      Serial.println();
      Serial.println("*************************************************");
      Serial.printf(" 🚨 REMOTE ALARM TRIGGERED! Peer: %s | RSSI: %d dBm\n",
        macText(event.mac).c_str(), event.rssi);
      Serial.println(" 👉 Press Button 1 (GPIO 4) to cancel remote alarm!");
      Serial.println("*************************************************");
      break;

    case ALARM_CANCEL:
      remoteAlarmActive = false;
      if (alarmActive) {
        alarmActive = false;
        buzzerOff();
        Serial.println();
        Serial.println("EVENT:ALARM_CANCELLED");
        Serial.println("*************************************************");
        Serial.printf(" ✅ ALARM CANCELLED BY REMOTE NODE (%s)!\n", macText(event.mac).c_str());
        Serial.println(" 🔇 Buzzer turned OFF.");
        Serial.println("*************************************************");
      } else {
        Serial.println("ℹ️ Received ALARM_CANCEL, but alarm was not active.");
      }
      updateLcdStatus(true);

      // Send ACK back to the remote cancelling node
      queueMessage(peerMac, ALARM_CANCEL_ACK, msg.sequence, true);
      break;

    case ALARM_CANCEL_ACK:
      remoteAlarmActive = false;
      updateLcdStatus(true);
      Serial.println();
      Serial.println("*************************************************");
      Serial.println(" ✅ REMOTE CONFIRMED: Alarm cancelled & buzzer OFF!");
      Serial.println("*************************************************");
      break;
  }
}

// =========================================
// TX scheduler
// Only one ESP-NOW transmission in flight
// =========================================

void processTransmitter() {

  uint8_t result;

  // Process WiFi MAC send completion.
  if (xQueueReceive(
        txResultQueue, &result, 0
      ) == pdTRUE) {

    txBusy = false;
    lastTxFinished = millis();

    if (!result) {
      macFailures++;

      Serial.printf(
        "MAC TX FAILURE | Total %lu\n",
        (unsigned long)macFailures
      );
    }
  }

  if (txBusy) {
    return;
  }

  if (millis() - lastTxFinished < TX_GAP) {
    return;
  }

  TxRequest request;

  // Urgent queue gets first priority.
  bool hasMessage =
    xQueueReceive(
      urgentQueue, &request, 0
    ) == pdTRUE;

  if (!hasMessage) {
    hasMessage =
      xQueueReceive(
        txQueue, &request, 0
      ) == pdTRUE;
  }

  if (!hasMessage) {
    return;
  }

  esp_err_t resultCode = esp_now_send(
    request.mac,
    (const uint8_t *)&request.msg,
    sizeof(request.msg)
  );

  if (resultCode == ESP_OK) {

    txBusy = true;

    if (request.msg.type == PING) {
      awaitingPong = true;
      pendingSequence =
        request.msg.sequence;
      pendingSince = millis();

      Serial.printf(
        "TX PING #%lu\n",
        (unsigned long)pendingSequence
      );
    }

  } else {

    Serial.printf(
      "esp_now_send ERROR: %d (%s)\n",
      resultCode,
      typeName(request.msg.type)
    );

    macFailures++;
    lastTxFinished = millis();
  }
}

// =========================================
// Setup
// =========================================

void setup() {

  Serial.begin(115200);
  delay(1500);
  Serial.println();
  Serial.println("========================================");
  Serial.println(" ESP32-S3 BOOTING - INITIALIZING HARDWARE");
  Serial.println("========================================");
  Serial.flush();

  pinMode(BUTTON1_PIN, INPUT_PULLUP);
  pinMode(BUTTON2_PIN, INPUT_PULLUP);
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  buzzerOff();

  // Initialize I2C for LCD
  Wire.begin(LCD_SDA_PIN, LCD_SCL_PIN);
  Wire.setTimeOut(50); // Prevent hanging if I2C bus is held low or unpowered
  delay(100);

  // Auto-detect PCF8574 address: 0x27 or 0x3F
  uint8_t lcdAddr = 0;
  Wire.beginTransmission(0x27);
  if (Wire.endTransmission() == 0) {
    lcdAddr = 0x27;
  } else {
    Wire.beginTransmission(0x3F);
    if (Wire.endTransmission() == 0) {
      lcdAddr = 0x3F;
    }
  }

  if (lcdAddr != 0) {
    lcd = LiquidCrystal_I2C(lcdAddr, LCD_COLS, LCD_ROWS);
    lcd.init();
    lcd.backlight();
    lcdAvailable = true;
    Serial.printf("LCD initialized at I2C 0x%02X\n", lcdAddr);

    lcd.setCursor(0, 0);
    lcd.print("ESP-NOW MESH v4 ");
    lcd.setCursor(0, 1);
    lcd.print("STARTING...     ");
  } else {
    Serial.println("Notice: No I2C LCD detected at 0x27 or 0x3F (running without LCD)");
  }

  Serial.println();
  Serial.println("==========================");
  Serial.println(" ESP-NOW RELIABLE v4");
  Serial.println("==========================");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  esp_err_t channelResult =
    esp_wifi_set_channel(
      CHANNEL, WIFI_SECOND_CHAN_NONE
    );

  if (channelResult != ESP_OK) {
    Serial.printf(
      "Channel setup error: %d\n",
      channelResult
    );
    while (true) delay(1000);
  }

  if (esp_wifi_get_mac(
        WIFI_IF_STA, myMac
      ) != ESP_OK) {
    Serial.println("MAC read failed");
    while (true) delay(1000);
  }

  Serial.printf(
    "MAC: %s\n",
    macText(myMac).c_str()
  );

  myBootId = esp_random();
  if (myBootId == 0) myBootId = 1;

  Serial.printf(
    "Boot ID: %08lX\n",
    (unsigned long)myBootId
  );

  Serial.printf(
    "Channel: %u\n", CHANNEL
  );

  rxQueue = xQueueCreate(
    32, sizeof(RxEvent)
  );

  txQueue = xQueueCreate(
    16, sizeof(TxRequest)
  );

  urgentQueue = xQueueCreate(
    16, sizeof(TxRequest)
  );

  txResultQueue = xQueueCreate(
    8, sizeof(uint8_t)
  );

  if (!rxQueue || !txQueue ||
      !urgentQueue || !txResultQueue) {

    Serial.println("Queue creation failed");
    while (true) delay(1000);
  }

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    while (true) delay(1000);
  }

  esp_now_register_recv_cb(onReceive);
  esp_now_register_send_cb(onSent);

  esp_now_peer_info_t broadcastPeer = {};

  memcpy(
    broadcastPeer.peer_addr,
    BROADCAST, 6
  );

  broadcastPeer.channel = CHANNEL;
  broadcastPeer.ifidx = WIFI_IF_STA;
  broadcastPeer.encrypt = false;

  if (esp_now_add_peer(
        &broadcastPeer
      ) != ESP_OK) {

    Serial.println("Broadcast peer failed");
    while (true) delay(1000);
  }

  // Different timing on each board.
  randomSeed(esp_random());

  lastPing = millis() + random(0, 500);
  lastHello = millis() - HELLO_INTERVAL;

  Serial.println("Starting discovery...");
}

// =========================================
// Main loop
// =========================================

void loop() {

  RxEvent event;

  // Process received radio packets.
  while (xQueueReceive(
           rxQueue, &event, 0
         ) == pdTRUE) {

    processReceived(event);
  }

  uint32_t now = millis();

  // HELLO broadcast continues forever.
  if (now - lastHello >= HELLO_INTERVAL) {

    lastHello = now;

    queueMessage(
      BROADCAST, HELLO
    );

    if (!peerKnown) {
      Serial.println("Searching...");
    }
  }

  // Application-level PONG timeout.
  if (awaitingPong &&
      now - pendingSince >= PONG_TIMEOUT) {

    awaitingPong = false;
    pingLost++;

    Serial.printf(
      "PING TIMEOUT | Lost %lu\n",
      (unsigned long)pingLost
    );
  }

  // Health requires recent successful PONG.
  if (peerHealthy &&
      now - lastPong >= OFFLINE_TIMEOUT) {

    peerHealthy = false;

    Serial.println(
      "*** PEER UNHEALTHY / OFFLINE ***"
    );
  }

  // PING a discovered peer even when
  // it is currently marked unhealthy.
  if (peerKnown &&
      !awaitingPong &&
      now - lastPing >= PING_INTERVAL) {

    lastPing = now;
    nextSequence++;

    queueMessage(
      peerMac, PING, nextSequence
    );
  }

  // =========================================
  // Process Serial Commands from Raspberry Pi
  // =========================================
  processSerialInput();

  // =========================================
  // Button 2: Trigger Alarm (Single Press)
  // GPIO 6 (external button to GND) or GPIO 0 (onboard BOOT button)
  // =========================================
  bool b2State = (digitalRead(BUTTON2_PIN) == LOW) || (digitalRead(BOOT_BUTTON_PIN) == LOW);
  static bool b2LastState = false;
  static uint32_t b2DebounceTime = 0;

  if (b2State && !b2LastState && (now - b2DebounceTime > 200)) {
    b2DebounceTime = now;
    b2LastState = true;
    triggerLocalAlarm("Button 2");
  } else if (!b2State && b2LastState && (now - b2DebounceTime > 50)) {
    b2LastState = false;
  }

  // =========================================
  // Button 1: Cancel Alarm (Single Press)
  // GPIO 4 (external button to GND)
  // =========================================
  bool b1State = (digitalRead(BUTTON1_PIN) == LOW);
  static bool b1LastState = false;
  static uint32_t b1DebounceTime = 0;

  if (b1State && !b1LastState && (now - b1DebounceTime > 200)) {
    b1DebounceTime = now;
    b1LastState = true;

    Serial.println("\n>>> Button 1 pressed: Sending ALARM_CANCEL to remote node... <<<");
    cancelLocalAlarm("Button 1");

    if (peerKnown) {
      nextSequence++;
      queueMessage(peerMac, ALARM_CANCEL, nextSequence, true);
    } else {
      Serial.println("Notice: Peer not discovered yet.");
    }
  } else if (!b1State && b1LastState && (now - b1DebounceTime > 50)) {
    b1LastState = false;
  }

  processTransmitter();

  // =========================================
  // Update 16x2 I2C LCD Status Display
  // =========================================
  updateLcdStatus();

  delay(2);
}
