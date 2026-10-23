"""
Main synchronization script.
Fetches previous day's consumption from Vandebron and imports into Home Assistant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure src directory is in sys.path when invoked directly
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dotenv import find_dotenv, load_dotenv, set_key

from ha_client import HomeAssistantClient, StatisticMetadata
from vandebron_client import VandebronClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vandebron-sync")


def get_env_file_path() -> str | None:
    """Finds .env file in cwd or project root."""
    found = find_dotenv(usecwd=True)
    if found:
        return found
    candidate = Path(__file__).resolve().parent.parent / ".env"
    if candidate.exists():
        return str(candidate)
    return None


def generate_date_range(start_date: str, end_date: str) -> list[str]:
    """Generates an ordered list of YYYY-MM-DD dates from start_date to end_date inclusive."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    if start_dt > end_dt:
        raise ValueError(f"start_date ({start_date}) cannot be after end_date ({end_date})")

    dates = []
    curr = start_dt
    while curr <= end_dt:
        dates.append(curr.strftime("%Y-%m-%d"))
        curr += timedelta(days=1)
    return dates


def load_settings(config_path: Path | None = None) -> tuple[dict, str | None, Path | None]:
    """
    Loads settings with the following priority:
    1. Environment variables (including .env file).
    2. config.json file.
    Returns (config_dict, env_file_path, config_file_path).
    """
    env_file = get_env_file_path()
    if env_file:
        load_dotenv(env_file, override=True)

    # Check if configured via environment variables
    vdb_token = os.environ.get("VANDEBRON_REFRESH_TOKEN")
    ha_token = os.environ.get("HOMEASSISTANT_ACCESS_TOKEN") or os.environ.get("HASS_ACCESS_TOKEN")

    if vdb_token and ha_token:
        config = {
            "vandebron": {
                "refresh_token": vdb_token,
                "consumer_id": os.environ.get("VANDEBRON_CONSUMER_ID", ""),
                "consumer_uuid": os.environ.get("VANDEBRON_CONSUMER_UUID", ""),
                "gas_connection_id": os.environ.get("VANDEBRON_GAS_CONNECTION_ID", ""),
                "electricity_connection_id": os.environ.get(
                    "VANDEBRON_ELECTRICITY_CONNECTION_ID", ""
                ),
            },
            "homeassistant": {
                "ws_url": os.environ.get("HOMEASSISTANT_WS_URL")
                or os.environ.get("HASS_WS_URL", "ws://homeassistant.local/api/websocket"),
                "access_token": ha_token,
                "gas_statistic_id": os.environ.get(
                    "HOMEASSISTANT_GAS_STATISTIC_ID", "sensor.vandebron_gas_consumption"
                ),
                "electricity_statistic_id": os.environ.get(
                    "HOMEASSISTANT_ELECTRICITY_STATISTIC_ID",
                    "sensor.vandebron_electricity_consumption",
                ),
                "electricity_cost_statistic_id": os.environ.get(
                    "HOMEASSISTANT_ELECTRICITY_COST_STATISTIC_ID",
                    "sensor.vandebron_electricity_cost",
                ),
                "gas_cost_statistic_id": os.environ.get(
                    "HOMEASSISTANT_GAS_COST_STATISTIC_ID",
                    "sensor.vandebron_gas_cost",
                ),
            },
            "rates": {
                "electricity_peak_rate": float(
                    os.environ.get("ELECTRICITY_PEAK_RATE")
                    or os.environ.get("ELECTRICITY_PRICE_EUR_PER_KWH")
                    or 0.29002
                ),
                "electricity_off_peak_rate": float(
                    os.environ.get("ELECTRICITY_OFF_PEAK_RATE")
                    or os.environ.get("ELECTRICITY_PRICE_EUR_PER_KWH")
                    or 0.24246
                ),
                "gas_rate": float(
                    os.environ.get("GAS_RATE")
                    or os.environ.get("GAS_PRICE_EUR_PER_M3")
                    or 1.47196
                ),
            },
        }
        return config, env_file, (config_path if config_path and config_path.exists() else None)

    # Fallback to config.json
    if config_path and config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if "rates" not in cfg:
                cfg["rates"] = {
                    "electricity_peak_rate": 0.29002,
                    "electricity_off_peak_rate": 0.24246,
                    "gas_rate": 1.47196,
                }
            return cfg, env_file, config_path

    logger.error("Configuration not found! Please provide a .env file or config.json.")
    logger.error("See .env.example or config.example.json for required variables.")
    sys.exit(1)


def update_env_file_inplace(env_file_path: str, key: str, value: str) -> None:
    """Updates a key in a .env file in-place without triggering Docker mount point errors."""
    path = Path(env_file_path)
    if not path.exists():
        path.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    key_found = False
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            key_found = True
        else:
            new_lines.append(line)

    if not key_found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"{key}={value}\n")

    # In-place write avoids inode replacement (fixes [Errno 16] Device or resource busy)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def persist_refresh_token(
    new_token: str,
    env_file: str | None,
    config_path: Path | None,
    config_dict: dict,
) -> None:
    """Updates the rotated refresh token in memory, .env, and/or config.json."""
    os.environ["VANDEBRON_REFRESH_TOKEN"] = new_token
    if "vandebron" in config_dict:
        config_dict["vandebron"]["refresh_token"] = new_token

    if env_file and Path(env_file).exists():
        logger.info("Persisting rotated refresh token to %s...", env_file)
        update_env_file_inplace(env_file, "VANDEBRON_REFRESH_TOKEN", new_token)
    if config_path and config_path.exists():
        logger.info("Persisting rotated refresh token to %s...", config_path)
        config_dict["vandebron"]["refresh_token"] = new_token
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, indent=2)


async def sync_single_day(
    vdb_client: VandebronClient,
    ha_client: HomeAssistantClient,
    vdb_cfg: dict,
    ha_cfg: dict,
    rates: dict,
    target_date: str,
    dry_run: bool = False,
) -> None:
    consumer_id = vdb_cfg["consumer_id"]
    consumer_uuid = vdb_cfg.get("consumer_uuid", "")
    gas_conn_id = vdb_cfg.get("gas_connection_id")
    elec_conn_id = vdb_cfg.get("electricity_connection_id")

    logger.info("Syncing consumption data for date: %s", target_date)

    # --- Gas Synchronization ---
    if gas_conn_id:
        logger.info("Fetching Gas usage (Connection ID: %s)...", gas_conn_id)
        try:
            raw_gas = vdb_client.get_usage(
                consumer_id=consumer_id,
                connection_id=gas_conn_id,
                start_date=target_date,
                end_date=target_date,
                resolution="Hours",
            )
            hourly_gas = vdb_client.aggregate_to_hourly(raw_gas)
            total_gas = sum(r.consumption for r in hourly_gas)
            logger.info(
                "Fetched %d hours of Gas usage. Total: %.3f m³", len(hourly_gas), total_gas
            )

            gas_meta = StatisticMetadata(
                statistic_id=ha_cfg.get("gas_statistic_id", "sensor.vandebron_gas_consumption"),
                name="Vandebron Gas Consumption",
                unit_of_measurement="m³",
                unit_class="volume",
            )

            if dry_run:
                payload = ha_client.build_import_payload(1, gas_meta, hourly_gas, starting_sum=0.0)
                logger.info(
                    "[DRY RUN] Gas Consumption Payload that would be sent to Home Assistant:\n%s",
                    json.dumps(payload, indent=2),
                )
            else:
                await ha_client.import_statistics(gas_meta, hourly_gas)

            # --- Gas Cost Synchronization ---
            gas_rate = float(rates.get("gas_rate", 1.47196))
            if gas_rate > 0.0:
                hourly_gas_costs = ha_client.calculate_gas_cost_records(
                    hourly_gas, gas_rate=gas_rate
                )
                total_gas_cost = sum(r.consumption for r in hourly_gas_costs)
                logger.info(
                    "Calculated Gas Cost for %d hours: €%.2f (@ €%.5f/m³)",
                    len(hourly_gas_costs),
                    total_gas_cost,
                    gas_rate,
                )

                gas_cost_meta = StatisticMetadata(
                    statistic_id=ha_cfg.get(
                        "gas_cost_statistic_id", "sensor.vandebron_gas_cost"
                    ),
                    name="Vandebron Gas Cost",
                    unit_of_measurement="EUR",
                    unit_class=None,
                )

                if dry_run:
                    payload = ha_client.build_import_payload(
                        3, gas_cost_meta, hourly_gas_costs, starting_sum=0.0
                    )
                    logger.info(
                        "[DRY RUN] Gas Cost Payload that would be sent to Home Assistant:\n%s",
                        json.dumps(payload, indent=2),
                    )
                else:
                    await ha_client.import_statistics(gas_cost_meta, hourly_gas_costs)

        except Exception as e:
            logger.error("Failed to sync Gas data: %s", e)

    # --- Electricity Synchronization ---
    if elec_conn_id:
        logger.info("Fetching Electricity usage (Connection ID: %s)...", elec_conn_id)
        try:
            raw_elec = vdb_client.get_usage(
                consumer_id=consumer_id,
                connection_id=elec_conn_id,
                start_date=target_date,
                end_date=target_date,
                resolution="Hours",
            )
            hourly_elec = vdb_client.aggregate_to_hourly(raw_elec)
            total_elec = sum(r.consumption for r in hourly_elec)
            logger.info(
                "Fetched %d hours of Electricity usage. Total: %.3f kWh",
                len(hourly_elec),
                total_elec,
            )

            elec_meta = StatisticMetadata(
                statistic_id=ha_cfg.get(
                    "electricity_statistic_id", "sensor.vandebron_electricity_consumption"
                ),
                name="Vandebron Electricity Consumption",
                unit_of_measurement="kWh",
                unit_class="energy",
            )

            if dry_run:
                payload = ha_client.build_import_payload(
                    2, elec_meta, hourly_elec, starting_sum=0.0
                )
                logger.info(
                    "[DRY RUN] Electricity Consumption Payload that would be sent to Home Assistant:\n%s",
                    json.dumps(payload, indent=2),
                )
            else:
                await ha_client.import_statistics(elec_meta, hourly_elec)

            # --- Electricity Cost Synchronization (Dual-Tariff Peak / Off-Peak) ---
            peak_rate = float(rates.get("electricity_peak_rate", 0.29002))
            off_peak_rate = float(rates.get("electricity_off_peak_rate", 0.24246))

            hourly_elec_costs = ha_client.calculate_electricity_cost_records(
                hourly_elec, peak_rate=peak_rate, off_peak_rate=off_peak_rate
            )
            total_elec_cost = sum(r.consumption for r in hourly_elec_costs)
            total_peak_kwh = sum(r.consumption_peak for r in hourly_elec)
            total_off_peak_kwh = sum(r.consumption_off_peak for r in hourly_elec)

            logger.info(
                "Calculated Electricity Cost for %d hours: €%.2f (Peak: %.3f kWh @ €%.5f, Off-Peak: %.3f kWh @ €%.5f)",
                len(hourly_elec_costs),
                total_elec_cost,
                total_peak_kwh,
                peak_rate,
                total_off_peak_kwh,
                off_peak_rate,
            )

            elec_cost_meta = StatisticMetadata(
                statistic_id=ha_cfg.get(
                    "electricity_cost_statistic_id", "sensor.vandebron_electricity_cost"
                ),
                name="Vandebron Electricity Cost",
                unit_of_measurement="EUR",
                unit_class=None,
            )

            if dry_run:
                payload = ha_client.build_import_payload(
                    4, elec_cost_meta, hourly_elec_costs, starting_sum=0.0
                )
                logger.info(
                    "[DRY RUN] Electricity Cost Payload that would be sent to Home Assistant:\n%s",
                    json.dumps(payload, indent=2),
                )
            else:
                await ha_client.import_statistics(elec_cost_meta, hourly_elec_costs)

        except Exception as e:
            logger.error("Failed to sync Electricity data: %s", e)

    # --- Optional Costs Summary ---
    if consumer_uuid and gas_conn_id:
        try:
            costs = vdb_client.get_costs(
                consumer_uuid=consumer_uuid,
                connection_id=gas_conn_id,
                start_date=target_date,
                end_date=target_date,
            )
            fixed = costs.get("totalFixed", 0.0)
            variable = costs.get("totalVariable", 0.0)
            logger.info(
                "Costs for %s -> Fixed: €%.2f, Variable: €%.2f", target_date, fixed, variable
            )
        except Exception as e:
            logger.debug("Could not fetch costs: %s", e)


async def run_sync(
    config_path: Path | None,
    target_dates: list[str] | str,
    dry_run: bool = False,
) -> None:
    if isinstance(target_dates, str):
        dates = [target_dates]
    else:
        dates = sorted(target_dates)

    config, env_file, actual_config_path = load_settings(config_path)

    vdb_cfg = config["vandebron"]
    ha_cfg = config["homeassistant"]
    rates = config.get("rates", {})

    refresh_token = vdb_cfg.get("refresh_token")
    if not refresh_token:
        logger.error("No refresh_token found in configuration!")
        sys.exit(1)

    vdb_client = VandebronClient(refresh_token=refresh_token)

    # 1. Refresh Keycloak access token and persist new refresh token if rotated
    logger.info("Authenticating with Vandebron Keycloak...")
    vdb_client.refresh_access_token()
    if vdb_client.refresh_token != refresh_token:
        persist_refresh_token(vdb_client.refresh_token, env_file, actual_config_path, config)

    ha_client = HomeAssistantClient(
        ws_url=ha_cfg["ws_url"],
        access_token=ha_cfg["access_token"],
    )

    total_days = len(dates)
    for idx, target_date in enumerate(dates, start=1):
        if total_days > 1:
            logger.info("=== [%d/%d] Processing date: %s ===", idx, total_days, target_date)
        try:
            await sync_single_day(
                vdb_client=vdb_client,
                ha_client=ha_client,
                vdb_cfg=vdb_cfg,
                ha_cfg=ha_cfg,
                rates=rates,
                target_date=target_date,
                dry_run=dry_run,
            )
        except Exception as e:
            logger.error("Failed to sync date %s: %s", target_date, e)

    # Persist token if rotated during any calls
    if vdb_client.refresh_token != vdb_cfg.get("refresh_token"):
        persist_refresh_token(vdb_client.refresh_token, env_file, actual_config_path, config)

    logger.info("Sync finished successfully.")


async def token_keepalive_loop(config_path: Path | None, interval_hours: float = 4.0) -> None:
    """Proactively refreshes Keycloak tokens periodically to prevent expiration."""
    interval_seconds = interval_hours * 3600
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            logger.info("Running proactive token keep-alive refresh...")
            config, env_file, actual_config_path = load_settings(config_path)
            refresh_token = config["vandebron"].get("refresh_token")
            if not refresh_token:
                logger.warning("Keep-alive: No refresh token found.")
                continue

            vdb_client = VandebronClient(refresh_token=refresh_token)
            vdb_client.refresh_access_token()
            if vdb_client.refresh_token != refresh_token:
                persist_refresh_token(vdb_client.refresh_token, env_file, actual_config_path, config)
            logger.info("Proactive token keep-alive successful.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Proactive token keep-alive failed: %s", e)


async def run_daemon(
    config_path: Path | None = None,
    run_time: str = "08:30",
    run_on_start: bool = True,
    keepalive_hours: float = 4.0,
) -> None:
    """Runs sync on startup (optional) and continuously every day at run_time, with token keep-alive."""
    keepalive_task = asyncio.create_task(
        token_keepalive_loop(config_path, interval_hours=keepalive_hours)
    )

    try:
        if run_on_start:
            logger.info("Executing initial sync on daemon startup...")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                await run_sync(config_path, target_dates=yesterday)
            except Exception as e:
                logger.error("Initial startup sync failed: %s", e)

        target_hour, target_minute = map(int, run_time.split(":"))
        logger.info(
            "Daemon active. Will run daily at %02d:%02d local time (keep-alive every %.1fh).",
            target_hour,
            target_minute,
            keepalive_hours,
        )

        while True:
            now = datetime.now()
            target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            logger.info(
                "Next sync scheduled at %s (sleeping for %.1f hours)...",
                target.strftime("%Y-%m-%d %H:%M:%S"),
                sleep_seconds / 3600,
            )
            await asyncio.sleep(sleep_seconds)

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            logger.info("Waking up for scheduled sync for date: %s", yesterday)
            try:
                await run_sync(config_path, target_dates=yesterday)
            except Exception as e:
                logger.error("Scheduled sync failed: %s", e)
    finally:
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    root_config = Path(__file__).resolve().parent.parent / "config.json"
    src_config = Path(__file__).resolve().parent / "config.json"
    default_config = (
        root_config if root_config.exists() else (src_config if src_config.exists() else None)
    )
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    parser = argparse.ArgumentParser(
        description="Sync Vandebron energy data to Home Assistant Energy Dashboard"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help="Path to config.json (optional if using .env)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (default: yesterday)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date for backfill range in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date for backfill range in YYYY-MM-DD format (defaults to yesterday if start-date is given)",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=None,
        help="Number of past days to backfill up to yesterday",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print payloads without sending to Home Assistant"
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        default=os.environ.get("DAEMON_MODE", "").lower() in ("true", "1", "yes"),
        help="Run continuously in background, syncing daily at specified time",
    )
    parser.add_argument(
        "--time",
        type=str,
        default=os.environ.get("SYNC_TIME", "08:30"),
        help="Daily execution time for daemon mode in HH:MM (default: 08:30)",
    )
    parser.add_argument(
        "--keepalive-hours",
        type=float,
        default=float(os.environ.get("TOKEN_KEEPALIVE_HOURS", "4.0")),
        help="Interval in hours for proactive Keycloak token refresh in daemon mode (default: 4.0)",
    )
    parser.add_argument(
        "--no-initial-run",
        action="store_true",
        help="Skip immediate sync on daemon start",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.start_date:
        end_date = args.end_date or yesterday
        target_dates = generate_date_range(args.start_date, end_date)
    elif args.backfill_days:
        if args.backfill_days < 1:
            parser.error("--backfill-days must be at least 1")
        start_date = (datetime.now() - timedelta(days=args.backfill_days)).strftime("%Y-%m-%d")
        target_dates = generate_date_range(start_date, yesterday)
    elif args.date:
        target_dates = [args.date]
    else:
        target_dates = [yesterday]

    if args.daemon:
        asyncio.run(
            run_daemon(
                config_path=args.config,
                run_time=args.time,
                run_on_start=not args.no_initial_run,
                keepalive_hours=args.keepalive_hours,
            )
        )
    else:
        asyncio.run(run_sync(config_path=args.config, target_dates=target_dates, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
