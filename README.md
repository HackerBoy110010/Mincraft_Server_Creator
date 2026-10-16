# Minecraft Server Creator

SSH-freundlicher Minecraft-Server-Manager mit einfachem Python-Textmenü. Das Repository befindet sich in einer frühen Umsetzungsphase.

## Aktueller Funktionsumfang

- lokale Konfiguration und getrennte Serverprofile
- Erstkonfiguration mit Serverordner und globalen Port-Standardwerten; Werte lassen sich pro Profil überschreiben
- Installation eines Vanilla-Java-Servers aus den offiziellen Mojang-Versionsmetadaten
- neueste stabile Version als Vorgabe; konkrete veröffentlichte Versionen können angegeben werden
- passende Java-Laufzeit über Adoptium/Temurin verwalten und SHA-256 vor dem Entpacken prüfen
- EULA-Zustimmung ausdrücklich vor dem ersten Start
- Start/Stop/Status über einen systemd-Benutzerdienst; optionaler Autostart nach Host-Neustart
- Portkonfliktprüfung und Anzeige von LAN-/öffentlicher IP mit konfigurierten Endpunkten
- geführte `server.properties`-Einstellungen für MOTD, Spielerlimit, Schwierigkeit, Spielmodus, PvP, Whitelist und Sichtweiten
- manuelle Backups gestoppter Serverprofile
- Python-Quellinstallation und interaktives Terminalmenü

**Noch nicht implementiert:** Fabric, Forge, NeoForge, Arclight, Mod-/Plugin-Downloads, automatische Firewallregeln und RCON-Spielerverwaltung. Diese sind als Ausbaustufen geplant; das Menü kennzeichnet noch nicht verfügbare Servertypen.

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
mcsc list
mcsc start zuhause
mcsc status
mcsc configure zuhause
mcsc logs zuhause
mcsc backup zuhause
mcsc stop zuhause
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

Der RCON-Paketcodec ist vorhanden, aber eine sichere RCON-Verbindung sowie Spielerstatistiken und Live-Inventarbearbeitung sind noch nicht an die CLI angebunden. RCON darf später nicht zum Internet weitergeleitet werden; nicht unterstützte Kombinationen müssen die Inventarbearbeitung deaktivieren, ohne den Server zu sperren. Eine Backup-Wiederherstellung ist ebenfalls noch offen.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Geplante Ausbaustufen

1. Vanilla-Grundlage, Profile, Ports, Java-Verwaltung und systemd (Umsetzung begonnen; Live-Serverbetrieb noch nicht verifiziert)
2. Fabric, Forge, NeoForge und Arclight
3. geprüfter Mod-/Plugin-Katalog über Modrinth und CurseForge
4. sichere RCON-Verbindung, Spielerstatistiken, kompatible Live-Inventarverwaltung und Backup-Wiederherstellung
5. Linux-x64-/ARM64-Release-Pakete und Distributionstests; später Windows/macOS

Projektplanung und Arbeitspakete sind in der Notion-Projektzentrale und im Linear-Projekt dokumentiert.
