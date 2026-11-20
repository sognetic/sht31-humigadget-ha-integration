"""DataUpdateCoordinator for Sensirion SHT31 Smart Gadget. """
from __future__ import annotations

import asyncio
import logging
import math
import struct
from datetime import timedelta
from typing import Any

from bleak import BleakClient, BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BATT_CHAR_UUID,
    DEFAULT_SCAN_INTERVAL,
    HUM_CHAR_UUID,
    TEMP_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)

# How many connection attempts establish_connection() should make. On top of
# this, bleak-retry-connector retries transient errors (out of connection
# slots, device not currently advertising, ...) with its own backoff.
MAX_CONNECT_ATTEMPTS = 4


def _calculate_derived_values(temperature: float, humidity: float) -> dict[str, float | None]:
    """Calculate humidity signals using Sensirion's published formulas."""
    if not math.isfinite(temperature) or not math.isfinite(humidity):
        return {"dew_point": None, "absolute_humidity": None, "heat_index": None}

    # Sensirion's formulas use RH clamped to the physical range 0-100%.
    humidity = min(max(humidity, 0.0), 100.0)
    derived: dict[str, float | None] = {
        "dew_point": None,
        "absolute_humidity": None,
        "heat_index": None,
    }

    if humidity > 0 and temperature > -243.12:
        alpha = math.log(humidity / 100.0) + (17.62 * temperature) / (243.12 + temperature)
        denominator = 17.62 - alpha
        if denominator != 0:
            derived["dew_point"] = round(243.12 * alpha / denominator, 2)

    if temperature > -243.12:
        absolute_humidity = 216.7 * (
            humidity / 100.0
            * 6.112
            * math.exp(17.62 * temperature / (243.12 + temperature))
            / (273.15 + temperature)
        )
        derived["absolute_humidity"] = round(absolute_humidity, 2)

    # Sensirion's heat-index method uses the simple result below at lower
    # temperatures and the NOAA regression, including its humidity adjustments,
    # when the averaged estimate reaches 26.7 °C.
    heat_index = 1.1 * temperature + 5 * (0.047 * humidity - 7.1) / 9
    if (heat_index + temperature) / 2 >= 26.7:
        heat_index = (
            -8.78469475556
            + 1.61139411 * temperature
            + 2.33854883889 * humidity
            - 0.14611605 * temperature * humidity
            - 0.012308094 * temperature**2
            - 0.0164248277778 * humidity**2
            + 0.002211732 * temperature**2 * humidity
            + 0.00072546 * temperature * humidity**2
            - 0.000003582 * temperature**2 * humidity**2
        )
        if humidity < 13 and 26.7 < temperature < 44.4:
            heat_index += (5 / 36) * (humidity - 13) * math.sqrt(
                (17 - abs(1.8 * temperature - 63)) / 17
            ) - 160 / 9
        elif humidity > 85 and 26.7 < temperature < 30.6:
            heat_index += 5 * (humidity - 85) * (55 - 1.8 * temperature) / 450 - 160 / 9

    derived["heat_index"] = round(heat_index, 2)
    return derived


class SHT31Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage polling data from Sensirion SHT31 Smart Gadget."""

    def __init__(self, hass: HomeAssistant, address: str, name: str, scan_interval: int = DEFAULT_SCAN_INTERVAL) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"SHT31 {name} ({address})",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.address = address
        self.device_name = name

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the SHT31 gadget via BLE GATT connection."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

        if not ble_device:
            raise UpdateFailed(
                f"Bluetooth device {self.address} ({self.device_name}) not found or not connectable in range"
            )

        _LOGGER.debug("Connecting to Sensirion SHT31 at %s", self.address)

        try:
            # establish_connection() resolves the best available scanner/backend
            # for every attempt, retries transient failures with a backoff and
            # integrates with Home Assistant's Bluetooth connection slot
            # management. Calling BleakClient.connect() directly bypasses all
            # of that and fails on the first error.
            client = await establish_connection(
                BleakClient,
                ble_device,
                self.address,
                max_attempts=MAX_CONNECT_ATTEMPTS,
            )
        except (BleakError, asyncio.TimeoutError) as err:
            raise UpdateFailed(
                f"Error communicating with Sensirion SHT31 ({self.address}): {err}"
            ) from err

        try:
            return await self._async_read_data(client)
        except Exception as err:  # noqa: BLE001 - surface any read error as UpdateFailed
            raise UpdateFailed(
                f"Error communicating with Sensirion SHT31 ({self.address}): {err}"
            ) from err
        finally:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001 - never mask the read result
                _LOGGER.debug(
                    "Error disconnecting from Sensirion SHT31 at %s: %s",
                    self.address,
                    err,
                )

    async def _async_read_data(self, client: BleakClient) -> dict[str, Any]:
        """Read the sensor values from an established GATT connection."""
        # Read temperature (IEEE-754 32-bit float, little-endian)
        raw_temp = await client.read_gatt_char(TEMP_CHAR_UUID)
        temperature = round(struct.unpack("<f", raw_temp)[0], 2)

        # Read humidity (IEEE-754 32-bit float, little-endian)
        raw_hum = await client.read_gatt_char(HUM_CHAR_UUID)
        humidity = round(struct.unpack("<f", raw_hum)[0], 2)

        derived_values = _calculate_derived_values(temperature, humidity)

        # Read battery level (uint8, 0-100%)
        battery: int | None = None
        try:
            raw_bat = await client.read_gatt_char(BATT_CHAR_UUID)
            if raw_bat:
                battery = int(raw_bat[0])
        except Exception as bat_err:  # noqa: BLE001 - battery is optional
            _LOGGER.debug(
                "Could not read battery level from %s: %s", self.address, bat_err
            )

        _LOGGER.debug(
            "SHT31 %s read success -> Temp: %s°C, Hum: %s%%, Bat: %s%%",
            self.address,
            temperature,
            humidity,
            battery,
        )

        return {
            "temperature": temperature,
            "humidity": humidity,
            "battery": battery,
            **derived_values,
        }
