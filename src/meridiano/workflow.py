"""Run the scheduled Meridiano processing workflow."""

import argparse
import importlib
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from meridiano import config_base as config
from meridiano import database
from meridiano.run_briefing import generate_brief, process_articles, rate_articles, scrape_articles

load_dotenv()


def load_feed_config(feed_profile: str):
    """Load a feed profile and merge it with the base configuration."""
    try:
        feed_config = importlib.import_module(f".feeds.{feed_profile}", package="meridiano")
    except ImportError as error:
        raise RuntimeError(
            f"Could not import feed configuration for '{feed_profile}'. "
            f"Expected src/meridiano/feeds/{feed_profile}.py."
        ) from error

    settings = {key: value for key, value in vars(config).items() if not key.startswith("__")}
    settings.update({key: value for key, value in vars(feed_config).items() if not key.startswith("__")})
    return SimpleNamespace(**settings)


def sync_database() -> None:
    """Upload the database and atomically replace the homelab copy."""
    database_path = Path(os.getenv("DATABASE_FILE", config.DATABASE_FILE))
    target = os.getenv("HOMELAB_SSH_TARGET")
    remote_path = os.getenv("HOMELAB_DB_PATH")
    service = os.getenv("HOMELAB_SERVICE")

    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")
    if not target or not remote_path or not service:
        raise RuntimeError(
            "Database sync requires HOMELAB_SSH_TARGET, HOMELAB_DB_PATH, and HOMELAB_SERVICE."
        )

    remote_path = remote_path.rstrip("/")
    temporary_path = f"{remote_path}.next"
    ssh_options = shlex.split(os.getenv("HOMELAB_SSH_OPTS", ""))
    rsync_target = f"{target}:{temporary_path}"

    print(f"Syncing {database_path} to {target}:{remote_path}")
    rsync_ssh_command = "ssh"
    if ssh_options:
        rsync_ssh_command += " " + " ".join(shlex.quote(option) for option in ssh_options)
    subprocess.run(
        ["rsync", "--partial", "-e", rsync_ssh_command, str(database_path), rsync_target],
        check=True,
    )

    remote_command = (
        f"sudo systemctl stop {shlex.quote(service)} && "
        f"sudo mv {shlex.quote(temporary_path)} {shlex.quote(remote_path)} && "
        f"sudo systemctl start {shlex.quote(service)}"
    )
    try:
        subprocess.run(["ssh", *ssh_options, target, remote_command], check=True)
    except subprocess.CalledProcessError:
        print(f"Database replacement failed. The uploaded file remains at {temporary_path}.")
        raise

    print("Database sync completed.")


def run_workflow(feed_profile: str, limit: int, generate: bool, sync: bool) -> None:
    """Run all processing stages and optionally publish the database."""
    effective_config = load_feed_config(feed_profile)
    rss_feeds = getattr(effective_config, "RSS_FEEDS", [])
    if not rss_feeds:
        raise RuntimeError(f"Feed profile '{feed_profile}' has no RSS_FEEDS.")

    database.init_db()
    scrape_articles(feed_profile, rss_feeds)
    process_articles(feed_profile, effective_config, limit=limit)
    rate_articles(feed_profile, effective_config, limit=limit)

    if generate:
        generate_brief(feed_profile, effective_config)
    if sync:
        sync_database()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Meridiano's scheduled news workflow.")
    parser.add_argument("--feed", default=config.DEFAULT_FEED_PROFILE, help="Feed profile to process.")
    parser.add_argument("--limit", "-n", type=int, default=1000, help="Maximum articles to process and rate.")
    parser.add_argument(
        "--generate-brief",
        action="store_true",
        help="Generate a briefing after processing and rating articles.",
    )
    parser.add_argument("--sync", action="store_true", help="Publish the SQLite database to the homelab.")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be greater than zero")

    run_workflow(args.feed, args.limit, args.generate_brief, args.sync)


if __name__ == "__main__":
    main()
