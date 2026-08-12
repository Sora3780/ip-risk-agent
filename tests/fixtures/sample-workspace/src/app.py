"""Nova Dashboard 진입점."""

import requests

from utils import human_bytes, slugify
from vendor.gpl_helper import fill_gaps, resample
from vendor.mystery_util import ratio

METRICS_URL = "https://example.internal/api/metrics"


def load_metrics(session: requests.Session):
    resp = session.get(METRICS_URL, timeout=10)
    resp.raise_for_status()
    return [(row["ts"], row["value"]) for row in resp.json()["rows"]]


def build_panel(name, points):
    series = fill_gaps(resample(points, 300), 300)
    return {
        "id": slugify(name),
        "title": name,
        "points": series,
        "size": human_bytes(len(series) * 16),
    }


def dedupe_titles(titles, threshold=0.9):
    kept = []
    for title in titles:
        if all(ratio(title, other) < threshold for other in kept):
            kept.append(title)
    return kept


if __name__ == "__main__":
    with requests.Session() as s:
        print(build_panel("요청 처리량", load_metrics(s)))
