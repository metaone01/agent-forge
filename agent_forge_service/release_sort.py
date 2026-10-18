"""Rebuildable ordering by explicitly declared releases of canonical latest versions."""
from __future__ import annotations

import json
import sqlite3
from .storage import encode_json, timestamp

POLICY = "canonical-declared-release-time-v1"
SCOPE = "canonical-latest-explicit-distribution"
COLUMNS = {"public_release_sort_keys": {"package_id", "published_at", "data_json"}}
KINDS = frozenset(("exact_declared_release", "exact_declared_asset"))


def create_schema(connection):
    connection.executescript("""
        CREATE TABLE public_release_sort_keys (
            package_id TEXT PRIMARY KEY REFERENCES packages(id),published_at TEXT,data_json TEXT NOT NULL
        );
        CREATE INDEX public_release_package_rank ON public_release_sort_keys(published_at DESC,package_id ASC);
    """)


def derive(connection, as_of):
    as_of = timestamp(as_of)
    rows = {}
    for row in connection.execute("SELECT p.id,p.canonical_latest,v.payload_hash FROM packages p LEFT JOIN package_versions v ON v.package_id=p.id AND v.version=p.canonical_latest ORDER BY p.id"):
        rows[row['id']] = {
            'policyVersion': POLICY, 'canonicalVersion': row['canonical_latest'], 'canonicalHash': row['payload_hash'],
            'status': 'unknown', 'publishedAt': None, 'reason': 'no_exact_declared_release' if row['payload_hash'] else 'no_canonical_version',
            'candidateCount': 0, 'eligibleCount': 0, 'futureCount': 0, 'selected': None,
        }
    candidates = connection.execute("""
        SELECT l.package_id,l.repository_id,l.release_id,l.data_json AS link_json,i.data_json AS release_json
        FROM public_release_package_links l JOIN packages p ON p.id=l.package_id AND p.canonical_latest=l.version
        JOIN public_release_items i ON i.repository_id=l.repository_id AND i.release_id=l.release_id
        ORDER BY l.package_id,l.repository_id,l.release_id
    """)
    for row in candidates:
        link = json.loads(row['link_json'])
        kinds = sorted({proof['kind'] for proof in link['assertions']} & KINDS)
        if not kinds:
            continue
        value = rows[row['package_id']]
        if link['canonicalHash'] != value['canonicalHash']:
            raise ValueError('release sort canonical hash differs')
        item = json.loads(row['release_json'])
        value['candidateCount'] += 1
        published = timestamp(item['publishedAt'])
        if published > as_of:
            value['futureCount'] += 1
            if value['selected'] is None:
                value['reason'] = 'future_published_time'
            continue
        value['eligibleCount'] += 1
        # SQL visits equal-time candidates by numeric repository/release IDs.
        if value['publishedAt'] is not None and published <= value['publishedAt']:
            continue
        value.update(status='known', publishedAt=published, reason='explicit_declared_release')
        value['selected'] = {
            'repositoryId': item['repositoryId'], 'releaseId': item['releaseId'], 'tagName': item['tagName'],
            'prerelease': item['prerelease'], 'publishedAt': published, 'associationKinds': kinds,
            'observation': item['observation'],
        }
    coverage = {
        'policyVersion': POLICY, 'scope': SCOPE, 'asOf': as_of, 'packages': len(rows),
        'known': sum(value['status'] == 'known' for value in rows.values()),
        'unknown': sum(value['status'] == 'unknown' for value in rows.values()),
        'exactCandidates': sum(value['candidateCount'] for value in rows.values()),
        'futureExcluded': sum(value['futureCount'] for value in rows.values()),
        'prereleasesIncluded': True, 'selection': 'maximum-published-at-then-numeric-repository-release-id',
        'timestampField': 'published_at', 'versionPolicy': 'canonical-latest-only',
    }
    return rows, coverage


def project(connection, as_of):
    values, coverage = derive(connection, as_of)
    create_schema(connection)
    connection.executemany('INSERT INTO public_release_sort_keys VALUES (?,?,?)',
        [(key, value['publishedAt'], encode_json(value)) for key, value in values.items()])
    return coverage


def validate_projection(connection, manifest):
    reference = sqlite3.connect(':memory:')
    try:
        create_schema(reference)
        expected = dict(reference.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))
        actual = dict(connection.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))
        if any(actual.get(name) != sql for name, sql in expected.items()):
            raise ValueError('release sort SQL schema differs')
    finally:
        reference.close()
    values, coverage = derive(connection, manifest['generatedAt'])
    actual = {}
    for row in connection.execute('SELECT * FROM public_release_sort_keys'):
        value = json.loads(row['data_json'])
        if not isinstance(value, dict) or row['data_json'] != encode_json(value) or row['published_at'] != value.get('publishedAt'):
            raise ValueError('release sort columns or canonical JSON differ')
        actual[row['package_id']] = value
    if encode_json(actual) != encode_json(values) or encode_json(manifest.get('releaseSortCoverage')) != encode_json(coverage):
        raise ValueError('release sort projection differs from explicit canonical associations')
    sorts = manifest.get('availableSorts')
    if manifest.get('releaseSortPolicyVersion') != POLICY or not isinstance(sorts, list) or sorts.count('latest_release') != 1:
        raise ValueError('release sort policy or advertised capability differs')
