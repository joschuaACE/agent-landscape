"""Minimal 5-field cron (minute hour day-of-month month day-of-week) in local time – no dependency.

Supports ``*``, values, ranges ``a-b``, steps ``*/n`` / ``a-b/n``, lists ``a,b``, day names (mon … sun),
``0`` and ``7`` = Sunday, and the aliases @hourly, @daily, @weekdays, @weekly. Like Vixie cron, when both
day-of-month and day-of-week are restricted a day matches if *either* matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekdays": "0 12 * * 1-5", "@weekly": "0 0 * * 0"}
DAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
DAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


def _value(tok: str, lo: int, hi: int, names: bool) -> int:
    tok = tok.strip().lower()
    v = DAY_NAMES.get(tok[:3]) if names and tok[:3] in DAY_NAMES else None
    if v is None:
        if not tok.isdigit():
            raise CronError(f"'{tok}' is not a number")
        v = int(tok)
    if not lo <= v <= hi:
        raise CronError(f"{v} is outside {lo}-{hi}")
    return v


def _field(spec: str, lo: int, hi: int, names: bool = False) -> tuple[frozenset[int], bool]:
    out: set[int] = set()
    for part in spec.split(","):
        rng, _, step_s = part.partition("/")
        step = _value(step_s, 1, hi, False) if step_s else 1
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a_s, b_s = rng.split("-", 1)
            a, b = _value(a_s, lo, hi, names), _value(b_s, lo, hi, names)
            if a > b:
                raise CronError(f"range {rng} is reversed")
        else:
            a = _value(rng, lo, hi, names)
            b = hi if step_s else a
        out.update(range(a, b + 1, step))
    return frozenset(out), spec.strip() == "*"


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        raw = " ".join(str(expr or "").split())
        parts = ALIASES.get(raw.lower(), raw).split(" ")
        if len(parts) != 5:
            raise CronError("a schedule needs 5 fields: minute hour day month weekday (e.g. '0 12 * * 1-5')")
        try:
            (mi, _), (h, _), (d, any_d), (mo, _), (wd, any_wd) = (
                _field(p, lo, hi, names=(i == 4)) for i, (p, (lo, hi)) in enumerate(zip(parts, RANGES, strict=True)))
        except CronError as exc:
            raise CronError(f"invalid schedule '{raw}': {exc}") from None
        wd = frozenset(0 if x == 7 else x for x in wd)
        return cls(raw, mi, h, d, mo, wd, any_d, any_wd)

    def _day_ok(self, dt: datetime) -> bool:
        dom, dow = dt.day in self.days, (dt.isoweekday() % 7) in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def next_after(self, after: datetime) -> datetime:
        """First matching minute strictly after ``after`` (naive local datetime)."""
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = dt + timedelta(days=366 * 5)
        while dt < limit:
            if dt.month not in self.months:
                dt = (dt.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_ok(dt):
                dt = dt.replace(hour=0, minute=0) + timedelta(days=1)
            elif dt.hour not in self.hours:
                dt = dt.replace(minute=0) + timedelta(hours=1)
            elif dt.minute not in self.minutes:
                dt += timedelta(minutes=1)
            else:
                return dt
        raise CronError(f"schedule '{self.expr}' never fires")

    def describe(self) -> str:
        """Human label, e.g. 'Mon–Fri 12:00' – falls back to the expression."""
        if len(self.minutes) != 1 or len(self.hours) != 1 or not self.any_day or len(self.months) != 12:
            return self.expr
        time_s = f"{next(iter(self.hours)):02d}:{next(iter(self.minutes)):02d}"
        wd = sorted(self.weekdays)
        if self.any_weekday or len(wd) == 7:
            days = "daily"
        elif wd == [1, 2, 3, 4, 5]:
            days = "Mon–Fri"
        elif wd == [0, 6]:
            days = "weekends"
        else:
            days = ", ".join(DAY_LABELS[d] for d in wd)
        return f"{days} {time_s}"
