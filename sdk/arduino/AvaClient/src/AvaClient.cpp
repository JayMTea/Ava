#include "AvaClient.h"

AvaClient::AvaClient()
    : host_(nullptr), port_(8096), cid_(nullptr), token_(nullptr),
      tls_(false), push_(nullptr), nTools_(0) {}

void AvaClient::begin(const char *host, uint16_t port, const char *cid,
                      const char *token, bool tls) {
  host_ = host;
  port_ = port ? port : (tls ? 443 : 80);
  cid_ = cid;
  token_ = token;
  tls_ = tls;
}

void AvaClient::setPushClient(Client &client) { push_ = &client; }

// ── PUSH ─────────────────────────────────────────────────────────────────────
bool AvaClient::reading(const char *name, double value, const char *unit) {
  String b = "{\"type\":\"reading\",\"name\":\"";
  b += jsonEscape(name);
  b += "\",\"value\":";
  b += String(value, 4);
  if (unit) { b += ",\"unit\":\""; b += jsonEscape(unit); b += "\""; }
  b += "}";
  return postEvent(b);
}

bool AvaClient::event(const char *name, const char *message,
                      const char *severity, bool notify) {
  String b = "{\"type\":\"event\",\"name\":\"";
  b += jsonEscape(name);
  b += "\"";
  if (message) { b += ",\"message\":\""; b += jsonEscape(message); b += "\""; }
  if (severity) { b += ",\"severity\":\""; b += jsonEscape(severity); b += "\""; }
  if (notify) b += ",\"notify\":true";
  b += "}";
  return postEvent(b);
}

bool AvaClient::postEvent(const String &body) {
  if (!push_ || !host_ || !cid_) return false;
  if (!push_->connect(host_, port_)) return false;
  push_->print(F("POST /api/connectors/"));
  push_->print(cid_);
  push_->print(F("/events HTTP/1.1\r\nHost: "));
  push_->print(host_);
  push_->print(F("\r\nAuthorization: Bearer "));
  push_->print(token_ ? token_ : "");
  push_->print(F("\r\nContent-Type: application/json\r\nContent-Length: "));
  push_->print(body.length());
  push_->print(F("\r\nConnection: close\r\n\r\n"));
  push_->print(body);

  unsigned long t0 = millis();
  while (!push_->available() && millis() - t0 < 5000) delay(1);
  String status = push_->readStringUntil('\n');  // "HTTP/1.1 200 OK"
  push_->stop();
  int sp = status.indexOf(' ');
  int code = (sp > 0) ? status.substring(sp + 1, sp + 4).toInt() : 0;
  return code >= 200 && code < 300;
}

void AvaClient::emitReading(Stream &io, const char *name, double value,
                            const char *unit) {
  io.print(F("{\"push\":{\"type\":\"reading\",\"name\":\""));
  io.print(jsonEscape(name));
  io.print(F("\",\"value\":"));
  io.print(String(value, 4));
  if (unit) { io.print(F(",\"unit\":\"")); io.print(jsonEscape(unit)); io.print(F("\"")); }
  io.print(F("}}\n"));
}

void AvaClient::emitEvent(Stream &io, const char *name, const char *message,
                          const char *severity, bool notify) {
  io.print(F("{\"push\":{\"type\":\"event\",\"name\":\""));
  io.print(jsonEscape(name));
  io.print(F("\""));
  if (message) { io.print(F(",\"message\":\"")); io.print(jsonEscape(message)); io.print(F("\"")); }
  if (severity) { io.print(F(",\"severity\":\"")); io.print(jsonEscape(severity)); io.print(F("\"")); }
  if (notify) io.print(F(",\"notify\":true"));
  io.print(F("}}\n"));
}

// ── PULL registration ────────────────────────────────────────────────────────
bool AvaClient::addSensor(const char *name, const char *unit,
                          const char *description, AvaSensorFn fn) {
  if (nTools_ >= AVA_MAX_TOOLS) return false;
  tools_[nTools_++] = {name, description, unit, fn, nullptr};
  return true;
}

bool AvaClient::addCommand(const char *name, const char *description,
                           AvaCommandFn fn) {
  if (nTools_ >= AVA_MAX_TOOLS) return false;
  tools_[nTools_++] = {name, description, nullptr, nullptr, fn};
  return true;
}

String AvaClient::toolsJson() {
  String out = "{\"tools\":[";
  for (uint8_t i = 0; i < nTools_; i++) {
    if (i) out += ",";
    out += "{\"name\":\"";
    out += jsonEscape(tools_[i].name);
    out += "\",\"description\":\"";
    out += jsonEscape(tools_[i].desc ? tools_[i].desc : "");
    out += "\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}}";
  }
  out += "]}";
  return out;
}

String AvaClient::dispatch(const String &name, const String &argsJson) {
  for (uint8_t i = 0; i < nTools_; i++) {
    if (name == tools_[i].name) {
      if (tools_[i].sensor) {
        double v = tools_[i].sensor();
        String t = name + ": " + String(v, 4);
        if (tools_[i].unit) { t += " "; t += tools_[i].unit; }
        return t;
      }
      if (tools_[i].command) return tools_[i].command(argsJson);
    }
  }
  return String("unknown tool: ") + name;
}

// ── PULL over a Stream (host-adapter relay) ──────────────────────────────────
static String extractObject(const String &json, const char *key) {
  String needle = String("\"") + key + "\"";
  int k = json.indexOf(needle);
  if (k < 0) return "{}";
  int b = json.indexOf('{', k);
  if (b < 0) return "{}";
  int depth = 0;
  for (int i = b; i < (int)json.length(); i++) {
    char c = json[i];
    if (c == '{') depth++;
    else if (c == '}') { depth--; if (depth == 0) return json.substring(b, i + 1); }
  }
  return "{}";
}

bool AvaClient::handleStream(Stream &io) {
  if (!io.available()) return false;
  String line = io.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return false;
  if (line.indexOf("\"req\":\"tools\"") >= 0) {
    io.print(F("{\"resp\":\"tools\",\"tools\":"));
    String tj = toolsJson();               // {"tools":[...]}
    int lb = tj.indexOf('['), rb = tj.lastIndexOf(']');
    io.print(tj.substring(lb, rb + 1));     // just the [...] array
    io.print(F("}\n"));
    return true;
  }
  if (line.indexOf("\"req\":\"call\"") >= 0) {
    String name = argStr(line, "name");
    String args = extractObject(line, "arguments");
    String text = dispatch(name, args);
    io.print(F("{\"resp\":\"call\",\"text\":\""));
    io.print(jsonEscape(text.c_str()));
    io.print(F("\"}\n"));
    return true;
  }
  return false;
}

// ── PULL over HTTP (networked board) ─────────────────────────────────────────
static void sendHttp(Client &c, int code, const char *status, const String &json) {
  c.print(F("HTTP/1.1 "));
  c.print(code);
  c.print(' ');
  c.print(status);
  c.print(F("\r\nContent-Type: application/json\r\nContent-Length: "));
  c.print(json.length());
  c.print(F("\r\nConnection: close\r\n\r\n"));
  c.print(json);
}

void AvaClient::handleHttp(Client &client) {
  String reqLine = client.readStringUntil('\n');
  int s1 = reqLine.indexOf(' ');
  int s2 = reqLine.indexOf(' ', s1 + 1);
  if (s1 < 0 || s2 < 0) { client.stop(); return; }
  String method = reqLine.substring(0, s1);
  String path = reqLine.substring(s1 + 1, s2);
  int q = path.indexOf('?');
  if (q >= 0) path = path.substring(0, q);

  int contentLen = 0;
  while (true) {
    String h = client.readStringUntil('\n');
    if (h.length() <= 1) break;  // blank line ("\r")
    String hl = h; hl.toLowerCase();
    if (hl.startsWith("content-length:"))
      contentLen = hl.substring(15).toInt();
  }

  if (method == "GET" && path == "/health") {
    sendHttp(client, 200, "OK", "{\"ok\":true}");
  } else if (method == "GET" && path == "/tools") {
    sendHttp(client, 200, "OK", toolsJson());
  } else if (method == "POST" && path == "/call") {
    String body;
    unsigned long t0 = millis();
    while ((int)body.length() < contentLen && millis() - t0 < 3000) {
      if (client.available()) body += (char)client.read();
    }
    String name = argStr(body, "name");
    String args = extractObject(body, "arguments");
    String text = dispatch(name, args);
    String out = "{\"text\":\"";
    out += jsonEscape(text.c_str());
    out += "\"}";
    sendHttp(client, 200, "OK", out);
  } else {
    sendHttp(client, 404, "Not Found", "{\"error\":\"not found\"}");
  }
  client.stop();
}

// ── minimal JSON helpers ─────────────────────────────────────────────────────
String AvaClient::jsonEscape(const char *s) {
  String out;
  if (!s) return out;
  for (const char *p = s; *p; p++) {
    char c = *p;
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if (c == '\n') out += "\\n";
    else if (c == '\r') out += "\\r";
    else if (c == '\t') out += "\\t";
    else out += c;
  }
  return out;
}

String AvaClient::argStr(const String &json, const char *key) {
  String needle = String("\"") + key + "\"";
  int k = json.indexOf(needle);
  if (k < 0) return String();
  int colon = json.indexOf(':', k + needle.length());
  if (colon < 0) return String();
  int q1 = json.indexOf('"', colon + 1);
  if (q1 < 0) return String();
  int q2 = json.indexOf('"', q1 + 1);
  if (q2 < 0) return String();
  return json.substring(q1 + 1, q2);
}

double AvaClient::argNum(const String &json, const char *key, double dflt) {
  String needle = String("\"") + key + "\"";
  int k = json.indexOf(needle);
  if (k < 0) return dflt;
  int colon = json.indexOf(':', k + needle.length());
  if (colon < 0) return dflt;
  int i = colon + 1;
  while (i < (int)json.length() && (json[i] == ' ' || json[i] == '\t')) i++;
  int j = i;
  while (j < (int)json.length() &&
         (isdigit(json[j]) || json[j] == '-' || json[j] == '+' ||
          json[j] == '.' || json[j] == 'e' || json[j] == 'E')) j++;
  if (j == i) return dflt;
  return json.substring(i, j).toFloat();
}

bool AvaClient::argBool(const String &json, const char *key, bool dflt) {
  String needle = String("\"") + key + "\"";
  int k = json.indexOf(needle);
  if (k < 0) return dflt;
  int colon = json.indexOf(':', k + needle.length());
  if (colon < 0) return dflt;
  String rest = json.substring(colon + 1);
  rest.trim();
  if (rest.startsWith("true")) return true;
  if (rest.startsWith("false")) return false;
  if (rest.startsWith("1")) return true;
  if (rest.startsWith("0")) return false;
  return dflt;
}
