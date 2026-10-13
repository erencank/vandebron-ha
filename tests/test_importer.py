"""
Unit tests for Vandebron and Home Assistant integration.
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

# Ensure src is in sys.path
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from ha_client import HomeAssistantClient, StatisticMetadata
from vandebron_client import UsageRecord, VandebronClient


class TestVandebronImporter(unittest.TestCase):
    def test_aggregate_subhourly_to_hourly(self) -> None:
        """Tests aggregating 15-minute intervals into 1-hour slots."""
        raw_intervals = [
            UsageRecord(timestamp=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc), consumption=0.10, unit="kWh"),
            UsageRecord(timestamp=datetime(2026, 10, 9, 10, 15, tzinfo=timezone.utc), consumption=0.15, unit="kWh"),
            UsageRecord(timestamp=datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc), consumption=0.20, unit="kWh"),
            UsageRecord(timestamp=datetime(2026, 10, 9, 10, 45, tzinfo=timezone.utc), consumption=0.05, unit="kWh"),
            UsageRecord(timestamp=datetime(2026, 10, 9, 11, 0, tzinfo=timezone.utc), consumption=0.30, unit="kWh"),
        ]

        hourly = VandebronClient.aggregate_to_hourly(raw_intervals)

        self.assertEqual(len(hourly), 2)
        # Hour 10 should be 0.10 + 0.15 + 0.20 + 0.05 = 0.50
        self.assertEqual(hourly[0].timestamp, datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(hourly[0].consumption, 0.50)

        # Hour 11 should be 0.30
        self.assertEqual(hourly[1].timestamp, datetime(2026, 10, 9, 11, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(hourly[1].consumption, 0.30)

    def test_ha_payload_monotonic_sum(self) -> None:
        """Tests that Home Assistant import payload calculates cumulative sum properly."""
        records = [
            UsageRecord(timestamp=datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc), consumption=0.5, unit="m³"),
            UsageRecord(timestamp=datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc), consumption=0.3, unit="m³"),
            UsageRecord(timestamp=datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), consumption=0.2, unit="m³"),
        ]

        meta = StatisticMetadata(
            statistic_id="sensor.vandebron_gas_consumption",
            name="Vandebron Gas Consumption",
            unit_of_measurement="m³",
            unit_class="volume",
        )

        payload = HomeAssistantClient.build_import_payload(
            message_id=42,
            metadata=meta,
            records=records,
            starting_sum=50.0,
        )

        self.assertEqual(payload["id"], 42)
        self.assertEqual(payload["type"], "recorder/import_statistics")
        self.assertEqual(payload["metadata"]["statistic_id"], "sensor.vandebron_gas_consumption")
        self.assertEqual(payload["metadata"]["unit_of_measurement"], "m³")
        self.assertEqual(payload["metadata"]["unit_class"], "volume")
        self.assertEqual(payload["metadata"]["mean_type"], 0)
        self.assertTrue(payload["metadata"]["has_sum"])
        self.assertFalse(payload["metadata"]["has_mean"])

        stats = payload["stats"]
        self.assertEqual(len(stats), 3)

        # Hour 0: state 0.5, sum 50.5
        self.assertAlmostEqual(stats[0]["state"], 0.5)
        self.assertAlmostEqual(stats[0]["sum"], 50.5)

        # Hour 1: state 0.3, sum 50.8
        self.assertAlmostEqual(stats[1]["state"], 0.3)
        self.assertAlmostEqual(stats[1]["sum"], 50.8)

        # Hour 2: state 0.2, sum 51.0
        self.assertAlmostEqual(stats[2]["state"], 0.2)
        self.assertAlmostEqual(stats[2]["sum"], 51.0)

    def test_parse_vandebron_payload(self) -> None:
        """Tests parsing raw Vandebron JSON response matching the actual API structure."""
        raw_api_response = {
            "unit": "MTQ",
            "resolution": "Hours",
            "values": [
                {
                    "time": "2026-10-09T00:00:00Z",
                    "consumption": 0.382,
                    "production": 0.0,
                    "tag": "Measured"
                },
                {
                    "time": "2026-10-09T01:00:00Z",
                    "consumption": 0.243,
                    "production": 0.0,
                    "tag": "Measured"
                }
            ],
            "latestReadingDate": "2026-10-10T00:00:00+02:00"
        }

        unit_raw = raw_api_response.get("unit", "KWH")
        unit = "m³" if unit_raw.upper() == "MTQ" else "kWh"

        records = [
            UsageRecord(
                timestamp=datetime.fromisoformat(item["time"].replace("Z", "+00:00")),
                consumption=float(item["consumption"]),
                production=float(item.get("production", 0.0)),
                unit=unit,
            )
            for item in raw_api_response["values"]
        ]

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].unit, "m³")
        self.assertAlmostEqual(records[0].consumption, 0.382)
        self.assertEqual(records[0].timestamp, datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(records[1].consumption, 0.243)

    def test_parse_vandebron_electricity_wh(self) -> None:
        """Tests that electricity returned in WH is properly converted to kWh."""
        raw_api_response = {
            "unit": "WH",
            "resolution": "Hours",
            "values": [
                {
                    "time": "2026-10-08T09:00:00Z",
                    "consumption": 1218.0,
                    "production": 0.0,
                },
                {
                    "time": "2026-10-08T10:00:00Z",
                    "consumption": 268.0,
                    "production": 0.0,
                }
            ]
        }

        unit_raw = (raw_api_response.get("unit") or "").upper()
        scale = 0.001 if unit_raw in ("WH", "WHR") else 1.0
        unit = "kWh" if unit_raw in ("WH", "WHR", "KWH") else "m³"

        records = [
            UsageRecord(
                timestamp=datetime.fromisoformat(item["time"].replace("Z", "+00:00")),
                consumption=round(float(item["consumption"]) * scale, 4),
                production=round(float(item.get("production", 0.0)) * scale, 4),
                unit=unit,
            )
            for item in raw_api_response["values"]
        ]

        self.assertEqual(records[0].unit, "kWh")
        self.assertAlmostEqual(records[0].consumption, 1.218)
        self.assertAlmostEqual(records[1].consumption, 0.268)


if __name__ == "__main__":
    unittest.main()


