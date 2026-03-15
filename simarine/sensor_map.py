"""Simarine Pico element decoding and sensor mapping."""

from __future__ import annotations

from typing import Callable

DecodeFn = Callable[[int], float | int]


def decode_voltage(raw: int) -> float:
    return round(raw / 1000.0, 3)


def decode_resistance(raw: int) -> int:
    return raw


def decode_soc(raw: int) -> float:
    return round(raw / 160.0, 1)


def decode_pressure(raw: int) -> int:
    return raw


def decode_current(raw: int) -> float:
    signed = raw - 65536 if raw >= 32768 else raw
    return round(signed / 100.0, 2)


# Elements that are Pico internal counters/timers (not user-facing sensors)
INTERNAL_ELEMENTS: set[int] = {0, 1, 9}


def is_disconnected(a: int, b: int) -> bool:
    """Return True if the element values indicate a disconnected/empty slot."""
    if a == 0xFFFF and b >= 0xFC00:
        return True
    if a == 0 and b == 0xFFFF:
        return True
    if a == 0x7FFF and b == 0xFFFF:
        return True
    return False


# Sensor map: element_id -> config dict
# field: which 16-bit value to decode ('a' or 'b')
SENSOR_MAP: dict[int, dict] = {
    3: {
        "name": "barometric_pressure_raw",
        "unit": "raw",
        "field": "b",
        "decode": decode_pressure,
        "description": "Pico internal barometric pressure (raw)",
    },
    5: {
        "name": "pico_internal_voltage_v",
        "unit": "V",
        "field": "b",
        "decode": decode_voltage,
        "description": "Pico internal voltage sense",
    },
    11: {
        "name": "shunt_current_raw",
        "unit": "raw",
        "field": "b",
        "decode": decode_current,
        "description": "SC303 shunt current (scaling TBD)",
    },
    14: {
        "name": "house_battery_voltage_v",
        "unit": "V",
        "field": "b",
        "decode": decode_voltage,
        "description": "SC303 ch2 house battery voltage at shunt",
    },
    15: {
        "name": "water_tank_resistance_ohm",
        "unit": "Ω",
        "field": "b",
        "decode": decode_resistance,
        "description": "SC303 ch1 water tank resistance sensor",
    },
    26: {
        "name": "house_battery_soc_pct",
        "unit": "%",
        "field": "a",
        "decode": decode_soc,
        "description": "House battery state of charge",
    },
    28: {
        "name": "house_battery_voltage_dup_v",
        "unit": "V",
        "field": "b",
        "decode": decode_voltage,
        "description": "Duplicate of house battery voltage (el[14])",
    },
}
