"""Bounded source rotation on existing durable scans and Worker metadata."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from .errors import ServiceError
from .recommendation_projection import public_source_identity
from .recommendations import enqueue_recommendation, source_identity, validate_source
from .scheduling import _time, utc_slot
from .storage import encode_json, timestamp

LANE = 'recommendations'
PREFIX = 'recommendation-refresh:v1:'
MAX_STATE_BYTES = 131072


@dataclass(frozen=True)
class RecommendationsBatchPolicy:
    budget: int
    max_sources: int
    interval_hours: int
    retry_hours: int
    sources: tuple[str, ...]

    def __post_init__(self):
        for value, maximum in ((self.budget, 1000), (self.max_sources, 100)):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError('curated request/source budgets must be bounded integers')
        if bool(self.budget) != bool(self.max_sources):
            raise ValueError('curated request/source budgets must be enabled together')
        for value in (self.interval_hours, self.retry_hours):
            if type(value) is not int or not 4 <= value <= 168:
                raise ValueError('curated refresh/retry intervals must be explicit hours in [4,168]')
        if not isinstance(self.sources, tuple) or len(self.sources) > 100 or self.budget and not self.sources:
            raise ValueError('curated sources must be an explicit bounded immutable tuple')
        for definition in self.sources:
            if not isinstance(definition, str) or len(definition.encode('utf8')) > 16384:
                raise ValueError('curated source definition exceeds its byte budget')
            value = _json(definition)
            if encode_json(validate_source(value)) != definition:
                raise ValueError('curated source definition must use canonical JSON')
        if len(set(self.sources)) != len(self.sources):
            raise ValueError('duplicate curated source configuration')

    def grouped(self):
        sources = {}
        for raw in self.sources:
            definition = validate_source(_json(raw))
            identity = public_source_identity(definition)
            old = sources.get(identity)
            # A stable representative avoids duplicate GETs for labels/budget aliases.
            if old is None or encode_json(definition) < encode_json(old):
                sources[identity] = definition
        return sources


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate curated refresh key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite curated refresh JSON')))


def _empty(policy):
    return {'schemaVersion': 1, 'policyId': policy.policy_id, 'lastPlannedAt': None, 'resources': {}}


def _state(connection, policy):
    row = connection.execute('SELECT value FROM metadata WHERE key=?', (PREFIX + policy.policy_id,)).fetchone()
    if row is None:
        return _empty(policy)
    try:
        if len(row[0].encode('utf8')) > MAX_STATE_BYTES:
            raise ValueError('curated refresh state too large')
        value = _json(row[0])
        if not isinstance(value, dict) or set(value) != {'schemaVersion', 'policyId', 'lastPlannedAt', 'resources'} or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or value['policyId'] != policy.policy_id:
            raise ValueError('curated refresh state identity differs')
        if value['lastPlannedAt'] is not None and timestamp(value['lastPlannedAt']) != value['lastPlannedAt']:
            raise ValueError('invalid curated planning clock')
        resources = value['resources']
        if not isinstance(resources, dict) or set(resources) != set(policy.recommendations.grouped()):
            raise ValueError('curated refresh sources differ from policy')
        for resource in resources.values():
            if not isinstance(resource, dict) or set(resource) != {'nextDueAt', 'lastSuccessAt', 'lastAttemptAt'}:
                raise ValueError('invalid curated refresh resource')
            for key, date in resource.items():
                if date is None and key != 'nextDueAt':
                    continue
                if not isinstance(date, str) or timestamp(date) != date:
                    raise ValueError('invalid curated refresh timestamp')
        return value
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise ServiceError('recommendation_refresh_state_invalid', 'curated refresh state must be recovered before planning', 503) from error


def _save(connection, policy, state):
    encoded = encode_json(state)
    if len(encoded.encode('utf8')) > MAX_STATE_BYTES:
        raise ValueError('curated refresh state exceeds its byte budget')
    connection.execute('INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (PREFIX + policy.policy_id, encoded))


def _aliases(connection, grouped):
    values = {identity: [] for identity in grouped}
    for row in connection.execute('SELECT id,definition_json FROM recommendation_sources'):
        definition = validate_source(_json(row['definition_json']))
        if source_identity(definition) != row['id']:
            raise ValueError('curated source definition changed without its identity')
        identity = public_source_identity(definition)
        if identity in values:
            values[identity].append(row['id'])
    return values


def _scan_facts(connection, aliases, now):
    successes, pending, failures = {}, {}, {}
    for identity, ids in aliases.items():
        if not ids:
            continue
        placeholders = ','.join('?' for _ in ids)
        success_row = connection.execute('SELECT s.completed_at FROM recommendation_snapshots s JOIN recommendation_scans r ON r.id=s.scan_id WHERE s.source_id IN (' + placeholders + ") AND r.state='complete' AND s.completed_at<=? AND s.selection_at<=? AND r.finished_at<=? ORDER BY s.selection_at DESC,s.completed_at DESC,s.id LIMIT 1", (*ids, now, now, now)).fetchone()
        success = success_row[0] if success_row else None
        if success:
            successes[identity] = success
        rows = connection.execute('SELECT r.* FROM recommendation_scans r WHERE r.source_id IN (' + placeholders + ") AND r.state IN ('queued','running') AND r.created_at<=? AND r.slot<=? ORDER BY r.created_at,r.id", (*ids, now, now)).fetchall()
        if success:
            rows = [row for row in rows if row['created_at'] > success]
        if len(rows) > 1:
            # Concurrent manual scans are persisted evidence, not extra scheduled work.
            rows = rows[:1]
        if rows:
            pending[identity] = rows[0]['id']
        failed = connection.execute('SELECT max(finished_at) FROM recommendation_scans WHERE source_id IN (' + placeholders + ") AND state='failed' AND finished_at<=? AND created_at>?", (*ids, now, success or '')).fetchone()[0]
        if failed:
            failures[identity] = failed
    return successes, pending, failures


def _next_slot(value, hours, slot_hours):
    return timestamp(_time(utc_slot(value, slot_hours)) + timedelta(hours=hours))


def _sync(connection, policy, state, now):
    grouped = policy.recommendations.grouped()
    aliases = _aliases(connection, grouped)
    successes, pending, failures = _scan_facts(connection, aliases, now)
    for identity in grouped:
        row = state['resources'].setdefault(identity, {'nextDueAt': now, 'lastSuccessAt': None, 'lastAttemptAt': None})
        success = successes.get(identity)
        if success and (row['lastSuccessAt'] is None or success > row['lastSuccessAt']):
            row['lastSuccessAt'] = success
            row['nextDueAt'] = _next_slot(success, policy.recommendations.interval_hours, policy.slot_hours)
        failure = failures.get(identity)
        if failure and (success is None or failure > success):
            row['nextDueAt'] = max(row['nextDueAt'], _next_slot(failure, policy.recommendations.retry_hours, policy.slot_hours))
    return pending


def plan_recommendation_batch(connection, policy, *, now, slot):
    config = policy.recommendations
    if config is None or config.budget == 0:
        return {}, {'status': 'disabled'}
    now = timestamp(now)
    state = _state(connection, policy)
    if state['lastPlannedAt'] and now < state['lastPlannedAt']:
        raise ServiceError('recommendation_refresh_clock_wait', 'curated planning clock moved backward', 409)
    pending = _sync(connection, policy, state, now)
    grouped = config.grouped()
    candidates = [identity for identity, row in state['resources'].items() if identity in pending or row['nextDueAt'] <= now]
    # Least recently selected first lets unfinished and new sources share scarce slots.
    candidates.sort(key=lambda identity: (state['resources'][identity]['lastAttemptAt'] or '', state['resources'][identity]['nextDueAt'], identity))
    selected = candidates[:config.max_sources]
    scans, reused = [], 0
    full_cost = {identity: 4+len(grouped[identity]['path'].split('/')) for identity in selected}
    remaining_cost = {}
    for identity in selected:
        if identity in pending:
            scan = pending[identity]; reused += 1
        else:
            scan = enqueue_recommendation(connection, definition=grouped[identity], slot=slot, now=now)
        step = connection.execute('SELECT step FROM recommendation_scans WHERE id=?',(scan,)).fetchone()[0]
        remaining_cost[identity] = max(0,full_cost[identity]-step)
        scans.append(scan)
        state['resources'][identity]['lastAttemptAt'] = now
    state['lastPlannedAt'] = now
    _save(connection, policy, state)
    return {LANE: config.budget}, {'status': 'configured', 'configuredAliases': len(config.sources), 'sources': len(grouped),
        'due': len(candidates), 'selected': len(selected), 'deferred': len(candidates)-len(selected), 'resumedScans': reused,
        'selectedScanIds': scans, 'intervalHours': config.interval_hours, 'retryHours': config.retry_hours,
        'minimumRequestsPerCompleteScan': full_cost, 'remainingRequestsAtPlanning': remaining_cost,
        'dailyRequestBudget': config.budget*(24//policy.slot_hours), 'coverage': 'configured-scopes-only',
        'selectionSemantics': 'source_refresh_priority_not_public_recommendation',
        'capacityWarnings': ['request_budget_cannot_complete_selected_scans_in_one_slot'] if sum(remaining_cost.values())>config.budget else []}


def selected_scans(connection, batch_id):
    row = connection.execute('SELECT summary_json FROM batch_runs WHERE id=?', (batch_id,)).fetchone()
    value = json.loads(row[0]).get('recommendations', {}) if row else {}
    ids = value.get('selectedScanIds', [])
    if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(identity, str) or len(identity) != 64 or any(char not in '0123456789abcdef' for char in identity) for identity in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid curated batch scan membership')
    return tuple(ids)


def finish_recommendation_checks(connection, policy_id, now):
    raw = json.loads(connection.execute('SELECT config_json FROM batch_policies WHERE id=?', (policy_id,)).fetchone()[0])
    config = raw.get('recommendations')
    if not config:
        return
    from types import SimpleNamespace
    policy = SimpleNamespace(policy_id=policy_id, slot_hours=raw['slot_hours'], recommendations=RecommendationsBatchPolicy(**{**config, 'sources': tuple(config['sources'])}))
    state = _state(connection, policy)
    _sync(connection, policy, state, timestamp(now))
    _save(connection, policy, state)
