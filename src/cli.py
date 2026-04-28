"""CLI entry point.

    python -m src.cli onboard --csv samples/new_hires.csv --dry-run
    python -m src.cli offboard --email alan.turing@example.com --dry-run
    python -m src.cli audit group --name admins
    python -m src.cli audit status --status PROVISIONED

--dry-run is the default on every command that mutates state. You have
to pass --apply to actually hit the API. That's deliberate: it's a lot
easier to type the wrong thing than to un-create 300 Okta users.
"""

from __future__ import annotations

import logging
import sys
from typing import List

import click
from rich.console import Console
from rich.table import Table

from . import logging_setup
from .config import RoleMap, Settings
from .lifecycle import BatchReport, EmployeeResult, Lifecycle
from .okta_client import OktaClient
from .providers import GoogleWorkspaceProvider, SlackProvider
from .providers.base import Employee

console = Console()
log = logging.getLogger("cli")


# ----------------------------------------------------------------------
# shared setup
# ----------------------------------------------------------------------

def _build_lifecycle(role_map_path: str) -> Lifecycle:
    settings = Settings.from_env()
    logging_setup.configure(level=settings.log_level)
    role_map = RoleMap.from_yaml(role_map_path)
    okta = OktaClient(settings.okta_org_url, settings.okta_api_token)
    providers = [
        # SlackProvider runs in real mode when SLACK_BOT_TOKEN is set in .env;
        # otherwise it falls back to mock mode automatically.
        SlackProvider(role_map.slack_channels_for, token=settings.slack_bot_token),
        GoogleWorkspaceProvider(role_map.google_ou_for),
    ]
    return Lifecycle(okta, role_map, providers)


def _render_report(title: str, report: BatchReport) -> None:
    table = Table(title=title, show_lines=False)
    table.add_column("Email", style="cyan", no_wrap=True)
    table.add_column("OK", style="green")
    table.add_column("Actions")
    table.add_column("Error", style="red")
    for r in report.results:
        table.add_row(
            r.email,
            "yes" if r.ok else "no",
            _summarize_actions(r),
            r.error,
        )
    console.print(table)
    console.print(
        f"[bold]{report.ok_count} ok / {report.fail_count} failed[/bold]"
    )


def _summarize_actions(r: EmployeeResult) -> str:
    by_status: dict = {}
    for a in r.actions:
        by_status[a.status] = by_status.get(a.status, 0) + 1
    return ", ".join(f"{k}:{v}" for k, v in sorted(by_status.items())) or "-"


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

@click.group(help="Identity lifecycle orchestrator (Okta + providers).")
@click.option(
    "--role-map",
    default="config/role_mappings.yaml",
    show_default=True,
    help="Path to role mapping YAML.",
)
@click.pass_context
def cli(ctx: click.Context, role_map: str) -> None:
    ctx.ensure_object(dict)
    ctx.obj["role_map_path"] = role_map


@cli.command(help="Onboard new hires from a CSV file.")
@click.option("--csv", "csv_path", required=True, type=click.Path(exists=True))
@click.option(
    "--apply/--dry-run",
    default=False,
    help="Actually call Okta. Default is --dry-run (no mutations).",
)
@click.pass_context
def onboard(ctx: click.Context, csv_path: str, apply: bool) -> None:
    lifecycle = _build_lifecycle(ctx.obj["role_map_path"])
    report = lifecycle.onboard_csv(csv_path, dry_run=not apply)
    _render_report(
        f"Onboard {'(APPLIED)' if apply else '(dry-run)'}", report
    )
    sys.exit(0 if report.fail_count == 0 else 1)


@cli.command(help="Offboard a single user or a CSV of users.")
@click.option("--email", help="Offboard a single user by email.")
@click.option("--csv", "csv_path", type=click.Path(exists=True),
              help="Offboard multiple users from a CSV with an 'email' column.")
@click.option(
    "--apply/--dry-run",
    default=False,
    help="Actually call Okta. Default is --dry-run (no mutations).",
)
@click.pass_context
def offboard(ctx: click.Context, email: str, csv_path: str, apply: bool) -> None:
    if not email and not csv_path:
        raise click.UsageError("Pass --email or --csv.")
    lifecycle = _build_lifecycle(ctx.obj["role_map_path"])
    if csv_path:
        report = lifecycle.offboard_csv(csv_path, dry_run=not apply)
    else:
        report = BatchReport(results=[lifecycle.offboard(email, dry_run=not apply)])
    _render_report(
        f"Offboard {'(APPLIED)' if apply else '(dry-run)'}", report
    )
    sys.exit(0 if report.fail_count == 0 else 1)


@cli.group(help="Read-only audit commands.")
def audit() -> None:
    ...


@audit.command("group", help="List members of an Okta group.")
@click.option("--name", "group_name", required=True)
@click.pass_context
def audit_group(ctx: click.Context, group_name: str) -> None:
    lifecycle = _build_lifecycle(ctx.obj["role_map_path"])
    users = lifecycle.audit_by_group(group_name)
    _user_table(f"Group: {group_name}", users)


@audit.command("status", help="List users with a given Okta status.")
@click.option("--status", default="PROVISIONED", show_default=True,
              help="Okta user status, e.g. STAGED, PROVISIONED, ACTIVE, SUSPENDED, DEPROVISIONED.")
@click.pass_context
def audit_status(ctx: click.Context, status: str) -> None:
    lifecycle = _build_lifecycle(ctx.obj["role_map_path"])
    users = lifecycle.audit_status(status)
    _user_table(f"Status: {status}", users)


def _user_table(title: str, users: List) -> None:
    table = Table(title=title)
    table.add_column("Email", style="cyan")
    table.add_column("Name")
    table.add_column("Status")
    for u in users:
        table.add_row(u.email, f"{u.first_name} {u.last_name}", u.status)
    console.print(table)
    console.print(f"[bold]{len(users)} user(s)[/bold]")


def main() -> None:
    """CLI entry point with friendly error handling for common misconfigurations."""
    try:
        cli(standalone_mode=False)
    except click.exceptions.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
    except click.exceptions.Abort:
        console.print("[yellow]aborted[/yellow]")
        sys.exit(130)
    except RuntimeError as e:
        # e.g. missing env vars, unknown role/group — user-facing errors
        console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
