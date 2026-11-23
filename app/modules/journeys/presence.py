"""Conservative GPS to vehicle trajectory matching for boarding suggestions."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from typing import Any


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    radius = 6_371_000.0
    lat1, lat2 = radians(a["lat"]), radians(b["lat"])
    dlat, dlon = lat2 - lat1, radians(b["lon"] - a["lon"])
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * radius * atan2(sqrt(h), sqrt(max(0.0, 1 - h)))


def _direction(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, float, float]:
    seconds = (b["observed_at"] - a["observed_at"]).total_seconds()
    if seconds <= 0:
        return 0.0, 0.0, 0.0
    lat_scale = 111_320.0
    dx = (b["lon"] - a["lon"]) * lat_scale * cos(radians((a["lat"] + b["lat"]) / 2))
    dy = (b["lat"] - a["lat"]) * lat_scale
    return dx / seconds, dy / seconds, sqrt(dx * dx + dy * dy) / seconds


def estimate_boarding(gps: list[dict[str, Any]], positions: list[dict[str, Any]], *, now: datetime,
                      max_gps_age_seconds: float) -> dict[str, Any]:
    """Return a user-confirmable suggestion; never changes journey movement state."""
    valid_gps = [p for p in gps if (now - p["observed_at"]).total_seconds() <= max_gps_age_seconds
                 and (p.get("accuracy_m") is None or p["accuracy_m"] <= 75)]
    if len(valid_gps) < 2:
        reason = "location_stale" if gps and not valid_gps else "need_consecutive_accurate_locations"
        return {"status": "insufficient_evidence", "reason_codes": [reason],
                "suggestion": None}
    if not positions:
        return {"status": "insufficient_evidence", "reason_codes": ["vehicle_positions_unavailable"],
                "suggestion": None}

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for p in positions:
        if p.get("vehicle_id"):
            grouped[(p["route_id"], p["pattern_key"], p["direction_code"], p["vehicle_id"])].append(p)

    matches = []
    for (route_id, pattern, direction, vehicle), track in grouped.items():
        track.sort(key=lambda p: p["observed_at"])
        pairs = []
        used = set()
        for user_point in valid_gps:
            choices = [(abs((bus["observed_at"] - user_point["observed_at"]).total_seconds()), i, bus)
                       for i, bus in enumerate(track) if i not in used]
            if not choices:
                continue
            delta, index, bus_point = min(choices, key=lambda item: item[0])
            if delta <= 75:
                accuracy = min(75.0, user_point.get("accuracy_m") or 35.0)
                distance = _distance(user_point, bus_point)
                if distance <= max(45.0, accuracy + 20.0):
                    pairs.append((user_point, bus_point, distance))
                    used.add(index)
        if len(pairs) < 2:
            continue
        user_v = _direction(pairs[0][0], pairs[-1][0])
        bus_v = _direction(pairs[0][1], pairs[-1][1])
        user_speed, bus_speed = user_v[2], bus_v[2]
        dot = user_v[0] * bus_v[0] + user_v[1] * bus_v[1]
        alignment = dot / (user_speed * bus_speed) if user_speed > 0.8 and bus_speed > 0.8 else -1.0
        # Walking beside a stopped bus or noisy stationary GPS is not enough to infer boarding.
        if alignment < 0.72 or not (1.0 <= user_speed <= 22.0 and 1.0 <= bus_speed <= 28.0):
            continue
        mean_distance = sum(pair[2] for pair in pairs) / len(pairs)
        matches.append({"route_id": str(route_id), "pattern_key": pattern, "direction_code": direction,
                        "vehicle_id": vehicle, "matched_samples": len(pairs), "mean_distance_m": round(mean_distance, 1),
                        "direction_alignment": round(alignment, 2), "_score": alignment - mean_distance / 500})

    if not matches:
        return {"status": "no_match", "reason_codes": ["no_vehicle_trajectory_match"], "suggestion": None}
    winner = max(matches, key=lambda item: item["_score"])
    runner_up = max((item["_score"] for item in matches if item is not winner), default=-1.0)
    confidence = "high" if winner["matched_samples"] >= 3 and winner["_score"] - runner_up >= 0.12 else "possible"
    winner.pop("_score", None)
    return {"status": "suggested", "reason_codes": [], "confidence": confidence,
            "suggestion": {**winner, "requires_user_confirmation": True}}
