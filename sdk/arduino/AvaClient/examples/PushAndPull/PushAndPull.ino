/*
  PushAndPull — a networked board that BOTH pushes readings and answers Ava's
  on-demand reads/commands over HTTP (the pull path).

  Board-agnostic: WiFi is just the illustration; any Client/Server pair works.

  In the device connector's connector.yaml, point the pull path at this board:
    actions:
      discover:
        base: "http://<board-ip>:9001"
        list: "/tools"
        call: "/call"
  Then `ava connector policies <id> --write` and deploy, so the agent may reach it.
*/
#include <WiFi.h>
#include <AvaClient.h>

const char *WIFI_SSID = "your-wifi";
const char *WIFI_PASS = "your-pass";
const char *AVA_HOST  = "192.168.1.50";
const char *AVA_CID   = "greenhouse";
const char *AVA_TOKEN = "PASTE_TOKEN";

WiFiClient net;
WiFiServer server(9001);   // serves /tools + /call for the pull path
AvaClient ava;
bool relayOn = false;

double readTemperature() { return 21.5; }              // your sensor

String setRelay(const String &args) {                  // your actuator
  relayOn = AvaClient::argBool(args, "on");
  digitalWrite(LED_BUILTIN, relayOn ? HIGH : LOW);
  return relayOn ? "Relay is now ON" : "Relay is now OFF";
}

unsigned long lastPush = 0;

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);

  ava.begin(AVA_HOST, 8096, AVA_CID, AVA_TOKEN);
  ava.setPushClient(net);
  ava.addSensor("read_temperature", "C", "Read the temperature", readTemperature);
  ava.addCommand("set_relay", "Turn the relay on or off", setRelay);
  server.begin();
}

void loop() {
  // PULL: answer one request if a client is knocking.
  WiFiClient c = server.available();
  if (c) ava.handleHttp(c);

  // PUSH: a reading every 30s.
  if (millis() - lastPush > 30000) {
    lastPush = millis();
    ava.reading("temperature", readTemperature(), "C");
  }
}
