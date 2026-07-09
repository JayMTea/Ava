/*
  AvaClient — wire any board to your self-hosted Ava assistant.

  Board-agnostic by design: you bring the transport, AvaClient handles the two
  contracts Ava speaks (see docs/DEVICE_CONNECTORS.md).

    PUSH  device -> Ava, when your code decides something happened
          ava.reading("temperature", 21.5, "C");
          ava.event("motion", "Front door", "warn", true);

    PULL  Ava -> device, on demand (agent reads a sensor / runs a command)
          ava.addSensor("read_temperature", "C", "Read the temperature", readTemp);
          ava.addCommand("set_relay", "Turn the relay on/off", setRelay);
          // then, per transport:
          ava.handleHttp(client);     // networked board serving /tools + /call
          ava.handleStream(Serial);   // USB board, relayed by the host adapter

  No external libraries. No hardcoded boards: push uses whatever Client you pass
  (WiFiClient, EthernetClient, ...); pull answers over any Client or Stream.
*/
#ifndef AVA_CLIENT_H
#define AVA_CLIENT_H

#include <Arduino.h>
#include <Client.h>

#ifndef AVA_MAX_TOOLS
#define AVA_MAX_TOOLS 12
#endif

typedef double (*AvaSensorFn)();
typedef String (*AvaCommandFn)(const String &argsJson);

class AvaClient {
 public:
  AvaClient();

  // Configure where Ava is and who this device is. `token` comes from
  // `ava device token <cid>`. `tls` only affects the default port (443 vs 80);
  // pass a TLS-capable Client to setPushClient() for real HTTPS.
  void begin(const char *host, uint16_t port, const char *cid,
             const char *token, bool tls = false);

  // The network client used to POST pushes (your WiFiClient/EthernetClient/...).
  // Not needed on serial-only boards that push via emit*() over a Stream.
  void setPushClient(Client &client);

  // ---- PUSH ----------------------------------------------------------------
  bool reading(const char *name, double value, const char *unit = nullptr);
  bool event(const char *name, const char *message = nullptr,
             const char *severity = nullptr, bool notify = false);

  // Push over a Stream instead of the network (serial-only boards): writes one
  // {"push":{...}}\n line for the host adapter to forward to Ava.
  void emitReading(Stream &io, const char *name, double value,
                   const char *unit = nullptr);
  void emitEvent(Stream &io, const char *name, const char *message = nullptr,
                 const char *severity = nullptr, bool notify = false);

  // ---- PULL registration ---------------------------------------------------
  bool addSensor(const char *name, const char *unit, const char *description,
                 AvaSensorFn fn);
  bool addCommand(const char *name, const char *description, AvaCommandFn fn);

  // ---- PULL transports -----------------------------------------------------
  // Answer one line-delimited JSON request on `io` if one is available
  // (host adapter relay). Call from loop(). Returns true if it handled a line.
  bool handleStream(Stream &io);

  // Serve one HTTP request on a connected client (GET /tools, POST /call,
  // GET /health). Call with each client your board's server accepts.
  void handleHttp(Client &client);

  // ---- arg helpers (minimal, for command callbacks) ------------------------
  static String argStr(const String &json, const char *key);
  static double argNum(const String &json, const char *key, double dflt = 0);
  static bool argBool(const String &json, const char *key, bool dflt = false);

 private:
  struct Entry {
    const char *name;
    const char *desc;
    const char *unit;   // sensors only
    AvaSensorFn sensor; // one of sensor/command is set
    AvaCommandFn command;
  };

  bool postEvent(const String &body);
  String toolsJson();
  String dispatch(const String &name, const String &argsJson);
  static String jsonEscape(const char *s);

  const char *host_;
  uint16_t port_;
  const char *cid_;
  const char *token_;
  bool tls_;
  Client *push_;
  Entry tools_[AVA_MAX_TOOLS];
  uint8_t nTools_;
};

#endif  // AVA_CLIENT_H
