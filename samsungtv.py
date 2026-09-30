#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
samsungtv.py – Lokale Steuerung für Samsung Tizen-TVs (2016–2019),
ausgelegt auf UE55KU6079U (KU-Serie, Modelljahr 2016).

Genutzte Schnittstellen (alle über LAN, keine Cloud nötig):
  * WebSocket-Remote  wss://<tv>:8002 (Token), Fallback ws://<tv>:8001
      -> Fernbedienungstasten, Long-Press, Texteingabe, App-Liste, App-Start
  * REST              http://<tv>:8001/api/v2/
      -> Geräteinfo / Erreichbarkeit, App-Status, App starten/beenden
  * UPnP/DLNA         http://<tv>:9197/dmr
      -> Lautstärke & Mute absolut setzen/lesen, Medien-URL abspielen
  * Wake-on-LAN       -> Einschalten aus dem Standby
  * optional SmartThings-Cloud -> Eingangsquelle (HDMI1 ...), TV-Kanal

Abhängigkeit:  pip install websocket-client

Beispiele:
  samsungtv.py --host 192.168.1.50 --mac AA:BB:CC:DD:EE:FF pair
  samsungtv.py on | off | state
  samsungtv.py keys "KEY_MENU, 1000, KEY_DOWN, KEY_ENTER, KEY_RETURN@3000"
  samsungtv.py volume 15 | volume +3 | mute toggle
  samsungtv.py apps | app YouTube | close YouTube
  samsungtv.py serve --bind 0.0.0.0 --port 8765      # HTTP-API für openHAB
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape as xml_escape

import websocket  # websocket-client

log = logging.getLogger("samsungtv")

CLIENT_NAME = "PythonTVControl"  # Name, unter dem der Client am TV erscheint – konstant halten!
TOKEN_FILE = Path(os.environ.get("SAMSUNGTV_TOKEN_FILE",
                                 Path.home() / ".config" / "samsungtv" / "tokens.json"))
INSTANT_ON_WINDOW = 65.0  # s, in denen der TV nach "Aus" noch im Netz antwortet


class TVError(Exception):
    """Fehler bei der Kommunikation mit dem TV."""


# --------------------------------------------------------------------------- Hilfen
def _http(method: str, url: str, data: bytes | None = None,
          headers: dict | None = None, timeout: float = 3.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def parse_bool(value: str | None) -> bool | None:
    """ON/OFF/true/false/1/0 -> bool, 'toggle'/leer -> None."""
    v = (value or "toggle").strip().lower()
    if v in ("on", "true", "1", "yes", "an", "ein"):
        return True
    if v in ("off", "false", "0", "no", "aus"):
        return False
    if v == "toggle":
        return None
    raise ValueError(f"Ungültiger Schaltwert: {value!r}")


def wake_on_lan(mac: str, broadcast: str = "255.255.255.255",
                extra_targets: tuple[str, ...] = (), repeats: int = 3) -> None:
    raw = bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", mac))
    if len(raw) != 6:
        raise ValueError(f"Ungültige MAC-Adresse: {mac}")
    packet = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeats):
            for target in (broadcast, *extra_targets):
                for port in (9, 7):
                    try:
                        s.sendto(packet, (target, port))
                    except OSError as e:
                        log.debug("WOL an %s:%s fehlgeschlagen: %s", target, port, e)
            time.sleep(0.1)


# Kommas außerhalb von "..." trennen
_SEQ_SPLIT = re.compile(r',(?=(?:[^"]*"[^"]*")*[^"]*$)')


def parse_sequence(seq: str) -> list[tuple]:
    """
    Syntax (angelehnt an den keyCode-Kanal des openHAB-Bindings):
      KEY_X            kurzer Tastendruck
      KEY_X@3000       Taste 3000 ms gedrückt halten
      1000             1000 ms Pause (ersetzt die Standardpause von 300 ms)
      "text"           Text in ein geöffnetes Eingabefeld schreiben
    """
    steps: list[tuple] = []
    for tok in _SEQ_SPLIT.split(seq):
        tok = tok.strip()
        if not tok:
            continue
        if len(tok) >= 2 and tok[0] == tok[-1] == '"':
            steps.append(("text", tok[1:-1]))
        elif tok.isdigit():
            steps.append(("sleep", int(tok)))
        else:
            m = re.fullmatch(r"(KEY_\w+)(?:@(\d+))?", tok, re.IGNORECASE)
            if not m:
                raise ValueError(f"Unbekanntes Sequenzelement: {tok!r}")
            key = m.group(1).upper()
            steps.append(("hold", key, int(m.group(2))) if m.group(2) else ("key", key))
    return steps


# --------------------------------------------------------------------------- Token
class TokenStore:
    def __init__(self, path: Path = TOKEN_FILE):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, host: str) -> str | None:
        return self._load().get(host)

    def set(self, host: str, token: str) -> None:
        data = self._load()
        data[host] = token
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
        os.chmod(self.path, 0o600)
        log.info("Token für %s gespeichert (%s)", host, self.path)


# --------------------------------------------------------------------------- REST
class Rest:
    def __init__(self, host: str):
        self.base = f"http://{host}:8001/api/v2"

    def info(self, timeout: float = 2.0) -> dict | None:
        try:
            _, body = _http("GET", self.base + "/", timeout=timeout)
            return json.loads(body)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def app_status(self, app_id: str) -> dict | None:
        try:
            _, body = _http("GET", f"{self.base}/applications/{app_id}")
            return json.loads(body)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def app_start(self, app_id: str) -> None:
        try:
            _http("POST", f"{self.base}/applications/{app_id}", data=b"")
        except urllib.error.HTTPError as e:
            raise TVError(f"App {app_id} nicht startbar (HTTP {e.code})") from e
        except OSError as e:
            raise TVError(f"App-Start fehlgeschlagen: {e}") from e

    def app_stop(self, app_id: str) -> None:
        try:
            _http("DELETE", f"{self.base}/applications/{app_id}")
        except urllib.error.HTTPError as e:
            raise TVError(f"App {app_id} nicht beendbar (HTTP {e.code})") from e
        except OSError as e:
            raise TVError(f"App-Stop fehlgeschlagen: {e}") from e


# --------------------------------------------------------------------------- UPnP
class UPnP:
    _NS = {"d": "urn:schemas-upnp-org:device-1-0"}

    def __init__(self, host: str, port: int = 9197):
        self.base = f"http://{host}:{port}"
        self._ctrl: dict[str, str] = {}

    def _control_url(self, service: str) -> str:
        if service in self._ctrl:
            return self._ctrl[service]
        url = f"{self.base}/upnp/control/{service}1"  # Samsung-Default
        try:
            _, body = _http("GET", f"{self.base}/dmr")
            for s in ET.fromstring(body).iter(f"{{{self._NS['d']}}}service"):
                if f":service:{service}:" in s.findtext("d:serviceType", "", self._NS):
                    url = self.base + "/" + s.findtext("d:controlURL", "", self._NS).lstrip("/")
                    break
            self._ctrl[service] = url  # nur bei Erfolg cachen
        except (OSError, ET.ParseError, urllib.error.URLError) as e:
            log.debug("dmr-Beschreibung nicht lesbar (%s), nutze %s", e, url)
        return url

    def call(self, service: str, action: str, **args) -> dict:
        st = f"urn:schemas-upnp-org:service:{service}:1"
        argxml = "".join(f"<{k}>{xml_escape(str(v))}</{k}>" for k, v in args.items())
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{st}">{argxml}</u:{action}>'
                '</s:Body></s:Envelope>')
        headers = {"Content-Type": 'text/xml; charset="utf-8"',
                   "SOAPACTION": f'"{st}#{action}"'}
        try:
            _, resp = _http("POST", self._control_url(service), body.encode(), headers)
        except urllib.error.HTTPError as e:
            raise TVError(f"UPnP {action}: HTTP {e.code}") from e
        except OSError as e:
            raise TVError(f"UPnP {action}: {e}") from e
        out = {}
        for el in ET.fromstring(resp).iter():
            if el.tag.endswith(f"{action}Response"):
                out = {c.tag.split("}")[-1]: c.text for c in el}
        return out

    def get_volume(self) -> int:
        return int(self.call("RenderingControl", "GetVolume",
                             InstanceID=0, Channel="Master")["CurrentVolume"])

    def set_volume(self, value: int) -> None:
        self.call("RenderingControl", "SetVolume", InstanceID=0, Channel="Master",
                  DesiredVolume=max(0, min(100, int(value))))

    def get_mute(self) -> bool:
        return self.call("RenderingControl", "GetMute", InstanceID=0,
                         Channel="Master")["CurrentMute"] in ("1", "true")

    def set_mute(self, mute: bool) -> None:
        self.call("RenderingControl", "SetMute", InstanceID=0, Channel="Master",
                  DesiredMute=1 if mute else 0)

    def play_url(self, url: str) -> None:
        self.call("AVTransport", "SetAVTransportURI", InstanceID=0,
                  CurrentURI=url, CurrentURIMetaData="")
        self.call("AVTransport", "Play", InstanceID=0, Speed=1)


# --------------------------------------------------------------------------- WebSocket
class Remote:
    def __init__(self, host: str, name: str = CLIENT_NAME, tokens: TokenStore | None = None,
                 timeout: float = 5.0, pair_timeout: float = 35.0, idle_timeout: float = 30.0):
        self.host, self.name = host, name
        self.tokens = tokens or TokenStore()
        self.timeout, self.pair_timeout, self.idle_timeout = timeout, pair_timeout, idle_timeout
        self._ws: websocket.WebSocket | None = None
        self._last_use = 0.0
        self._lock = threading.RLock()

    def _urls(self) -> list[str]:
        name = base64.b64encode(self.name.encode()).decode()
        path = f"/api/v2/channels/samsung.remote.control?name={name}"
        token = self.tokens.get(self.host)
        return [f"wss://{self.host}:8002{path}" + (f"&token={token}" if token else ""),
                f"ws://{self.host}:8001{path}"]

    def close(self) -> None:
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
            self._ws = None

    def connect(self) -> None:
        with self._lock:
            # Leerlaufende Verbindungen verwerfen: der TV trennt sie still,
            # ein send() würde dann ins Leere gehen.
            if self._ws and time.monotonic() - self._last_use > self.idle_timeout:
                self.close()
            if self._ws and self._ws.connected:
                return
            last: Exception | None = None
            for url in self._urls():
                try:
                    ws = websocket.create_connection(
                        url, timeout=self.timeout,
                        sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})
                except (OSError, websocket.WebSocketException) as e:
                    log.debug("Verbindung zu %s fehlgeschlagen: %s", url.split("?")[0], e)
                    last = e
                    continue
                try:
                    ws.settimeout(self.pair_timeout)  # Zeit zum Bestätigen am TV
                    msg = json.loads(ws.recv())
                except (OSError, ValueError, websocket.WebSocketException) as e:
                    ws.close()
                    last = e
                    continue
                event = msg.get("event")
                if event == "ms.channel.connect":
                    token = (msg.get("data") or {}).get("token")
                    if token and token != self.tokens.get(self.host):
                        self.tokens.set(self.host, token)
                    ws.settimeout(self.timeout)
                    self._ws, self._last_use = ws, time.monotonic()
                    log.debug("Verbunden: %s", url.split("?")[0])
                    return
                ws.close()
                if event == "ms.channel.unauthorized":
                    raise TVError(
                        "Zugriff am TV abgelehnt. Einstellungen > Allgemein > Externer "
                        "Geräte-Manager > Geräteverbindungs-Manager > Geräteliste: "
                        f"'{self.name}' zulassen oder löschen und erneut koppeln.")
                last = TVError(f"Unerwartete Antwort: {msg}")
            raise TVError(f"Remote-Schnittstelle nicht erreichbar: {last}")

    def send(self, payload: dict, _retry: bool = True) -> None:
        with self._lock:
            self.connect()
            try:
                self._ws.send(json.dumps(payload))
                self._last_use = time.monotonic()
            except (OSError, websocket.WebSocketException):
                self.close()
                if not _retry:
                    raise
                self.send(payload, _retry=False)

    def request(self, payload: dict, event: str, timeout: float = 5.0) -> dict:
        """Senden und auf ein bestimmtes Antwort-Event warten."""
        with self._lock:
            self.send(payload)
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    self._ws.settimeout(max(0.1, deadline - time.monotonic()))
                    try:
                        raw = self._ws.recv()
                    except websocket.WebSocketTimeoutException:
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("event") == event:
                        return msg
            except (OSError, websocket.WebSocketException) as e:
                self.close()
                raise TVError(f"Verbindung während {event} verloren: {e}") from e
            finally:
                if self._ws:
                    self._ws.settimeout(self.timeout)
            raise TVError(f"Keine Antwort auf {event}")

    # -- Fernbedienung
    def key(self, key: str, cmd: str = "Click") -> None:
        self.send({"method": "ms.remote.control",
                   "params": {"Cmd": cmd, "DataOfCmd": key.upper(), "Option": "false",
                              "TypeOfRemote": "SendRemoteKey"}})

    def hold(self, key: str, ms: int) -> None:
        with self._lock:
            self.key(key, "Press")
            time.sleep(ms / 1000)
            self.key(key, "Release")

    def text(self, text: str) -> None:
        with self._lock:
            self.send({"method": "ms.remote.control",
                       "params": {"Cmd": base64.b64encode(text.encode()).decode(),
                                  "DataOfCmd": "base64", "TypeOfRemote": "SendInputString"}})
            self.send({"method": "ms.remote.control",
                       "params": {"TypeOfRemote": "SendInputEnd"}})

    def emit(self, event: str, data=None) -> dict:
        return {"method": "ms.channel.emit",
                "params": {"event": event, "to": "host", "data": data if data is not None else ""}}


# --------------------------------------------------------------------------- SmartThings
class SmartThings:
    """Optional. Nur für Dinge ohne lokale API (Eingangsquelle, Kanal setzen)."""
    API = "https://api.smartthings.com/v1"

    def __init__(self, token: str, device_id: str):
        self.token, self.device_id = token, device_id

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        try:
            _, resp = _http(method, f"{self.API}{path}", data, headers, timeout=8)
        except urllib.error.HTTPError as e:
            raise TVError(f"SmartThings HTTP {e.code}: {e.read()[:300]!r}") from e
        except OSError as e:
            raise TVError(f"SmartThings nicht erreichbar: {e}") from e
        return json.loads(resp or b"{}")

    def status(self) -> dict:
        return self._req("GET", f"/devices/{self.device_id}/status")

    def command(self, capability: str, command: str, *args) -> dict:
        return self._req("POST", f"/devices/{self.device_id}/commands",
                         {"commands": [{"component": "main", "capability": capability,
                                        "command": command, "arguments": list(args)}]})

    def set_input(self, source: str) -> dict:
        return self.command("mediaInputSource", "setInputSource", source)

    def set_channel(self, channel: str) -> dict:
        return self.command("tvChannel", "setTvChannel", str(channel))


# --------------------------------------------------------------------------- Fassade
class SamsungTV:
    def __init__(self, host: str, mac: str | None = None, name: str = CLIENT_NAME,
                 broadcast: str = "255.255.255.255",
                 st_token: str | None = None, st_device: str | None = None):
        self.host, self.mac, self.broadcast = host, mac, broadcast
        self.rest = Rest(host)
        self.upnp = UPnP(host)
        self.remote = Remote(host, name)
        self.st = SmartThings(st_token, st_device) if st_token and st_device else None
        self._apps: list[dict] = []
        self._off_since: float | None = None
        self._lock = threading.RLock()
        self._waking = threading.Event()

    # -- Power
    def _in_instant_on(self) -> bool:
        return self._off_since is not None and \
            time.monotonic() - self._off_since < INSTANT_ON_WINDOW

    def is_on(self) -> bool:
        info = self.rest.info()
        if not info:
            self._off_since = None
            return False
        if self._in_instant_on():
            return False  # antwortet noch im Netz, Bild ist aber aus
        # 2018+ liefern PowerState, 2016/2017 nicht -> Erreichbarkeit = an
        return info.get("device", {}).get("PowerState", "on") == "on"

    def power_on(self, timeout: float = 30.0) -> bool:
        with self._lock:
            if self._in_instant_on() and self.rest.info():
                # TV ist noch "halb wach": WOL wird ignoriert, KEY_POWER wirkt
                self.remote.key("KEY_POWER")
                self._off_since = None
                return True
            if self.is_on():
                return True
            if not self.mac:
                raise TVError("Einschalten braucht die MAC-Adresse (--mac / SAMSUNGTV_MAC).")
            self._waking.set()
            try:
                self.remote.close()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    wake_on_lan(self.mac, self.broadcast, extra_targets=(self.host,))
                    time.sleep(2)
                    if self.rest.info(timeout=1.5):
                        break
                else:
                    return False
                # WebSocket-Dienst startet oft ein paar Sekunden nach REST
                while time.monotonic() < deadline:
                    try:
                        self.remote.connect()
                        return True
                    except TVError:
                        time.sleep(1)
                return True
            finally:
                self._waking.clear()

    def power_off(self) -> bool:
        with self._lock:
            if self.is_on():
                self.remote.key("KEY_POWER")
                self._off_since = time.monotonic()
                self.remote.close()
            return False

    def set_power(self, on: bool | None) -> bool:
        if on is None:
            on = not self.is_on()
        return self.power_on() if on else self.power_off()

    # -- Tasten
    def send_keys(self, seq: str, default_delay_ms: int = 300) -> None:
        steps = parse_sequence(seq)
        with self._lock:
            for i, step in enumerate(steps):
                kind = step[0]
                if kind == "sleep":
                    time.sleep(step[1] / 1000)
                    continue
                if kind == "key":
                    self.remote.key(step[1])
                elif kind == "hold":
                    self.remote.hold(step[1], step[2])
                elif kind == "text":
                    self.remote.text(step[1])
                if i + 1 < len(steps) and steps[i + 1][0] != "sleep":
                    time.sleep(default_delay_ms / 1000)

    # -- Audio
    def volume(self, value: str | int | None = None, step: int = 2) -> int:
        if value is None:
            return self.upnp.get_volume()
        v = str(value).strip().upper()
        if v in ("UP", "INCREASE"):
            target = self.upnp.get_volume() + step
        elif v in ("DOWN", "DECREASE"):
            target = self.upnp.get_volume() - step
        elif v[:1] in "+-":
            target = self.upnp.get_volume() + int(v)
        else:
            target = int(float(v))
        target = max(0, min(100, target))
        self.upnp.set_volume(target)
        return target

    def mute(self, value: bool | None = None) -> bool:
        if value is None:
            value = not self.upnp.get_mute()
        self.upnp.set_mute(value)
        return value

    # -- Apps
    def apps(self, refresh: bool = False) -> list[dict]:
        if refresh or not self._apps:
            msg = self.remote.request(self.remote.emit("ed.installedApp.get"),
                                      "ed.installedApp.get")
            items = (msg.get("data") or {}).get("data") or []
            self._apps = sorted(({"id": a.get("appId"), "name": a.get("name"),
                                  "type": a.get("app_type", 2)} for a in items),
                                key=lambda a: (a["name"] or "").lower())
        return self._apps

    def resolve_app(self, name_or_id: str) -> dict:
        key = name_or_id.strip().lower()
        try:
            apps = self.apps()
        except TVError:
            apps = []
        for a in apps:
            if key in (str(a["id"]).lower(), (a["name"] or "").lower()):
                return a
        for a in apps:
            if key in (a["name"] or "").lower():
                return a
        return {"id": name_or_id, "name": name_or_id, "type": 2}

    def _launch(self, app_id: str, app_type: int, meta: str | None = None) -> None:
        data = {"appId": app_id,
                "action_type": "NATIVE_LAUNCH" if app_type == 4 else "DEEP_LINK"}
        if meta:
            data["metaTag"] = meta
        try:
            self.remote.request(self.remote.emit("ed.apps.launch", data),
                                "ed.apps.launch", timeout=3)
        except TVError as e:
            log.debug("WS-Start fehlgeschlagen (%s), versuche REST", e)
            self.rest.app_start(app_id)

    def launch_app(self, name_or_id: str, meta: str | None = None) -> dict:
        with self._lock:
            app = self.resolve_app(name_or_id)
            self._launch(app["id"], app["type"], meta)
            return app

    def close_app(self, name_or_id: str) -> dict:
        app = self.resolve_app(name_or_id)
        self.rest.app_stop(app["id"])
        return app

    def current_app(self) -> str | None:
        try:
            apps = self.apps()
        except TVError:
            return None
        for a in apps:
            st = self.rest.app_status(a["id"])
            if st and st.get("visible"):
                return a["name"]
        return None

    def open_url(self, url: str) -> None:
        with self._lock:
            self._launch("org.tizen.browser", 4, url)

    # -- Quelle (nur via SmartThings wirklich gezielt)
    def set_source(self, source: str) -> None:
        if self.st:
            self.st.set_input(source)
        elif source.upper() == "TV":
            self.remote.key("KEY_TV")
        else:
            raise TVError("Gezielte Quellenwahl braucht SmartThings (--st-token/--st-device). "
                          "Lokal geht nur 'KEY_SOURCE' bzw. 'KEY_HDMI' (zyklisch).")

    # -- Gesamtstatus
    def state(self, with_app: bool = False) -> dict:
        on = self.is_on()
        st: dict = {"power": on, "waking": self._waking.is_set(), "volume": None, "mute": None}
        if on:
            try:
                st["volume"] = self.upnp.get_volume()
                st["mute"] = self.upnp.get_mute()
            except (TVError, KeyError, ValueError) as e:
                log.debug("UPnP-Status nicht lesbar: %s", e)
            if with_app:
                st["app"] = self.current_app()
        return st


# --------------------------------------------------------------------------- HTTP-API
def route(tv: SamsungTV, parts: list[str], q: dict, body: str):
    def arg(name: str) -> str:
        return (q.get(name) or [body])[0]

    if not parts or parts[0] == "state":
        return tv.state(with_app=arg("app") in ("1", "true"))
    head, rest = parts[0].lower(), parts[1:]

    if head == "info":
        return tv.rest.info() or {"reachable": False}
    if head == "power":
        on = parse_bool(rest[0] if rest else body or "toggle")
        if on is None:
            on = not tv.is_on()
        if on:
            if not tv._waking.is_set():  # Einschalten kann dauern -> asynchron
                threading.Thread(target=tv.power_on, daemon=True).start()
            return {"power": "switching_on"}
        return {"power": tv.power_off()}
    if head == "key" and rest:
        tv.remote.key(rest[0])
        return {"ok": True}
    if head == "keys":
        tv.send_keys("/".join(rest) if rest else arg("seq"))
        return {"ok": True}
    if head == "text":
        tv.remote.text("/".join(rest) if rest else arg("t"))
        return {"ok": True}
    if head == "volume":
        return {"volume": tv.volume(rest[0] if rest else (body or None))}
    if head == "mute":
        return {"mute": tv.mute(parse_bool(rest[0] if rest else body or "toggle"))}
    if head == "apps":
        return tv.apps(refresh=arg("refresh") in ("1", "true"))
    if head == "app":
        if not rest or rest[0].lower() == "current":
            return {"app": tv.current_app()}
        if len(rest) > 1 and rest[1].lower() == "close":
            return {"closed": tv.close_app(rest[0])}
        return {"launched": tv.launch_app(rest[0])}
    if head == "url":
        tv.open_url(arg("u"))
        return {"ok": True}
    if head == "play":
        tv.upnp.play_url(arg("u"))
        return {"ok": True}
    if head == "source" and rest:
        tv.set_source(rest[0])
        return {"ok": True}
    raise KeyError(f"Unbekannter Pfad: /{'/'.join(parts)}")


def make_handler(tv: SamsungTV):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SamsungTVBridge/1.0"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _reply(self, code: int, obj) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _handle(self) -> None:
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace").strip() if n else ""
            try:
                self._reply(200, route(tv, parts, parse_qs(u.query), body))
            except KeyError as e:
                self._reply(404, {"error": e.args[0] if e.args else str(e)})
            except ValueError as e:
                self._reply(400, {"error": str(e)})
            except TVError as e:
                self._reply(503, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("Fehler bei %s", self.path)
                self._reply(500, {"error": str(e)})

        do_GET = do_POST = do_PUT = _handle

    return Handler


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(description="Samsung Tizen-TV (2016–2019) lokal steuern")
    p.add_argument("--host", default=env("SAMSUNGTV_HOST"), help="IP/Hostname des TV")
    p.add_argument("--mac", default=env("SAMSUNGTV_MAC"), help="MAC für Wake-on-LAN")
    p.add_argument("--broadcast", default=env("SAMSUNGTV_BROADCAST", "255.255.255.255"))
    p.add_argument("--name", default=env("SAMSUNGTV_NAME", CLIENT_NAME))
    p.add_argument("--st-token", default=env("SMARTTHINGS_TOKEN"))
    p.add_argument("--st-device", default=env("SMARTTHINGS_DEVICE"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    for c in ("pair", "info", "state", "on", "off", "toggle", "apps", "current-app"):
        sub.add_parser(c)
    sub.add_parser("key").add_argument("keys", nargs="+")
    sub.add_parser("keys").add_argument("sequence")
    h = sub.add_parser("hold"); h.add_argument("key"); h.add_argument("ms", type=int)
    sub.add_parser("text").add_argument("text")
    sub.add_parser("volume").add_argument("value", nargs="?")
    sub.add_parser("mute").add_argument("value", nargs="?", default="toggle")
    sub.add_parser("app").add_argument("name")
    sub.add_parser("close").add_argument("name")
    sub.add_parser("url").add_argument("url")
    sub.add_parser("play").add_argument("url")
    sub.add_parser("source").add_argument("source")
    s = sub.add_parser("serve")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.host:
        print("Fehler: --host oder SAMSUNGTV_HOST angeben")
        return 2
    tv = SamsungTV(args.host, args.mac, args.name, args.broadcast, args.st_token, args.st_device)
    out = lambda o: print(json.dumps(o, indent=2, ensure_ascii=False))  # noqa: E731

    try:
        c = args.cmd
        if c == "pair":
            print("Bitte am TV 'Zulassen' bestätigen (30 s) ...")
            tv.remote.connect()
            print("Verbunden. Token:", tv.remote.tokens.get(args.host) or "(Port 8001, kein Token)")
        elif c == "info":
            out(tv.rest.info() or {"reachable": False})
        elif c == "state":
            out(tv.state(with_app=True))
        elif c == "on":
            print("an" if tv.power_on() else "Einschalten fehlgeschlagen (WOL aktiviert?)")
        elif c == "off":
            tv.power_off(); print("aus")
        elif c == "toggle":
            print("an" if tv.set_power(None) else "aus")
        elif c == "key":
            tv.send_keys(",".join(args.keys))
        elif c == "keys":
            tv.send_keys(args.sequence)
        elif c == "hold":
            tv.remote.hold(args.key, args.ms)
        elif c == "text":
            tv.remote.text(args.text)
        elif c == "volume":
            print(tv.volume(args.value))
        elif c == "mute":
            print("stumm" if tv.mute(parse_bool(args.value)) else "Ton an")
        elif c == "apps":
            for a in tv.apps(refresh=True):
                print(f"{a['id']:<28} type={a['type']}  {a['name']}")
        elif c == "current-app":
            print(tv.current_app() or "-")
        elif c == "app":
            out(tv.launch_app(args.name))
        elif c == "close":
            out(tv.close_app(args.name))
        elif c == "url":
            tv.open_url(args.url)
        elif c == "play":
            tv.upnp.play_url(args.url)
        elif c == "source":
            tv.set_source(args.source)
        elif c == "serve":
            srv = ThreadingHTTPServer((args.bind, args.port), make_handler(tv))
            log.info("HTTP-API auf http://%s:%d", args.bind, args.port)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                tv.remote.close()
    except TVError as e:
        print(f"Fehler: {e}")
        return 1
    finally:
        tv.remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
