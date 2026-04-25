"""
Handles all sensor polling via Home Assistant DataUpdateCoordinator,
with per-sensor intervals and optional skipping if not due.

Block-read optimization:
  Sensors whose register addresses are close together are grouped and read
  in a single Modbus request instead of one request per sensor.
  This reduces TCP round-trips to the RS485 gateway by ~50-70% per cycle.
  See block_reader.py for implementation details.
"""

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_SCAN_INTERVALS, SUPPORTED_VERSIONS, DEFAULT_UNIT_ID
from .helpers.modbus_client import MarstekModbusClient
from .block_reader import async_block_prefetch, resolve_pymodbus_client

from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def get_entity_type(entity) -> str:
    """Determine entity type based on its class inheritance."""
    for base in entity.__class__.__mro__:
        if issubclass(base, Entity) and base.__name__.endswith("Entity"):
            return base.__name__.replace("Entity", "").lower()
    return "entity"


class MarstekCoordinator(DataUpdateCoordinator):
    """Coordinator managing all Marstek Venus Modbus sensors."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize the coordinator with connection parameters and update interval."""
        self.hass = hass
        self.host = entry.data["host"]
        self.port = entry.data["port"]
        self.message_wait_ms = entry.data.get("message_wait_milliseconds")
        self.timeout = entry.data.get("timeout")
        self.unit_id = entry.data.get("unit_id", DEFAULT_UNIT_ID)

        # Mapping from sensor key to entity type for logging and processing
        self._entity_types: dict[str, str] = {}

        # Store the config entry for potential future use
        self.config_entry = entry

        # Scaling factors for sensors, if applicable
        self._scales: dict[str, float] = {}

        # Placeholder definitions — actual register definitions are loaded
        # asynchronously to avoid blocking the event loop during __init__.
        self.SENSOR_DEFINITIONS = []
        self.BINARY_SENSOR_DEFINITIONS = []
        self.SELECT_DEFINITIONS = []
        self.SWITCH_DEFINITIONS = []
        self.NUMBER_DEFINITIONS = []
        self.BUTTON_DEFINITIONS = []
        self.EFFICIENCY_SENSOR_DEFINITIONS = []
        self.STORED_ENERGY_SENSOR_DEFINITIONS = []
        self.CYCLE_SENSOR_DEFINITIONS = []

        # Combined list of all pollable sensor definitions
        self._all_definitions = []

        # Initialize Modbus client for communication
        self.client = MarstekModbusClient(
            self.host,
            self.port,
            message_wait_ms=self.message_wait_ms,
            timeout=self.timeout,
            unit_id=self.unit_id,
        )

        # Data storage for sensor values and timestamps of last updates
        self.data: dict = {}
        self._last_update_times: dict = {}
        # Timestamps of last successful writes per key (for post-write read suppression)
        self._last_write_times: dict = {}
        # Timestamps when a read was last started per key (for stale-read detection)
        self._read_start_times: dict = {}

        # Connection throttling to prevent endless retry attempts after repeated failures
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5
        self._connection_suspended = False
        self._suspension_reset_time = None

        self._consecutive_timeout_cycles = 0
        self._max_consecutive_timeout_cycles = 3
        self._timeout_ratio_reconnect_threshold = 0.5

        # Connection health tracking for diagnostics
        self._last_successful_read = None
        self._connection_established_at = None

        # Per-register failure tracking for exponential backoff.
        # Counts consecutive failed reads per key; resets to 0 on first success.
        # Effective poll interval = base_interval * 2^min(failures, 6), capped at 3600s.
        self._register_failures: dict[str, int] = {}
        # Tracks last *attempt* time (success or failure) for backoff interval calculation.
        self._last_attempt_times: dict = {}

        # Cached reference to the underlying pymodbus client.
        # Resolved once via resolve_pymodbus_client() and reused across cycles.
        # Reset to None after reconnects or write operations to force re-resolution.
        self._pymodbus_client = None

        # Prepare scan intervals (from config_entry.options or default)
        options = entry.options or {}
        self._update_scan_intervals(options)

        # Initialize the base DataUpdateCoordinator with the calculated interval
        super().__init__(
            hass,
            _LOGGER,
            name="MarstekCoordinator",
            update_interval=self.update_interval,
        )

        _LOGGER.debug("Coordinator initialized with update_interval: %s", self.update_interval)

    def _update_scan_intervals(self, options: dict):
        """Update scan intervals from config options and compute update_interval (lowest interval always used)."""
        old_intervals = getattr(self, "scan_intervals", {}).copy() if hasattr(self, "scan_intervals") else {}
        self.scan_intervals = DEFAULT_SCAN_INTERVALS.copy()

        for key in DEFAULT_SCAN_INTERVALS:
            if key in options:
                try:
                    self.scan_intervals[key] = int(options[key])
                except Exception:
                    _LOGGER.warning("Invalid scan interval for %s: %s", key, options[key])

        # Use the shortest configured interval as the coordinator tick rate
        min_interval = min(self.scan_intervals.values()) if self.scan_intervals else 30
        self.update_interval = timedelta(seconds=min_interval)

        # Update DataUpdateCoordinator's update_interval if already initialized
        if hasattr(self, "_listeners") and self._listeners is not None:
            try:
                super(MarstekCoordinator, self.__class__).update_interval.fset(self, self.update_interval)
                _LOGGER.debug(
                    "Coordinator update_interval changed dynamically to %s due to options change",
                    self.update_interval,
                )
            except Exception as e:
                _LOGGER.warning("Failed to update coordinator update_interval: %s", e)

        _LOGGER.debug(
            "Scan intervals updated. Old: %s, New: %s, Coordinator update_interval: %s",
            old_intervals,
            self.scan_intervals,
            self.update_interval,
        )

    def register_entity_type(self, key: str, entity_type: str):
        """Register the entity type for a given sensor key.
        For calculated sensors with dependencies, ensure all dependency keys are registered.
        """
        self._entity_types[key] = entity_type

        # Register all dependency keys with entity type and scale
        definition = next((d for d in self.SENSOR_DEFINITIONS if d.get("key") == key), None)
        if definition and "dependency_keys" in definition:
            for dep_alias, dep_key in definition["dependency_keys"].items():
                if dep_key not in self._entity_types:
                    self._entity_types[dep_key] = entity_type

                dep_def = next((d for d in self.SENSOR_DEFINITIONS if d.get("key") == dep_key), None)
                if dep_def:
                    scale = dep_def.get("scale")
                    if scale is not None:
                        self._scales[dep_key] = scale

    def get_connection_diagnostics(self) -> dict:
        """Return diagnostic information about the connection."""
        from homeassistant.util.dt import utcnow
        now = utcnow()

        diagnostics = {
            "host": self.host,
            "port": self.port,
            "consecutive_failures": self._consecutive_failures,
            "connection_suspended": self._connection_suspended,
            "last_successful_read": self._last_successful_read.isoformat() if self._last_successful_read else None,
            "connection_established_at": self._connection_established_at.isoformat() if self._connection_established_at else None,
        }

        if self._connection_suspended and self._suspension_reset_time:
            diagnostics["suspension_expires_in_seconds"] = (self._suspension_reset_time - now).total_seconds()

        return diagnostics

    async def async_init(self):
        """Asynchronously initialize the Modbus connection."""
        from homeassistant.util.dt import utcnow
        connected = await self.client.async_connect()
        if not connected:
            _LOGGER.error("Failed to connect to Modbus device at %s:%d", self.host, self.port)
        else:
            self._connection_established_at = utcnow()
            _LOGGER.info("Successfully connected to Modbus device at %s:%d", self.host, self.port)
        return connected

    async def async_load_registers(self, version: str | None = None):
        """Load register definitions from YAML (off the event loop) and populate coordinator attributes.

        Must be called from async context; runs the blocking YAML load in the
        executor to avoid performing file I/O inside __init__.
        """
        raw_device_version = (version or "") or ""
        if not str(raw_device_version).strip():
            used_version = SUPPORTED_VERSIONS[0]
        else:
            used_version = raw_device_version

        try:
            data = await self.hass.async_add_executor_job(get_registers, used_version)
            self.SENSOR_DEFINITIONS = data.get("SENSOR_DEFINITIONS", [])
            self.BINARY_SENSOR_DEFINITIONS = data.get("BINARY_SENSOR_DEFINITIONS", [])
            self.SELECT_DEFINITIONS = data.get("SELECT_DEFINITIONS", [])
            self.SWITCH_DEFINITIONS = data.get("SWITCH_DEFINITIONS", [])
            self.NUMBER_DEFINITIONS = data.get("NUMBER_DEFINITIONS", [])
            self.BUTTON_DEFINITIONS = data.get("BUTTON_DEFINITIONS", [])
            self.EFFICIENCY_SENSOR_DEFINITIONS = data.get("EFFICIENCY_SENSOR_DEFINITIONS", [])
            self.STORED_ENERGY_SENSOR_DEFINITIONS = data.get("STORED_ENERGY_SENSOR_DEFINITIONS", [])
            self.CYCLE_SENSOR_DEFINITIONS = data.get("CYCLE_SENSOR_DEFINITIONS", [])

            self._all_definitions = (
                self.SENSOR_DEFINITIONS
                + self.BINARY_SENSOR_DEFINITIONS
                + self.SELECT_DEFINITIONS
                + self.NUMBER_DEFINITIONS
                + self.SWITCH_DEFINITIONS
            )
            _LOGGER.debug(
                "Loaded register definitions for version '%s' (%d entries)",
                used_version, len(self._all_definitions),
            )
        except Exception as e:
            _LOGGER.warning(
                "Failed to load register definitions for version '%s': %s", used_version, e
            )
            self._all_definitions = []

    # ------------------------------------------------------------------
    # Read / write helpers
    # ------------------------------------------------------------------

    async def async_read_value(self, sensor: dict, key: str, track_failure: bool = True):
        """Read a single sensor value from Modbus with logging and type checking.

        Used as a fallback when block reads are unavailable or when the sensor
        has a complex data type (e.g. schedule) that requires special handling.

        Args:
            sensor:        Sensor definition dict.
            key:           Sensor key (used for logging).
            track_failure: If False, timeouts are not counted towards timeout metrics.
        """
        entity_type = self._entity_types.get(key, get_entity_type(sensor))
        scale = self._scales.get(key, sensor.get("scale", 1))
        unit = sensor.get("unit", "N/A")

        if not hasattr(self, "client") or self.client is None:
            _LOGGER.error("Modbus client is not available when reading %s '%s'", entity_type, key)
            return None

        try:
            value = await asyncio.wait_for(
                self.client.async_read_register(
                    register=sensor["register"],
                    data_type=sensor.get("data_type", "uint16"),
                    count=sensor.get("count", 1),
                    sensor_key=key,
                ),
                timeout=10.0,
            )

            if isinstance(value, (int, float, bool, str, dict, list)):
                _LOGGER.debug(
                    "Updated %s '%s': register=%d, value=%s, scale=%s, unit=%s",
                    entity_type, key, sensor["register"], value, scale, unit,
                )
                return value

            _LOGGER.warning(
                "Invalid value for %s '%s': %r (type %s)",
                entity_type, key, value, type(value).__name__,
            )
            return None

        except asyncio.TimeoutError:
            if track_failure:
                self._timeouts_in_cycle = getattr(self, "_timeouts_in_cycle", 0) + 1
            _LOGGER.warning(
                "Timeout reading %s '%s' at register %d from %s:%d"
                " - connection may be slow or incorrect",
                entity_type, key, sensor["register"], self.client.host, self.client.port,
            )
            return None
        except Exception as e:
            _LOGGER.error(
                "Error reading %s '%s' at register %d: %s",
                entity_type, key, sensor["register"], e,
            )
            return None

    async def async_write_value(
        self,
        register: int,
        value: int,
        key: str,
        scale=None,
        unit=None,
        entity_type="unknown",
    ):
        """Write a value to a Modbus register asynchronously and log the operation."""
        if not hasattr(self, "client") or self.client is None:
            _LOGGER.error("Modbus client is not available when writing %s '%s'", entity_type, key)
            return False

        _LOGGER.debug(
            "Writing to %s '%s': register=%d (0x%04X), value=%s",
            entity_type, key, register, register, value,
        )

        data_type = None
        try:
            defn = next((d for d in self.NUMBER_DEFINITIONS if d.get("key") == key), None)
            if not defn:
                defn = next((d for d in self.SWITCH_DEFINITIONS if d.get("key") == key), None)
            if defn:
                data_type = defn.get("data_type")
        except Exception:
            data_type = None

        if not data_type:
            data_type = "uint16"

        value_to_send = None
        if data_type == "int16":
            if not isinstance(value, int):
                _LOGGER.error("Value for %s '%s' must be int for data_type int16", entity_type, key)
                return False
            value_to_send = value & 0xFFFF
        elif data_type == "uint16":
            if not isinstance(value, int) or not (0 <= value <= 0xFFFF):
                _LOGGER.error(
                    "Value for %s '%s' must be 0..65535 for data_type uint16", entity_type, key
                )
                return False
            value_to_send = value
        else:
            _LOGGER.error("Unsupported data_type '%s' for key '%s' on write", data_type, key)
            return False

        try:
            import asyncio as _asyncio
            try:
                success = await _asyncio.wait_for(
                    self.client.async_write_register(register=register, value=value_to_send),
                    timeout=10.0,
                )
            except _asyncio.TimeoutError:
                _LOGGER.error(
                    "Timeout writing to register 0x%X for %s '%s'"
                    " - connection may be half-open",
                    register, entity_type, key,
                )
                return False

            if success:
                _LOGGER.debug(
                    "Successfully wrote to %s '%s': register=%d (0x%04X),"
                    " value=%s, scale=%s, unit=%s",
                    entity_type, key, register, register, value_to_send,
                    scale if scale is not None else 1,
                    unit if unit is not None else "N/A",
                )
                from homeassistant.util.dt import utcnow as _utcnow
                self._last_write_times[key] = _utcnow()
                # Invalidate cached pymodbus client so it is re-resolved after reconnect
                self._pymodbus_client = None
                return True

            _LOGGER.warning(
                "Write operation failed for %s '%s': register=%d (0x%04X), value=%s",
                entity_type, key, register, register, value,
            )
            return False

        except Exception as e:
            _LOGGER.error(
                "Failed to write value %s to register 0x%X for %s '%s': %s",
                value, register, entity_type, key, e,
            )
            return False

    # ------------------------------------------------------------------
    # Main poll method
    # ------------------------------------------------------------------

    async def _async_update_data(self):
        """Update all sensors asynchronously with per-sensor interval skipping.

        Buttons are excluded as they are not polled.
        Sensors disabled in Home Assistant are skipped, except dependencies
        which are always fetched.

        Poll cycle is split into four phases:

        Phase 1 - Collect:
          Walk all definitions and apply existing filtering rules
          (disabled check, interval check, post-write suppression, backoff).
          Build a list of sensors that are due for polling this cycle.

        Phase 2 - Block prefetch:
          Group due sensors by contiguous register addresses and read each
          group in a single Modbus request (block_reader.async_block_prefetch).
          Complex data types (e.g. schedule) are excluded from block reads.

        Phase 3 - Process results:
          For each due sensor, look up its value in the block cache.
          Fall back to an individual async_read_value() call for sensors not
          covered by block reads (complex types or block read failures).
          Apply all existing success/failure bookkeeping.

        Phase 4 - Connection retry:
          Unchanged from the original implementation.
        """
        from homeassistant.util.dt import utcnow
        from homeassistant.helpers import entity_registry as er

        now = utcnow()
        updated_data = {}

        attempted_reads = 0
        successful_reads = 0
        self._timeouts_in_cycle = 0

        # --- Connection throttling ---
        if self._connection_suspended:
            if self._suspension_reset_time and now > self._suspension_reset_time:
                _LOGGER.info("Connection suspension expired - attempting reconnection")
                self._connection_suspended = False
                self._consecutive_failures = 0
                try:
                    connected = await self.client.async_reconnect()
                    if connected:
                        _LOGGER.info("Successfully reconnected after suspension")
                    else:
                        _LOGGER.warning("Failed to reconnect after suspension - will retry next cycle")
                        return self.data or {}
                except Exception as exc:
                    _LOGGER.error("Exception during reconnect: %s", exc)
                    return self.data or {}
            else:
                _LOGGER.debug("Connection suspended - skipping update to prevent resource exhaustion")
                return self.data or {}

        _LOGGER.debug("Coordinator poll tick at %s", now.isoformat())

        entity_registry = er.async_get(self.hass)

        # Collect all dependency keys so disabled entities that are dependencies
        # are still polled.
        all_definitions_for_deps = (
            self.EFFICIENCY_SENSOR_DEFINITIONS
            + self.STORED_ENERGY_SENSOR_DEFINITIONS
            + self.CYCLE_SENSOR_DEFINITIONS
        )
        dependency_keys_set = {
            dep_key
            for defn in all_definitions_for_deps
            for dep_key in defn.get("dependency_keys", {}).values()
            if dep_key
        }

        for dep_key in dependency_keys_set:
            _LOGGER.debug("Dependency key '%s'", dep_key)

        # ----------------------------------------------------------------
        # Phase 1: Collect sensors due for polling this cycle
        # ----------------------------------------------------------------
        due_sensors: list[tuple[dict, str, int]] = []

        for sensor in self._all_definitions:
            key = sensor["key"]
            entity_type = self._entity_types.get(key, get_entity_type(sensor))
            unique_id = f"{self.config_entry.entry_id}_{sensor['key']}"
            registry_entry = entity_registry.async_get_entity_id(
                entity_type, self.config_entry.domain, unique_id
            )

            is_disabled = False
            entry = entity_registry.entities.get(registry_entry) if registry_entry else None
            if entry:
                is_disabled = entry.disabled or entry.disabled_by is not None

            is_dependency = key in dependency_keys_set

            if is_disabled:
                if is_dependency:
                    _LOGGER.debug("Fetching disabled dependency key '%s'", key)
                else:
                    _LOGGER.debug("Skipping disabled entity '%s'", sensor.get("name", key))
                    continue

            interval_name = sensor.get("scan_interval")
            interval = None
            if interval_name:
                interval = self.scan_intervals.get(interval_name)

            if interval is None:
                _LOGGER.warning(
                    "%s '%s' has no scan_interval defined, skipping this poll",
                    entity_type, key,
                )
                continue

            # Suppress reads for 3 s after a write to avoid reading back stale state
            last_write = self._last_write_times.get(key)
            if last_write is not None and (now - last_write).total_seconds() < 3:
                _LOGGER.debug("Suppressing read of '%s' after recent write", key)
                continue

            # Exponential backoff: skip sensors with repeated failures
            failures = self._register_failures.get(key, 0)
            backoff = min(2 ** failures, 64)
            effective_interval = min(interval * backoff, 3600)

            last_attempt = self._last_attempt_times.get(key)
            elapsed = (now - last_attempt).total_seconds() if last_attempt else None

            if elapsed is not None and elapsed < effective_interval:
                _LOGGER.debug(
                    "Skipping %s '%s', last attempt %.1fs ago"
                    " (effective interval %ds, failures=%d)",
                    entity_type, key, elapsed, effective_interval, failures,
                )
                continue

            due_sensors.append((sensor, key, interval))

        # ----------------------------------------------------------------
        # Phase 2: Block prefetch - read due registers in bulk
        # ----------------------------------------------------------------
        if due_sensors:
            # Resolve the underlying pymodbus client once per coordinator lifetime.
            # Reset to None on reconnect or write to force re-resolution.
            if self._pymodbus_client is None:
                self._pymodbus_client = resolve_pymodbus_client(self.client)

            block_cache = await async_block_prefetch(
                pymodbus_client=self._pymodbus_client,
                unit_id=self.unit_id,
                due_sensors=due_sensors,
                scales=self._scales,
            )
        else:
            block_cache = {}

        # ----------------------------------------------------------------
        # Phase 3: Process results (cache-first, individual read as fallback)
        # ----------------------------------------------------------------
        for sensor, key, interval in due_sensors:
            entity_type = self._entity_types.get(key, get_entity_type(sensor))

            attempted_reads += 1
            self._read_start_times[key] = now

            if key in block_cache:
                # Value was read as part of a block request
                value = block_cache[key]
                _LOGGER.debug("Cache hit for '%s': value=%s (block read)", key, value)
            else:
                # Fallback: individual read for complex types or block read failures
                value = await self.async_read_value(sensor, key)

            if value is not None:
                # Special handling for schedule sensors: store both raw registers and
                # decoded attributes so entities can expose both.
                if sensor.get("data_type") == "schedule" and isinstance(value, dict):
                    try:
                        days = int(value.get("days") or 0)
                    except Exception:
                        days = value.get("days")
                    try:
                        start = int(value.get("start") or 0)
                    except Exception:
                        start = value.get("start")
                    try:
                        end = int(value.get("end") or 0)
                    except Exception:
                        end = value.get("end")
                    try:
                        enabled = int(value.get("enabled") or 0)
                    except Exception:
                        enabled = value.get("enabled")

                    # Mode is signed in attrs; convert to unsigned 16-bit for the raw register
                    try:
                        mode_signed = int(value.get("mode") or 0)
                        mode_raw = mode_signed & 0xFFFF
                    except Exception:
                        mode_raw = value.get("mode")

                    raw_regs = [days, start, end, mode_raw, enabled]
                    updated_data[key] = raw_regs
                    try:
                        updated_data[f"{key}_attrs"] = value
                    except Exception:
                        _LOGGER.exception("Failed to populate %s_attrs", key)

                    _LOGGER.debug(
                        "Stored raw schedule for %s: %s and attrs: %s",
                        key, raw_regs, value,
                    )
                else:
                    updated_data[key] = value

                self._last_update_times[key] = now
                self._last_attempt_times[key] = now
                prev_failures = self._register_failures.get(key, 0)
                if prev_failures > 0:
                    _LOGGER.info(
                        "%s '%s' recovered after %d consecutive failure(s)",
                        entity_type, key, prev_failures,
                    )
                self._register_failures[key] = 0
                successful_reads += 1
            else:
                self._last_attempt_times[key] = now
                new_failures = self._register_failures.get(key, 0) + 1
                self._register_failures[key] = new_failures
                next_backoff = min(2 ** new_failures, 64)
                next_interval = min(interval * next_backoff, 3600)
                if new_failures <= 3 or new_failures % 10 == 0:
                    _LOGGER.warning(
                        "Failed to read %s '%s' - value is None"
                        " (consecutive failures: %d, next poll in %ds)",
                        entity_type, key, new_failures, next_interval,
                    )
                else:
                    _LOGGER.debug(
                        "Failed to read %s '%s' - value is None (failure #%d)",
                        entity_type, key, new_failures,
                    )

        # ----------------------------------------------------------------
        # Phase 4: Connection retry logic (unchanged)
        # ----------------------------------------------------------------
        if attempted_reads > 0:
            timeout_reads = int(getattr(self, "_timeouts_in_cycle", 0) or 0)
            if successful_reads > 0:
                if self._consecutive_failures > 0:
                    _LOGGER.info(
                        "Connection recovered after %d failures (successful reads: %d/%d)",
                        self._consecutive_failures, successful_reads, attempted_reads,
                    )
                self._consecutive_failures = 0
                self._connection_suspended = False
                self._last_successful_read = now

                if timeout_reads and (timeout_reads / attempted_reads) >= self._timeout_ratio_reconnect_threshold:
                    self._consecutive_timeout_cycles += 1
                    _LOGGER.warning(
                        "High timeout rate detected (%d/%d) - consecutive timeout cycles: %d/%d",
                        timeout_reads, attempted_reads,
                        self._consecutive_timeout_cycles, self._max_consecutive_timeout_cycles,
                    )
                else:
                    self._consecutive_timeout_cycles = 0

                if self._consecutive_timeout_cycles >= self._max_consecutive_timeout_cycles:
                    try:
                        _LOGGER.info(
                            "Attempting reconnect due to repeated timeouts (%d/%d cycles)",
                            self._consecutive_timeout_cycles, self._max_consecutive_timeout_cycles,
                        )
                        connected = await self.client.async_reconnect()
                        if connected:
                            _LOGGER.info("Successfully reconnected after repeated timeouts")
                            self._consecutive_timeout_cycles = 0
                            self._connection_established_at = now
                            self._pymodbus_client = None
                        else:
                            _LOGGER.warning("Reconnect attempt after repeated timeouts failed")
                    except Exception as exc:
                        _LOGGER.error(
                            "Exception during reconnect after repeated timeouts: %s", exc
                        )
            elif successful_reads == 0:
                self._consecutive_failures += 1
                _LOGGER.warning(
                    "All read attempts failed (%d/%d) - consecutive failures: %d/%d",
                    successful_reads, attempted_reads,
                    self._consecutive_failures, self._max_consecutive_failures,
                )

                try:
                    _LOGGER.info("Attempting immediate reconnection after read failures")
                    connected = await self.client.async_reconnect()
                    if connected:
                        _LOGGER.info("Successfully reconnected")
                        self._consecutive_failures = 0
                        self._connection_established_at = now
                        # Invalidate cached pymodbus client after reconnect
                        self._pymodbus_client = None
                    else:
                        _LOGGER.warning("Immediate reconnection failed")
                except Exception as exc:
                    _LOGGER.error("Exception during immediate reconnect: %s", exc)

                if self._consecutive_failures >= self._max_consecutive_failures:
                    self._connection_suspended = True
                    self._suspension_reset_time = now + timedelta(minutes=1)
                    _LOGGER.error(
                        "Connection suspended after %d consecutive failures."
                        " Will retry in 1 minute to prevent resource exhaustion.",
                        self._consecutive_failures,
                    )
                self._consecutive_timeout_cycles = 0
        else:
            _LOGGER.debug("No sensors due for update in this cycle")

        # Defensive check
        if self.data is None:
            self.data = {}

        # Discard reads that were overtaken by a write during this cycle.
        # If a write completed after the read was started, the read observed
        # a pre-write device state and must not overwrite the fresh write result.
        for _k in list(updated_data.keys()):
            _read_start = self._read_start_times.get(_k)
            _last_write = self._last_write_times.get(_k)
            if _read_start and _last_write and _last_write > _read_start:
                _LOGGER.debug(
                    "Discarding stale read of '%s' - write completed after read started", _k
                )
                del updated_data[_k]

        self.data.update(updated_data)
        return self.data

    async def async_close(self):
        """Close the Modbus client connection cleanly."""
        try:
            await self.client.async_close()
            _LOGGER.debug("Closed Modbus connection to %s:%d", self.host, self.port)
        except Exception as e:
            _LOGGER.warning("Error closing Modbus client: %s", e)


# ------------------------------------------------------------------
# Register loader (unchanged)
# ------------------------------------------------------------------

def get_registers(version: str):
    """
    Return a dict with entity/register definitions for the given device version.

    The returned dict contains the keys:
      SENSOR_DEFINITIONS, BINARY_SENSOR_DEFINITIONS, SELECT_DEFINITIONS,
      SWITCH_DEFINITIONS, NUMBER_DEFINITIONS, BUTTON_DEFINITIONS,
      EFFICIENCY_SENSOR_DEFINITIONS, STORED_ENERGY_SENSOR_DEFINITIONS,
      CYCLE_SENSOR_DEFINITIONS.

    Falls back to the v1/v2 register set for unknown versions.
    """
    version_raw = (version or "").strip()
    version = version_raw.lower()
    _LOGGER.info("Version '%s' mapped to '%s'", version_raw, version)

    legacy_to_new = {
        "v1/v2": "e v1/v2",
        "v3": "e v3",
    }
    if version in legacy_to_new:
        mapped = legacy_to_new[version]
        _LOGGER.info(
            "Mapping legacy device version '%s' to '%s' for backwards compatibility",
            version_raw, mapped,
        )
        version = mapped

    allowed = {str(item).lower() for item in SUPPORTED_VERSIONS}
    if version not in allowed:
        raise ValueError(
            "Unsupported or missing device version %r. Supported versions: %s"
            % (version_raw, ", ".join(sorted(allowed)))
        )

    def _normalize_section(section):
        """Convert mapping-based sections into the legacy list-of-dicts format."""
        if isinstance(section, dict):
            normalized = []
            for key, value in section.items():
                entry = dict(value or {})
                entry.setdefault("key", key)
                normalized.append(entry)
            return normalized
        if isinstance(section, list):
            return section
        return []

    filename_map = {
        "e v1/v2": "e_v12.yaml",
        "e v3": "e_v3.yaml",
        "d": "d.yaml",
        "a": "a.yaml",
    }

    yaml_filename = filename_map.get(version)
    if yaml_filename:
        yaml_path = Path(__file__).parent / "registers" / yaml_filename
        if yaml_path.exists():
            try:
                import yaml

                with open(yaml_path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}

                return {
                    "SENSOR_DEFINITIONS": _normalize_section(data.get("SENSOR_DEFINITIONS")),
                    "BINARY_SENSOR_DEFINITIONS": _normalize_section(data.get("BINARY_SENSOR_DEFINITIONS")),
                    "SELECT_DEFINITIONS": _normalize_section(data.get("SELECT_DEFINITIONS")),
                    "SWITCH_DEFINITIONS": _normalize_section(data.get("SWITCH_DEFINITIONS")),
                    "NUMBER_DEFINITIONS": _normalize_section(data.get("NUMBER_DEFINITIONS")),
                    "BUTTON_DEFINITIONS": _normalize_section(data.get("BUTTON_DEFINITIONS")),
                    "EFFICIENCY_SENSOR_DEFINITIONS": _normalize_section(
                        data.get("EFFICIENCY_SENSOR_DEFINITIONS")
                    ),
                    "STORED_ENERGY_SENSOR_DEFINITIONS": _normalize_section(
                        data.get("STORED_ENERGY_SENSOR_DEFINITIONS")
                    ),
                    "CYCLE_SENSOR_DEFINITIONS": _normalize_section(
                        data.get("CYCLE_SENSOR_DEFINITIONS")
                    ),
                }
            except Exception as e:
                _LOGGER.warning("Failed to load YAML registers %s: %s", yaml_path, e)
