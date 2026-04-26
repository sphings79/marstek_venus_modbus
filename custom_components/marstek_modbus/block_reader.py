"""
Modbus block-read optimization for the Marstek Venus integration.

Instead of issuing one read_holding_registers request per sensor, this module
groups sensors whose register addresses are close together into a single bulk
request. This significantly reduces the number of TCP round-trips to the
RS485-to-Ethernet gateway.

Typical improvement: 50-70% fewer TCP requests per poll cycle.

Supported data types for block reads: uint16, int16, uint32, int32, char.
Complex types (e.g. schedule) are excluded and fall back to individual reads.

Compatible with pymodbus >= 3.x (AsyncModbusTcpClient).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Modbus FC03 allows at most 125 holding registers per request (spec: 0x7D).
BLOCK_MAX_REGISTERS: int = 125

# Gaps of this many registers or fewer are bridged within a single block read.
# Bridged registers are read but their values are discarded.
# 5 is a good balance for the Marstek Venus register layout.
BLOCK_MAX_GAP: int = 15

# Data types that can be decoded from a raw register array.
# Types not listed here (e.g. "schedule") fall back to individual reads.
BLOCKABLE_DATA_TYPES: frozenset[str] = frozenset({"uint16", "int16", "uint32", "int32", "char"})


# ---------------------------------------------------------------------------
# Block grouping
# ---------------------------------------------------------------------------


def build_register_blocks(
    due_sensors: list[tuple[dict, str, int]],
    max_gap: int = BLOCK_MAX_GAP,
) -> list[list[tuple[dict, str, int]]]:
    """
    Group a list of due sensors into contiguous register blocks.

    Sensors with complex data types (not in BLOCKABLE_DATA_TYPES) are placed
    into single-element groups so the caller can route them to individual reads.

    Algorithm:
      1. Separate sensors into "simple" (blockable) and "complex" types.
      2. Sort simple sensors by register address.
      3. Walk the sorted list: extend the current block if the gap to the next
         sensor is within max_gap AND the resulting block stays within
         BLOCK_MAX_REGISTERS. Otherwise start a new block.
      4. Append complex sensors as single-element groups at the end.

    Args:
        due_sensors: List of (sensor_def, key, interval) tuples that are due
                     for polling in the current cycle.
        max_gap:     Maximum register gap to bridge within a single block read.
                     Registers in the gap are read but their values are ignored.

    Returns:
        List of groups. Each group is a list of (sensor_def, key, interval)
        tuples that should be covered by one Modbus request.
    """
    simple: list[tuple[dict, str, int]] = []
    complex_sensors: list[tuple[dict, str, int]] = []

    for entry in due_sensors:
        sensor, _key, _interval = entry
        if sensor.get("data_type", "uint16") in BLOCKABLE_DATA_TYPES:
            simple.append(entry)
        else:
            complex_sensors.append(entry)

    # Sort by register address so adjacent sensors end up in the same block.
    simple.sort(key=lambda e: e[0]["register"])

    groups: list[list[tuple[dict, str, int]]] = []
    current_group: list[tuple[dict, str, int]] = []
    current_end: int = -1

    for entry in simple:
        sensor, _key, _interval = entry
        reg_start = sensor["register"]
        reg_end = reg_start + sensor.get("count", 1) - 1

        if not current_group:
            current_group = [entry]
            current_end = reg_end
            continue

        gap = reg_start - current_end - 1
        first_reg = current_group[0][0]["register"]
        potential_count = reg_end - first_reg + 1

        if gap <= max_gap and potential_count <= BLOCK_MAX_REGISTERS:
            current_group.append(entry)
            current_end = max(current_end, reg_end)
        else:
            groups.append(current_group)
            current_group = [entry]
            current_end = reg_end

    if current_group:
        groups.append(current_group)

    # Append complex-type sensors as single-element groups.
    for entry in complex_sensors:
        groups.append([entry])

    return groups


# ---------------------------------------------------------------------------
# Value decoding
# ---------------------------------------------------------------------------


def decode_raw_registers(
    registers: list[int],
    block_start: int,
    sensor: dict,
    scales: dict[str, float] | None = None,
) -> Any:
    """
    Decode a single sensor value from a raw register array returned by a block read.

    Supports uint16, int16, uint32, int32, and char (returned as a raw list).
    Returns None on unsupported data type, index error, or decode failure.

    Word order is big-endian (high word first), which is the Marstek standard.

    Args:
        registers:   Raw register list from the Modbus response.
        block_start: Start address of the block (first register address read).
        sensor:      Sensor definition dict containing at least "register",
                     "data_type", "count", and optionally "scale".
        scales:      Optional dict of per-key scale overrides
                     (coordinator._scales). Falls back to sensor["scale"].

    Returns:
        Decoded value (int, float, or list[int] for char), or None on failure.
    """
    key = sensor["key"]
    data_type = sensor.get("data_type", "uint16")
    count = sensor.get("count", 1)
    scale = (scales or {}).get(key, sensor.get("scale", 1))
    offset = sensor["register"] - block_start

    try:
        raw_regs = registers[offset: offset + count]
    except (IndexError, TypeError):
        _LOGGER.error(
            "Block decode: offset %d+%d out of range (array len=%d) for '%s'",
            offset, count, len(registers), key,
        )
        return None

    if len(raw_regs) < count:
        _LOGGER.warning(
            "Block decode: not enough registers for '%s' (expected %d, got %d)",
            key, count, len(raw_regs),
        )
        return None

    try:
        if data_type == "uint16":
            raw = raw_regs[0]
            return raw * scale if scale != 1 else raw

        if data_type == "int16":
            raw = raw_regs[0]
            if raw > 0x7FFF:
                raw -= 0x10000
            return raw * scale if scale != 1 else raw

        if data_type == "uint32":
            # Big-endian word order: high word first
            raw = (raw_regs[0] << 16) | raw_regs[1]
            return raw * scale if scale != 1 else raw

        if data_type == "int32":
            raw = (raw_regs[0] << 16) | raw_regs[1]
            if raw > 0x7FFFFFFF:
                raw -= 0x100000000
            return raw * scale if scale != 1 else raw

        if data_type == "char":
            # Return the raw register list; caller converts to string if needed.
            return raw_regs

    except Exception as exc:
        _LOGGER.error("Block decode error for '%s': %s", key, exc)
        return None

    _LOGGER.debug(
        "Block decode: unsupported data_type '%s' for '%s' - falling back to individual read",
        data_type, key,
    )
    return None


# ---------------------------------------------------------------------------
# Async block prefetch
# ---------------------------------------------------------------------------


async def async_block_prefetch(
    pymodbus_client: Any,
    unit_id: int,
    due_sensors: list[tuple[dict, str, int]],
    scales: dict[str, float] | None = None,
    max_gap: int = BLOCK_MAX_GAP,
) -> dict[str, Any]:
    """
    Pre-fetch all due sensors in optimized register blocks.

    Sensors whose register addresses are close together are read in a single
    Modbus request. Sensors with complex data types or those whose block read
    fails are absent from the returned dict; the caller should fall back to
    individual reads for those keys.

    Args:
        pymodbus_client: A connected pymodbus AsyncModbusTcpClient instance.
                         Pass None to skip block reads entirely (returns {}).
        unit_id:         Modbus slave / unit ID.
        due_sensors:     List of (sensor_def, key, interval) due for polling.
        scales:          Per-key scale overrides (coordinator._scales).
        max_gap:         Maximum register gap to bridge in a single block.

    Returns:
        Dict mapping sensor key to decoded value for all successfully block-read
        sensors. Keys absent from the dict must be read individually by the caller.
    """
    if pymodbus_client is None:
        return {}

    groups = build_register_blocks(due_sensors, max_gap)

    saved = sum(len(g) - 1 for g in groups if len(g) > 1)
    _LOGGER.debug(
        "Block prefetch: %d sensors -> %d groups (%d TCP requests saved)",
        len(due_sensors), len(groups), saved,
    )

    cache: dict[str, Any] = {}

    for group in groups:
        sensor_first = group[0][0]
        data_type = sensor_first.get("data_type", "uint16")

        # Single-element groups with complex types: skip prefetch.
        if len(group) == 1 and data_type not in BLOCKABLE_DATA_TYPES:
            continue

        block_start = sensor_first["register"]
        block_end = max(s["register"] + s.get("count", 1) - 1 for s, _, _ in group)
        block_count = block_end - block_start + 1

        _LOGGER.debug(
            "Block read [%d..%d] (%d registers, %d sensors): %s",
            block_start, block_end, block_count, len(group),
            [k for _, k, _ in group],
        )

        try:
            response = await asyncio.wait_for(
                pymodbus_client.read_holding_registers(
                    address=block_start,
                    count=block_count,
                    slave=unit_id,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Block read timeout [%d..%d] - falling back to individual reads for: %s",
                block_start, block_end, [k for _, k, _ in group],
            )
            continue
        except Exception as exc:
            _LOGGER.warning(
                "Block read error [%d..%d]: %s - falling back to individual reads for: %s",
                block_start, block_end, exc, [k for _, k, _ in group],
            )
            continue

        if response.isError():
            _LOGGER.warning(
                "Block read Modbus error [%d..%d]: %s - falling back to individual reads for: %s",
                block_start, block_end, response, [k for _, k, _ in group],
            )
            continue

        for sensor, key, _ in group:
            value = decode_raw_registers(response.registers, block_start, sensor, scales)
            if value is not None:
                cache[key] = value
                _LOGGER.debug(
                    "Block decode '%s': register=%d value=%s", key, sensor["register"], value
                )
            else:
                _LOGGER.debug(
                    "Block decode '%s': failed - individual read will be used", key
                )

    _LOGGER.debug(
        "Block prefetch complete: %d/%d sensors in cache", len(cache), len(due_sensors)
    )
    return cache


# ---------------------------------------------------------------------------
# pymodbus client resolver
# ---------------------------------------------------------------------------


def resolve_pymodbus_client(marstek_client: Any) -> Any | None:
    """
    Locate the underlying pymodbus client inside a MarstekModbusClient instance.

    Probes common attribute names used by wrapper classes. Returns None if no
    compatible client is found, in which case block reads are unavailable and
    all reads fall back to individual async_read_value() calls.

    Args:
        marstek_client: A MarstekModbusClient instance (self.client in coordinator).

    Returns:
        A pymodbus AsyncModbusTcpClient with a read_holding_registers method, or None.
    """
    for attr in ("client", "_client", "modbus_client", "_modbus_client", "async_client"):
        candidate = getattr(marstek_client, attr, None)
        if candidate is not None and hasattr(candidate, "read_holding_registers"):
            _LOGGER.debug("Block reader: pymodbus client found via .%s", attr)
            return candidate

    _LOGGER.debug(
        "Block reader: no pymodbus client found in MarstekModbusClient - "
        "block reads disabled, falling back to individual reads. "
        "Available attributes: %s",
        [a for a in dir(marstek_client) if not a.startswith("__")],
    )
    return None
