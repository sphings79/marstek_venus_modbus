"""
Modbus Block-Reader für die Marstek Venus Integration.

Optimiert die Register-Abfrage, indem nahe beieinander liegende Register
zu einzelnen Bulk-Reads zusammengefasst werden. Reduziert die Anzahl der
TCP-Roundtrips zum RS485-Gateway deutlich.

Vorher: 1 TCP-Request pro Register/Sensor (~15–25 Requests pro Poll-Zyklus)
Nachher: 1 TCP-Request pro zusammenhängendem Register-Block (~3–5 Requests)

Kompatibel mit pymodbus >= 3.x (AsyncModbusTcpClient).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException, ModbusIOException

_LOGGER = logging.getLogger(__name__)

# Modbus FC03 erlaubt max. 125 Holding-Register pro Request (Spec: 0x7D)
MODBUS_MAX_REGISTERS_PER_REQUEST = 125

# Standard-Lückengröße: Lücken ≤ diesem Wert werden überbrückt.
# 5 ist ein guter Kompromiss für das Marstek-Register-Layout.
DEFAULT_MAX_GAP = 5


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------


@dataclass
class RegisterRequest:
    """
    Beschreibt einen einzelnen anzufordernden Register-Bereich.

    Attributes:
        address:  Modbus-Registeradresse (wie im Gerät dokumentiert, z.B. 32100).
                  Wird intern als 0-basierter Offset behandelt (address - 1 bei
                  manchen pymodbus-Versionen — wird im Reader korrekt behandelt).
        count:    Anzahl der 16-bit-Register (uint16 = 1, int32/uint32 = 2).
        key:      Eindeutiger Schlüssel im Ergebnis-Dict (z.B. "battery_voltage").
        signed:   True wenn der Wert als vorzeichenbehaftete Zahl interpretiert
                  werden soll (int16, int32).
        scale:    Multiplikator für den Rohwert (z.B. 0.01 für Volt-Werte mit
                  2 Dezimalstellen).
        data_type: Hinweis auf den Datentyp. Unterstützt: "int", "string".
                   Bei "string" wird der Rohwert als Liste zurückgegeben.
    """

    address: int
    count: int
    key: str
    signed: bool = False
    scale: float = 1.0
    data_type: str = "int"  # "int" | "string"

    @property
    def end_address(self) -> int:
        """Letzte (inklusive) Registeradresse dieses Requests."""
        return self.address + self.count - 1


@dataclass
class _ReadBlock:
    """
    Interne Darstellung eines zusammengefassten Block-Requests.
    Mehrere RegisterRequests, deren Adressen nah genug beieinander liegen,
    werden zu einem _ReadBlock zusammengefasst.
    """

    start: int
    end: int  # inklusive letzte Adresse
    requests: list[RegisterRequest] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Gesamtzahl der Register im Block (inkl. überbrückte Lücken)."""
        return self.end - self.start + 1

    def try_merge(
        self, req: RegisterRequest, max_gap: int
    ) -> bool:
        """
        Versucht, einen Request in diesen Block aufzunehmen.

        Returns:
            True wenn der Request aufgenommen wurde, False wenn ein neuer
            Block angelegt werden muss.
        """
        gap = req.address - self.end - 1
        potential_end = req.end_address
        potential_count = potential_end - self.start + 1

        if gap <= max_gap and potential_count <= MODBUS_MAX_REGISTERS_PER_REQUEST:
            self.end = max(self.end, potential_end)
            self.requests.append(req)
            return True
        return False


# ---------------------------------------------------------------------------
# Kernlogik: Block-Aufbau
# ---------------------------------------------------------------------------


def build_read_blocks(
    requests: list[RegisterRequest],
    max_gap: int = DEFAULT_MAX_GAP,
) -> list[_ReadBlock]:
    """
    Gruppiert eine Liste von RegisterRequests in optimierte Read-Blöcke.

    Algorithmus:
    1. Requests nach Startadresse sortieren.
    2. Einen neuen Block anlegen.
    3. Jeden weiteren Request in den letzten Block aufnehmen, wenn:
       - Die Lücke zum vorherigen Ende ≤ max_gap ist UND
       - Der resultierende Block ≤ MODBUS_MAX_REGISTERS_PER_REQUEST Register hat.
    4. Sonst: neuen Block starten.

    Args:
        requests: Liste der anzufordernden Register.
        max_gap:  Maximale Lücke (in Registern) die überbrückt wird.
                  Lückenregister werden mitgelesen, ihr Wert aber ignoriert.

    Returns:
        Optimierte Liste von _ReadBlock-Objekten.
    """
    if not requests:
        return []

    sorted_reqs = sorted(requests, key=lambda r: r.address)
    blocks: list[_ReadBlock] = []

    for req in sorted_reqs:
        if not blocks or not blocks[-1].try_merge(req, max_gap):
            blocks.append(
                _ReadBlock(
                    start=req.address,
                    end=req.end_address,
                    requests=[req],
                )
            )

    return blocks


# ---------------------------------------------------------------------------
# Wert-Extraktion
# ---------------------------------------------------------------------------


def _extract_value(
    registers: list[int],
    offset: int,
    req: RegisterRequest,
) -> Any:
    """
    Extrahiert und konvertiert einen Wert aus dem Register-Array.

    Args:
        registers: Rohes Register-Array aus der Modbus-Antwort.
        offset:    Start-Index in registers für diesen Request.
        req:       Der ursprüngliche RegisterRequest.

    Returns:
        Konvertierter Wert (float, int oder list[int] für Strings).

    Raises:
        IndexError: Wenn der Offset außerhalb des Arrays liegt.
        ValueError: Wenn count einen unbekannten Wert hat.
    """
    raw_regs = registers[offset : offset + req.count]

    if req.data_type == "string":
        return raw_regs

    if req.count == 1:
        raw = raw_regs[0]
        if req.signed and raw > 0x7FFF:
            raw -= 0x10000

    elif req.count == 2:
        # Big-Endian Word-Order (high word zuerst — Marstek-Standard)
        raw = (raw_regs[0] << 16) | raw_regs[1]
        if req.signed and raw > 0x7FFFFFFF:
            raw -= 0x100000000

    else:
        raise ValueError(
            f"Unsupported register count {req.count} for key '{req.key}'. "
            "Use data_type='string' for multi-register strings."
        )

    return raw * req.scale


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------


async def bulk_read_registers(
    client: AsyncModbusTcpClient,
    slave_id: int,
    requests: list[RegisterRequest],
    max_gap: int = DEFAULT_MAX_GAP,
) -> dict[str, Any]:
    """
    Liest alle angeforderten Register in optimierten Block-Reads.

    Anstatt jeden Register einzeln abzufragen, werden nahe beieinander
    liegende Register zu einzelnen TCP-Requests zusammengefasst.

    Args:
        client:    Bereits verbundener AsyncModbusTcpClient.
        slave_id:  Modbus Slave/Unit ID des Geräts.
        requests:  Liste der anzufordernden Register (RegisterRequest).
        max_gap:   Maximale Lücke zwischen Registern, die überbrückt wird.
                   Größerer Wert → weniger Requests, aber mehr unnötig gelesene Daten.
                   Empfehlung: 5 für Marstek Venus.

    Returns:
        Dict { key: wert } für alle erfolgreich gelesenen Register.
        Fehlgeschlagene Register/Blöcke fehlen im Dict (kein KeyError-Risiko).

    Example:
        >>> requests = [
        ...     RegisterRequest(32100, 1, "battery_voltage", scale=0.01),
        ...     RegisterRequest(32101, 1, "battery_current", signed=True, scale=0.01),
        ...     RegisterRequest(32102, 2, "battery_power",   signed=True),
        ... ]
        >>> data = await bulk_read_registers(client, slave_id=1, requests=requests)
        >>> print(data["battery_voltage"])  # z.B. 51.2
    """
    blocks = build_read_blocks(requests, max_gap)

    total_reqs = len(requests)
    total_blocks = len(blocks)
    _LOGGER.debug(
        "Bulk-Read: %d Register-Requests → %d TCP-Blocks (max_gap=%d, slave=%d)",
        total_reqs,
        total_blocks,
        max_gap,
        slave_id,
    )

    result: dict[str, Any] = {}

    for block in blocks:
        _LOGGER.debug(
            "  Block [%d..%d] count=%d, enthält %d Requests",
            block.start,
            block.end,
            block.count,
            len(block.requests),
        )

        try:
            response = await client.read_holding_registers(
                address=block.start,
                count=block.count,
                slave=slave_id,
            )
        except (ModbusException, ModbusIOException, OSError) as exc:
            _LOGGER.warning(
                "Block-Read [%d..%d] fehlgeschlagen: %s — betroffene Keys: %s",
                block.start,
                block.end,
                exc,
                [r.key for r in block.requests],
            )
            continue

        if response.isError():
            _LOGGER.warning(
                "Modbus-Fehler bei Block [%d..%d]: %s — betroffene Keys: %s",
                block.start,
                block.end,
                response,
                [r.key for r in block.requests],
            )
            continue

        registers = response.registers

        # Einzelne Werte aus dem Block-Array extrahieren
        for req in block.requests:
            offset = req.address - block.start

            # Plausibilitätsprüfung
            if offset < 0 or offset + req.count > len(registers):
                _LOGGER.error(
                    "Offset %d+%d außerhalb Block-Array (len=%d) für Key '%s' "
                    "— Block [%d..%d]",
                    offset,
                    req.count,
                    len(registers),
                    req.key,
                    block.start,
                    block.end,
                )
                continue

            try:
                result[req.key] = _extract_value(registers, offset, req)
            except (ValueError, IndexError) as exc:
                _LOGGER.error(
                    "Fehler beim Extrahieren von '%s': %s",
                    req.key,
                    exc,
                )

    _LOGGER.debug(
        "Bulk-Read abgeschlossen: %d/%d Keys erfolgreich gelesen",
        len(result),
        total_reqs,
    )
    return result


async def bulk_read_registers_with_fallback(
    client: AsyncModbusTcpClient,
    slave_id: int,
    requests: list[RegisterRequest],
    max_gap: int = DEFAULT_MAX_GAP,
) -> dict[str, Any]:
    """
    Wie bulk_read_registers, aber mit Einzelread-Fallback bei Block-Fehlern.

    Wenn ein ganzer Block fehlschlägt (z.B. wegen einer ungültigen Adresse im
    Block), werden die einzelnen Requests des Blocks separat wiederholt.
    Das ist besonders nützlich beim ersten Start, wenn noch nicht bekannt ist,
    welche Register das Gerät unterstützt.

    Performance-Hinweis: Der Fallback aktiviert sich nur bei Fehlern.
    Im Normalbetrieb ist das Verhalten identisch mit bulk_read_registers.
    """
    blocks = build_read_blocks(requests, max_gap)
    result: dict[str, Any] = {}

    for block in blocks:
        try:
            response = await client.read_holding_registers(
                address=block.start,
                count=block.count,
                slave=slave_id,
            )
        except (ModbusException, ModbusIOException, OSError) as exc:
            _LOGGER.warning(
                "Block-Read [%d..%d] fehlgeschlagen (%s) — "
                "Fallback auf Einzelreads für %d Requests",
                block.start,
                block.end,
                exc,
                len(block.requests),
            )
            # Einzelreads als Fallback
            for req in block.requests:
                try:
                    single = await client.read_holding_registers(
                        address=req.address,
                        count=req.count,
                        slave=slave_id,
                    )
                    if not single.isError():
                        result[req.key] = _extract_value(
                            single.registers, 0, req
                        )
                    else:
                        _LOGGER.debug(
                            "Einzelread '%s' @ %d: Modbus-Fehler %s",
                            req.key,
                            req.address,
                            single,
                        )
                except Exception as single_exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "Einzelread '%s' @ %d: %s", req.key, req.address, single_exc
                    )
            continue

        if response.isError():
            _LOGGER.warning(
                "Modbus-Fehler bei Block [%d..%d]: %s — Fallback auf Einzelreads",
                block.start,
                block.end,
                response,
            )
            for req in block.requests:
                try:
                    single = await client.read_holding_registers(
                        address=req.address,
                        count=req.count,
                        slave=slave_id,
                    )
                    if not single.isError():
                        result[req.key] = _extract_value(
                            single.registers, 0, req
                        )
                except Exception:  # noqa: BLE001
                    pass
            continue

        for req in block.requests:
            offset = req.address - block.start
            if offset + req.count > len(response.registers):
                continue
            try:
                result[req.key] = _extract_value(response.registers, offset, req)
            except (ValueError, IndexError) as exc:
                _LOGGER.error("Fehler beim Extrahieren von '%s': %s", req.key, exc)

    return result
