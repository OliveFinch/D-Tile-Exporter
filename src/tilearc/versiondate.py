"""Working out when a map version is from.

A version is identified by a server code, and the codes are not ordered. WDW
runs 47, 105, 106 ... and then jumps to 900014458 and 671203034, which are
neither sequential nor comparable with what came before. So sorting an archive
by code tells you nothing about which map came first -- and with ninety of them
spanning nine years, which came first is most of what you want to know.

The date is not recorded as a date anywhere. It lives in the human label the
viewer shows ("May '17", "Aug '18 (Late)", "April 2019"), and sometimes in the
code itself, where Tokyo uses a timestamp. A park with no version history is
filed under the date its snapshot was taken, so there the folder name *is* the
date.

Three places to look, then, and for some versions -- Hong Kong lists "Unknown
1" through "Unknown 9" -- there is no answer at all. That stays an answer:
better an empty cell than a date nobody can stand behind.
"""

from __future__ import annotations

import re

__all__ = ["version_date", "version_sort_key", "UNDATED_SORT_KEY"]

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

#: A dated snapshot folder: what `archive_version` writes for a park with no
#: selectable versions.
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: Tokyo's codes are `YYYYMMDDHHMMSS`. The year check below is what stops a
#: nine-digit serial like 900014458 being read as the year 9000.
_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})\d*$")

#: "May '17", "Aug '18 (Late)", "April 2019". The apostrophe is optional and
#: the trailing qualifier is ignored -- it orders two maps within a month, it
#: does not date them.
_MONTH_YEAR = re.compile(r"([A-Za-z]+)\s*'?\s*(\d{2,4})")

#: Sorts after every real date, so undated versions collect at one end instead
#: of being scattered through the run they belong to.
UNDATED_SORT_KEY = "9999"


def _year(digits: str) -> int | None:
    value = int(digits)
    if len(digits) == 4:
        return value if 2000 <= value <= 2100 else None
    if len(digits) == 2:
        # No park map predates 2000, and none will reach 2070.
        return 2000 + value if value < 70 else None
    return None


def version_date(version: str | None, label: str | None = None) -> str | None:
    """When this version's map is from, as ``YYYY-MM`` or ``YYYY-MM-DD``.

    ``None`` when nothing says. The version code is tried before the label
    because a code that is a date is exact, where a label is a month at best.
    """
    code = (version or "").strip()

    match = _ISO.match(code)
    if match:
        year, month, day = (int(part) for part in match.groups())
        if 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
            return code

    match = _STAMP.match(code)
    if match:
        year = _year(match.group(1))
        month, day = int(match.group(2)), int(match.group(3))
        if year and 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"

    for name, digits in _MONTH_YEAR.findall(label or ""):
        month = _MONTHS.get(name.lower())
        year = _year(digits)
        if month and year:
            return f"{year:04d}-{month:02d}"
    return None


def version_sort_key(version: str | None, label: str | None = None) -> tuple[str, str, str]:
    """An ordering for a column of versions, oldest first.

    The label follows the date in the key so that two maps from the same month
    -- "Oct '18" and "Oct '18 (Late)" -- keep a stable order between them
    rather than falling back to the code, which would put 105 before 47.

    A tuple rather than a joined string, because any separator character would
    have to sort below a space to keep "Oct '18" ahead of "Oct '18 (Late)",
    and one that does is one nobody will guess is load-bearing.
    """
    return (
        version_date(version, label) or UNDATED_SORT_KEY,
        label or "",
        version or "",
    )
