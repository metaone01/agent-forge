"""Read-only deployment checks; never installs dependencies or activates snapshots."""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys
from .app import _origins
from .bundles import _verify_snapshot, DEFAULT_MAX_DATABASE_BYTES
from .errors import ServiceError
from .publishing import SnapshotStore
from .settings import Settings


def dependency_status(*, require_test_client=False):
    result = {}
    for name in ('fastapi', 'uvicorn') + (('httpx',) if require_test_client else ()):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        result[name] = {'available': version is not None and importlib.util.find_spec(name) is not None, 'version': version}
    return result


def inspect(settings, *, revision=None, allowed_origins=(), require_test_client=False):
    origins = _origins(allowed_origins)
    dependencies = dependency_status(require_test_client=require_test_client)
    errors = []
    if any(not item['available'] for item in dependencies.values()):
        errors.append('http_dependencies_missing')
    store = SnapshotStore(settings.snapshots)
    current = None
    try:
        current = store.current()['revision']
    except ServiceError as error:
        errors.append(error.code)
    selected = revision if revision is not None else current
    snapshot = None
    if selected is not None:
        try:
            # Use the public bundle validator, including all optional projections.
            store.load(selected)
            manifest, manifest_hash, database_hash = _verify_snapshot(settings.snapshots/selected, selected, DEFAULT_MAX_DATABASE_BYTES)
            snapshot = {'revision': selected, 'manifestSha256': manifest_hash, 'databaseSha256': database_hash,
                'schemaVersion': manifest['schemaVersion'], 'counts': manifest['counts'], 'features': manifest['features']}
        except (ServiceError, OSError, ValueError, KeyError, TypeError) as error:
            errors.append(error.code if isinstance(error, ServiceError) else 'snapshot_validation_failed')
    if selected is not None and current is not None and selected != current:
        errors.append('snapshot_not_current')
    return {'ready': not errors and snapshot is not None, 'scope': 'local-read-only-deployment-preflight',
        'python': sys.version.split()[0], 'dependencies': dependencies, 'allowedOrigins': origins,
        'currentRevision': current, 'selectedRevision': selected, 'snapshot': snapshot,
        'errors': sorted(set(errors)), 'httpStarted': False, 'deploymentVerified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, default=Path.cwd())
    parser.add_argument('--revision')
    parser.add_argument('--allow-origin', action='append', default=[])
    parser.add_argument('--require-test-client', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = inspect(Settings(args.catalog,args.runtime),revision=args.revision,
            allowed_origins=args.allow_origin,require_test_client=args.require_test_client)
    except ValueError:
        result = {'ready':False,'errors':['invalid_configuration'],'httpStarted':False,'deploymentVerified':False}
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0 if result['ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
