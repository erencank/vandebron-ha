"""
Vandebron API client.
Handles Keycloak OpenID Connect authentication, token refresh,
usage retrieval, and hourly aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests


def is_off_peak_time(dt: datetime) -> bool:
    """
    Determines whether a timestamp falls in Dutch off-peak (dal) hours.
    Off-peak is:
    - Weekdays between 23:00 and 07:00 (i.e. hour >= 23 or hour < 7)
    - Weekends: all 48 hours (Saturday=5, Sunday=6)
    Evaluated in Europe/Amsterdam timezone.
    """
    amsterdam_tz = ZoneInfo("Europe/Amsterdam")
    local_dt = (
        dt.astimezone(amsterdam_tz)
        if dt.tzinfo
        else dt.replace(tzinfo=timezone.utc).astimezone(amsterdam_tz)
    )
    if local_dt.weekday() in (5, 6):
        return True
    return local_dt.hour < 7 or local_dt.hour >= 23


@dataclass
class UsageRecord:
    timestamp: datetime
    consumption: float
    production: float = 0.0
    unit: str = "kWh"
    consumption_peak: float = 0.0
    consumption_off_peak: float = 0.0
    production_peak: float = 0.0
    production_off_peak: float = 0.0


class VandebronClient:
    BASE_URL = "https://mijn.vandebron.nl/api"
    AUTH_URL = "https://vandebron.nl/auth/realms/vandebron/protocol/openid-connect/token"
    CLIENT_ID = "website"

    def __init__(self, refresh_token: str, access_token: str | None = None) -> None:
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:157.0) Gecko/20100101 Firefox/157.0",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def refresh_access_token(self) -> str:
        """
        Exchanges the refresh token for a fresh Keycloak access token.
        Updates both access_token and refresh_token (if rotated).
        """
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.CLIENT_ID,
            "refresh_token": self.refresh_token,
            "scope": "openid",
        }
        try:
            response = self.session.post(self.AUTH_URL, data=payload, timeout=30)
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            err_details = response.text if response is not None else ""
            raise RuntimeError(
                f"Vandebron Keycloak authentication failed (HTTP {response.status_code}): {err_details}"
            ) from exc

        data = response.json()
        self.access_token = data["access_token"]
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]

        return self.access_token

    def _ensure_access_token(self) -> str:
        if not self.access_token:
            return self.refresh_access_token()
        return self.access_token

    def _auth_headers(self) -> dict[str, str]:
        token = self._ensure_access_token()
        return {"Authorization": f"Bearer {token}"}

    def get_usage(
        self,
        consumer_id: str,
        connection_id: str,
        start_date: str,
        end_date: str,
        resolution: str = "Hours",
    ) -> list[UsageRecord]:
        """
        Fetches usage intervals from Vandebron.
        Resolutions typically supported: "Hours", "Days", "Months".
        Unit returned is mapped: "MTQ" -> "m³", "KWH" -> "kWh".
        """
        url = f"{self.BASE_URL}/consumers/{consumer_id}/connections/{connection_id}/usage"
        params = {
            "resolution": resolution,
            "startDate": start_date,
            "endDate": end_date,
            "ff": "true",
        }

        response = self.session.get(url, params=params, headers=self._auth_headers(), timeout=30)
        if response.status_code == 401:
            self.refresh_access_token()
            response = self.session.get(
                url, params=params, headers=self._auth_headers(), timeout=30
            )

        response.raise_for_status()
        data = response.json()

        unit_raw = (data.get("unit") or "KWH").upper()
        if unit_raw == "MTQ":
            unit = "m³"
            scale = 1.0
        elif unit_raw in ("WH", "WHR"):
            unit = "kWh"
            scale = 0.001
        elif unit_raw == "KWH":
            unit = "kWh"
            scale = 1.0
        else:
            unit = unit_raw
            scale = 1.0

        records: list[UsageRecord] = []
        for item in data.get("values", []):
            time_str = item["time"]
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            consumption = round(float(item.get("consumption", 0.0) or 0.0) * scale, 4)
            production = round(float(item.get("production", 0.0) or 0.0) * scale, 4)
            consumption_peak = round(float(item.get("consumptionPeak", 0.0) or 0.0) * scale, 4)
            consumption_off_peak = round(
                float(item.get("consumptionOffPeak", 0.0) or 0.0) * scale, 4
            )
            production_peak = round(float(item.get("productionPeak", 0.0) or 0.0) * scale, 4)
            production_off_peak = round(
                float(item.get("productionOffPeak", 0.0) or 0.0) * scale, 4
            )

            # Fallback if API did not differentiate peak/off-peak but consumption > 0 (for electricity)
            if (
                (consumption_peak + consumption_off_peak) == 0.0
                and consumption > 0.0
                and unit == "kWh"
            ):
                if is_off_peak_time(dt):
                    consumption_off_peak = consumption
                else:
                    consumption_peak = consumption

            records.append(
                UsageRecord(
                    timestamp=dt,
                    consumption=consumption,
                    production=production,
                    unit=unit,
                    consumption_peak=consumption_peak,
                    consumption_off_peak=consumption_off_peak,
                    production_peak=production_peak,
                    production_off_peak=production_off_peak,
                )
            )

        return records

    def get_costs(
        self,
        consumer_uuid: str,
        connection_id: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """Fetches costs for the given connection ID."""
        url = f"{self.BASE_URL}/v2/consumers/{consumer_uuid}/connections/costs"
        params = {
            "connectionIds": connection_id,
            "startDate": start_date,
            "endDate": end_date,
        }

        response = self.session.get(url, params=params, headers=self._auth_headers(), timeout=30)
        if response.status_code == 401:
            self.refresh_access_token()
            response = self.session.get(
                url, params=params, headers=self._auth_headers(), timeout=30
            )

        response.raise_for_status()
        return response.json()

    @staticmethod
    def aggregate_to_hourly(records: list[UsageRecord]) -> list[UsageRecord]:
        """
        Aggregates sub-hourly intervals (e.g. 15-minute intervals) into 1-hour slots.
        Each resulting record's timestamp is rounded to the top of the hour.
        If records are already 1-hour slots, returns them sorted.
        """
        if not records:
            return []

        unit = records[0].unit
        hourly_map: dict[datetime, dict[str, float]] = {}

        for rec in records:
            # Round down to the hour (minute=0, second=0, microsecond=0)
            hour_dt = rec.timestamp.replace(minute=0, second=0, microsecond=0)
            if hour_dt not in hourly_map:
                hourly_map[hour_dt] = {
                    "consumption": 0.0,
                    "production": 0.0,
                    "consumption_peak": 0.0,
                    "consumption_off_peak": 0.0,
                    "production_peak": 0.0,
                    "production_off_peak": 0.0,
                }
            hourly_map[hour_dt]["consumption"] += rec.consumption
            hourly_map[hour_dt]["production"] += rec.production
            hourly_map[hour_dt]["consumption_peak"] += rec.consumption_peak
            hourly_map[hour_dt]["consumption_off_peak"] += rec.consumption_off_peak
            hourly_map[hour_dt]["production_peak"] += rec.production_peak
            hourly_map[hour_dt]["production_off_peak"] += rec.production_off_peak

        sorted_hours = sorted(hourly_map.keys())
        return [
            UsageRecord(
                timestamp=h,
                consumption=round(hourly_map[h]["consumption"], 4),
                production=round(hourly_map[h]["production"], 4),
                unit=unit,
                consumption_peak=round(hourly_map[h]["consumption_peak"], 4),
                consumption_off_peak=round(hourly_map[h]["consumption_off_peak"], 4),
                production_peak=round(hourly_map[h]["production_peak"], 4),
                production_off_peak=round(hourly_map[h]["production_off_peak"], 4),
            )
            for h in sorted_hours
        ]

