"""Powershop sensors."""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_DOLLAR, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import AuthError, PowershopAPIClient
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_PROPERTY_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)

# Exact API tariff labels for each Home Assistant rate sensor.
# Exact matching prevents "Peak" matching "Off Peak" or "Super Off Peak".
_RATE_LABELS = {
    "peak_rate": {
        "weekday peak",
        "peak",
    },
    "shoulder_rate": {
        "weekday shoulder",
        "shoulder",
    },
    "off_peak_rate": {
        "weekday off peak",
        "off peak",
        "offpeak",
    },
    "super_off_peak_rate": {
        "super off peak",
        "super offpeak",
        "superoffpeak",
    },
    "weekend_rate": {
        "weekend day",
        "weekend night",
        "weekend",
    },
}

# Fallback keywords retained for less common API labels.
_OFF_PEAK_KEYWORDS = {"off peak", "offpeak", "night", "low", "uncontrolled"}
_PEAK_KEYWORDS = {"peak", "day", "weekday peak", "high"}
_SHOULDER_KEYWORDS = {"shoulder", "controlled"}

SENSORS = [
    SensorEntityDescription(
        key="balance",
        name="Balance",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:currency-usd",
    ),
    SensorEntityDescription(
        key="off_peak_rate",
        name="Off Peak Rate",
        native_unit_of_measurement="c/kWh",
        state_class=None,
        icon="mdi:clock-outline",
    ),
    SensorEntityDescription(
        key="peak_rate",
        name="Peak Rate",
        native_unit_of_measurement="c/kWh",
        state_class=None,
        icon="mdi:clock-alert",
    ),
    SensorEntityDescription(
        key="shoulder_rate",
        name="Shoulder Rate",
        native_unit_of_measurement="c/kWh",
        state_class=None,
        icon="mdi:clock",
    ),
    SensorEntityDescription(
        key="super_off_peak_rate",
        name="Super Off Peak Rate",
        native_unit_of_measurement="c/kWh",
        state_class=None,
        icon="mdi:weather-night",
    ),
    SensorEntityDescription(
        key="weekend_rate",
        name="Weekend Rate",
        native_unit_of_measurement="c/kWh",
        state_class=None,
        icon="mdi:calendar-weekend",
    ),
    SensorEntityDescription(
        key="usage_today",
        name="Usage Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:lightning-bolt",
    ),
    SensorEntityDescription(
        key="usage_billing_period",
        name="Usage This Billing Period",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:lightning-bolt-circle",
    ),
    SensorEntityDescription(
        key="cost_billing_period",
        name="Cost This Billing Period",
        native_unit_of_measurement="NZD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:cash",
    ),
    SensorEntityDescription(
        key="voucher_balance",
        name="Voucher Balance",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:ticket-percent",
    ),
    SensorEntityDescription(
        key="period_used_cost",
        name="Used This Billing Period",
        native_unit_of_measurement="NZD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:cash-check",
    ),
    SensorEntityDescription(
        key="period_estimated_cost",
        name="Estimated Cost This Billing Period",
        native_unit_of_measurement="NZD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:cash-clock",
    ),
    SensorEntityDescription(
        key="period_still_to_buy",
        name="Still To Buy This Billing Period",
        native_unit_of_measurement="NZD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:cart-plus",
    ),
    SensorEntityDescription(
        key="period_coverage_pct",
        name="Billing Period Pack Coverage",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
    ),
    SensorEntityDescription(
        key="daily_charge",
        name="Daily Standing Charge",
        native_unit_of_measurement="NZD",
        state_class=None,
        icon="mdi:calendar-today",
    ),
]


def _normalise_label(label: str) -> str:
    """Normalise API tariff labels for reliable matching."""
    return " ".join(
        label.casefold()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )


def _match_exact_rate(
    rate_periods: Dict[str, Any], accepted_labels: set[str]
) -> Optional[float]:
    """Return a rate whose API label exactly matches an accepted label."""
    normalised_labels = {_normalise_label(label) for label in accepted_labels}
    for label, data in rate_periods.items():
        if _normalise_label(label) in normalised_labels:
            return data.get("rate")
    return None


def _match_rate(
    rate_periods: Dict[str, Any],
    keywords: set,
    exclude_keywords: Optional[set] = None,
) -> Optional[float]:
    """Return the first rate whose label contains any keyword."""
    for label, data in rate_periods.items():
        label_lower = _normalise_label(label)
        if exclude_keywords and any(kw in label_lower for kw in exclude_keywords):
            continue
        if any(kw in label_lower for kw in keywords):
            return data.get("rate")
    return None


class PowershopDataUpdateCoordinator(DataUpdateCoordinator):
    """Manage fetching data from the Powershop GraphQL API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PowershopAPIClient,
        config_entry: ConfigEntry,
    ) -> None:
        self.client = client
        self._config_entry = config_entry
        self._measurement_fail_count: int = 0
        self._cached_measurement_data: Dict[str, Any] = {}
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)

    async def async_shutdown(self) -> None:
        await self.client.close()

    async def _async_update_data(self) -> Dict[str, Any]:
        account_number = self._config_entry.data[CONF_ACCOUNT_NUMBER]
        property_id = self._config_entry.data.get(CONF_PROPERTY_ID)

        try:
            data = await self.client.get_rate_data(account_number, property_id)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Powershop API: {err}") from err

        measurement_keys = (
            "usage_today_kwh",
            "usage_period_kwh",
            "cost_period_nzd",
            "cost_used_nzd",
            "cost_estimated_nzd",
            "cost_still_to_buy_nzd",
            "period_coverage_pct",
            "upcoming_periods",
            "daily_charge_nzd",
            "hourly_usage",
            "daily_usage",
        )
        if not data.get("measurement_ok", True):
            self._measurement_fail_count += 1
            _LOGGER.warning(
                "Powershop measurement data unavailable (failure %d/12); using cached values",
                self._measurement_fail_count,
            )
            if self._measurement_fail_count >= 12:
                raise UpdateFailed(
                    "Powershop measurement data has been unavailable for "
                    f"{self._measurement_fail_count} consecutive polls (~3 hours)"
                )
            data.update(self._cached_measurement_data)
        else:
            self._measurement_fail_count = 0
            self._cached_measurement_data = {
                key: data[key] for key in measurement_keys if key in data
            }

        if self.client.refresh_token != self._config_entry.data.get(CONF_REFRESH_TOKEN):
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_REFRESH_TOKEN: self.client.refresh_token,
                },
            )

        data["last_updated"] = datetime.now()
        return data


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Powershop sensor platform."""
    coordinator: PowershopDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(
        PowershopSensor(coordinator, description, config_entry)
        for description in SENSORS
    )


class PowershopSensor(CoordinatorEntity, SensorEntity):
    """A sensor that reads from the Powershop coordinator."""

    def __init__(
        self,
        coordinator: PowershopDataUpdateCoordinator,
        description: SensorEntityDescription,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        account_number = config_entry.data.get(CONF_ACCOUNT_NUMBER, "unknown")
        self._attr_unique_id = f"{DOMAIN}_{account_number}_{description.key}"
        self._attr_name = f"Powershop NZ {description.name}"
        self.entity_id = f"sensor.{DOMAIN}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, account_number)},
            "name": "Powershop NZ",
            "manufacturer": "Powershop NZ",
            "model": "Customer Account",
        }

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None

        data = self.coordinator.data
        key = self.entity_description.key

        if key == "balance":
            return data.get("balance")

        rate_periods = data.get("rate_periods", {})

        if key in _RATE_LABELS:
            exact_rate = _match_exact_rate(rate_periods, _RATE_LABELS[key])
            if exact_rate is not None:
                return exact_rate

        # Fallbacks for legacy or alternative labels.
        if key == "off_peak_rate":
            return _match_rate(
                rate_periods,
                _OFF_PEAK_KEYWORDS,
                exclude_keywords={"super off peak", "super offpeak", "superoffpeak"},
            )
        if key == "peak_rate":
            return _match_rate(
                rate_periods,
                _PEAK_KEYWORDS,
                exclude_keywords=_OFF_PEAK_KEYWORDS
                | {"super off peak", "super offpeak", "superoffpeak"},
            )
        if key == "shoulder_rate":
            return _match_rate(rate_periods, _SHOULDER_KEYWORDS)
        if key == "usage_today":
            return data.get("usage_today_kwh")
        if key == "usage_billing_period":
            return data.get("usage_period_kwh")
        if key == "cost_billing_period":
            return data.get("cost_period_nzd")
        if key == "voucher_balance":
            return data.get("voucher_balance_nzd")
        if key == "period_used_cost":
            return data.get("cost_used_nzd")
        if key == "period_estimated_cost":
            return data.get("cost_estimated_nzd")
        if key == "period_still_to_buy":
            return data.get("cost_still_to_buy_nzd")
        if key == "period_coverage_pct":
            return data.get("period_coverage_pct")
        if key == "daily_charge":
            return data.get("daily_charge_nzd")

        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data
        attrs: Dict[str, Any] = {
            "account_number": data.get("account_number"),
            "last_updated": data.get("last_updated"),
        }

        if self.entity_description.key == "balance":
            attrs["next_billing_date"] = data.get("next_billing_date")
            attrs["overdue_balance"] = data.get("overdue_balance")

        if self.entity_description.key == "cost_billing_period":
            attrs["billing_period_start"] = data.get("period_start")
            attrs["billing_period_end"] = data.get("period_end")

        if self.entity_description.key == "period_estimated_cost":
            attrs["billing_period_start"] = data.get("period_start")
            attrs["billing_period_end"] = data.get("period_end")
            attrs["upcoming_periods"] = data.get("upcoming_periods", [])

        if self.entity_description.key == "voucher_balance":
            attrs["voucher_count"] = data.get("voucher_count")
            attrs["vouchers"] = data.get("voucher_list", [])

        if self.entity_description.key == "usage_today":
            attrs["hourly_usage"] = data.get("hourly_usage", [])

        if self.entity_description.key == "usage_billing_period":
            attrs["billing_period_start"] = data.get("period_start")
            attrs["billing_period_end"] = data.get("period_end")
            attrs["daily_usage"] = data.get("daily_usage", [])

        rate_periods = data.get("rate_periods", {})
        if self.entity_description.key in _RATE_LABELS:
            attrs["all_rates"] = {
                label: rate_data.get("rate_formatted")
                for label, rate_data in rate_periods.items()
            }

        return attrs
