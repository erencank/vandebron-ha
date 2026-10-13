"""
Home Assistant WebSocket Client.
Interacts with Home Assistant's WebSocket API to query previous cumulative sums
and import long-term statistics into the recorder database.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

import websockets

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from vandebron_client import UsageRecord

logger = logging.getLogger(__name__)


class HomeAssistantError(Exception):
    """Raised when an operation against Home Assistant fails."""


@dataclass
class StatisticMetadata:
    statistic_id: str
    name: str
    unit_of_measurement: str
    unit_class: str | None = None
    has_mean: bool = False
    has_sum: bool = True
    mean_type: int = 0
    source: str = "recorder"



class HomeAssistantClient:
    def __init__(self, ws_url: str, access_token: str) -> None:
        self.ws_url = ws_url
        self.access_token = access_token
        self._message_id = 1

    def _next_id(self) -> int:
        msg_id = self._message_id
        self._message_id += 1
        return msg_id

    async def _authenticate(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Performs Home Assistant WebSocket authentication handshake."""
        auth_req = json.loads(await ws.recv())
        if auth_req.get("type") != "auth_required":
            raise HomeAssistantError(f"Unexpected initial message: {auth_req}")

        await ws.send(json.dumps({"type": "auth", "access_token": self.access_token}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise HomeAssistantError(
                f"Authentication failed: {auth_resp.get('message', 'Invalid token')}"
            )

    async def get_latest_statistic_sum(
        self,
        statistic_id: str,
        before_time: datetime | None = None,
    ) -> float:
        """
        Retrieves the cumulative sum for the given statistic_id immediately before before_time.
        If before_time is None, retrieves the latest sum across all periods.
        Returns 0.0 if no statistics exist prior to before_time.
        """
        async with websockets.connect(self.ws_url) as ws:
            await self._authenticate(ws)

            # Query statistics for the 30 days prior to before_time
            req_id = self._next_id()
            ref_time = before_time if before_time is not None else datetime.now(timezone.utc)
            start_time = (ref_time - timedelta(days=30)).isoformat()
            payload: dict[str, Any] = {
                "id": req_id,
                "type": "recorder/statistics_during_period",
                "start_time": start_time,
                "statistic_ids": [statistic_id],
                "period": "hour",
            }
            if before_time is not None:
                payload["end_time"] = before_time.isoformat()

            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())

            if not response.get("success"):
                logger.warning(
                    "Could not query existing statistics for %s: %s", statistic_id, response
                )
                return 0.0

            stats_dict = response.get("result", {})
            series = stats_dict.get(statistic_id, [])
            if not series:
                return 0.0

            if before_time is not None:
                before_ts_ms = int(before_time.timestamp() * 1000)
                valid_entries = [e for e in series if e.get("start", 0) < before_ts_ms]
                if not valid_entries:
                    return 0.0
                return float(valid_entries[-1].get("sum", 0.0) or 0.0)

            last_entry = series[-1]
            return float(last_entry.get("sum", 0.0) or 0.0)


    @staticmethod
    def build_import_payload(
        message_id: int,
        metadata: StatisticMetadata,
        records: list[UsageRecord],
        starting_sum: float = 0.0,
    ) -> dict[str, Any]:
        """
        Converts hourly usage records into Home Assistant's recorder/import_statistics payload.
        Computes cumulative sum monotonically for each hour.
        """
        stats: list[dict[str, Any]] = []
        running_sum = starting_sum

        for rec in records:
            running_sum += rec.consumption
            stats.append(
                {
                    "start": rec.timestamp.isoformat(),
                    "state": round(rec.consumption, 4),
                    "sum": round(running_sum, 4),
                }
            )

        return {
            "id": message_id,
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": metadata.has_mean,
                "has_sum": metadata.has_sum,
                "name": metadata.name,
                "source": metadata.source,
                "statistic_id": metadata.statistic_id,
                "unit_of_measurement": metadata.unit_of_measurement,
                "unit_class": metadata.unit_class,
                "mean_type": metadata.mean_type,
            },
            "stats": stats,
        }

    @staticmethod
    def calculate_electricity_cost_records(
        records: list[UsageRecord],
        peak_rate: float,
        off_peak_rate: float,
    ) -> list[UsageRecord]:
        """
        Calculates hourly monetary cost records for electricity using dual peak / off-peak rates.
        """
        cost_records: list[UsageRecord] = []
        for rec in records:
            hourly_cost = round(
                (rec.consumption_peak * peak_rate) + (rec.consumption_off_peak * off_peak_rate),
                4,
            )
            cost_records.append(
                UsageRecord(
                    timestamp=rec.timestamp,
                    consumption=hourly_cost,
                    unit="EUR",
                )
            )
        return cost_records

    @staticmethod
    def calculate_gas_cost_records(
        records: list[UsageRecord],
        gas_rate: float,
    ) -> list[UsageRecord]:
        """
        Calculates hourly monetary cost records for gas using a fixed rate per m³.
        """
        cost_records: list[UsageRecord] = []
        for rec in records:
            hourly_cost = round(rec.consumption * gas_rate, 4)
            cost_records.append(
                UsageRecord(
                    timestamp=rec.timestamp,
                    consumption=hourly_cost,
                    unit="EUR",
                )
            )
        return cost_records

    async def import_statistics(
        self,
        metadata: StatisticMetadata,
        records: list[UsageRecord],
        starting_sum: float | None = None,
    ) -> None:
        """Imports hourly usage records into Home Assistant's long-term statistics database."""
        if not records:
            logger.info("No records to import for %s.", metadata.statistic_id)
            return

        async with websockets.connect(self.ws_url) as ws:
            await self._authenticate(ws)

            if starting_sum is None:
                # Query HA for existing sum immediately prior to the first record
                starting_sum = await self.get_latest_statistic_sum(
                    metadata.statistic_id,
                    before_time=records[0].timestamp,
                )

            req_id = self._next_id()
            payload = self.build_import_payload(req_id, metadata, records, starting_sum)

            await ws.send(json.dumps(payload))
            response = json.loads(await ws.recv())

            if not response.get("success"):
                raise HomeAssistantError(
                    f"Failed to import statistics for {metadata.statistic_id}: {response.get('error')}"
                )

            logger.info(
                "Successfully imported %d hours of statistics for %s into Home Assistant.",
                len(records),
                metadata.statistic_id,
            )
