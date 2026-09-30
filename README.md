# samsungtv.py – UE55KU6079U lokal steuern

## 1. TV einmalig einstellen

| Einstellung | Pfad (Tizen 2016, Bezeichnungen können leicht abweichen) | Wert |
|---|---|---|
| Zugriffsbenachrichtigung | Einstellungen → Allgemein → Externer Geräte-Manager → Geräteverbindungs-Manager | **Nur beim ersten Mal** |
| Geräteliste | ebenda → Geräteliste | Client `PythonTVControl` → **Zulassen** |
| Wake-on-LAN | Einstellungen → Allgemein → Netzwerk → Experteneinstellungen → Mit Mobilgerät einschalten | **Ein** |

Feste IP (DHCP-Reservierung) für den TV vergeben.

## 2. Installation & Kopplung

```bash
pip install websocket-client
export SAMSUNGTV_HOST=192.168.1.50 SAMSUNGTV_MAC=AA:BB:CC:DD:EE:FF
./samsungtv.py pair          # am TV "Zulassen" drücken -> Token in ~/.config/samsungtv/tokens.json
./samsungtv.py info          # Geräteinfo (REST)
./samsungtv.py apps          # installierte Apps mit IDs
```

Der Client-Name (`--name`) muss konstant bleiben, sonst fragt der TV erneut.

## 3. CLI

```bash
./samsungtv.py on | off | toggle | state
./samsungtv.py key KEY_VOLUP KEY_VOLUP
./samsungtv.py keys "KEY_MENU, 1000, KEY_DOWN, KEY_ENTER, 2000, KEY_EXIT"
./samsungtv.py keys "KEY_RETURN@3000"          # 3 s gedrückt halten
./samsungtv.py text "Suchbegriff"               # in offenes Eingabefeld
./samsungtv.py volume 12 | volume +3 | mute on
./samsungtv.py app YouTube | close YouTube | current-app
./samsungtv.py url https://example.org          # Browser öffnen
./samsungtv.py play http://nas/film.mp4         # DLNA-Wiedergabe
./samsungtv.py source HDMI1                     # nur mit SmartThings
```

## 4. Als Dienst (systemd)

`/etc/systemd/system/samsungtv.service`

```ini
[Unit]
Description=Samsung TV Bridge
After=network-online.target
Wants=network-online.target

[Service]
User=openhab
Environment=SAMSUNGTV_HOST=192.168.1.50
Environment=SAMSUNGTV_MAC=AA:BB:CC:DD:EE:FF
ExecStart=/usr/bin/python3 /opt/samsungtv/samsungtv.py serve --bind 127.0.0.1 --port 8765
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`pair` einmal als derselbe User ausführen (`sudo -u openhab ...`), damit das Token im richtigen Home liegt.

## 5. HTTP-API

| Pfad | Wirkung |
|---|---|
| `/state` (`?app=1`) | `{"power", "waking", "volume", "mute", "app"}` |
| `/power/{ON,OFF,true,false,toggle}` | Einschalten läuft asynchron (WOL) |
| `/key/KEY_X`, `/keys/<sequenz>` bzw. `/keys?seq=…` | Tasten |
| `/text?t=…` | Texteingabe |
| `/volume`, `/volume/{0-100,+n,-n,INCREASE,DECREASE}` | Lautstärke (UPnP) |
| `/mute/{ON,OFF,toggle}` | Mute (UPnP) |
| `/apps`, `/app/<name|id>`, `/app/<name>/close`, `/app/current` | Apps |
| `/url?u=…`, `/play?u=…`, `/source/HDMI1` | Browser, DLNA, Quelle |

## 6. openHAB (HTTP-Binding)

```java
Thing http:url:samsungtv "Samsung TV Bridge" [ baseURL="http://127.0.0.1:8765", refresh=5, timeout=10000 ] {
    Channels:
        Type switch : power  "Power"  [ stateExtension="/state", stateTransformation="JSONPATH:$.power",
                                        commandExtension="/power/%2$s", onValue="true", offValue="false" ]
        Type dimmer : volume "Volume" [ stateExtension="/state", stateTransformation="JSONPATH:$.volume",
                                        commandExtension="/volume/%2$s" ]
        Type switch : mute   "Mute"   [ stateExtension="/state", stateTransformation="JSONPATH:$.mute",
                                        commandExtension="/mute/%2$s", onValue="true", offValue="false" ]
        Type string : keys   "Keys"   [ commandExtension="/keys/%2$s", mode="WRITEONLY" ]
        Type string : app    "App"    [ commandExtension="/app/%2$s", mode="WRITEONLY" ]
}
```

Das Samsung-TV-Binding parallel dazu deaktivieren, damit sich nicht zwei Clients um die Verbindung streiten.

## Bekannte Grenzen

- **Einschalten:** WOL kann über LAN scheitern, wenn eine Soundbar per ARC hängt. Dann HDMI-CEC (z. B. Raspberry Pi + `cec-client`) oder IR nutzen.
- **Instant-On:** Nach dem Ausschalten antwortet der TV noch ~1 min im Netz. Das Skript wertet das als „aus“ und schaltet in dieser Zeit per `KEY_POWER` wieder ein. Wird der TV in diesem Fenster per Fernbedienung eingeschaltet, stimmt der Status bis zu 65 s nicht.
- **Quelle/Kanal gezielt setzen:** Lokal gibt es dafür keine API, nur zyklisch (`KEY_SOURCE`, `KEY_HDMI`). Gezielt geht es per SmartThings (`--st-token`, `--st-device`).
- **Bild-Einstellungen** (Helligkeit, Kontrast): keine API ab 2016, nur per Menü-Tastensequenz.
