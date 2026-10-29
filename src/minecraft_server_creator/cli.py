from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys

from .catalog import CatalogError, ContentCatalog
from .config import AppConfig, ConfigError, ConfigStore
from .geyser import GeyserBetaOptInRequired, GeyserError
from .manager import ManagerError, ServerManager
from .minecraft import MinecraftDownloadError
from .mod_updates import ModUpdateError, SafeModUpdater, mod_update_lock, tcp_listener_present
from .models import ARCLIGHT_LOADER_FAMILIES, IMPLEMENTED_SERVER_TYPES, SERVER_TYPES, PortSettings, ValidationError, validate_port
from .network import describe_endpoints, get_lan_ip, get_public_ip, port_available
from .properties import PropertiesError, read_properties
from .rcon import inventory_editing_disabled_reason
from .runtimes import RuntimeErrorMCSC
from .systemd import SystemdError, SystemdUserService

_UNSET = object()


def _yes_no(prompt: str, *, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "j", "ja"}


def _ask_port(prompt: str, default: int | None, *, optional: bool = False) -> int | None:
    if not sys.stdin.isatty():
        return default
    shown = "aus" if default is None else str(default)
    answer = input(f"{prompt} [{shown}]: ").strip()
    if not answer:
        return default
    if optional and answer.lower() in {"aus", "none", "off", "-"}:
        return None
    try:
        return int(validate_port(int(answer), prompt))
    except (ValueError, ValidationError) as exc:
        raise ManagerError(str(exc)) from exc


def _optional_port_arg(value: str) -> int | None:
    if value.lower() in {"none", "off", "-"}:
        return None
    try:
        return int(validate_port(int(value), "Port"))
    except (ValueError, ValidationError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _ensure_setup(manager: ServerManager, store: ConfigStore) -> AppConfig:
    if not store.exists:
        print("Willkommen beim Minecraft Server Creator. Zuerst richten wir die Standardwerte ein.\n")
        return _setup(manager)
    return manager.load()


def _setup(manager: ServerManager) -> AppConfig:
    current = manager.load()
    default_root = current.servers_root or str(Path.home() / "MinecraftServers")
    if sys.stdin.isatty():
        root_raw = input(f"Ordner für Serverprofile [{default_root}]: ").strip()
        root = Path(root_raw).expanduser() if root_raw else Path(default_root).expanduser()
    else:
        root = Path(default_root).expanduser()
    defaults = current.port_defaults
    try:
        ports = PortSettings(
            java_tcp=_ask_port("Java-Port (TCP)", defaults.java_tcp),
            bedrock_udp=_ask_port("Bedrock/Geyser-Port (TCP+UDP für NetherNet; 'aus' falls nicht gebraucht)", defaults.bedrock_udp, optional=True),
            voice_udp=_ask_port("Simple Voice Chat-Port (UDP; 'aus' falls nicht gebraucht)", defaults.voice_udp, optional=True),
        )
        config = manager.setup(root, ports)
    except (OSError, ValueError, ManagerError) as exc:
        raise ManagerError(f"Ersteinrichtung fehlgeschlagen: {exc}") from exc
    print(f"\nEinstellungen gespeichert. Serverordner: {config.servers_root}")
    print("Hinweis: Die Portwerte öffnen weder Router noch Betriebssystem-Firewall.\n")
    return config


def _choose_profile(manager: ServerManager) -> str:
    config = manager.load()
    if not config.profiles:
        raise ManagerError("Es gibt noch keine Serverprofile. Lege zuerst einen Server an.")
    print("Serverprofile:")
    for index, profile in enumerate(config.profiles, 1):
        state = "läuft" if manager.services.is_active(profile) else "gestoppt"
        server_type = f"arclight/{profile.loader_family}" if profile.loader_family else profile.server_type
        print(f"  {index}. {profile.name} ({server_type}, MC {profile.minecraft_version}) — {state}")
    answer = input("Auswahl (Nummer oder Name): ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(config.profiles):
        return config.profiles[int(answer) - 1].slug
    return manager.get_profile(answer).slug


def _create(manager: ServerManager, args: argparse.Namespace) -> None:
    config = manager.load()
    interactive = args.name is None and sys.stdin.isatty()
    if args.name is None and not interactive:
        raise ManagerError("Bitte gib beim nichtinteraktiven Aufruf einen Servernamen als Argument an.")
    name = (args.name or input("Name des neuen Servers: ")).strip()
    if not name:
        raise ManagerError("Der Servername darf nicht leer sein.")
    server_type = args.type
    if not server_type and interactive:
        print("Servertypen:")
        for item in SERVER_TYPES:
            status = "verfügbar" if item in IMPLEMENTED_SERVER_TYPES else "geplant, noch nicht verfügbar"
            print(f"  - {item}: {status}")
        server_type = input("Servertyp [vanilla]: ").strip().lower() or "vanilla"
    server_type = server_type or "vanilla"
    if server_type not in IMPLEMENTED_SERVER_TYPES:
        raise ManagerError(
            f"{server_type} ist noch nicht verfügbar; momentan können Vanilla, Fabric, Forge, NeoForge und Arclight installiert werden."
        )
    arclight_loader = getattr(args, "arclight_loader", None)
    if server_type == "arclight":
        if arclight_loader is None and interactive:
            print("Arclight-Basis: " + ", ".join(ARCLIGHT_LOADER_FAMILIES))
            arclight_loader = input("Basis-Loader (fabric/forge/neoforge): ").strip().lower()
        if arclight_loader not in ARCLIGHT_LOADER_FAMILIES:
            raise ManagerError("Für Arclight musst du --arclight-loader fabric, forge oder neoforge angeben.")
    elif arclight_loader is not None:
        raise ManagerError("--arclight-loader darf nur zusammen mit --type arclight verwendet werden.")
    version = args.version
    if version is None:
        if interactive:
            version = input("Minecraft-Version [latest = neueste stabile Version]: ").strip() or "latest"
        else:
            version = "latest"
    memory = args.memory_mb
    if memory is None:
        raw_memory = input("Maximaler RAM in MB [4096]: ").strip() if interactive else ""
        memory = int(raw_memory) if raw_memory else 4096

    defaults = config.port_defaults
    java_port = args.java_port if args.java_port is not None else (
        _ask_port("Java-Port (TCP)", defaults.java_tcp) if interactive else defaults.java_tcp
    )
    bedrock_port = args.bedrock_port if args.bedrock_port is not _UNSET else (
        _ask_port("Bedrock/Geyser-Port (TCP+UDP für NetherNet; 'aus' falls nicht gebraucht)", defaults.bedrock_udp, optional=True)
        if interactive else defaults.bedrock_udp
    )
    voice_port = args.voice_port if args.voice_port is not _UNSET else (
        _ask_port("Simple Voice Chat-Port (UDP; 'aus' falls nicht gebraucht)", defaults.voice_udp, optional=True)
        if interactive else defaults.voice_udp
    )
    ports = PortSettings(java_tcp=java_port, bedrock_udp=bedrock_port, voice_udp=voice_port)
    display_type = f"Arclight ({arclight_loader})" if server_type == "arclight" else server_type.capitalize()
    download_message = f"{display_type}-Server '{name}' wird geladen."
    if server_type in {"forge", "neoforge"}:
        download_message += " Die passende verwaltete Temurin-JRE wird für den Installer bei Bedarf ebenfalls geladen."
    if not _yes_no(download_message + " Fortfahren?", default=True):
        print("Abgebrochen.")
        return
    profile = manager.create_server(
        name,
        server_type=server_type,
        version=version,
        arclight_loader=arclight_loader,
        ports=ports,
        memory_mb=memory,
        auto_start_on_boot=args.autostart,
    )
    print(f"\nServerprofil '{profile.name}' wurde angelegt.")
    print(f"Minecraft: {profile.minecraft_version}; Java {profile.java_major}; Typ: {profile.server_type}")
    if profile.loader_version:
        family = f" ({profile.loader_family})" if profile.loader_family else ""
        print(f"Loader-Version{family}: {profile.loader_version}")
    print(f"Dateien: {profile.server_dir}")
    print("Vor dem ersten Start musst du der Minecraft-EULA ausdrücklich zustimmen.\n")


def _start(manager: ServerManager, selector: str) -> None:
    profile = manager.get_profile(selector)
    if not profile.eula_accepted:
        print("\nMinecraft EULA: https://aka.ms/MinecraftEULA")
        print("Du musst die EULA selbst lesen und entscheiden, ob du zustimmst.")
        if not _yes_no("Stimmst du der Minecraft-EULA zu?", default=False):
            print("Der Server wurde nicht gestartet.")
            return
        manager.accept_eula(profile.slug, True)
    runtime_path = manager.runtimes.runtime_path(profile.java_major)
    if not runtime_path.exists():
        print(f"Für Minecraft {profile.minecraft_version} wird Java {profile.java_major} benötigt.")
        if not _yes_no("Passende Temurin-JRE herunterladen und lokal speichern?", default=True):
            print("Ohne passende Java-Laufzeit wurde der Server nicht gestartet.")
            return
    if not port_available(profile.ports.java_tcp, "tcp"):
        print(f"Warnung: Java-Port {profile.ports.java_tcp}/TCP scheint bereits belegt zu sein.")
        if not _yes_no("Trotzdem fortfahren?", default=False):
            return
    if profile.ports.bedrock_udp or profile.ports.voice_udp:
        print("Hinweis: Geyser nutzt den Bedrock-Port über TCP und UDP; Voicechat nutzt seinen UDP-Port.")
    profile = manager.start(profile.slug, confirm_linger=_confirm_linger)
    print(f"Server '{profile.name}' wird im Hintergrund gestartet.")
    print(f"Logs: mcsc logs {shlex.quote(profile.slug)}")


def _confirm_linger() -> bool:
    return _yes_no(
        "Damit der Server nach der SSH-Abmeldung weiterläuft, einmalig systemd-Lingering mit sudo aktivieren?",
        default=False,
    )


def _show_list(manager: ServerManager) -> None:
    config = manager.load()
    if not config.profiles:
        print("Noch keine Serverprofile. Nutze im Menü 'Server anlegen'.")
        return
    print("\nServerprofile:")
    for profile in config.profiles:
        state = "läuft" if manager.services.is_active(profile) else "gestoppt"
        auto = "Autostart an" if profile.auto_start_on_boot else "Autostart aus"
        bedrock = str(profile.ports.bedrock_udp) if profile.ports.bedrock_udp else "aus"
        voice = str(profile.ports.voice_udp) if profile.ports.voice_udp else "aus"
        family = f" ({profile.loader_family})" if profile.loader_family else ""
        loader = f" / Loader{family} {profile.loader_version}" if profile.loader_version else ""
        print(
            f"- {profile.name} [{profile.slug}] | {profile.server_type} {profile.minecraft_version}{loader} | {state} | {auto}\n"
            f"  Java TCP {profile.ports.java_tcp}; Bedrock/Geyser TCP+UDP {bedrock}; Voice UDP {voice}"
        )
    print()


def _show_ip(manager: ServerManager) -> None:
    config = manager.load()
    lan_ip = get_lan_ip()
    public_ip = get_public_ip()
    if not config.profiles:
        print("Lege zuerst ein Serverprofil an, damit konfigurierte Ports angezeigt werden.")
        if public_ip:
            print(f"Öffentliche IP: {public_ip}")
        if lan_ip:
            print(f"LAN-IP: {lan_ip}")
        return
    for profile in config.profiles:
        print(f"\n{profile.name}:")
        for line in describe_endpoints(profile.ports, public_ip, lan_ip):
            print(f"  {line}")
    print("\nDas sind konfigurierte Adressen, keine Erreichbarkeitsgarantie. Router, Firewall und Provider prüfen.")
    print("Geyser nutzt den Bedrock-Port mit NetherNet über TCP und UDP; Simple Voice Chat nutzt UDP.")
    print("Geyser/Simple Voice Chat müssen trotzdem installiert und konfiguriert sein; Router und Firewall bleiben manuell.")


def _configure(manager: ServerManager, selector: str) -> None:
    if not sys.stdin.isatty():
        raise ManagerError("Geführte Servereinstellungen benötigen ein interaktives Terminal.")
    profile = manager.get_profile(selector)
    if manager.services.is_active(profile):
        raise ManagerError("Stoppe den Server vor dem Ändern dieser Einstellungen.")
    path = Path(profile.server_dir) / "server.properties"
    current = read_properties(path)

    def ask(label: str, key: str, fallback: str) -> str:
        value = current.get(key, fallback)
        answer = input(f"{label} [{value}]: ").strip()
        return answer or value

    settings = {
        "motd": ask("Serverbeschreibung (MOTD)", "motd", profile.name),
        "max-players": ask("Maximale Spielerzahl", "max-players", "20"),
        "difficulty": ask("Schwierigkeit (peaceful/easy/normal/hard)", "difficulty", "normal"),
        "gamemode": ask("Spielmodus (survival/creative/adventure/spectator)", "gamemode", "survival"),
        "pvp": ask("Spieler gegen Spieler erlauben (true/false)", "pvp", "true"),
        "white-list": ask("Whitelist aktivieren (true/false)", "white-list", "false"),
        "view-distance": ask("Sichtweite (2–32 Chunks)", "view-distance", "10"),
        "simulation-distance": ask("Simulationsweite (2–32 Chunks)", "simulation-distance", "10"),
        "spawn-protection": ask("Spawn-Schutzradius (0–64 Blöcke)", "spawn-protection", "16"),
    }
    memory = int(ask("Maximaler RAM in MB", "__memory_mb__", str(profile.memory_mb)))
    if memory < 1024:
        raise ManagerError("Mindestens 1024 MB RAM müssen zugewiesen werden.")
    manager.update_settings(profile.slug, settings)
    manager.set_memory(profile.slug, memory)
    print("Servereinstellungen gespeichert. Sie werden beim nächsten Start übernommen.")


def _rcon_menu(manager: ServerManager) -> None:
    profile = manager.get_profile(_choose_profile(manager))
    if not manager.rcon_is_enabled(profile.slug):
        if manager.services.is_active(profile):
            raise ManagerError("Stoppe den Server, bevor du lokales RCON einrichtest.")
        if not _yes_no(
            "RCON aktivieren? Es wird ausschließlich an 127.0.0.1 gebunden; das Passwort bleibt lokal.",
            default=False,
        ):
            return
        manager.enable_rcon(profile.slug)
        print("Lokales RCON ist eingerichtet. Starte den Server, damit es aktiv wird.")
        return
    if not manager.services.is_active(profile):
        raise ManagerError("RCON ist eingerichtet. Starte den Server, bevor du die Spielertools verwendest.")

    while True:
        print(
            f"{chr(10)}RCON-Spielertools für {profile.name} (nur online befindliche Spieler){chr(10)}"
            "  1) Online-Spieler anzeigen\n"
            "  2) Spielerwerte/Scoreboard anzeigen\n"
            "  3) Live-Inventar anzeigen\n"
            "  4) Inventarplatz ändern\n"
            "  0) Zurück"
        )
        choice = input("Auswahl: ").strip()
        if choice == "0":
            return
        if choice == "1":
            print(manager.rcon_players(profile.slug))
        elif choice in {"2", "3"}:
            player = input("Online-Spielername: ").strip()
            result = (
                manager.rcon_player_stats(profile.slug, player)
                if choice == "2"
                else manager.rcon_inventory(profile.slug, player)
            )
            print(result or "Keine Daten zurückgegeben.")
        elif choice == "4":
            reason = inventory_editing_disabled_reason(profile.server_type, profile.minecraft_version)
            if reason:
                print(f"Inventarbearbeitung deaktiviert: {reason}")
                continue
            player = input("Online-Spielername: ").strip()
            slot = int(input("Inventarplatz (0–35): ").strip())
            item = input("Item-ID (zum Beispiel minecraft:diamond oder air): ").strip()
            count_raw = input("Stückzahl [1]: ").strip()
            count = int(count_raw) if count_raw else 1
            if not _yes_no(f"{item} × {count} in Platz {slot} von {player} einsetzen?", default=False):
                print("Nicht geändert.")
                continue
            result = manager.rcon_set_inventory_item(profile.slug, player, slot, item, count)
            print(result or "Befehl gesendet; der Server hat keine Textantwort geliefert.")
        else:
            print("Bitte eine gültige Menüoption eingeben.")


def _install_catalog_project(
    manager: ServerManager,
    catalog: ContentCatalog,
    profile_slug: str,
    source: str,
    kind: str,
    project_id: str,
    *,
    assume_yes: bool = False,
    include_beta: bool = False,
    version_id: str | None = None,
) -> None:
    profile = manager.get_profile(profile_slug)
    if not manager.services.supported():
        raise ManagerError("Katalogdateien können nur installiert werden, wenn MCSC den systemd-Serverstatus prüfen kann.")
    if manager.services.is_active(profile) or tcp_listener_present(profile.ports.java_tcp):
        raise ManagerError("Stoppe den Server vor der Installation von Mods, Plugins oder Datapacks.")
    destination = catalog.destination(profile, kind)
    plan = catalog.prepare_install(
        source,
        kind,
        project_id,
        profile,
        include_beta=include_beta,
        version_id=version_id,
    )
    print(f"{plan.project_title} — {plan.file_version} ({plan.release_channel})")
    print(f"Quelle: {plan.source}; Datei: {plan.file_name}; Ziel: {destination}/")
    print(f"Projektseite: {plan.project_url}")
    if plan.required_dependencies:
        print(
            "Warnung: Das Projekt nennt erforderliche Abhängigkeiten, die nicht automatisch installiert werden: "
            + ", ".join(plan.required_dependencies)
        )
    if kind == "plugin":
        print("Kompatibilitätshinweis: Arclight unterstützt Bukkit/Spigot; Paper-only-Plugins sind nicht abgedeckt. Prüfe die Herstellerangaben.")
    if kind == "mod" and source == "curseforge":
        print("Kompatibilitätshinweis: CurseForge liefert keinen verlässlichen Server-/Client-Filter; prüfe die Herstellerangaben.")
    if kind == "datapack":
        print("Nach dem Start im Server prüfen: /datapack list available; falls nötig mit /datapack enable file/<Dateiname> aktivieren.")
    if not assume_yes:
        if not sys.stdin.isatty():
            raise ManagerError("Nichtinteraktive Kataloginstallationen benötigen die ausdrückliche Option --yes.")
        if not _yes_no("Diese Katalogdatei installieren?", default=False):
            print("Installation abgebrochen.")
            return
    with mod_update_lock(profile):
        if manager.services.is_active(profile) or tcp_listener_present(profile.ports.java_tcp):
            raise ManagerError("Der Server wurde während der Installation gestartet; Datei wurde nicht verändert.")
        installed = catalog.install(plan, profile, allow_beta=include_beta)
    print(f"Installiert: {installed}")


def _catalog_command(manager: ServerManager, args: argparse.Namespace) -> None:
    profile = manager.get_profile(args.server)
    catalog = ContentCatalog()
    if args.catalog_action == "list":
        files = catalog.installed(profile)
        if not files:
            print("Es sind keine Kataloginstallationen registriert.")
            return
        for item in files:
            state = "vorhanden" if (Path(profile.server_dir) / str(item.get("path", ""))).is_file() else "fehlt"
            print(
                f"- {item.get('project_title', item.get('project_id', '?'))} "
                f"[{item.get('source', '?')}/{item.get('kind', '?')}] "
                f"MC {item.get('minecraft_version', '?')} / {item.get('file_version', '?')} "
                f"({item.get('release_channel', 'release')}) — {item.get('path', '?')} ({state})"
            )
        return
    if args.catalog_action == "search":
        projects = catalog.search(args.source, args.kind, args.query, profile, limit=args.limit)
        if args.kind == "mod" and args.source == "curseforge":
            print("Hinweis: CurseForge filtert nicht verlässlich nach Server-/Client-Seite; bitte die Herstellerangaben prüfen.")
        if not projects:
            print("Keine passenden Projekte gefunden.")
            return
        for project in projects:
            print(f"{project.project_id}\t{project.title} — {project.summary} [{project.downloads:,} Downloads]")
            print(f"  {project.url}")
        print("\nZum Installieren: mcsc catalog install <server> <projekt-id> --source <quelle> --kind <mod|plugin|datapack>")
        return
    if args.catalog_action == "install":
        _install_catalog_project(
            manager,
            catalog,
            profile.slug,
            args.source,
            args.kind,
            args.project_id,
            assume_yes=args.yes,
            include_beta=args.allow_beta,
            version_id=args.version_id,
        )


def _mods_command(manager: ServerManager, args: argparse.Namespace) -> None:
    profile = manager.get_profile(args.server)
    catalog = ContentCatalog()
    if args.mods_action == "install-clumps":
        _install_catalog_project(
            manager,
            catalog,
            profile.slug,
            "modrinth",
            "mod",
            "Wnxd13zP",
            assume_yes=args.yes,
        )
        return

    updater = SafeModUpdater(manager, catalog)
    if args.mods_action == "backups":
        backups = updater.list_backups(profile)
        if not backups:
            print("Es gibt noch keine Mod-Update-Backups.")
            return
        for item in backups:
            print(
                f"- {item['transaction_id']} [{item['status']}] — {item['created_at']}"
                + (f"; Server-Backup: {item['full_server_backup']}" if item["full_server_backup"] else "")
            )
        return

    if args.mods_action == "rollback":
        backups = updater.list_backups(profile)
        latest = backups[0] if backups else None
        if args.transaction_id:
            transaction_id = args.transaction_id
        elif latest is not None and latest["status"] in {"complete", "in_progress"}:
            transaction_id = latest["transaction_id"]
        else:
            transaction_id = None
        if transaction_id is None:
            raise ModUpdateError(
                "Das neueste Mod-Update hat keinen zurücksetzbaren Stand. "
                "Wähle bei Bedarf eine ältere Transaktion ausdrücklich aus 'mcsc mods backups'."
            )
        if not args.yes:
            if not sys.stdin.isatty():
                raise ManagerError("Nichtinteraktives Mod-Rollback benötigt die ausdrückliche Option --yes.")
            if not _yes_no(f"Die Mod-Dateien von Transaktion {transaction_id} zurücksetzen?", default=False):
                print("Rollback abgebrochen.")
                return
        updater.rollback(profile, transaction_id=transaction_id)
        print(f"Mod-Update {transaction_id} wurde zurückgesetzt. Der Server bleibt gestoppt.")
        return

    prepared = updater.prepare(profile, allow_beta=args.allow_beta)
    if not prepared.candidates:
        print("Es gibt keine neuen kompatiblen Mod-Versionen.")
        for item in prepared.skipped:
            print(f"- {item.title}: {item.status} — {item.detail}")
        return

    print(f"Geplante Einzelupdates für {profile.name} (Minecraft {profile.minecraft_version}):")
    for candidate in prepared.candidates:
        print(
            f"- {candidate.plan.project_title}: "
            f"{candidate.record.get('file_version', 'unbekannt')} → {candidate.plan.file_version}"
        )
    for item in prepared.skipped:
        print(f"- {item.title}: {item.status} — {item.detail}")

    create_full_backup = args.backup
    if create_full_backup is None:
        if not sys.stdin.isatty():
            raise ManagerError("Nichtinteraktives Mod-Update benötigt --backup oder --no-backup.")
        create_full_backup = _yes_no(
            "Vorher ein vollständiges Server-Backup erstellen? Das kann je nach Weltgröße dauern und Speicherplatz benötigen.",
            default=True,
        )
    if not args.yes:
        if not sys.stdin.isatty():
            raise ManagerError("Nichtinteraktives Mod-Update benötigt die ausdrückliche Option --yes.")
        if not _yes_no(
            f"{len(prepared.candidates)} Mod(s) einzeln ersetzen, nach jedem Update starten und prüfen?",
            default=False,
        ):
            print("Mod-Update abgebrochen.")
            return

    report = updater.apply(
        profile,
        prepared,
        startup_timeout=args.startup_timeout,
        confirm_linger=_confirm_linger,
        full_server_backup=bool(create_full_backup),
        progress=lambda message: print(message, flush=True),
    )
    for item in report.outcomes:
        print(f"- {item.title}: {item.status}" + (f" — {item.detail}" if item.detail else ""))
    if report.backup_dir:
        print(f"Rollback-Sicherung der verwalteten Mods: {report.backup_dir} (ID {report.transaction_id})")
    if report.full_server_backup:
        print(f"Vollständiges Server-Backup: {report.full_server_backup}")
    reverted = [item for item in report.outcomes if item.status == "zurückgesetzt"]
    if reverted:
        print("Mindestens ein Mod-Update schlug fehl; die jeweilige alte Version wurde geprüft und der nächste Mod wurde fortgesetzt.")
    if report.fatal_error:
        raise ModUpdateError(report.fatal_error)
    print("Mod-Update abgeschlossen.")


def _resource_pack_command(manager: ServerManager, args: argparse.Namespace) -> None:
    properties_path = manager.configure_resource_pack(
        args.server,
        url=args.url,
        sha1=args.sha1,
        required=args.required,
    )
    state = "verpflichtend" if args.required else "optional"
    print(f"Server-Ressourcenpaket ({state}) in {properties_path} konfiguriert.")
    print("Die Änderung wird beim nächsten Serverstart übernommen.")


def _geyser_command(manager: ServerManager, args: argparse.Namespace) -> None:
    if args.geyser_action == "configure":
        profile = manager.get_profile(args.server)
        config_path = manager.configure_geyser(profile.slug)
        print(
            f"Geyser-Fabric ist auf {profile.ports.bedrock_udp}/TCP und /UDP mit NetherNet konfiguriert: "
            f"{config_path}"
        )
        print("Für Verbindungen von außen müssen TCP und UDP auf diesem Port manuell freigegeben/weitergeleitet werden.")
        return

    allow_beta = args.allow_beta
    try:
        plan = manager.prepare_geyser_install(args.server, allow_beta=allow_beta)
    except GeyserBetaOptInRequired as exc:
        if not sys.stdin.isatty():
            raise ManagerError(f"{exc} Nichtinteraktiv musst du --allow-beta angeben.") from exc
        if not _yes_no(f"{exc} Beta ausdrücklich zulassen?", default=False):
            print("Geyser-Installation abgebrochen.")
            return
        allow_beta = True
        plan = manager.prepare_geyser_install(args.server, allow_beta=True)

    channel = "Beta" if plan.geyser.release_channel == "beta" else "stabil"
    print(f"Geyser-Fabric {plan.geyser.version} ({channel})")
    print(f"Fabric API {plan.fabric_api.version} (stabil)")
    print(f"Bedrock-Port: {plan.bedrock_port}/TCP + {plan.bedrock_port}/UDP; Transport: NetherNet")
    needs_install = not plan.geyser.already_installed or not plan.fabric_api.already_installed
    if needs_install and not args.yes:
        if not sys.stdin.isatty():
            raise ManagerError("Nichtinteraktive Geyser-Installationen benötigen die ausdrückliche Option --yes.")
        if not _yes_no("Geyser-Fabric und die passende Fabric API installieren?", default=False):
            print("Geyser-Installation abgebrochen.")
            return

    result = manager.install_geyser(args.server, plan)
    geyser_state = "bereits vorhanden" if result.geyser_already_installed else "installiert"
    api_state = "bereits vorhanden" if result.fabric_api_already_installed else "installiert"
    print(f"Geyser-Fabric {result.geyser_version} ({result.geyser_release_channel}): {geyser_state} — {result.geyser_path}")
    print(f"Fabric API {result.fabric_api_version}: {api_state} — {result.fabric_api_path}")
    if result.configuration_path:
        print(f"Geyser-Port und NetherNet wurden konfiguriert: {result.configuration_path}")
    else:
        profile = manager.get_profile(args.server)
        print("Die Geyser-Konfiguration wird beim ersten Serverstart erzeugt.")
        print("Danach den Server stoppen und den Port/NetherNet-Transport setzen:")
        print(f"  mcsc stop {profile.slug}")
        print(f"  mcsc geyser configure {profile.slug}")
        print(f"  mcsc start {profile.slug}")
    print("Netzwerkhinweis: Geyser benötigt TCP und UDP auf dem Bedrock-Port; Firewall/Router werden nicht geändert.")


def _world_regenerate_command(manager: ServerManager, args: argparse.Namespace) -> None:
    profile = manager.get_profile(args.server)
    interactive = sys.stdin.isatty()
    if manager.services.is_active(profile):
        raise ManagerError("Stoppe den Server, bevor du die Welt neu generierst.")

    seed = args.seed
    if seed is not None:
        seed = str(seed).strip() or None
    if seed is None and args.seed is None and interactive:
        answer = input("Neuer Welt-Seed (leer = zufällig): ").strip()
        seed = answer or None

    create_backup = args.backup
    if create_backup is None:
        if not interactive:
            raise ManagerError("Nichtinteraktive Welt-Resets benötigen --backup oder --no-backup.")
        create_backup = _yes_no("Vorher ein vollständiges Server-Backup erstellen?", default=True)

    if not args.yes:
        if not interactive:
            raise ManagerError("Nichtinteraktive Welt-Resets benötigen die ausdrückliche Option --yes.")
        print("Der Reset entfernt Oberwelt, Nether, End, Spielerfortschritt und Datapacks der Welt.")
        answer = input(f"Zum endgültigen Bestätigen den Profil-Slug '{profile.slug}' eingeben: ").strip()
        if answer != profile.slug:
            print("Welt-Neugenerierung abgebrochen; es wurden keine Weltdateien verändert.")
            return

    world_name, backup_path = manager.regenerate_world(
        profile.slug,
        seed=seed,
        create_backup=bool(create_backup),
    )
    print(f"Welt '{world_name}' wird beim nächsten Start mit allen Dimensionen neu generiert.")
    print(f"Seed: {seed if seed is not None else 'zufällig'}")
    if backup_path:
        print(f"Vorheriges vollständiges Server-Backup: {backup_path}")
    else:
        print("Für diesen Reset wurde kein Backup erstellt.")


def _catalog_menu(manager: ServerManager) -> None:
    profile = manager.get_profile(_choose_profile(manager))
    kind = input("Installieren (mod/plugin/datapack): ").strip().lower()
    if kind not in {"mod", "plugin", "datapack"}:
        print("Abgebrochen: Bitte 'mod', 'plugin' oder 'datapack' eingeben.")
        return
    source = input("Katalog (modrinth/curseforge): ").strip().lower()
    if source not in {"modrinth", "curseforge"}:
        print("Abgebrochen: Bitte 'modrinth' oder 'curseforge' eingeben.")
        return
    query = input("Suchbegriff: ").strip()
    catalog = ContentCatalog()
    projects = catalog.search(source, kind, query, profile, limit=10)
    if kind == "mod" and source == "curseforge":
        print("Hinweis: CurseForge filtert nicht verlässlich nach Server-/Client-Seite; bitte die Herstellerangaben prüfen.")
    if not projects:
        print("Keine passenden Projekte gefunden.")
        return
    for index, project in enumerate(projects, 1):
        print(f"  {index}. {project.title} [{project.project_id}] — {project.summary}")
    choice = input("Projekt-Nummer (0 = abbrechen): ").strip()
    if not choice.isdigit() or int(choice) == 0:
        return
    if not 1 <= int(choice) <= len(projects):
        raise ManagerError("Ungültige Projektauswahl.")
    _install_catalog_project(manager, catalog, profile.slug, source, kind, projects[int(choice) - 1].project_id)


def _menu(manager: ServerManager, store: ConfigStore) -> None:
    _ensure_setup(manager, store)
    while True:
        print(
            "\nMinecraft Server Creator\n"
            "  1) Server anzeigen\n"
            "  2) Server anlegen (Vanilla/Fabric/Forge/NeoForge/Arclight)\n"
            "  3) Server starten\n"
            "  4) Server stoppen\n"
            "  5) Geführte Servereinstellungen ändern\n"
            "  6) Ports eines Servers ändern\n"
            "  7) Autostart nach Rechnerneustart ein-/ausschalten\n"
            "  8) Logs anzeigen\n"
            "  9) Backup erstellen (Server muss gestoppt sein)\n"
            " 10) IP-Adressen und Ports anzeigen\n"
            " 11) Grundeinstellungen / Standardports\n"
            " 12) RCON-Spielertools (nur lokale Verbindung)\n"
            " 13) Mod-/Plugin-/Datapack-Katalog (Modrinth/CurseForge)\n"
            " 14) Geyser-Fabric + Fabric API installieren\n"
            " 15) Geyser-Port/NetherNet konfigurieren (nach erstem Serverstart)\n"
            " 16) Welt neu generieren (optional mit Seed)\n"
            "  0) Beenden"
        )
        choice = input("Auswahl: ").strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                _show_list(manager)
            elif choice == "2":
                _create(manager, argparse.Namespace(name=None, type=None, version=None, arclight_loader=None,
                                                    memory_mb=None, java_port=None, bedrock_port=_UNSET,
                                                    voice_port=_UNSET, autostart=False))
            elif choice == "3":
                _start(manager, _choose_profile(manager))
            elif choice == "4":
                profile = manager.stop(_choose_profile(manager))
                print(f"Server '{profile.name}' wurde gestoppt.")
            elif choice == "5":
                _configure(manager, _choose_profile(manager))
            elif choice == "6":
                selector = _choose_profile(manager)
                profile = manager.get_profile(selector)
                ports = PortSettings(
                    java_tcp=_ask_port("Neuer Java-Port (TCP)", profile.ports.java_tcp),
                    bedrock_udp=_ask_port("Neuer Bedrock/Geyser-Port (TCP+UDP)", profile.ports.bedrock_udp, optional=True),
                    voice_udp=_ask_port("Neuer Voicechat-Port (UDP)", profile.ports.voice_udp, optional=True),
                )
                updated = manager.update_ports(selector, ports)
                print("Ports gespeichert. Ein laufender Server übernimmt Änderungen erst nach dem Neustart.")
                if updated.server_type == "fabric":
                    print(
                        f"Falls Geyser installiert ist, vor dem nächsten Start mit `mcsc geyser configure {updated.slug}` "
                        "den Bedrock-Port aktualisieren."
                    )
            elif choice == "7":
                selector = _choose_profile(manager)
                profile = manager.get_profile(selector)
                enabled = _yes_no(f"Autostart für '{profile.name}' aktivieren?", default=not profile.auto_start_on_boot)
                manager.set_autostart(selector, enabled)
                print(f"Autostart {'aktiviert' if enabled else 'deaktiviert'}.")
            elif choice == "8":
                manager.services.logs(manager.get_profile(_choose_profile(manager)))
            elif choice == "9":
                backup = manager.backup(_choose_profile(manager))
                print(f"Backup erstellt: {backup}")
            elif choice == "10":
                _show_ip(manager)
            elif choice == "11":
                _setup(manager)
            elif choice == "12":
                _rcon_menu(manager)
            elif choice == "13":
                _catalog_menu(manager)
            elif choice == "14":
                _geyser_command(
                    manager,
                    argparse.Namespace(
                        geyser_action="install",
                        server=_choose_profile(manager),
                        allow_beta=False,
                        yes=False,
                    ),
                )
            elif choice == "15":
                _geyser_command(
                    manager,
                    argparse.Namespace(geyser_action="configure", server=_choose_profile(manager)),
                )
            elif choice == "16":
                _world_regenerate_command(
                    manager,
                    argparse.Namespace(server=_choose_profile(manager), seed=None, backup=None, yes=False),
                )
            else:
                print("Bitte eine gültige Menüoption eingeben.")
        except (CatalogError, GeyserError, ManagerError, ConfigError, SystemdError, RuntimeErrorMCSC, MinecraftDownloadError, PropertiesError, ValueError, OSError) as exc:
            print(f"Fehler: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcsc", description="SSH-freundlicher Minecraft-Server-Creator")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="Interaktives Terminalmenü öffnen")
    sub.add_parser("setup", help="Ersteinrichtung und Port-Standardwerte konfigurieren")
    sub.add_parser("list", help="Serverprofile anzeigen")
    sub.add_parser("status", help="Serverstatus anzeigen")
    configure = sub.add_parser("configure", help="Geführte Servereinstellungen öffnen")
    configure.add_argument("server", help="Servername oder Profil-Slug")
    create = sub.add_parser("create", help="Minecraft-Serverprofil anlegen")
    create.add_argument("name", nargs="?", help="Anzeigename des Servers")
    create.add_argument("--type", choices=SERVER_TYPES, default=None, help="Servertyp (alle fünf Typen verfügbar)")
    create.add_argument(
        "--arclight-loader",
        choices=ARCLIGHT_LOADER_FAMILIES,
        default=None,
        help="Arclight-Basis auswählen (fabric, forge oder neoforge)",
    )
    create.add_argument("--version", default=None, help="Minecraft-Version oder 'latest'")
    create.add_argument("--memory-mb", type=int, default=None, help="Maximaler Arbeitsspeicher in MB")
    create.add_argument("--java-port", type=int, default=None, help="Java-Port/TCP")
    create.add_argument("--bedrock-port", type=_optional_port_arg, default=_UNSET, help="Bedrock/Geyser-Port/TCP+UDP oder 'off'")
    create.add_argument("--voice-port", type=_optional_port_arg, default=_UNSET, help="Voicechat-Port/UDP oder 'off'")
    create.add_argument("--autostart", action="store_true", help="Nach Host-Neustart automatisch starten")

    for command, help_text in (("start", "Server starten"), ("stop", "Server stoppen"), ("logs", "Serverlogs anzeigen"), ("backup", "Backup erstellen")):
        cmd = sub.add_parser(command, help=help_text)
        cmd.add_argument("server", help="Servername oder Profil-Slug")
        if command == "logs":
            cmd.add_argument("--follow", action="store_true", help="Logs live verfolgen")

    ports = sub.add_parser("ports", help="Ports eines Serverprofils ändern")
    ports.add_argument("server")
    ports.add_argument("--java", type=int, required=True, help="Java/TCP-Port")
    ports.add_argument("--bedrock", type=_optional_port_arg, default=_UNSET, help="Bedrock/Geyser-Port/TCP+UDP oder off")
    ports.add_argument("--voice", type=_optional_port_arg, default=_UNSET, help="Voicechat/UDP-Port oder off")

    rcon = sub.add_parser("rcon", help="Lokale RCON-Spielertools für online befindliche Spieler")
    rcon_sub = rcon.add_subparsers(dest="rcon_action", required=True)
    for action in ("enable", "disable", "players"):
        rcon_action = rcon_sub.add_parser(action)
        rcon_action.add_argument("server", help="Servername oder Profil-Slug")
    for action in ("stats", "inventory"):
        rcon_action = rcon_sub.add_parser(action)
        rcon_action.add_argument("server", help="Servername oder Profil-Slug")
        rcon_action.add_argument("player", help="Online-Java-Spielername")
    set_item = rcon_sub.add_parser("set-item", help="Einen Live-Inventarplatz setzen (Minecraft 1.17+)")
    set_item.add_argument("server", help="Servername oder Profil-Slug")
    set_item.add_argument("player", help="Online-Java-Spielername")
    set_item.add_argument("slot", type=int, help="Inventarplatz 0–35")
    set_item.add_argument("item", help="Item-ID, zum Beispiel minecraft:diamond")
    set_item.add_argument("count", type=int, nargs="?", default=1, help="Stückzahl 1–64")
    set_item.add_argument("--yes", action="store_true", help="Inventaränderung ausdrücklich bestätigen (nichtinteraktiv erforderlich)")

    catalog = sub.add_parser("catalog", help="Mods, Plugins und Datapacks über Modrinth oder CurseForge suchen und installieren")
    catalog_sub = catalog.add_subparsers(dest="catalog_action", required=True)
    catalog_search = catalog_sub.add_parser("search", help="Kompatible Mods, Plugins oder Datapacks suchen")
    catalog_search.add_argument("server", help="Servername oder Profil-Slug")
    catalog_search.add_argument("query", help="Suchbegriff")
    catalog_search.add_argument("--source", choices=("modrinth", "curseforge"), required=True)
    catalog_search.add_argument("--kind", choices=("mod", "plugin", "datapack"), required=True)
    catalog_search.add_argument("--limit", type=int, default=10, help="Maximale Trefferzahl (1–50)")
    catalog_install = catalog_sub.add_parser("install", help="Neueste stabile kompatible Datei oder festgelegte Modrinth-Version installieren")
    catalog_install.add_argument("server", help="Servername oder Profil-Slug")
    catalog_install.add_argument("project_id", help="Projekt-ID aus der Suche")
    catalog_install.add_argument("--source", choices=("modrinth", "curseforge"), required=True)
    catalog_install.add_argument("--kind", choices=("mod", "plugin", "datapack"), required=True)
    catalog_install.add_argument("--version-id", help="Feste Modrinth-Versions-ID statt neuester passender Version")
    catalog_install.add_argument("--allow-beta", action="store_true", help="Beta-Version ausdrücklich zulassen (nur Modrinth)")
    catalog_install.add_argument("--yes", action="store_true", help="Dateiinstallation ausdrücklich bestätigen")
    catalog_list = catalog_sub.add_parser("list", help="Kataloginstallationen eines Profils anzeigen")
    catalog_list.add_argument("server", help="Servername oder Profil-Slug")

    mods = sub.add_parser("mods", help="Katalogverwaltete Mods sicher aktualisieren, zurücksetzen oder Clumps installieren")
    mods_sub = mods.add_subparsers(dest="mods_action", required=True)
    mods_clumps = mods_sub.add_parser("install-clumps", help="Kompatible Clumps-Version für dieses Profil installieren")
    mods_clumps.add_argument("server", help="Servername oder Profil-Slug")
    mods_clumps.add_argument("--yes", action="store_true", help="Installation ausdrücklich bestätigen")
    mods_update = mods_sub.add_parser("update", help="Verwaltete Mods einzeln aktualisieren und nach jedem Start prüfen")
    mods_update.add_argument("server", help="Servername oder Profil-Slug")
    mods_update.add_argument("--allow-beta", action="store_true", help="Kompatible Beta-Versionen ausdrücklich zulassen")
    mods_update.add_argument("--startup-timeout", type=int, default=180, help="Sekunden bis Minecraft 'Done' melden muss (30–1800)")
    mods_update.add_argument("--yes", action="store_true", help="Mod-Updates ausdrücklich bestätigen")
    mods_backup_group = mods_update.add_mutually_exclusive_group()
    mods_backup_group.add_argument("--backup", dest="backup", action="store_true", help="Vorher vollständiges Server-Backup erstellen")
    mods_backup_group.add_argument("--no-backup", dest="backup", action="store_false", help="Ohne vollständiges Server-Backup aktualisieren (Mod-Rollback bleibt verfügbar)")
    mods_update.set_defaults(backup=None)
    mods_rollback = mods_sub.add_parser("rollback", help="Letztes Mod-Update oder eine bestimmte Transaktion zurücksetzen")
    mods_rollback.add_argument("server", help="Servername oder Profil-Slug")
    mods_rollback.add_argument("transaction_id", nargs="?", help="Optionale Transaktions-ID aus 'mcsc mods backups'")
    mods_rollback.add_argument("--yes", action="store_true", help="Rollback ausdrücklich bestätigen")
    mods_backups = mods_sub.add_parser("backups", help="Mod-Update-Transaktionen und Sicherungen anzeigen")
    mods_backups.add_argument("server", help="Servername oder Profil-Slug")

    resource_pack = sub.add_parser("resource-pack", help="Server-Ressourcenpaket in server.properties konfigurieren")
    resource_pack_sub = resource_pack.add_subparsers(dest="resource_pack_action", required=True)
    resource_pack_set = resource_pack_sub.add_parser("set", help="HTTPS-Ressourcenpaket setzen oder erzwingen")
    resource_pack_set.add_argument("server", help="Servername oder Profil-Slug")
    resource_pack_set.add_argument("--url", required=True, help="Direkte HTTPS-URL zur ZIP-Datei")
    resource_pack_set.add_argument("--sha1", required=True, help="SHA-1-Hash der ZIP-Datei (40 Hex-Zeichen)")
    required_group = resource_pack_set.add_mutually_exclusive_group(required=True)
    required_group.add_argument("--required", dest="required", action="store_true", help="Spieler müssen das Pack annehmen")
    required_group.add_argument("--optional", dest="required", action="store_false", help="Spieler dürfen ohne Pack beitreten")

    geyser = sub.add_parser("geyser", help="Geyser-Fabric für kompatible Fabric-Profile installieren/konfigurieren")
    geyser_sub = geyser.add_subparsers(dest="geyser_action", required=True)
    geyser_install = geyser_sub.add_parser("install", help="Geyser-Fabric und Fabric API installieren")
    geyser_install.add_argument("server", help="Servername oder Profil-Slug")
    geyser_install.add_argument("--allow-beta", action="store_true", help="Beta-Build ausdrücklich zulassen, falls kein stabiler passt")
    geyser_install.add_argument("--yes", action="store_true", help="Dateiinstallation ausdrücklich bestätigen")
    geyser_configure = geyser_sub.add_parser("configure", help="Geyser-Port und NetherNet nach dem ersten Serverstart setzen")
    geyser_configure.add_argument("server", help="Servername oder Profil-Slug")

    world = sub.add_parser("world", help="Minecraft-Welten verwalten")
    world_sub = world.add_subparsers(dest="world_action", required=True)
    world_regenerate = world_sub.add_parser("regenerate", help="Welt mit optionalem Seed neu generieren")
    world_regenerate.add_argument("server", help="Servername oder Profil-Slug")
    world_regenerate.add_argument("--seed", default=None, help="Welt-Seed; ohne diese Option wird zufällig generiert")
    backup_group = world_regenerate.add_mutually_exclusive_group()
    backup_group.add_argument("--backup", dest="backup", action="store_true", help="Vorher vollständiges Server-Backup erstellen")
    backup_group.add_argument("--no-backup", dest="backup", action="store_false", help="Reset ohne vorheriges Backup ausführen")
    world_regenerate.set_defaults(backup=None)
    world_regenerate.add_argument("--yes", action="store_true", help="Welt-Reset ausdrücklich bestätigen")

    auto = sub.add_parser("autostart", help="Autostart nach Host-Neustart einstellen")
    auto.add_argument("server")
    auto.add_argument("state", choices=("on", "off"))
    sub.add_parser("ip", help="LAN-/öffentliche IP und Ports anzeigen")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = ConfigStore()
    manager = ServerManager(store=store)
    try:
        if args.command in (None, "menu"):
            _menu(manager, store)
        elif args.command == "setup":
            _setup(manager)
        elif args.command == "create":
            _ensure_setup(manager, store)
            _create(manager, args)
        elif args.command == "list":
            _ensure_setup(manager, store)
            _show_list(manager)
        elif args.command == "status":
            _ensure_setup(manager, store)
            _show_list(manager)
        elif args.command == "configure":
            _configure(manager, args.server)
        elif args.command == "start":
            _ensure_setup(manager, store)
            _start(manager, args.server)
        elif args.command == "stop":
            profile = manager.stop(args.server)
            print(f"Server '{profile.name}' wurde gestoppt.")
        elif args.command == "logs":
            manager.services.logs(manager.get_profile(args.server), follow=args.follow)
        elif args.command == "backup":
            print(f"Backup erstellt: {manager.backup(args.server)}")
        elif args.command == "catalog":
            _ensure_setup(manager, store)
            _catalog_command(manager, args)
        elif args.command == "mods":
            _ensure_setup(manager, store)
            _mods_command(manager, args)
        elif args.command == "resource-pack":
            _ensure_setup(manager, store)
            _resource_pack_command(manager, args)
        elif args.command == "geyser":
            _ensure_setup(manager, store)
            _geyser_command(manager, args)
        elif args.command == "world":
            _ensure_setup(manager, store)
            if args.world_action == "regenerate":
                _world_regenerate_command(manager, args)
        elif args.command == "rcon":
            if args.rcon_action == "enable":
                manager.enable_rcon(args.server)
                print("RCON ist für 127.0.0.1 eingerichtet. Passwort mit Dateirechten 0600 lokal gespeichert.")
                print("Starte den Server, damit die RCON-Einstellungen übernommen werden.")
            elif args.rcon_action == "disable":
                manager.disable_rcon(args.server)
                print("RCON wurde deaktiviert und das lokal gespeicherte Passwort entfernt.")
            elif args.rcon_action == "players":
                print(manager.rcon_players(args.server))
            elif args.rcon_action == "stats":
                print(manager.rcon_player_stats(args.server, args.player))
            elif args.rcon_action == "inventory":
                print(manager.rcon_inventory(args.server, args.player))
            elif args.rcon_action == "set-item":
                if not args.yes:
                    if not sys.stdin.isatty():
                        raise ManagerError("Nichtinteraktive Inventaränderungen benötigen die ausdrückliche Option --yes.")
                    if not _yes_no(
                        f"Inventarplatz {args.slot} von {args.player} mit {args.item} × {args.count} ersetzen?",
                        default=False,
                    ):
                        print("Abgebrochen.")
                        return 0
                print(manager.rcon_set_inventory_item(args.server, args.player, args.slot, args.item, args.count))
        elif args.command == "ports":
            profile = manager.get_profile(args.server)
            bedrock = args.bedrock if args.bedrock is not _UNSET else profile.ports.bedrock_udp
            voice = args.voice if args.voice is not _UNSET else profile.ports.voice_udp
            updated = manager.update_ports(
                args.server,
                PortSettings(java_tcp=args.java, bedrock_udp=bedrock, voice_udp=voice),
            )
            print(f"Ports für '{updated.name}' aktualisiert.")
            if updated.server_type == "fabric":
                print(
                    f"Falls Geyser installiert ist, vor dem nächsten Start mit `mcsc geyser configure {updated.slug}` "
                    "den Bedrock-Port aktualisieren."
                )
        elif args.command == "autostart":
            profile = manager.set_autostart(args.server, args.state == "on")
            print(f"Autostart für '{profile.name}' {'aktiviert' if profile.auto_start_on_boot else 'deaktiviert'}.")
        elif args.command == "ip":
            _ensure_setup(manager, store)
            _show_ip(manager)
        else:
            parser.print_help()
    except (
        ConfigError,
        CatalogError,
        GeyserError,
        ManagerError,
        ModUpdateError,
        SystemdError,
        RuntimeErrorMCSC,
        MinecraftDownloadError,
        PropertiesError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    return 0
