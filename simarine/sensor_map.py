"""Simarine Pico element decoding and sensor mapping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

DecodeFn = Callable[[int, int], float | int]


@dataclass(frozen=True, slots=True)
class SensorDefinition:
    """Maps a Simarine element to a decoded sensor value."""

    key: str
    module: str
    description: str
    decoder: DecodeFn


def decode_voltage(_: int, b: int) -> float:
    return round(b / 1000.0, 3)


def decode_resistance(_: int, b: int) -> int:
    return b


def decode_soc(a: int, _: int) -> float:
    return round(a / 160.0, 2)


def decode_pressure(_: int, b: int) -> int:
    # Pressure unit is not confirmed from firmware docs, so keep it raw.
    return b


def decode_current(_: int, b: int) -> float:
    # Common Simarine current representation is signed 16-bit in centi-amps.
    signed = b - 65536 if b >= 32768 else b
    return round(signed / 100.0, 2)


SENSOR_MAP: dict[int, SensorDefinition] = {
    3: SensorDefinition(
        key="barometric_pressure_raw",
        module="PICO",
        description="Pico internal barometric pressure (raw)",
        decoder=decode_pressure,
    ),
    5: SensorDefinition(
        key="pico_internal_voltage_v",
        module="PICO",
        description="Pico internal voltage",
        decoder=decode_voltage,
    ),
    14: SensorDefinition(
        key="house_battery_voltage_v",
        module="SC303",
        description="SC303 ch2 house battery voltage",
        decoder=decode_voltage,
    ),
    15: SensorDefinition(
        key="water_tank_resistance_ohm",
        module="SC303",
        description="SC303 ch1 water tank resistance",
        decoder=decode_resistance,
    ),
    26: SensorDefinition(
        key="house_battery_soc_pct",
        module="SC303",
        description="House battery state-of-charge",
        decoder=decode_soc,
    ),
    28: SensorDefinition(
        key="house_battery_voltage_duplicate_v",
        module="SC303",
        description="Duplicate house battery voltage",
        decoder=decode_voltage,
    ),
}


def decode_elements(elements: dict[int, tuple[int, int]]) -> dict[str, float | int]:
    """Decode known elements into a flat API-friendly dictionary."""
    decoded: dict[str, float | int] = {}
    for element_id, definition in SENSOR_MAP.items():
        values = elements.get(element_id)
        if values is None:
            continue

        a, b = values
        if a == 0xFFFF and b == 0xFFFF:
            continue

        decoded[definition.key] = definition.decoder(a, b)

    if (
        "house_battery_voltage_v" in decoded
        and "house_battery_voltage_duplicate_v" in decoded
        and decoded["house_battery_voltage_v"] == decoded["house_battery_voltage_duplicate_v"]
    ):
        decoded["house_battery_voltage_match"] = 1

    return decoded
