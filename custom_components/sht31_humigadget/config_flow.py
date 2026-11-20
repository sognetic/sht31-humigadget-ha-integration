"""Config flow for Sensirion SHT31 Smart Gadget integration."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_MAC,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

MAC_REGEX = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def _normalize_mac(mac: str) -> str | None:
    """Normalize a user supplied MAC address to the format used by HA."""
    normalized = format_mac(mac.strip()).upper()
    return normalized if MAC_REGEX.match(normalized) else None


def _clean_name(name: str | None, fallback: str) -> str:
    """Return a non-empty, trimmed device name."""
    return (name or "").strip() or fallback


class SHT31OptionsFlow(OptionsFlow):
    """Handle options for the Sensirion SHT31 Smart Gadget."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the integration options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )


class SHT31ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sensirion SHT31 Smart Gadget."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow."""
        self._discovered_info: bluetooth.BluetoothServiceInfoBleak | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SHT31OptionsFlow:
        """Return the options flow."""
        return SHT31OptionsFlow(config_entry)

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle bluetooth discovery."""
        # Use the address exactly as Home Assistant reports it. On macOS this
        # is a CoreBluetooth UUID rather than a MAC address, so do not validate
        # or reformat it here.
        formatted_address = discovery_info.address.upper()
        await self.async_set_unique_id(formatted_address)
        self._abort_if_unique_id_configured()

        self._discovered_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or DEFAULT_NAME,
            "address": formatted_address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm bluetooth discovery."""
        assert self._discovered_info is not None

        fallback_name = self._discovered_info.name or DEFAULT_NAME

        if user_input is not None:
            name = _clean_name(user_input.get(CONF_NAME), fallback_name)
            return self.async_create_entry(
                title=name,
                data={
                    CONF_MAC: self._discovered_info.address.upper(),
                    CONF_NAME: name,
                },
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": fallback_name,
                "address": self._discovered_info.address.upper(),
            },
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=fallback_name): str,
                }
            ),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated configuration flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mac = _normalize_mac(user_input[CONF_MAC])
            if mac is None:
                errors[CONF_MAC] = "invalid_mac"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

                name = _clean_name(user_input.get(CONF_NAME), DEFAULT_NAME)
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_MAC: mac,
                        CONF_NAME: name,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_MAC): str,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
