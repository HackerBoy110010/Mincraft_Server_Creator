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
- sichere Welt-Neugenerierung bei gestopptem Server: fragt vor jedem Reset nach einem Profil-Backup und unterstützt optionale Seeds
- lokales RCON nur über `127.0.0.1`, mit Passwortdatei und restriktiven Rechten
- Online-Spielerliste und Live-Spielerdaten; Inventar lesen sowie einzelne Plätze bei Vanilla 1.17+ ändern
- Mod-/Plugin-/Datapack-Suche und Installation aus Modrinth und CurseForge mit Minecraft-Version-/Loaderfiltern, Prüfsummen und getrennten Zielordnern; Datapacks landen in `<level-name>/datapacks/` (Standardwelt `world`)
- Geyser-Fabric-Installation für reine Fabric-Profile auf der neuesten stabilen Minecraft-Version; passende stabile Fabric API wird automatisch ergänzt, Beta-Geyser nur nach ausdrücklichem Opt-in (`--allow-beta`)
- Geyser-NetherNet-Konfiguration für den Profilport nach dem ersten Serverstart; die Portnummer gilt dabei für TCP und UDP
- Python-Quellinstallation und interaktives Terminalmenü

**Noch nicht implementiert:** automatische Firewallregeln und Backup-Wiederherstellung. Der CurseForge-Katalog benötigt einen API-Schlüssel in `CURSEFORGE_API_KEY`; er wird nicht in der Serverkonfiguration gespeichert. Katalogabhängigkeiten werden angezeigt, aber nicht automatisch installiert. Plugins können nur für Arclight ausgewählt werden; Modrinth-Ergebnisse werden auf Bukkit-/Spigot-Dateien gefiltert. CurseForge kann Paper-only-Dateien nicht sicher herausfiltern. Da Arclight keine Paper-only-Kompatibilität dokumentiert, prüfe vor dem Start die Herstellerangaben. RCON ist nur für Vanilla eingebunden und im echten Minecraft-Serverbetrieb noch nicht live getestet; Inventarzugriff betrifft ausschließlich online befindliche Spieler. Die Vanilla-, Fabric-, Forge-, NeoForge- und Arclight-Installer sowie Katalogfunktionen sind unit-getestet, aber echte Downloads und Serverstarts noch nicht live verifiziert. Forge/NeoForge führen den offiziellen Installer bei der Profilerstellung mit der passend verwalteten Temurin-JRE aus. Arclight verlangt die explizite Basis-Auswahl `fabric`, `forge` oder `neoforge`. Nicht unterstützte oder nicht kompatible Loader-Builds werden abgelehnt.

## Voraussetzungen

- Linux mit `systemd` für die erste nutzbare Serververwaltung
- Python 3.11 oder neuer für die Quellinstallation
- Internetzugang für Mojang-Metadaten und die gewählte Java-Laufzeit
- `systemctl --user`; damit der Dienst nach SSH-Abmeldung weiterläuft, muss systemd-Lingering einmalig aktiviert sein. Das Programm fragt vorher und verwendet dafür `sudo loginctl enable-linger`.

Die Python-Codebasis verwendet keine Ubuntu-spezifische Java-Paketinstallation. Zielplattformen der ersten Ausbaustufe sind gängige Linux-Distributionen mit systemd (Ubuntu/Debian, Arch/CachyOS, Fedora) auf x64 und ARM64. Windows und macOS sind für spätere Dienst-Adapter vorgesehen.

## Installation aus dem Repository

Das Repository ist privat. Für den Beta-Test direkt auf einem Linux-Server brauchst du deshalb einen GitHub-Zugang für den Server; ein GitHub-Kontopasswort funktioniert nicht für Git-HTTPS. Empfohlen ist ein schreibgeschützter Deploy Key (siehe Beta-Anleitung unten).

Mit einem bereits bei GitHub hinterlegten SSH-Schlüssel kannst du den Beta-Branch direkt klonen:

```bash
git clone --branch arena/461d7229-mincraft-server-creator git@github.com:HackerBoy110010/Mincraft_Server_Creator.git mcsc-beta
cd mcsc-beta
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mcsc
```

Für die Quellinstallation unter Windows wird die virtuelle Umgebung über `.venv\\Scripts\\activate` aktiviert. Die Hintergrunddienst-Integration ist derzeit Linux/systemd-spezifisch.

## Beta direkt über SSH testen (Linux)

Du musst kein Paket auf deinen PC laden oder übertragen. Der Server lädt den aktuellen Beta-Branch direkt von GitHub. Benötigt werden Python 3.11+, Git, OpenSSH und Linux mit systemd.

Da das Repository privat ist, richte einmalig einen **schreibgeschützten Deploy Key** ein. Erzeuge das Schlüsselpaar direkt auf dem Linux-Server:

```bash
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -C "mcsc read-only deploy key" -f "$HOME/.ssh/mcsc_github_ro" -N ""
cat "$HOME/.ssh/mcsc_github_ro.pub"
```

Füge den ausgegebenen **öffentlichen** Schlüssel im GitHub-Repository unter **Settings → Deploy keys → Add deploy key** hinzu. Lasse Schreibzugriff ausgeschaltet. Den privaten Schlüssel `~/.ssh/mcsc_github_ro` niemals weitergeben.

Danach in der SSH-Sitzung installieren:

```bash
python3 -m venv "$HOME/.venvs/mcsc-beta"
source "$HOME/.venvs/mcsc-beta/bin/activate"
GIT_SSH_COMMAND="ssh -i $HOME/.ssh/mcsc_github_ro -o IdentitiesOnly=yes" \
  python -m pip install --upgrade --force-reinstall \
  "git+ssh://git@github.com/HackerBoy110010/Mincraft_Server_Creator.git@arena/461d7229-mincraft-server-creator"
mcsc --help
mcsc setup
mcsc create mcsc-test --type vanilla --version latest
mcsc start mcsc-test
```

Für den ersten Test bitte ein neues Testprofil verwenden, die angezeigte EULA ausdrücklich beantworten und beim Start die Lingering-Abfrage bestätigen, falls sie erscheint. Zum Prüfen des Hintergrundbetriebs nach dem Start die SSH-Verbindung trennen, neu verbinden und `mcsc status` sowie danach `mcsc stop mcsc-test` ausführen. Für Updates denselben `GIT_SSH_COMMAND=... pip install --upgrade --force-reinstall ...`-Befehl erneut starten; dabei wird der Branch-Stand direkt von GitHub geholt.

Der CI-Workflow baut außerdem ein plattformunabhängiges Python-Wheel und Source-Archiv. Diese GitHub-Artefakte sowie Release-Downloads sind bei diesem privaten Repository nur mit GitHub-Zugang abrufbar. Das Wheel ist kein eigenständiges Binary.

## Erststart und Ports

`mcsc` öffnet das Textmenü. Beim ersten Lauf fragt `mcsc setup` nach dem Speicherordner und globalen Port-Standardwerten:

- Java: TCP (Standard 25565)
- Bedrock/Geyser: Portnummer (Standard 19132, optional; mit NetherNet benötigt Geyser dieselbe Portnummer über TCP und UDP)
- Simple Voice Chat: UDP (Standard 24454, optional)

Beim Anlegen eines Servers kann man die Werte übernehmen oder pro Serverprofil ändern. Das Setzen dieser Ports öffnet weder die Betriebssystem-Firewall noch den Heimrouter. Routerweiterleitung und lokale Firewallregeln bleiben manuell. Geyser-Fabric kann mit `mcsc geyser install <server>` installiert werden; die passende stabile Fabric API wird dabei automatisch ergänzt. Für den aktuellen 26.3-Build ist eine ausdrückliche Beta-Zustimmung erforderlich (im Terminal bestätigen, nichtinteraktiv `--allow-beta --yes`). Nach dem ersten Serverstart die Geyser-Konfiguration anlegen lassen, den Server stoppen und `mcsc geyser configure <server>` ausführen. Mit NetherNet müssen TCP und UDP auf dem Bedrock-Port erreichbar sein. Simple Voice Chat muss separat installiert und konfiguriert werden.

`mcsc configure <server>` führt durch die üblichen Spieleinstellungen. Der Server muss dafür gestoppt sein; Änderungen werden beim nächsten Start übernommen.

### Welt neu generieren

`mcsc world regenerate <server>` setzt Oberwelt, Nether und End zurück. Der Server muss gestoppt sein. Interaktiv fragt MCSC nach einem optionalen Seed (leere Eingabe = zufälliger Seed), fragt bei jedem Reset, ob vorher ein vollständiges Server-Backup erstellt werden soll, und verlangt zur Sicherheit die Eingabe des Profil-Slugs. Ein Reset entfernt Weltdateien, Spielerfortschritt und Datapacks der Welt; Servereinstellungen und Mods bleiben erhalten. Ohne Seed wird ein eventuell gespeicherter Seed gelöscht, damit Minecraft zufällig generiert.

Für nichtinteraktive Aufrufe müssen Backup-Entscheidung und Bestätigung ausdrücklich angegeben werden:

```bash
mcsc world regenerate zuhause --seed "12345" --backup --yes
mcsc world regenerate zuhause --no-backup --yes
```

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
# Katalogdateien nur bei gestopptem Server; install fragt vor dem Download nach Bestätigung:
mcsc catalog search zuhause minimap --source modrinth --kind mod
mcsc catalog install zuhause PROJEKT_ID --source modrinth --kind mod
mcsc catalog search zuhause terrain --source curseforge --kind datapack
mcsc catalog install zuhause PROJEKT_ID --source curseforge --kind datapack
mcsc catalog list zuhause
mcsc rcon disable zuhause
mcsc ip
```

Beim ersten Start zeigt das Programm die Minecraft-EULA an und schreibt `eula=true` nur nach ausdrücklicher Zustimmung. Java- und Serverdateien werden nicht mit Root-Rechten installiert. Für Lingering fragt das Programm gesondert nach einer `sudo`-Bestätigung.

## Verzeichnisse

- Konfiguration: `$XDG_CONFIG_HOME/mcsc/config.json` (standardmäßig `~/.config/mcsc/config.json`)
- verwaltete Java-Laufzeiten: `$XDG_DATA_HOME/mcsc/runtimes` (standardmäßig `~/.local/share/mcsc/runtimes`)
- Serverprofile: standardmäßig `~/MinecraftServers/<profil>/`

Jedes Profil enthält eigene Serverdateien, `server.properties`, Logs, Mods-/Plugin-Ordner, die Welt mit ihrem `datapacks/`-Unterordner und Backups. Zugangsdaten und private Welten gehören nicht ins Repository.

## Netzwerk und Sicherheit

Das Programm zeigt lokale und öffentliche IP sowie konfigurierte TCP-/UDP-Endpunkte. Eine angezeigte öffentliche Adresse beweist nicht, dass Verbindungen von außen funktionieren: Routerweiterleitung, Provider-CGNAT, IPv6 und Firewallregeln können den Zugriff verhindern. Die optionale öffentliche IP-Abfrage verwendet einen externen IP-Echo-Dienst.

RCON wird standardmäßig nicht aktiviert. `mcsc rcon enable <server>` muss bei gestopptem Server ausgeführt werden; es bindet RCON ausdrücklich an `127.0.0.1`, wählt den lokalen Standardport 25575 und speichert das erzeugte Passwort unter `config/rcon-password` mit Dateirechten `0600`. Die CLI bietet `players`, `stats`, `inventory` und (ab Vanilla 1.17) `set-item`. Beim `set-item` sind die Plätze 0–8 die Hotbar und 9–35 das Hauptinventar. Die Tools adressieren nur online befindliche Java-Spieler; Offline-Inventare werden nicht gelesen oder verändert. Inventaränderungen sind versions-/servertypgeprüft und schlagen geschlossen fehl, ohne den Serverbetrieb zu sperren. Der RCON-Port darf nicht über Router oder Firewall veröffentlicht werden. Diese Integration wurde mit simuliertem RCON-Verkehr getestet, aber noch nicht an einem echten Minecraft-Server.

## Mods, Plugins und Datapacks

`mcsc catalog search <server> <suchbegriff> --source modrinth|curseforge --kind mod|plugin|datapack` sucht passend zur Minecraft-Version und – bei Mods/Plugins – zum Loader des Profils. Mods können auf Fabric, Forge, NeoForge und den entsprechenden Arclight-Basen installiert werden; Plugins nur auf Arclight. Datapacks sind weltbezogen und werden in `<level-name>/datapacks/` installiert. Das Programm liest `level-name` aus `server.properties`; fehlt die Einstellung, gilt `world`. Der Weltordnername darf kein Pfad sein.

`catalog install` wählt standardmäßig die neueste stabile passende JAR-Datei bzw. für Datapacks eine ZIP-Datei. Vor dem Download wird interaktiv um Bestätigung gebeten (nichtinteraktive Aufrufe benötigen `--yes`); verfügbare SHA-1/SHA-512/MD5-Prüfsummen werden geprüft und vorhandene Dateien nicht überschrieben. Datapack-ZIPs müssen `pack.mcmeta` mit einem erkennbaren Format-Eintrag direkt im ZIP-Stammverzeichnis enthalten. Der Server muss während der Installation gestoppt sein. Ein Datapack wird nicht automatisch aktiviert: nach dem Start `/datapack list available` prüfen und, falls nötig, `/datapack enable file/<Dateiname>` ausführen. Installationsdaten werden in `config/catalog-installed.json` vermerkt; `mcsc catalog list <server>` zeigt sie an.

Modrinth ist ohne Schlüssel nutzbar. CurseForge verlangt einen offiziellen API-Schlüssel als Umgebungsvariable `CURSEFORGE_API_KEY`; der Schlüssel wird ausschließlich im Request-Header verwendet und nicht gespeichert. Die CurseForge-Data-Packs-Kategorie wird dynamisch über die Kategorien-API aufgelöst, eine Kategorie-ID ist nicht fest im Programm hinterlegt. Die CurseForge-API bietet keinen verlässlichen Server-/Client-Filter für Mods, daher sind Herstellerangaben zu prüfen. Erforderliche Projektabhängigkeiten werden angezeigt, aber nicht automatisch installiert. Bei Plugins sind ebenfalls die Herstellerangaben zur Kompatibilität zu prüfen; Paper-only-Dateien werden nicht als Bukkit-/Spigot-kompatibel vorausgesetzt.

## Geyser-Fabric für Minecraft 26.3

`mcsc geyser install <server>` ist für reine Fabric-Profile auf der neuesten stabilen Minecraft-Version freigeschaltet. Die Installation holt Geyser-Fabric und die dazugehörige Fabric API von Modrinth, prüft die veröffentlichten SHA-512-Prüfsummen und legt beide Dateien in `mods/`. Der aktuelle kompatible Geyser-Fabric-Build ist als Beta markiert. Interaktiv fragt das Programm vor dem Beta-Einsatz und vor der Dateiinstallation; nichtinteraktiv müssen `--allow-beta --yes` gesetzt werden. Fabric API bleibt auf dem neuesten passenden stabilen Release. Arclight und Bukkit-/Spigot-Plugins sind bei dieser reinen Fabric-Auswahl nicht enthalten.

Beispiel für einen neuen Crossplay-Server:

```bash
mcsc create crossplay --type fabric --version 26.3
mcsc geyser install crossplay --allow-beta
mcsc start crossplay
# EULA ausdrücklich bestätigen; Geyser erzeugt dabei seine Erstkonfiguration.
mcsc stop crossplay
mcsc geyser configure crossplay
mcsc start crossplay
```

`mcsc geyser configure` muss bei gestopptem Server laufen und setzt in `config/Geyser-Fabric/config.yml` den Profilport sowie `transport: nethernet`. Wenn du später den Profilport änderst, führe den Befehl vor dem nächsten Start erneut aus. Für den Standardport 19132 müssen Router und Firewall bei Verbindungen von außerhalb manuell sowohl **19132/TCP als auch 19132/UDP** freigeben/weiterleiten; das Programm ändert keine Netzwerkregeln. Der Bedrock-Port darf nicht mit dem Java-TCP-Port oder dem Voicechat-UDP-Port kollidieren. Geyser-Fabric funktioniert nur mit serverseitigen Mods; Mods, die auch auf dem Bedrock-/Java-Client installiert werden müssten, sind damit nicht abgedeckt. Floodgate für Bedrock-Spieler ohne Java-Konto wird nicht automatisch installiert. Siehe die offizielle [Geyser-Anleitung](https://geysermc.org/wiki/geyser/setup/) und [Versionsübersicht](https://geysermc.org/wiki/geyser/supported-versions/).

## Tests

```bash
python -m unittest discover -s tests -v
```

## Geplante Ausbaustufen

1. Vanilla-Grundlage, Profile, Ports, Java-Verwaltung und systemd (Umsetzung begonnen; Live-Serverbetrieb noch nicht verifiziert)
2. Live-Smoke-Tests für Vanilla, Fabric, Forge, NeoForge und Arclight
3. Mod-/Plugin-/Datapack-Katalog über Modrinth und CurseForge (Adapter unit-getestet; Live-API-/Download-Smoke-Test offen)
4. RCON-/Inventar-Kompatibilität für weitere Servertypen und Versionen, Backup-Wiederherstellung sowie Live-Serverprüfung
5. Linux-x64-/ARM64-Release-Pakete und Distributionstests; später Windows/macOS

Projektplanung und Arbeitspakete sind in der Notion-Projektzentrale und im Linear-Projekt dokumentiert.
