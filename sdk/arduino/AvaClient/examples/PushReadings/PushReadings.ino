/*
  PushReadings — a networked board hands Ava readings and events.

  This uses WiFi only as an illustration. AvaClient is board-agnostic: swap the
  include and the client type below for whatever YOUR board has (ESP8266WiFi.h,
  Ethernet.h + EthernetClient, a cellular client, ...). AvaClient just needs a
  Client to POST through.

  Setup in Ava:
    ava device new greenhouse          # create the connector
    ava device token greenhouse        # copy the token into AVA_TOKEN below
*/
#include <WiFi.h>          // <-- your board's networking library
#include <AvaClient.h>

const char *WIFI_SSID = "your-wifi";
const char *WIFI_PASS = "your-pass";
const char *AVA_HOST  = "192.168.1.50";   // where Ava runs
const uint16_t AVA_PORT = 8096;
const char *AVA_CID   = "greenhouse";
const char *AVA_TOKEN = "PASTE_TOKEN_FROM_ava_device_token";

WiFiClient net;            // <-- any Arduino Client works here
AvaClient ava;

float readTemperature() {
  // Replace with your real sensor read.
  return 21.5;
}

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print('.'); }
  Serial.println("\nWiFi up");

  ava.begin(AVA_HOST, AVA_PORT, AVA_CID, AVA_TOKEN);
  ava.setPushClient(net);
  ava.event("boot", "greenhouse online", "info", false);
}

void loop() {
  float t = readTemperature();
  ava.reading("temperature", t, "C");
  if (t > 30.0) ava.event("overheat", "Greenhouse is too hot", "warn", true);
  delay(30000);   // every 30s
}
