"""
Marstek Venus — zentrale Register-Definitionen für den Block-Reader.

Diese Datei definiert alle RegisterRequests geordnet nach:
  - Gerätevariante (v12 = Venus E v1/v2, v3 = Venus E v3, d = Venus D)
  - Abfrage-Priorität (high / medium / low / very_low)

Die Gruppen können direkt an bulk_read_registers() übergeben werden.
Lücken innerhalb einer Gruppe werden durch den Block-Reader automatisch
überbrückt (max_gap=5 Standard).

Adressierungsschema: Alle Adressen sind wie in der offiziellen Marstek-
Dokumentation angegeben (z.B. 32100). Der Block-Reader übergibt diese
direkt an pymodbus (keine -1 Offset-Korrektur nötig bei aktuellen Versionen).
"""

from __future__ import annotations

from .block_reader import RegisterRequest

# ---------------------------------------------------------------------------
# Venus E v1 / v2  (Reg12-Spalte aus dem README)
# ---------------------------------------------------------------------------

REGISTERS_V12_HIGH: list[RegisterRequest] = [
    # ---- Batterie-Kern (32100–32105, 6 Register, 1 Block) ----
    RegisterRequest(32100, 1, "battery_voltage",       signed=False, scale=0.01),    # V
    RegisterRequest(32101, 1, "battery_current",       signed=True,  scale=0.01),    # A
    RegisterRequest(32102, 2, "battery_power",         signed=True,  scale=1.0),     # W (int32)
    RegisterRequest(32104, 1, "battery_soc",           signed=False, scale=1.0),     # %
    RegisterRequest(32105, 1, "battery_total_energy",  signed=False, scale=0.001),   # kWh

    # ---- AC-Seite (32200–32204, 5 Register, 1 Block) ----
    RegisterRequest(32200, 1, "ac_voltage",            signed=False, scale=0.1),     # V
    RegisterRequest(32201, 1, "ac_current",            signed=True,  scale=0.01),    # A
    RegisterRequest(32202, 2, "ac_power",              signed=True,  scale=1.0),     # W (int32)
    RegisterRequest(32204, 1, "ac_frequency",          signed=True,  scale=0.01),    # Hz

    # ---- Wechselrichter-Status ----
    RegisterRequest(35100, 1, "inverter_state",        signed=False, scale=1.0),
]

REGISTERS_V12_MEDIUM: list[RegisterRequest] = [
    # ---- Temperaturen (35000–35011, mit Lücke 35003–35009) ----
    RegisterRequest(35000, 1, "temp_internal",         signed=True,  scale=0.1),     # °C
    RegisterRequest(35001, 1, "temp_mos1",             signed=True,  scale=0.1),     # °C
    RegisterRequest(35002, 1, "temp_mos2",             signed=True,  scale=0.1),     # °C
    RegisterRequest(35010, 1, "temp_cell_max",         signed=True,  scale=1.0),     # °C
    RegisterRequest(35011, 1, "temp_cell_min",         signed=True,  scale=1.0),     # °C

    # ---- Zellspannungen Min/Max ----
    RegisterRequest(37007, 1, "cell_voltage_max",      signed=False, scale=0.001),   # V
    RegisterRequest(37008, 1, "cell_voltage_min",      signed=False, scale=0.001),   # V

    # ---- AC-Offgrid (32300–32302) ----
    RegisterRequest(32300, 1, "ac_offgrid_voltage",    signed=False, scale=0.1),     # V
    RegisterRequest(32301, 1, "ac_offgrid_current",    signed=False, scale=0.01),    # A
    RegisterRequest(32302, 2, "ac_offgrid_power",      signed=True,  scale=1.0),     # W

    # ---- WiFi / Cloud-Status (30300–30303) ----
    RegisterRequest(30300, 1, "wifi_status",           signed=False, scale=1.0),
    RegisterRequest(30302, 1, "cloud_status",          signed=False, scale=1.0),
    RegisterRequest(30303, 1, "wifi_signal_strength",  signed=True,  scale=1.0),     # dBm
]

REGISTERS_V12_LOW: list[RegisterRequest] = [
    # ---- Energie-Totals (33000–33010, 11 Register, 1 Block) ----
    RegisterRequest(33000, 2, "total_charge_energy",          signed=False, scale=0.01),  # kWh
    RegisterRequest(33002, 2, "total_discharge_energy",       signed=True,  scale=0.01),  # kWh
    RegisterRequest(33004, 2, "daily_charge_energy",          signed=False, scale=0.01),  # kWh
    RegisterRequest(33006, 2, "daily_discharge_energy",       signed=True,  scale=0.01),  # kWh
    RegisterRequest(33008, 2, "monthly_charge_energy",        signed=False, scale=0.01),  # kWh
    RegisterRequest(33010, 2, "monthly_discharge_energy",     signed=True,  scale=0.01),  # kWh

    # ---- Alarm / Fault-Status ----
    RegisterRequest(36000, 2, "alarm_status",                 signed=False, scale=1.0),
    RegisterRequest(36100, 4, "fault_status",                 signed=False, data_type="string"),

    # ---- Grenzwerte (44000–44003, 4 Register, 1 Block) ----
    RegisterRequest(44000, 1, "charge_cutoff_capacity",       signed=False, scale=0.1),   # %
    RegisterRequest(44001, 1, "discharge_cutoff_capacity",    signed=False, scale=0.1),   # %
    RegisterRequest(44002, 1, "max_charge_power",             signed=False, scale=1.0),   # W
    RegisterRequest(44003, 1, "max_discharge_power",          signed=False, scale=1.0),   # W
]

REGISTERS_V12_VERY_LOW: list[RegisterRequest] = [
    # ---- Versionsinformationen ----
    RegisterRequest(30399, 1, "bms_version",                  signed=False, scale=1.0),
    RegisterRequest(30401, 1, "ems_version",                  signed=False, scale=1.0),
    RegisterRequest(31100, 1, "software_version",             signed=False, scale=0.01),

    # ---- Geräte-Info (Strings) ----
    RegisterRequest(30402, 6, "mac_address",                  data_type="string"),
    RegisterRequest(30800, 6, "comm_firmware",                data_type="string"),
    RegisterRequest(31000, 10,"device_name",                  data_type="string"),
    RegisterRequest(31200, 10,"serial_number",                data_type="string"),
    RegisterRequest(41100, 1, "modbus_address",               signed=False, scale=1.0),

    # ---- Entlade-Limit ----
    RegisterRequest(41010, 1, "discharge_limit",              signed=False, scale=1.0),
]

# ---- Control-Register (werden separat bei Bedarf gelesen) ----
REGISTERS_V12_CONTROL: list[RegisterRequest] = [
    RegisterRequest(41200, 1, "backup_function",              signed=False, scale=1.0),
    RegisterRequest(42000, 1, "rs485_control_mode",           signed=False, scale=1.0),
    RegisterRequest(42010, 1, "force_mode",                   signed=False, scale=1.0),
    RegisterRequest(42011, 1, "charge_to_soc",                signed=False, scale=1.0),   # %
    RegisterRequest(42020, 1, "forcible_charge_power",        signed=False, scale=1.0),   # W
    RegisterRequest(42021, 1, "forcible_discharge_power",     signed=False, scale=1.0),   # W
    RegisterRequest(43000, 1, "user_work_mode",               signed=False, scale=1.0),
    RegisterRequest(44100, 1, "grid_standard",                signed=False, scale=1.0),
]

# ---------------------------------------------------------------------------
# Venus E v3  (Reg3-Spalte aus dem README)
# ---------------------------------------------------------------------------

REGISTERS_V3_HIGH: list[RegisterRequest] = [
    # ---- Batterie-Kern ----
    RegisterRequest(30100, 1, "battery_voltage",       signed=False, scale=0.01),    # V
    RegisterRequest(30101, 1, "battery_current",       signed=True,  scale=0.01),    # A
    RegisterRequest(30001, 1, "battery_power",         signed=True,  scale=1.0),     # W (int16 bei v3)
    RegisterRequest(37005, 1, "battery_soc",           signed=False, scale=1.0),     # %

    # ---- AC-Seite (wie v12) ----
    RegisterRequest(32200, 1, "ac_voltage",            signed=False, scale=0.1),
    RegisterRequest(32201, 1, "ac_current",            signed=True,  scale=0.01),
    RegisterRequest(30006, 1, "ac_power",              signed=True,  scale=1.0),     # W (int16 bei v3)
    RegisterRequest(32204, 1, "ac_frequency",          signed=True,  scale=0.01),

    # ---- Wechselrichter-Status ----
    RegisterRequest(35100, 1, "inverter_state",        signed=False, scale=1.0),
]

REGISTERS_V3_MEDIUM: list[RegisterRequest] = [
    # ---- Temperaturen (identisch zu v12) ----
    RegisterRequest(35000, 1, "temp_internal",         signed=True,  scale=0.1),
    RegisterRequest(35001, 1, "temp_mos1",             signed=True,  scale=0.1),
    RegisterRequest(35002, 1, "temp_mos2",             signed=True,  scale=0.1),
    RegisterRequest(35010, 1, "temp_cell_max",         signed=True,  scale=1.0),
    RegisterRequest(35011, 1, "temp_cell_min",         signed=True,  scale=1.0),

    # ---- Zellspannungen (v3: 34018–34033, 16 Zellen, 1 Block!) ----
    RegisterRequest(34018, 1, "cell_voltage_1",        signed=True,  scale=0.001),
    RegisterRequest(34019, 1, "cell_voltage_2",        signed=True,  scale=0.001),
    RegisterRequest(34020, 1, "cell_voltage_3",        signed=True,  scale=0.001),
    RegisterRequest(34021, 1, "cell_voltage_4",        signed=True,  scale=0.001),
    RegisterRequest(34022, 1, "cell_voltage_5",        signed=True,  scale=0.001),
    RegisterRequest(34023, 1, "cell_voltage_6",        signed=True,  scale=0.001),
    RegisterRequest(34024, 1, "cell_voltage_7",        signed=True,  scale=0.001),
    RegisterRequest(34025, 1, "cell_voltage_8",        signed=True,  scale=0.001),
    RegisterRequest(34026, 1, "cell_voltage_9",        signed=True,  scale=0.001),
    RegisterRequest(34027, 1, "cell_voltage_10",       signed=True,  scale=0.001),
    RegisterRequest(34028, 1, "cell_voltage_11",       signed=True,  scale=0.001),
    RegisterRequest(34029, 1, "cell_voltage_12",       signed=True,  scale=0.001),
    RegisterRequest(34030, 1, "cell_voltage_13",       signed=True,  scale=0.001),
    RegisterRequest(34031, 1, "cell_voltage_14",       signed=True,  scale=0.001),
    RegisterRequest(34032, 1, "cell_voltage_15",       signed=True,  scale=0.001),
    RegisterRequest(34033, 1, "cell_voltage_16",       signed=True,  scale=0.001),

    # ---- WiFi / Cloud ----
    RegisterRequest(30300, 1, "wifi_status",           signed=False, scale=1.0),
    RegisterRequest(30302, 1, "cloud_status",          signed=False, scale=1.0),
    RegisterRequest(30303, 1, "wifi_signal_strength",  signed=True,  scale=1.0),
]

REGISTERS_V3_LOW: list[RegisterRequest] = [
    # ---- Energie-Totals (identisch zu v12) ----
    RegisterRequest(33000, 2, "total_charge_energy",          signed=False, scale=0.01),
    RegisterRequest(33002, 2, "total_discharge_energy",       signed=True,  scale=0.01),
    RegisterRequest(33004, 2, "daily_charge_energy",          signed=False, scale=0.01),
    RegisterRequest(33006, 2, "daily_discharge_energy",       signed=True,  scale=0.01),
    RegisterRequest(33008, 2, "monthly_charge_energy",        signed=False, scale=0.01),
    RegisterRequest(33010, 2, "monthly_discharge_energy",     signed=True,  scale=0.01),

    # ---- Grenzwerte ----
    RegisterRequest(44002, 1, "max_charge_power",             signed=False, scale=1.0),
    RegisterRequest(44003, 1, "max_discharge_power",          signed=False, scale=1.0),
]

REGISTERS_V3_VERY_LOW: list[RegisterRequest] = [
    RegisterRequest(30204, 1, "bms_version",                  signed=False, scale=1.0),
    RegisterRequest(30202, 1, "vms_version",                  signed=False, scale=1.0),
    RegisterRequest(30200, 1, "ems_version",                  signed=False, scale=1.0),
    RegisterRequest(30304, 6, "mac_address",                  data_type="string"),
    RegisterRequest(30350, 6, "comm_firmware",                data_type="string"),
    RegisterRequest(31000, 10,"device_name",                  data_type="string"),
    RegisterRequest(41100, 1, "modbus_address",               signed=False, scale=1.0),
]

REGISTERS_V3_CONTROL: list[RegisterRequest] = [
    RegisterRequest(41200, 1, "backup_function",              signed=False, scale=1.0),
    RegisterRequest(42000, 1, "rs485_control_mode",           signed=False, scale=1.0),
    RegisterRequest(42010, 1, "force_mode",                   signed=False, scale=1.0),
    RegisterRequest(42011, 1, "charge_to_soc",                signed=False, scale=1.0),
    RegisterRequest(42020, 1, "forcible_charge_power",        signed=False, scale=1.0),
    RegisterRequest(42021, 1, "forcible_discharge_power",     signed=False, scale=1.0),
    RegisterRequest(43000, 1, "user_work_mode",               signed=False, scale=1.0),
]

# ---------------------------------------------------------------------------
# Hilfsfunktion: Register-Set nach Geräteversion auswählen
# ---------------------------------------------------------------------------


def get_register_sets(
    device_version: str,
) -> dict[str, list[RegisterRequest]]:
    """
    Gibt das passende Register-Set für die angegebene Geräteversion zurück.

    Args:
        device_version: "v12" für Venus E v1/v2, "v3" für Venus E v3,
                        "d" für Venus D (fallback auf v12).

    Returns:
        Dict mit Schlüsseln "high", "medium", "low", "very_low", "control".
    """
    if device_version == "v3":
        return {
            "high":      REGISTERS_V3_HIGH,
            "medium":    REGISTERS_V3_MEDIUM,
            "low":       REGISTERS_V3_LOW,
            "very_low":  REGISTERS_V3_VERY_LOW,
            "control":   REGISTERS_V3_CONTROL,
        }
    # v12 und Venus D (als Fallback)
    return {
        "high":      REGISTERS_V12_HIGH,
        "medium":    REGISTERS_V12_MEDIUM,
        "low":       REGISTERS_V12_LOW,
        "very_low":  REGISTERS_V12_VERY_LOW,
        "control":   REGISTERS_V12_CONTROL,
    }
