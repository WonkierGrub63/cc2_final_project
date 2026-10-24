#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_idf_version.h>

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
// Push Button Configuration
// Connect an external momentary push button between GPIO 4 and GND.
// (Uses internal pull-up resistor, no external resistor required)
// =========================================
constexpr uint8_t BUTTON_PIN = 4;
constexpr uint32_t BUTTON_SEND_INTERVAL = 500; // Repeat interval while held (ms)

enum MessageType : uint8_t {
  HELLO = 1,
  HELLO_ACK = 2,
  PING = 3,
  PONG = 4,
  BUTTON_ALERT = 5,
  BUTTON_ACK = 6
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
    case BUTTON_ALERT: return "BUTTON_ALERT";
    case BUTTON_ACK: return "BUTTON_ACK";
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

    case BUTTON_ALERT:
      Serial.println();
      Serial.println("*************************************************");
      Serial.printf(" 🚨 REMOTE BUTTON PRESSED! Seq: #%lu | RSSI: %d dBm\n",
        (unsigned long)msg.sequence, event.rssi);
      Serial.println("*************************************************");

      // Acknowledge the button press back to sender with urgent priority
      queueMessage(
        peerMac, BUTTON_ACK,
        msg.sequence, true
      );
      break;

    case BUTTON_ACK:
      Serial.printf(
        "✅ REMOTE ACKNOWLEDGED BUTTON! Seq: #%lu | RSSI: %d dBm\n",
        (unsigned long)msg.sequence, event.rssi
      );
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

  pinMode(BUTTON_PIN, INPUT_PULLUP);

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
  // Push Button Handling
  // =========================================
  bool buttonPressed = (digitalRead(BUTTON_PIN) == LOW); // Active LOW with INPUT_PULLUP
  static uint32_t lastButtonSend = 0;
  static bool buttonWasPressed = false;

  if (buttonPressed) {
    if (!buttonWasPressed) {
      buttonWasPressed = true;
      lastButtonSend = now;

      if (peerKnown) {
        nextSequence++;
        Serial.printf(
          "\n>>> BUTTON HELD: Sending ALERT #%lu to %s <<<\n",
          (unsigned long)nextSequence, macText(peerMac).c_str()
        );
        queueMessage(peerMac, BUTTON_ALERT, nextSequence, true);
      } else {
        Serial.println(">>> Button pressed, but peer not discovered yet <<<");
      }
    } else if (now - lastButtonSend >= BUTTON_SEND_INTERVAL) {
      lastButtonSend = now;
      if (peerKnown) {
        nextSequence++;
        Serial.printf(
          ">>> BUTTON HELD: Sending ALERT #%lu <<<\n",
          (unsigned long)nextSequence
        );
        queueMessage(peerMac, BUTTON_ALERT, nextSequence, true);
      }
    }
  } else {
    if (buttonWasPressed) {
      buttonWasPressed = false;
      Serial.println(">>> Button released <<<\n");
    }
  }

  processTransmitter();

  delay(2);
}
