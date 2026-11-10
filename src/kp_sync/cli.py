"""Define and dispatch the kp-sync command-line interface."""

import argparse
import contextlib
import io
import json
import sys
import threading
from collections.abc import Sequence

import argcomplete

from kp_sync import __version__
from kp_sync.config import ConfigError, load_config
from kp_sync.credentials import CredentialProvider
from kp_sync.engine import SyncEngine, SyncError, endpoints_from_config
from kp_sync.log import error
from kp_sync.paths import ensure_run_dir
from kp_sync.pending import PendingError
from kp_sync.state import HistoryState, StateError
from kp_sync.ui.factory import create_ui

COMMANDS = (
    ("daemon", "Run the synchronization daemon"),
    ("run", "Synchronize and start an application"),
    ("up", "Attach a drive and start the default application"),
    ("attach", "Register and synchronize a drive"),
    ("detach", "Release a drive"),
    ("sync", "Synchronize configured copies"),
    ("resolve", "Handle pending decisions"),
    ("eject", "Synchronize and unmount a drive"),
    ("down", "Detach a drive and stop when idle"),
    ("drive-sync", "Submit a udevmon drive event to the daemon"),
    ("drive-setup", "Register a key drive with udevmon and fill it"),
    ("drive-retire", "Decommission a key drive, present or not"),
    ("session", "Start a confined remote session"),
    ("service", "Manage the daemon, named SSH sessions and kp-access owners"),
    ("access-op", "Handle a kp-access file operation"),
    ("sftp-serve", "Serve a confined session directory"),
    ("restore", "Restore older database content"),
    ("history", "List retained database versions"),
    ("retire-session", "Preview or retire exact legacy session records"),
    ("cleanup", "Preview or remove obsolete baseline files"),
    ("repair-history", "Preview or repair versions with missing bases"),
    ("forget", "Forget the cached passwords"),
    ("keepassxc-disconnect", "Remove local KeePassXC pairing"),
    ("status", "Show synchronization status"),
    ("doctor", "Check the installation"),
    ("completion", "Install Bash completion for kp-sync and aliases"),
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kp-sync",
        description="Synchronize a KeePass database across trusted copies.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument(
        "--config", metavar="FILE", help="configuration file"
    )
    config_group.add_argument(
        "--profile", metavar="NAME", help="named configuration profile"
    )
    parser.add_argument(
        "--ui",
        choices=("auto", "tty", "gui", "none"),
        help="user interaction mode",
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="return without waiting"
    )
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="run the engine directly instead of using the daemon",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show routine INFO messages on a terminal",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show DEBUG messages explaining decisions (implies --verbose)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, help_text in COMMANDS:
        command_parser = subparsers.add_parser(
            name, help=help_text, description=help_text
        )
        if name in {
            "sync",
            "resolve",
            "up",
            "down",
            "attach",
            "detach",
            "run",
            "eject",
        }:
            command_parser.add_argument(
                "--no-wait",
                action="store_true",
                default=argparse.SUPPRESS,
                help="return after the daemon accepts the request",
            )
        if name in {"sync", "resolve"}:
            command_parser.add_argument(
                "--no-daemon",
                action="store_true",
                default=argparse.SUPPRESS,
                help="run the engine directly instead of using the daemon",
            )
        if name == "sync":
            command_parser.add_argument(
                "--dry-run",
                action="store_true",
                help="classify copies without writing",
            )
            command_parser.add_argument(
                "--idle",
                action="store_true",
                help="the timer's idle sync: skipped while the cloud is "
                "clearly unreachable, and unreachable copies are not an "
                "error",
            )
        elif name == "status":
            command_parser.add_argument(
                "--long", action="store_true", help="show full version hashes"
            )
            command_parser.add_argument(
                "--json", action="store_true", help="emit JSON"
            )
        elif name == "cleanup":
            scope = command_parser.add_mutually_exclusive_group()
            scope.add_argument(
                "--store",
                metavar="NAME",
                help="select one configured baseline endpoint",
            )
            scope.add_argument(
                "--runtime",
                action="store_true",
                help="select only the runtime base cache",
            )
            command_parser.add_argument(
                "--apply",
                action="store_true",
                help="delete previewed obsolete bases in an explicit scope",
            )
            command_parser.add_argument(
                "--confirm-writers-paused",
                action="store_true",
                help="confirm other shared-history writers remain paused",
            )
        elif name == "repair-history":
            command_parser.add_argument(
                "--apply",
                action="store_true",
                help="restore bases and remove versions whose base is lost",
            )
            command_parser.add_argument(
                "--confirm-writers-paused",
                action="store_true",
                help="confirm other shared-history writers remain paused",
            )
        elif name == "retire-session":
            command_parser.add_argument(
                "--identity",
                metavar="IDENTITY",
                help="preview or select one exact session identity",
            )
            command_parser.add_argument(
                "--confirm",
                metavar="IDENTITY",
                help="confirm retirement of this exact identity",
            )
        elif name == "restore":
            source = command_parser.add_mutually_exclusive_group(required=True)
            source.add_argument(
                "--version", dest="restore_version", metavar="SHA"
            )
            source.add_argument(
                "--backup", dest="restore_backup", metavar="NAME"
            )
            source.add_argument("--file", dest="restore_file", metavar="PATH")
            mode = command_parser.add_mutually_exclusive_group()
            mode.add_argument(
                "--merge",
                dest="restore_mode",
                action="store_const",
                const="merge",
                help=(
                    "merge selected old content into the current "
                    "database (default)"
                ),
            )
            mode.add_argument(
                "--replace",
                dest="restore_mode",
                action="store_const",
                const="replace",
                help="replace current content with the selected database",
            )
            command_parser.set_defaults(restore_mode="merge")
            command_parser.add_argument("--dry-run", action="store_true")
        elif name == "doctor":
            command_parser.add_argument(
                "--self-test",
                action="store_true",
                help=(
                    "temporarily authorize a five-minute key for a "
                    "localhost SFTP round trip"
                ),
            )
        elif name == "daemon":
            command_parser.add_argument("--foreground", action="store_true")
            command_parser.add_argument(
                "--debug",
                action="store_true",
                default=argparse.SUPPRESS,
                help="show DEBUG messages explaining decisions",
            )
        elif name == "session":
            command_parser.add_argument(
                "-w", action="store_true", help="mount read-write"
            )
            command_parser.add_argument(
                "--unconfined",
                action="store_true",
                help="serve without bwrap confinement (not recommended)",
            )
            command_parser.add_argument("target", metavar="[USER@]HOST[:PATH]")
        elif name == "drive-sync":
            command_parser.add_argument("event", choices=("up", "down"))
            command_parser.add_argument(
                "--mode",
                required=True,
                choices=("main", "secondary"),
                help="main: up/down; secondary: attach/detach",
            )
            command_parser.add_argument(
                "--drive-id", required=True, metavar="SERIAL:FS_UUID"
            )
            command_parser.add_argument("mountpoint", nargs="?")
        elif name == "drive-setup":
            command_parser.add_argument(
                "mountpoint", help="where the key drive is mounted"
            )
            command_parser.add_argument(
                "name", help="udevmon device name, e.g. key-daily"
            )
            command_parser.add_argument(
                "--rclone",
                action="store_true",
                help="the drive also carries rclone credentials",
            )
            command_parser.add_argument(
                "--dry-run",
                action="store_true",
                help="show what would be done, change nothing",
            )
        elif name == "completion":
            command_parser.add_argument(
                "action", choices=("install",), help="what to do"
            )
            command_parser.add_argument(
                "--alias",
                action="append",
                default=[],
                metavar="NAME",
                help="also complete this alias of kp-sync --profile HOST "
                "(repeatable), e.g. kps",
            )
            command_parser.add_argument(
                "--dry-run",
                action="store_true",
                help="show what would be written, change nothing",
            )
        elif name == "drive-retire":
            command_parser.add_argument(
                "drive",
                metavar="DRIVE",
                help="udevmon device name, drives.aliases name or "
                "SERIAL:FS_UUID",
            )
            command_parser.add_argument(
                "--confirm",
                metavar="SERIAL:FS_UUID",
                help="apply, confirming this exact identity",
            )
            command_parser.add_argument(
                "--keep-device",
                action="store_true",
                help="set the udevmon device to ignore instead of removing",
            )
            command_parser.add_argument(
                "--last-history-drive",
                action="store_true",
                help="allow decommissioning the last key drive in history",
            )
        elif name == "sftp-serve":
            command_parser.add_argument(
                "--diagnostic", action="store_true", help=argparse.SUPPRESS
            )
            command_parser.add_argument("session_id", metavar="ID")
        elif name in {"run", "up", "attach", "detach", "down", "eject"}:
            if name in {"run", "up"}:
                command_parser.add_argument("--app", metavar="NAME")
            if name == "run":
                command_parser.add_argument(
                    "--raise-existing",
                    action="store_true",
                    help="raise an already-running KeePassXC window",
                )
            if name != "run":
                command_parser.add_argument(
                    "--drive-id", metavar="SERIAL:FS_UUID"
                )
                command_parser.add_argument("mountpoint", nargs="?")
            if name == "run":
                command_parser.add_argument(
                    "app_args", nargs=argparse.REMAINDER
                )
        elif name == "service":
            service_actions = command_parser.add_subparsers(
                dest="service_action",
                required=True,
                metavar="ACTION",
            )
            for action in (
                "start",
                "stop",
                "restart",
                "status",
                "enable",
                "disable",
                "install",
                "uninstall",
            ):
                action_parser = service_actions.add_parser(action)
                action_parser.add_argument(
                    "resource",
                    choices=("ssh-session", "access-owner", "daemon"),
                )
                name_argument = action_parser.add_argument(
                    "name", metavar="NAME", nargs="?"
                )
                name_argument.completer = _service_name_completer
                if action in {"enable", "disable"}:
                    action_parser.add_argument(
                        "--now",
                        action="store_true",
                        help="also start or stop activation units",
                    )
                if action in {"install", "uninstall"}:
                    action_parser.add_argument(
                        "--desktop",
                        action="store_true",
                        help="also manage this profile's desktop entries",
                    )

    internal_help = "Run a configured service owner (internal use only)"
    internal = subparsers.add_parser(
        "service-run", help=internal_help, description=internal_help
    )
    internal.add_argument("resource", choices=("ssh-session", "access-owner"))
    internal.add_argument("name")
    return parser


def _service_name_completer(prefix, parsed_args, **_):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            config = load_config(
                parsed_args.config, profile=parsed_args.profile
            )
    except (ConfigError, OSError, ValueError):
        return []
    services = (
        config.ssh_sessions
        if parsed_args.resource == "ssh-session"
        else config.access_owners
    )
    return [name for name in services if name.startswith(prefix)]


# Commands run by services, hooks and forced SSH commands. Their INFO
# messages are re-logged by the caller, so they are never hidden.
_LOGGING_COMMANDS = {
    "daemon",
    "service-run",
    "sftp-serve",
    "access-op",
    "drive-sync",
}


def _configure_logging(args) -> None:
    from kp_sync import log

    log.configure(
        verbose=args.verbose,
        debug=args.debug,
        interactive=(
            args.command not in _LOGGING_COMMANDS and _stderr_is_terminal()
        ),
    )


def _note_daemon_debug(args) -> None:
    """Say that --debug does not reach the daemon doing the work."""
    from kp_sync import log

    if not log.debug_enabled():
        return
    alternatives = (
        "use --no-daemon, or debug the daemon"
        if args.command in {"sync", "resolve", "restore"}
        else "debug the daemon"
    )
    log.debug(
        f"{args.command} runs in the daemon, which this --debug does not "
        f"reach; {alternatives} (kp-sync daemon --debug, or "
        "KP_SYNC_DEBUG=1 on its unit; see kp-sync(1))"
    )


def _stderr_is_terminal() -> bool:
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


_nested = threading.local()


def _submit_nested(argv: Sequence[str]) -> int:
    """Run a command submitted by a hook with the hook's logging."""
    _nested.active = True
    try:
        return main(argv)
    finally:
        _nested.active = False


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and execute the selected command.

    Returns the command's exit status. Commands use the daemon unless
    ``--no-daemon`` selects in-process sync.
    """
    parser = _build_parser()
    argcomplete.autocomplete(parser, always_complete_options=False)
    args = parser.parse_args(argv)
    if getattr(_nested, "active", False):
        return _dispatch(parser, args)
    from kp_sync import log

    previous = log.settings()
    _configure_logging(args)
    try:
        return _dispatch(parser, args)
    finally:
        log.restore_settings(previous)


def _dispatch(parser: argparse.ArgumentParser, args) -> int:
    if args.command == "completion":
        from kp_sync.completion import install

        try:
            return install(aliases=args.alias, dry_run=args.dry_run)
        except (OSError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "keepassxc-disconnect":
        try:
            from kp_sync.keepassxc import disconnect

            disconnect(load_config(args.config, profile=args.profile))
            print(
                "Local KeePassXC pairing removed. Revoke its connection "
                "in KeePassXC to remove remote authorization."
            )
            return 0
        except (OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "drive-sync":
        try:
            from kp_sync.drive_sync import drive_sync

            return drive_sync(
                args.event,
                args.mode,
                args.drive_id,
                args.mountpoint,
                profile=args.profile,
                config=args.config,
                submit=_submit_nested,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "drive-setup":
        from kp_sync.drive_setup import DriveSetupError, setup
        from kp_sync.drives import DriveError

        try:
            config = load_config(args.config, profile=args.profile)

            def enrol(identity: str) -> dict:
                run_directory = ensure_run_dir(config.profile)
                return SyncEngine(
                    config,
                    endpoints_from_config(config, run_directory),
                    run_directory=run_directory,
                ).enrol_drive(identity)

            return setup(
                config,
                args.mountpoint,
                args.name,
                rclone=args.rclone,
                dry_run=args.dry_run,
                enrol=enrol,
            )
        except (
            ConfigError,
            DriveSetupError,
            DriveError,
            OSError,
            StateError,
            SyncError,
        ) as exc:
            error(str(exc))
            return 1
    if args.command == "drive-retire":
        from kp_sync.drive_retire import DriveRetireError, retire

        try:
            config = load_config(args.config, profile=args.profile)
            run_directory = ensure_run_dir(config.profile)
            engine = SyncEngine(
                config,
                endpoints_from_config(config, run_directory),
                run_directory=run_directory,
            )
            return retire(
                config,
                engine,
                args.drive,
                confirm=args.confirm,
                keep_device=args.keep_device,
                last_history_drive=args.last_history_drive,
            )
        except (
            ConfigError,
            DriveRetireError,
            OSError,
            StateError,
            SyncError,
            RuntimeError,
            ValueError,
        ) as exc:
            error(str(exc))
            return 1
    daemon_commands = {"run", "up", "attach", "detach", "down", "eject"}
    if (
        args.command in {"daemon", "doctor"} | daemon_commands
        or (args.command == "restore" and not args.no_daemon)
        or (args.command in {"sync", "resolve"} and not args.no_daemon)
    ):
        try:
            config = load_config(args.config, profile=args.profile)
            if args.command == "daemon":
                from kp_sync.daemon.server import run

                run(config)
                return 0
            if args.command == "doctor":
                from kp_sync.integration import doctor

                checks = doctor(config, self_test=args.self_test)
                for check in checks:
                    status = (
                        "WARNING"
                        if check.message.startswith("WARNING:")
                        else "OK" if check.ok else "ERROR"
                    )
                    print(f"{check.name}: {status}: {check.message}")
                return 0 if all(check.ok for check in checks) else 1
            from kp_sync.daemon.client import Client

            idle = args.command == "sync" and getattr(args, "idle", False)
            if idle:
                from kp_sync import idle as idle_sync
                from kp_sync.log import info

                run_directory = ensure_run_dir(config.profile)
                skipped = idle_sync.skip_reason(config, run_directory)
                if skipped is not None:
                    info(skipped)
                    return 0
            fields = {}
            for name in ("mountpoint", "drive_id", "app", "dry_run"):
                value = getattr(args, name, None)
                if value is not None:
                    fields[name] = value
            if args.command == "restore":
                source_kind, source_value = _restore_source(args)
                fields.update(
                    source_kind=source_kind,
                    source_value=source_value,
                    restore_mode=args.restore_mode,
                )
            if args.command in {"run", "up"}:
                app_args = getattr(args, "app_args", [])
                fields["args"] = (
                    app_args[1:] if app_args[:1] == ["--"] else app_args
                )
                fields["interactive"] = bool(
                    sys.stdin.isatty()
                    or getattr(args, "raise_existing", False)
                )
            _note_daemon_debug(args)
            result = Client(config).request(
                args.command,
                wait=not (
                    args.no_wait
                    or args.command in {"up", "attach", "detach", "down"}
                ),
                ui=args.ui or config.ui["mode"],
                **fields,
            )
            _print_result(result, daemon=True)
            if idle:
                idle_sync.record_run(run_directory)
            if result.get("pending"):
                return 3
            if idle:
                return 0  # unreachable copies: retried by the next run
            if result.get("missing") or result.get("action") == "unavailable":
                return 4
            return 0
        except (OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "service":
        try:
            if (args.resource == "daemon") != (args.name is None):
                raise ValueError(
                    "daemon takes no NAME; named services require NAME"
                )
            if (
                args.service_action
                in {"restart", "enable", "disable", "install", "uninstall"}
                and args.resource != "daemon"
            ):
                raise ValueError(
                    f"{args.service_action} is supported only for daemon"
                )
            config = load_config(args.config, profile=args.profile)
            from kp_sync.services import manage_daemon_service, manage_service

            if args.resource == "daemon":
                if args.service_action in {
                    "enable",
                    "disable",
                    "install",
                    "uninstall",
                }:
                    return manage_daemon_service(
                        config,
                        args.service_action,
                        now=getattr(args, "now", False),
                        desktop=getattr(args, "desktop", False),
                    )
                return manage_daemon_service(config, args.service_action)

            return manage_service(
                config,
                args.service_action,
                args.resource,
                args.name,
            )
        except (ConfigError, OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "service-run":
        try:
            config = load_config(args.config, profile=args.profile)
            from kp_sync.services import run_service

            return run_service(config, args.resource, args.name)
        except (ConfigError, OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "history":
        try:
            config = load_config(args.config, profile=args.profile)
            from kp_sync.history_command import history_text

            print(history_text(config), end="")
            return 0
        except (
            ConfigError,
            OSError,
            RuntimeError,
            ValueError,
            StateError,
        ) as exc:
            error(str(exc))
            return 1
    if args.command == "cleanup":
        if args.apply and not (args.store or args.runtime):
            error("--apply requires --store NAME or --runtime")
            return 2
        if args.apply and args.store and not args.confirm_writers_paused:
            error("shared cleanup requires --confirm-writers-paused")
            return 2
        try:
            from kp_sync.cleanup import cleanup

            config = load_config(args.config, profile=args.profile)
            report = cleanup(
                config,
                store_name=args.store,
                runtime_only=args.runtime,
                apply=args.apply,
                writers_paused=args.confirm_writers_paused,
            )
            print(json.dumps(report, sort_keys=True))
            return 1 if report["errors"] else 0
        except (OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "repair-history":
        if args.apply and not args.confirm_writers_paused:
            error("repair-history --apply requires --confirm-writers-paused")
            return 2
        try:
            from kp_sync.repair import repair_history

            config = load_config(args.config, profile=args.profile)
            report = repair_history(
                config,
                apply=args.apply,
                writers_paused=args.confirm_writers_paused,
            )
            print(json.dumps(report, sort_keys=True))
            return 0
        except (OSError, RuntimeError, ValueError, StateError) as exc:
            error(str(exc))
            return 1
    if args.command == "retire-session":
        if args.confirm is not None:
            if not args.identity:
                error("--confirm requires --identity")
                return 2
            if args.confirm != args.identity:
                error("--confirm must exactly match --identity")
                return 2
        try:
            config = load_config(args.config, profile=args.profile)
            run_directory = ensure_run_dir(config.profile)
            engine = SyncEngine(
                config,
                endpoints_from_config(config, run_directory),
                run_directory=run_directory,
            )
            if args.confirm is not None:
                result = engine.retire_session(
                    args.identity, owner_confirmation=True
                )
                print(json.dumps(result, sort_keys=True))
                outcomes = result["shared_stores"]
                if (
                    result["publication_error"]
                    or not outcomes
                    or any(
                        status == "unavailable"
                        or status == "awaiting publication"
                        or status.startswith("failed:")
                        for status in outcomes.values()
                    )
                ):
                    return 3
                return 0
            report = engine.session_retirement_preview(args.identity)
            preview = report["candidates"]
            for name, result in report["shared_stores"].items():
                if result == "unavailable":
                    print(
                        f"Shared store {name}: unavailable; preview may "
                        "omit records from that store."
                    )
            if not preview:
                print(
                    "No matching legacy session records in available history."
                )
            for candidate in preview:
                status = (
                    "requires exact owner confirmation"
                    if candidate["eligible"]
                    else "blocked"
                )
                print(
                    f"{candidate['identity']}: {status}; "
                    f"{candidate['reason']}"
                )
            return 0
        except (
            ConfigError,
            OSError,
            StateError,
            SyncError,
            RuntimeError,
            ValueError,
        ) as exc:
            error(str(exc))
            return 1
    if args.command == "sync" and getattr(args, "idle", False):
        error("--idle runs through the daemon; drop --no-daemon")
        return 2
    if args.command in {"sync", "status", "resolve", "restore"}:
        try:
            config = load_config(args.config, profile=args.profile)
            run_directory = ensure_run_dir(config.profile)
            ui = create_ui(
                args.ui or config.ui["mode"],
                timeout=config.ui["dialog_timeout"],
                gui=config.ui["gui"],
            )
            engine = SyncEngine(
                config,
                (
                    endpoints_from_config(config, run_directory)
                    if args.command != "status"
                    else ()
                ),
                run_directory=run_directory,
                ui=ui,
            )
            result = (
                engine.status()
                if args.command == "status"
                else (
                    engine.restore(
                        *_restore_source(args),
                        mode=args.restore_mode,
                        dry_run=args.dry_run,
                    )
                    if args.command == "restore"
                    else engine.sync(dry_run=getattr(args, "dry_run", False))
                )
            )
            if (
                args.command == "status"
                and (run_directory / "control.sock").exists()
            ):
                from kp_sync.daemon.client import Client, DaemonUnavailable

                try:
                    # Query the socket checked above, not a new path.
                    result_dict = Client(
                        config, directory=run_directory
                    ).request("status", activate=False)
                except DaemonUnavailable:
                    from kp_sync.log import info

                    info("daemon is not running; reporting cached status")
                    result_dict = result.to_dict()
            else:
                result_dict = result.to_dict()
            status_versions = (
                HistoryState.load(run_directory / "state.json").versions
                if args.command == "status" and not args.json
                else ()
            )
        except (
            ConfigError,
            OSError,
            StateError,
            PendingError,
            SyncError,
            TimeoutError,
            RuntimeError,
        ) as exc:
            error(str(exc))
            return 1
        if args.command == "status" and args.json:
            print(json.dumps(result_dict, sort_keys=True))
        elif args.command == "status":
            _print_status(
                result_dict,
                long=args.long,
                versions=status_versions,
            )
        else:
            print(f"{result.action}: {result.current or 'no known version'}")
            for identity, state in sorted(result.classifications.items()):
                print(f"  {identity}: {state}")
            if result.pending:
                print(f"  pending: {result.pending}")
            if result.missing:
                print(f"  unreachable: {', '.join(result.missing)}")
        if args.command == "status":
            return 10 if result_dict["action"] != "unchanged" else 0
        if result.pending:
            return 3
        if result.missing or result.action == "unavailable":
            return 4
        return 0
    if args.command == "sftp-serve":
        from kp_sync.sessions.sftp_serve import serve

        return serve(args.profile, args.session_id, diagnostic=args.diagnostic)
    if args.command == "session":
        try:
            from kp_sync.sessions.session import run_session

            config = load_config(args.config, profile=args.profile)
            return run_session(
                config,
                args.target,
                mode="rw" if args.w else None,
                unconfined=args.unconfined,
                ui=args.ui,
            )
        except (ConfigError, OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "access-op":
        try:
            config = load_config(args.config, profile=args.profile)
            from kp_sync.access_op import run_access_op

            return run_access_op(config)
        except (ConfigError, OSError, RuntimeError, ValueError) as exc:
            error(str(exc))
            return 1
    if args.command == "forget":
        try:
            config = load_config(args.config, profile=args.profile)
            forgotten = CredentialProvider(config).forget_all()
        except (ConfigError, RuntimeError) as exc:
            error(str(exc))
            return 1
        print(
            "cached passwords forgotten" if forgotten else "no cached password"
        )
        return 0
    if args.command is None:
        parser.print_help()
        return 2
    parser.error("not implemented yet")


def _print_result(result, *, daemon=False):
    from kp_sync.log import info

    output = info if daemon else print
    output(
        (
            f"{result.get('action', 'completed')}: "
            f"{result.get('current') or 'no known version'}"
        )
    )
    for identity, classification in sorted(
        result.get("classifications", {}).items()
    ):
        output(f"  {identity}: {classification}")
    if result.get("pending"):
        output(f"  pending: {result['pending']}")
    if result.get("missing"):
        output(f"  unreachable: {', '.join(result['missing'])}")


def _print_status(result, *, long=False, versions=()):
    copies = result.get("copies", {})
    sessions = result.get("sessions", {})
    summary = result.get("summary", {})
    hashes = set(versions)
    hashes.update(
        item["version"]
        for item in (*copies.values(), *sessions.values())
        if item.get("version")
    )
    if result.get("current"):
        hashes.add(result["current"])

    def display(value):
        if long or not value:
            return value
        length = 12
        while length < len(value) and any(
            other != value and other.startswith(value[:length])
            for other in hashes
        ):
            length += 1
        return value[:length]

    persistent = {
        identity: copy
        for identity, copy in copies.items()
        if copy.get("kind") != "session"
    }
    unfinished = sum(
        item.get("lifecycle") in {"live", "unfinished"}
        for item in sessions.values()
    )
    print(
        f"{result.get('action', 'unchanged')}: "
        f"{display(result.get('current')) or 'no known version'}"
    )
    print("Cached history only; endpoints were not probed.")
    pending_decisions = summary.get(
        "pending_decisions", len(result.get("decisions", {}))
    )
    print(
        "Summary: "
        f"{summary.get('recorded_copies', len(copies))} recorded copies, "
        f"{summary.get('unfinished_sessions', unfinished)} unfinished "
        "sessions, "
        f"{pending_decisions} "
        "pending decisions"
    )
    for identity, contact in sorted(result.get("cloud", {}).items()):
        if isinstance(contact, dict) and contact.get("description"):
            print(f"Cloud {identity}: {contact['description']}.")
    if summary.get("hub_unsynced"):
        print(
            "Hub saved since the last sync: its changes are not "
            "synchronized yet."
        )
    print("Persistent copies:")
    if not persistent:
        print("  none")
    for identity, copy in sorted(persistent.items()):
        print(
            f"  {str(copy.get('kind', 'copy')).title()}: "
            f"{copy.get('name', identity)} - "
            f"{copy.get('state', 'unknown')}"
        )
        print(f"    Version: {display(copy.get('version', 'unknown'))}")
        print(f"    Identity: {identity}")
        timestamp = copy.get("last_synchronized_at")
        age = copy.get("synchronized_age")
        print(
            "    Last synchronized: "
            f"{timestamp or 'unknown'} ({age or 'age unknown'})"
        )
    print("Sessions:")
    if not sessions:
        print("  none")
    for identity, session in sorted(sessions.items()):
        print(
            f"  {session.get('lifecycle', 'historical_unknown')}: "
            f"{session.get('name', identity)} - "
            f"{session.get('state', 'unknown')}"
        )
        if session.get("version"):
            print(f"    Version: {display(session['version'])}")
        print(f"    Identity: {identity}")
        if session.get("last_synchronized_at"):
            print(
                "    Last synchronized: "
                f"{session['last_synchronized_at']} "
                f"({session.get('synchronized_age', 'age unknown')})"
            )
    for key, decision in result.get("decisions", {}).items():
        print(f"Decision {key}: {decision['reason']}")
    for name, lease in result.get("leases", {}).items():
        print(f"lease {name}: {lease['kind']}")


def _restore_source(args):
    for kind, value in (
        ("version", args.restore_version),
        ("backup", args.restore_backup),
        ("file", args.restore_file),
    ):
        if value is not None:
            return kind, value
    raise ValueError("restore requires exactly one source")


if __name__ == "__main__":
    raise SystemExit(main())
