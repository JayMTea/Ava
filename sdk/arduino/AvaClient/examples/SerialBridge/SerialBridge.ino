/*
  SerialBridge — a board with NO networking (classic Uno/Nano, or any board over
  USB). It answers pull requests and emits readings over the serial line; the
  host adapter (sdk/host/examples/serial_bridge.py) relays both to Ava.

  Nothing board-specific here — it works on anything with a Serial port.

  IMPORTANT: in this mode Serial carries the protocol, so do NOT print debug
  text to Serial — it would corrupt the JSON lines. Use another port for debug.
*/
#include <AvaClient.h>

AvaClient ava;
bool relayOn = false;

double readTemperature() {
  // Replace with your real sensor (e.g. analogRead + conversion).
  return 21.5;
}

String setRelay(const String &args) {
  relayOn = AvaClient::argBool(args, "on");
  digitalWrite(LED_BUILTIN, relayOn ? HIGH : LOW);
  return relayOn ? "Relay is now ON" : "Relay is now OFF";
}

unsigned long lastPush = 0;

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  ava.addSensor("read_temperature", "C", "Read the temperature", readTemperature);
  ava.addCommand("set_relay", "Turn the relay on or off", setRelay);
}

void loop() {
  // PULL: answer a {"req":...} line from the host adapter if one arrived.
  ava.handleStream(Serial);

  // PUSH: emit a reading every 30s; the host forwards it to Ava.
  if (millis() - lastPush > 30000) {
    lastPush = millis();
    ava.emitReading(Serial, "temperature", readTemperature(), "C");
  }
}
