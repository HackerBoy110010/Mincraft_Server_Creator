# Minecraft Server Creator

SSH-freundlicher Minecraft-Server-Manager mit einfachem Python-Textmenü. Das Repository befindet sich in einer frühen Umsetzungsphase.

## Aktueller Funktionsumfang

- lokale Konfiguration und getrennte Serverprofile
- Erstkonfiguration mit Serverordner und globalen Port-Standardwerten; Werte lassen sich pro Profil überschreiben
- Installation von Vanilla, Fabric, Forge, NeoForge und Arclight aus den jeweiligen offiziellen Metadaten/Downloads; Arclight verlangt eine explizite Basis-Auswahl (Fabric/Forge/NeoForge)
- neueste stabile Version als Vorgabe; konkrete veröffentlichte Versionen können angegeben werden
- passende Java-Laufzeit über Adoptium/Temurin verwalten und SHA-256 vor dem Entpacken prüfen
- Forge-/NeoForge-Installer verwenden verifizierte offizielle Installer-Dateien; systemd startet deren erzeugte `unix_args.txt` statt eines Vanilla-JARs
- Arclight lädt nur stabile passende Release-JARs vom offiziellen GitHub-Projekt; veröffentlichte SHA-256-Digests werden geprüft
- EULA-Zustimmung ausdrücklich vor dem ersten Start
- Start/Stop/Status über einen systemd-Benutzerdienst; optionaler Autostart nach Host-Neustart
- Portkonfliktprüfung und Anzeige von LAN-/öffentlicher IP mit konfigurierten Endpunkten
- geführte `server.properties`-Einstellungen für MOTD, Spielerlimit, Schwierigkeit, Spielmodus, PvP, Whitelist und Sichtweiten
- manuelle Backups gestoppter Serverprofile
- lokales RCON nur über `127.0.0.1`, mit Passwortdatei und restriktiven Rechten
- Online-Spielerliste und Live-Spielerdaten; Inventar lesen sowie einzelne Plätze bei Vanilla 1.17+ ändern
- Python-Quellinstallation und interaktives Terminalmenü

**Noch nicht implementiert:** Mod-/Plugin-Downloads, automatische Firewallregeln und Backup-Wiederherstellung. RCON ist nur für Vanilla eingebunden und im echten Minecraft-Serverbetrieb noch nicht live getestet; Inventarzugriff betrifft ausschließlich online befindliche Spieler. Die Vanilla-, Fabric-, Forge-, NeoForge- und Arclight-Installer sind unit-getestet, aber echte Downloads und Serverstarts noch nicht live verifiziert. Forge/NeoForge führen den offiziellen Installer bei der Profilerstellung mit der passend verwalteten Temurin-JRE aus. Arclight verlangt die explizite Basis-Auswahl `fabric`, `forge` oder `neoforge`. Nicht unterstützte oder nicht kompatible Loader-Builds werden abgelehnt.

## Voraussetzungen

- Linux mit `systemd` für die erste nutzbare Serververwaltung
- Python 3.11 oder neuer für die Quellinstallation
- Internetzugang für Mojang-Metadaten und die gewählte Java-Laufzeit
- `systemctl --user`; damit der Dienst nach SSH-Abmeldung weiterläuft, muss systemd-Lingering einmalig aktiviert sein. Das Programm fragt vorher und verwendet dafür `sudo loginctl enable-linger`.

Die Python-Codebasis verwendet keine Ubuntu-spezifische Java-Paketinstallation. Zielplattformen der ersten Ausbaustufe sind gängige Linux-Distributionen mit systemd (Ubuntu/Debian, Arch/CachyOS, Fedora) auf x64 und ARM64. Windows und macOS sind für spätere Dienst-Adapter vorgesehen.

## Installation aus dem Repository

```bash
git clone https://github.com/HackerBoy110010/Mincraft_Server_Creator.git
cd Mincraft_Server_Creator
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mcsc
```

Für die Quellinstallation unter Windows wird die virtuelle Umgebung über `.venv\\Scripts\\activate` aktiviert. Die Hintergrunddienst-Integration ist derzeit Linux/systemd-spezifisch.

## Erststart und Ports

`mcsc` öffnet das Textmenü. Beim ersten Lauf fragt `mcsc setup` nach dem Speicherordner und globalen Port-Standardwerten:

- Java: TCP (Standard 25565)
- Bedrock/Geyser: UDP (Standard 19132, optional)
- Simple Voice Chat: UDP (Standard 24454, optional)

Beim Anlegen eines Servers kann man die Werte übernehmen oder pro Serverprofil ändern. Das Setzen dieser Ports öffnet weder die Betriebssystem-Firewall noch den Heimrouter. Routerweiterleitung und lokale Firewallregeln bleiben manuell. In der aktuellen Vanilla-Ausbaustufe werden Bedrock- und Voicechat-Ports gespeichert, aber erst mit den passenden Mods/Plugins tatsächlich verwendet.

`mcsc configure <server>` führt durch die üblichen Spieleinstellungen. Der Server muss dafür gestoppt sein; Änderungen werden beim nächsten Start übernommen.

```bash
mcsc setup
mcsc create zuhause --type vanilla --version latest
# Arclight benötigt eine ausdrückliche Basis-Auswahl:
mcsc create hybrid --type arclight --arclight-loader forge --version latest
mcsc list
mcsc start zuhause
mcsc status
mcsc configure zuhause
mcsc logs zuhause
mcsc backup zuhause
mcsc stop zuhause
mcsc rcon enable zuhause
mcsc start zuhause
mcsc rcon players zuhause
mcsc rcon stats zuhause Steve
mcsc rcon inventory zuhause Steve
# Beispiel: --yes bestätigt explizit; ändert nur einen ONLINE-Spieler
mcsc rcon set-item zuhause Steve 0 minecraft:diamond 1 --yes
mcsc stop zuhause
mcsc rcon disable zuhause
mcsc ip
```

Beim ersten Start zeigt das Programm die Minecraft-EULA an und schreibt `eula=true` nur nach ausdrücklicher Zustimmung. Java- und Serverdateien werden nicht mit Root-Rechten installiert. Für Lingering fragt das Programm gesondert nach einer `sudo`-Bestätigung.

## Verzeichnisse

- Konfiguration: `$XDG_CONFIG_HOME/mcsc/config.json` (standardmäßig `~/.config/mcsc/config.json`)
- verwaltete Java-Laufzeiten: `$XDG_DATA_HOME/mcsc/runtimes` (standardmäßig `~/.local/share/mcsc/runtimes`)
- Serverprofile: standardmäßig `~/MinecraftServers/<profil>/`

Jedes Profil enthält eigene Serverdateien, `server.properties`, Logs, Mods-/Plugin-Ordner und Backups. Zugangsdaten und private Welten gehören nicht ins Repository.

## Netzwerk und Sicherheit

Das Programm zeigt lokale und öffentliche IP sowie konfigurierte TCP-/UDP-Endpunkte. Eine angezeigte öffentliche Adresse beweist nicht, dass Verbindungen von außen funktionieren: Routerweiterleitung, Provider-CGNAT, IPv6 und Firewallregeln können den Zugriff verhindern. Die optionale öffentliche IP-Abfrage verwendet einen externen IP-Echo-Dienst.

RCON wird standardmäßig nicht aktiviert. `mcsc rcon enable <server>` muss bei gestopptem Server ausgeführt werden; es bindet RCON ausdrücklich an `127.0.0.1`, wählt den lokalen Standardport 25575 und speichert das erzeugte Passwort unter `config/rcon-password` mit Dateirechten `0600`. Die CLI bietet `players`, `stats`, `inventory` und (ab Vanilla 1.17) `set-item`. Beim `set-item` sind die Plätze 0–8 die Hotbar und 9–35 das Hauptinventar. Die Tools adressieren nur online befindliche Java-Spieler; Offline-Inventare werden nicht gelesen oder verändert. Inventaränderungen sind versions-/servertypgeprüft und schlagen geschlossen fehl, ohne den Serverbetrieb zu sperren. Der RCON-Port darf nicht über Router oder Firewall veröffentlicht werden. Diese Integration wurde mit simuliertem RCON-Verkehr getestet, aber noch nicht an einem echten Minecraft-Server.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Geplante Ausbaustufen

1. Vanilla-Grundlage, Profile, Ports, Java-Verwaltung und systemd (Umsetzung begonnen; Live-Serverbetrieb noch nicht verifiziert)
2. Live-Smoke-Tests für Vanilla, Fabric, Forge, NeoForge und Arclight
3. geprüfter Mod-/Plugin-Katalog über Modrinth und CurseForge
4. RCON-/Inventar-Kompatibilität für weitere Servertypen und Versionen, Backup-Wiederherstellung sowie Live-Serverprüfung
5. Linux-x64-/ARM64-Release-Pakete und Distributionstests; später Windows/macOS

Projektplanung und Arbeitspakete sind in der Notion-Projektzentrale und im Linear-Projekt dokumentiert.
