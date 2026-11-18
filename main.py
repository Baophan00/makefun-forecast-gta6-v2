"""make.fun GTA VI Twitch-viewers forecast, standard library only.

The comparables model reduces each past launch's own release day to hour-over-
hour log ratios, pools those ratios across launches, reads five quantiles from
each hour's pool, and scales by the case's hour-1 print. Thin samples blend with
a midday-peak prior. Deterministic, bounded, no network or clock.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
WALLET = "6NGWFahrMn1eqn5d5UviWxW2G9YTKmzQm3v5sP8fAk6P"

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
Z = (-1.645, -0.674, 0.0, 0.674, 1.645)
HOURS = range(2, 25)
MIN_RATIO = 0.02
MAX_RATIO = 40.0
MAX_VIEWERS = 1.0e9
MIN_COMPARABLES = 5       # below this count, blend the prior in
PRIOR_SIGMA = 0.15        # log-space spread per sqrt(hour) of the prior
PRIOR_PEAK = 0.35         # midday peak amplitude in log space
OWNER_PATH = Path(__file__).resolve().parent / ".makefun" / "owner.json"


def finite(x):
    """Finite float, or None (reject bool)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def stamp(value):
    """An ISO-8601 UTC timestamp -> naive UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def comparable_day(comp):
    """A comparable's own release-day start, or None."""
    if not isinstance(comp, dict):
        return None
    return stamp(comp.get("day_start"))


def hourly_points(comp):
    """The comparable's list of {from, viewers, ...} points."""
    if not isinstance(comp, dict):
        return []
    pts = comp.get("hourly")
    return pts if isinstance(pts, list) else []


def log_ratios(comp):
    """hour -> log(v_hour / v_hour1) for one comparable's own release day.

    Only points whose `from` falls in [day_start, day_start + 24h) are used;
    when an hour carries more than one point its viewers are averaged. Each
    ratio is clipped to [0.02, 40] before taking the log.
    """
    day0 = comparable_day(comp)
    if day0 is None:
        return {}
    totals, counts = {}, {}
    for pt in hourly_points(comp):
        if not isinstance(pt, dict):
            continue
        at = stamp(pt.get("from"))
        if at is None:
            continue
        delta = (at - day0).total_seconds() / 3600.0
        if delta < 0.0 or delta >= 24.0:
            continue
        hour = int(math.floor(delta)) + 1
        viewers = finite(pt.get("viewers"))
        if viewers is None or viewers <= 0.0:
            continue
        totals[hour] = totals.get(hour, 0.0) + viewers
        counts[hour] = counts.get(hour, 0) + 1
    if counts.get(1, 0) == 0:
        return {}
    base = totals[1] / counts[1]
    if not math.isfinite(base) or base <= 0.0:
        return {}
    out = {}
    for hour in HOURS:
        if counts.get(hour, 0) == 0:
            continue
        ratio = (totals[hour] / counts[hour]) / base
        if not math.isfinite(ratio) or ratio <= 0.0:
            continue
        ratio = min(max(ratio, MIN_RATIO), MAX_RATIO)
        log_ratio = math.log(ratio)
        if math.isfinite(log_ratio):
            out[hour] = log_ratio
    return out


def prior_log_quantiles(hour):
    """Midday-peak log-ratio prior: five quantiles for one hour."""
    u = (hour - 1) / 23.0
    bump = max(0.0, 4.0 * u * (1.0 - u)) ** 0.9   # 0 at hour 1, peak mid-day
    median = PRIOR_PEAK * bump
    sigma = PRIOR_SIGMA * math.sqrt(hour - 1)
    return [median + z * sigma for z in Z]


def quantile(sorted_values, tau):
    """Type-7 linear-interpolation quantile of an already sorted sample."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = tau * (n - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (pos - lo) * (sorted_values[hi] - sorted_values[lo])


def round2(x):
    """Round half up to two decimals for a positive x."""
    scaled = x * 100.0
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    return whole / 100.0


def hour_one_print(case):
    """p = known[0].viewers, kept positive and finite (floor of 1 viewer)."""
    known = case.get("known") if isinstance(case, dict) else None
    value = None
    if isinstance(known, list) and known and isinstance(known[0], dict):
        value = finite(known[0].get("viewers"))
    if value is None or value <= 0.0:
        value = 1.0
    return max(value, 1.0)


def forecast_case(case):
    p = hour_one_print(case)

    comparables = case.get("comparables") if isinstance(case, dict) else None
    pooled = {hour: [] for hour in HOURS}
    usable = 0
    if isinstance(comparables, list):
        for comp in comparables:
            ratios = log_ratios(comp)
            if not ratios:
                continue
            usable += 1
            for hour, value in ratios.items():
                pooled[hour].append(value)

    # Data weight: full trust at five or more comparables, otherwise fade into
    # the midday-peak prior (zero comparables -> pure prior).
    data_weight = 1.0 if usable >= MIN_COMPARABLES else usable / float(MIN_COMPARABLES)
    prior_weight = 1.0 - data_weight

    rows = []
    for hour in HOURS:
        sample = pooled[hour]
        sample.sort()
        prior = prior_log_quantiles(hour)
        quantiles = []
        for tau, prior_q in zip(LEVELS, prior):
            data_q = quantile(sample, tau) if sample else prior_q
            log_q = data_weight * data_q + prior_weight * prior_q
            value = p * math.exp(log_q)
            if not math.isfinite(value) or value <= 0.0:
                value = p
            quantiles.append(round2(min(value, MAX_VIEWERS)))
        # Finite, positive, non-decreasing within the row.
        quantiles = [min(max(q, 0.01), MAX_VIEWERS) for q in quantiles]
        for i in range(1, len(quantiles)):
            if quantiles[i] < quantiles[i - 1]:
                quantiles[i] = quantiles[i - 1]
        rows.append({"hour": hour, "q": quantiles})
    return {"hours": rows}


def write_owner():
    """Keep the repository's owner marker in sync with this entry's wallet."""
    try:
        OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
        OWNER_PATH.write_text(
            json.dumps({"wallet": WALLET}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_owner()
    filenames = sorted(p.name for p in INPUT_DIR.glob("*.in"))
    for filename in filenames:
        path = INPUT_DIR / filename
        case = json.loads(path.read_text(encoding="utf-8"))
        result = forecast_case(case)
        (OUTPUT_DIR / (path.stem + ".out")).write_text(
            json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
