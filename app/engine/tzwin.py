"""Windows time zones: key name -> IANA (CLDR windowsZones, territory 001),
TZDEFINITION / TZSTRUCT parsing and a tzinfo built from Windows rules."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

WIN_TO_IANA = {
    "Dateline Standard Time": "Etc/GMT+12", "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak", "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas", "Alaskan Standard Time": "America/Anchorage",
    "UTC-09": "Etc/GMT+9", "Pacific Standard Time (Mexico)": "America/Tijuana",
    "UTC-08": "Etc/GMT+8", "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix", "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver", "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala", "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter", "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina", "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun", "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince", "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indianapolis", "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion", "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas", "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz", "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns", "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo", "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Buenos_Aires", "Greenland Standard Time": "America/Godthab",
    "Montevideo Standard Time": "America/Montevideo", "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon", "Bahia Standard Time": "America/Bahia",
    "UTC-02": "Etc/GMT+2", "Mid-Atlantic Standard Time": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores", "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "Etc/UTC", "Coordinated Universal Time": "Etc/UTC", "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik", "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca", "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest", "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw", "W. Central Africa Standard Time": "Africa/Lagos",
    "Jordan Standard Time": "Asia/Amman", "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut", "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau", "Syria Standard Time": "Asia/Damascus",
    "West Bank Standard Time": "Asia/Hebron", "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kiev", "Israel Standard Time": "Asia/Jerusalem",
    "South Sudan Standard Time": "Africa/Juba", "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum", "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek", "Arabic Standard Time": "Asia/Baghdad",
    "Turkey Standard Time": "Europe/Istanbul", "Arab Standard Time": "Asia/Riyadh",
    "Belarus Standard Time": "Europe/Minsk", "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi", "Volgograd Standard Time": "Europe/Volgograd",
    "Iran Standard Time": "Asia/Tehran", "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan", "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara", "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov", "Georgian Standard Time": "Asia/Tbilisi",
    "Caucasus Standard Time": "Asia/Yerevan", "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent", "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi", "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "India Standard Time": "Asia/Calcutta", "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Katmandu", "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka", "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Rangoon", "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul", "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk", "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk", "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk", "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth", "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar", "Aus Central W. Standard Time": "Australia/Eucla",
    "Transbaikal Standard Time": "Asia/Chita", "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang", "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk", "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin", "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney", "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Tasmania Standard Time": "Australia/Hobart", "Vladivostok Standard Time": "Asia/Vladivostok",
    "Lord Howe Standard Time": "Australia/Lord_Howe", "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk", "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk", "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal", "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland", "UTC+12": "Etc/GMT-12",
    "Fiji Standard Time": "Pacific/Fiji", "Chatham Islands Standard Time": "Pacific/Chatham",
    "UTC+13": "Etc/GMT-13", "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia", "Line Islands Standard Time": "Pacific/Kiritimati",
    "Mountain Standard Time (Mexico) ": "America/Mazatlan",
}

# A few frequent display names (PidLidTimeZoneDescription) -> key names.
DISPLAY_HINTS = {
    "brussels": "Romance Standard Time", "paris": "Romance Standard Time",
    "amsterdam": "W. Europe Standard Time", "berlin": "W. Europe Standard Time",
    "belgrade": "Central Europe Standard Time", "sarajevo": "Central European Standard Time",
    "london": "GMT Standard Time", "dublin": "GMT Standard Time",
    "eastern time": "Eastern Standard Time", "central time": "Central Standard Time",
    "mountain time": "Mountain Standard Time", "pacific time": "Pacific Standard Time",
    "athens": "GTB Standard Time", "helsinki": "FLE Standard Time", "moscow": "Russian Standard Time",
    "casablanca": "Morocco Standard Time", "tokyo": "Tokyo Standard Time",
    "beijing": "China Standard Time", "sydney": "AUS Eastern Standard Time",
    "montreal": "Eastern Standard Time", "quebec": "Eastern Standard Time",
    "coordinated universal time": "UTC", "monrovia": "Greenwich Standard Time",
}


def iana_for(key: str | None, description: str | None = None) -> str | None:
    if key:
        k = key.strip()
        if k in WIN_TO_IANA:
            return WIN_TO_IANA[k]
        if "/" in k:  # already IANA
            return k
    if description:
        d = description.lower()
        for hint, keyname in DISPLAY_HINTS.items():
            if hint in d:
                return WIN_TO_IANA.get(keyname)
    return None


@dataclass
class SysTime:
    year: int
    month: int
    dow: int
    day: int     # week-of-month (1..5) when year == 0
    hour: int
    minute: int
    second: int

    @classmethod
    def parse(cls, b: bytes, off: int) -> "SysTime":
        y, mo, dow, d, h, mi, s, _ms = struct.unpack_from("<8H", b, off)
        return cls(y, mo, dow, d, h, mi, s)


@dataclass
class WinRule:
    bias: int            # minutes, UTC = local + bias
    std_bias: int
    dst_bias: int
    std: SysTime         # transition to standard time
    dst: SysTime         # transition to daylight time

    @property
    def has_dst(self) -> bool:
        return self.std.month != 0 and self.dst.month != 0


def parse_tzstruct(b: bytes | None) -> WinRule | None:
    """PidLidTimeZoneStruct (48 bytes)."""
    if not b or len(b) < 48:
        return None
    bias, sbias, dbias = struct.unpack_from("<iii", b, 0)
    std = SysTime.parse(b, 14)
    dst = SysTime.parse(b, 32)
    return WinRule(bias, sbias, dbias, std, dst)


def parse_tzdefinition(b: bytes | None) -> tuple[str | None, WinRule | None]:
    """PidLidAppointmentTimeZoneDefinition* (TZDEFINITION). Returns (key, rule)."""
    if not b or len(b) < 8:
        return None, None
    try:
        major, minor, cb_header, _reserved, cch = struct.unpack_from("<BBHHH", b, 0)
        if major != 2:
            return None, None
        key = b[8:8 + cch * 2].decode("utf-16-le", "replace")
        off = 8 + cch * 2          # cRules follows KeyName (cbHeader includes it)
        crules = struct.unpack_from("<H", b, off)[0]
        off += 2
        rules = []
        for _ in range(crules):
            # major, minor, reserved, flags, year, x[14], bias, stdbias, dstbias, std[16], dst[16]
            year = struct.unpack_from("<H", b, off + 6)[0]
            bias, sbias, dbias = struct.unpack_from("<iii", b, off + 22)
            std = SysTime.parse(b, off + 34)
            dst = SysTime.parse(b, off + 50)
            flags = struct.unpack_from("<H", b, off + 4)[0]
            rules.append((flags, year, WinRule(bias, sbias, dbias, std, dst)))
            off += 66
        if not rules:
            return key, None
        effective = [r for r in rules if r[0] & 0x0002]
        return key, (effective[0] if effective else rules[-1])[2]
    except struct.error:
        return None, None


def _nth_weekday(year, month, dow, nth, hour, minute, second):
    """Windows 'day' n-th (5 = last) weekday of month."""
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    first = datetime(year, month, 1)
    delta = (dow - (first.weekday() + 1) % 7) % 7
    d = first + timedelta(days=delta + 7 * (nth - 1))
    while d >= nxt:
        d -= timedelta(days=7)
    return d.replace(hour=hour, minute=minute, second=second)


class WinTZ(tzinfo):
    """tzinfo from a Windows rule (annual, non-absolute transitions)."""

    def __init__(self, rule: WinRule, name: str = "Custom"):
        self.rule = rule
        self.name = name

    def _transitions(self, year):
        r = self.rule
        dst = _nth_weekday(year, r.dst.month, r.dst.dow, r.dst.day or 1, r.dst.hour, r.dst.minute, r.dst.second)
        std = _nth_weekday(year, r.std.month, r.std.dow, r.std.day or 1, r.std.hour, r.std.minute, r.std.second)
        return dst, std

    def _is_dst(self, dt):
        if not self.rule.has_dst:
            return False
        naive = dt.replace(tzinfo=None)
        dst, std = self._transitions(naive.year)
        if dst < std:          # northern hemisphere
            return dst <= naive < std
        return not (std <= naive < dst)

    def utcoffset(self, dt):
        r = self.rule
        b = r.bias + (r.dst_bias if self._is_dst(dt) else r.std_bias)
        return timedelta(minutes=-b)

    def dst(self, dt):
        return timedelta(minutes=-self.rule.dst_bias) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return self.name

    def offsets(self):
        r = self.rule
        return timedelta(minutes=-(r.bias + r.std_bias)), timedelta(minutes=-(r.bias + r.dst_bias))
