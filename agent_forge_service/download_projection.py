"""Immutable, source-scoped npm metrics over the selected canonical distribution."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .downloads import SOURCE, DownloadsRequest, declared_packages, normalize_downloads
from .metrics import MetricSample, WindowPolicy, rate_for_interval
from .storage import encode_json, timestamp

DOWNLOAD_POLICY = WindowPolicy("npm-daily-download-rate", "pilot-24h-v1", 24, 0, 24, 50)
DOWNLOAD_OBSERVATION_MAX_AGE_HOURS = 26


def create_download_schema(db):
    db.executescript("""
        CREATE TABLE package_npm_links (
            package_id TEXT NOT NULL REFERENCES packages(id), npm_package TEXT NOT NULL,
            evidence_json TEXT NOT NULL, PRIMARY KEY(package_id,npm_package)
        );
        CREATE INDEX npm_package_links ON package_npm_links(npm_package,package_id);
        CREATE TABLE npm_download_observations (
            package TEXT PRIMARY KEY, observed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
            evidence_hash TEXT NOT NULL, body_json TEXT NOT NULL
        );
        CREATE TABLE npm_download_signals (
            package TEXT PRIMARY KEY REFERENCES npm_download_observations(package), signal_json TEXT NOT NULL
        );
        CREATE TABLE package_download_sort_keys (
            package_id TEXT PRIMARY KEY REFERENCES packages(id), mapping_status TEXT NOT NULL,
            npm_package TEXT, downloads_value INTEGER, growth_value REAL
        );
        CREATE INDEX package_downloads_rank ON package_download_sort_keys(downloads_value DESC,package_id);
        CREATE INDEX package_download_growth_rank ON package_download_sort_keys(growth_value DESC,package_id);
    """)


def _midnight(day):
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def download_signal(body, *, observed_at, checked_at, as_of, policy):
    request = DownloadsRequest(body["package"], body["start"], body["end"])
    body = normalize_downloads(body, request)
    end = timestamp(_midnight(body["end"]) + timedelta(days=1))
    common = dict(source_id=SOURCE, subject_kind="registry_package", subject_id=body["package"], metric="downloads", unit="downloads",
        counter_identity="npm-package:" + body["package"] + ":all-versions", distribution_scope="npm_public_registry_all_versions",
        observed_at=observed_at)
    total = MetricSample(**common, value=sum(row["downloads"] for row in body["downloads"]), measurement_kind="rolling_window",
        window_start=timestamp(_midnight(body["start"])), window_end=end)
    daily = MetricSample(**common, value=body["downloads"][-1]["downloads"], measurement_kind="interval",
        window_start=timestamp(_midnight(body["end"])), window_end=end)
    growth = rate_for_interval(daily)
    qualification = policy.qualification(growth, as_of=as_of)
    age = (datetime.fromisoformat(timestamp(as_of).replace("Z", "+00:00")) - datetime.fromisoformat(timestamp(checked_at).replace("Z", "+00:00"))).total_seconds() / 3600
    if age < 0 or age > DOWNLOAD_OBSERVATION_MAX_AGE_HOURS:
        qualification.update(eligible=False, reason="check_in_future" if age < 0 else "check_stale")
    return {"sourceId": SOURCE, "subjectKind": "registry_package", "subjectId": body["package"],
        "scope": "npm_public_registry_all_versions", "sharedDistributionSignal": True, "observedAt": observed_at, "checkedAt": checked_at,
        "downloads30d": asdict(total), "dailyDownloads": asdict(daily),
        "downloadGrowth": {**asdict(growth), "qualification": qualification},
        "disclaimer": "下载请求次数不是安装次数、活跃用户或独立插件用户；没有跨生态合并"}


def project_downloads(state, snapshot, *, as_of, policy=DOWNLOAD_POLICY):
    cutoff = timestamp(as_of)
    associations = {}
    for row in snapshot.execute("SELECT p.id,p.canonical_latest,v.canonical_json,v.payload_hash FROM packages p "
        "LEFT JOIN package_versions v ON v.package_id=p.id AND v.version=p.canonical_latest"):
        names = declared_packages(json.loads(row["canonical_json"])) if row["canonical_json"] else {}
        associations[row["id"]] = names
        for name, declarations in names.items():
            evidence = {"selectedVersion": row["canonical_latest"], "payloadHash": row["payload_hash"], "declarations": declarations}
            snapshot.execute("INSERT INTO package_npm_links VALUES (?,?,?)", (row["id"], name, encode_json(evidence)))
    signals = {}
    all_names = sorted({name for names in associations.values() for name in names})
    for name in all_names:
        row = state.execute("SELECT * FROM npm_download_observations WHERE package=? AND checked_at<=? AND observed_at<=? "
            "ORDER BY end_day DESC,checked_at DESC,id DESC LIMIT 1", (name, cutoff, cutoff)).fetchone()
        if row is None:
            continue
        body = normalize_downloads(json.loads(row["body_json"]), DownloadsRequest(name, row["start_day"], row["end_day"]))
        if timestamp(_midnight(body["end"]) + timedelta(days=1)) > cutoff:
            continue
        signal = download_signal(body, observed_at=row["observed_at"], checked_at=row["checked_at"], as_of=cutoff, policy=policy)
        signal["evidenceHash"] = row["evidence_hash"]
        snapshot.execute("INSERT INTO npm_download_observations VALUES (?,?,?,?,?)", (name, row["observed_at"], row["checked_at"], row["evidence_hash"], encode_json(body)))
        snapshot.execute("INSERT INTO npm_download_signals VALUES (?,?)", (name, encode_json(signal)))
        signals[name] = signal
    for package_id, names in associations.items():
        name = next(iter(names)) if len(names) == 1 else None
        signal = signals.get(name)
        status = "unmapped" if not names else "ambiguous" if len(names) > 1 else "mapped" if signal else "pending"
        eligible = signal and signal["downloadGrowth"]["qualification"]["eligible"]
        snapshot.execute("INSERT INTO package_download_sort_keys VALUES (?,?,?,?,?)", (package_id, status, name,
            signal["downloads30d"]["value"] if eligible else None, signal["downloadGrowth"]["value_per_hour"] if eligible else None))
    counts = dict(snapshot.execute("SELECT mapping_status,count(*) FROM package_download_sort_keys GROUP BY mapping_status"))
    coverage = {"sourceId": SOURCE, "mappedPackages": counts.get("mapped", 0), "pendingPackages": counts.get("pending", 0),
        "ambiguousPackages": counts.get("ambiguous", 0), "unmappedPackages": counts.get("unmapped", 0), "sharedNpmPackages": len(signals),
        "eligiblePackages": snapshot.execute("SELECT count(*) FROM package_download_sort_keys WHERE growth_value IS NOT NULL").fetchone()[0],
        "countsWindowDays": 30, "rateWindowHours": 24, "policy": asdict(policy), "maximumCheckAgeHours": DOWNLOAD_OBSERVATION_MAX_AGE_HOURS}
    coverage["oldestWindowEnd"] = min((signal["downloadGrowth"]["window_end"] for signal in signals.values()), default=None)
    coverage["oldestCheckedAt"] = min((signal["checkedAt"] for signal in signals.values()), default=None)
    snapshot.execute("INSERT INTO sources VALUES (?,?)", ("npmjs.org:downloads", encode_json({"id": "npmjs.org:downloads", "sourceId": SOURCE,
        "kind": "public-download-observations", "asOf": cutoff, "coverage": coverage, "policy": asdict(policy),
        "disclaimer": "公开 npm 包下载请求统计，不是安装人数或活跃用户；不跨生态合并"})))
    return coverage
