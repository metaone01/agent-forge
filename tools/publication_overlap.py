"""Exercise reads during real publication in an explicitly new isolated runtime.

No HTTP or upstream requests. Production load/capacity is not established by this
experiment. The CLI refuses existing output roots; it never targets a live runtime.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_forge_service.bundles import _verify_snapshot,DEFAULT_MAX_DATABASE_BYTES
from agent_forge_service.errors import ServiceError
from agent_forge_service.publishing import build_and_publish,digest_file,validate_revision,atomic_json
from agent_forge_service.query import Query,QueryService,_decode_cursor
from agent_forge_service.settings import Settings
from agent_forge_service.state_recovery import restore_state_backup
from tools.query_benchmark import workload,execute,signature,summarize,rss_bytes

ROOT=Path(__file__).resolve().parents[1]


def run_overlap(settings,output,*,new_revision='overlap-next',workers=4,max_seconds=900,max_per_worker=5000,cursor_depth=100):
    """Only call on an isolated test runtime; this invokes the real build command."""
    output=Path(output).absolute()
    if output.exists() or output!=output.resolve() or output.is_relative_to(settings.runtime_root) or output.is_relative_to(settings.catalog_root/'sources'):
        raise ValueError('overlap output must be new and outside runtime and canonical sources')
    if type(workers) is not int or not 1<=workers<=32 or not 1<=max_seconds<=3600 or not 1<=max_per_worker<=10000 or not 0<=cursor_depth<=1000:
        raise ValueError('invalid overlap limits')
    validate_revision(new_revision)
    service=QueryService(settings);before=service.store.current();old_revision=before['revision']
    if (settings.snapshots/new_revision).exists():raise ValueError('new revision already exists')
    _verify_snapshot(settings.snapshots/old_revision,old_revision,DEFAULT_MAX_DATABASE_BYTES)
    old_database_hash=digest_file(settings.snapshots/old_revision/'catalog.db')
    old_manifest_hash=digest_file(settings.snapshots/old_revision/'manifest.json')
    cases=workload(service,before,cursor_depth)
    initial=service.packages(Query(sort='name',limit=1),revision=old_revision)
    probe=service.packages(Query(sort='name',limit=20),revision=old_revision)
    expected_probe=[row['id'] for row in probe['items']]
    output.mkdir(parents=True)
    experiment_started=datetime.now(timezone.utc).isoformat()
    stop=threading.Event();gate=threading.Barrier(workers+1)
    counts=[0]*workers;errors=[0]*workers;limited=[False]*workers
    samples=[];sample_stop=threading.Event()
    def sampler():
        while not sample_stop.is_set():
            value=rss_bytes()
            if value is not None:samples.append(value)
            sample_stop.wait(.1)
    sample_thread=threading.Thread(target=sampler,daemon=True);sample_thread.start()
    def reader(number):
        local=QueryService(settings);records=[]
        try:
            local.store.load(old_revision)
            local.packages(Query(limit=20),revision=old_revision)
            gate.wait(timeout=120)
            iteration=0
            while not stop.is_set() and iteration<max_per_worker:
                case=cases[(number+iteration*workers)%len(cases)]
                current_probe=iteration%8==0
                start=time.perf_counter();error=None;revision=None
                try:
                    if current_probe:
                        value=local.packages(Query(sort='name',limit=20))
                        revision=value['revision']
                        if revision not in (old_revision,new_revision) or [row['id'] for row in value['items']]!=expected_probe:
                            error='current_result_mismatch'
                        if value.get('nextCursor') and _decode_cursor(value['nextCursor'])['r']!=revision:
                            error='current_cursor_revision_mismatch'
                    else:
                        value=execute(local,case,old_revision);revision=value['revision']
                        if signature(value)!=case['expectedSignature'] or revision!=old_revision:error='pinned_result_mismatch'
                except ServiceError as failure:error=failure.code
                except Exception as failure:error=type(failure).__name__
                ended=time.perf_counter()
                records.append({'case':'current_probe' if current_probe else case['name'],'started':start,'ended':ended,
                    'durationMs':(ended-start)*1000,'revision':revision,'error':error})
                counts[number]+=1;errors[number]+=error is not None;iteration+=1
                if error and 'mismatch' in error:stop.set()
            limited[number]=iteration>=max_per_worker and not stop.is_set()
            return records
        except BaseException:
            gate.abort();stop.set();raise
    process=None;writer_started=None;writer_finished=None;timed_out=False;termination_reason=None
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures=[pool.submit(reader,number) for number in range(workers)]
            try:
                gate.wait(timeout=120)
                command=[sys.executable,'-m','agent_forge_service.worker','build','--catalog',str(settings.catalog_root),
                    '--runtime',str(settings.runtime_root),'--revision',new_revision]
                with (output/'writer.log').open('xb') as log:
                    writer_started=time.perf_counter()
                    process=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    next_progress=0
                    while process.poll() is None:
                        elapsed=time.perf_counter()-writer_started
                        if elapsed>=max_seconds:
                            timed_out=True;termination_reason='writer_timeout';break
                        if stop.is_set():termination_reason='reader_invariant_failure';break
                        if all(limited):termination_reason='reader_budget_exhausted';break
                        if elapsed>=next_progress:
                            staging=[{'bytes':p.stat().st_size} for p in (settings.snapshots/'.staging').glob('*/catalog.db') if p.is_file()]
                            atomic_json(output/'progress.json',{'elapsedSeconds':elapsed,'writerPid':process.pid,'writerRunning':True,
                                'readRequests':sum(counts),'readErrors':sum(errors),'stagingDatabases':staging})
                            next_progress=elapsed+5
                        stop.wait(.1)
                    if process.poll() is None:
                        process.terminate()
                        try:process.wait(timeout=10)
                        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
                    writer_finished=time.perf_counter()
            finally:
                stop.set()
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
            groups=[future.result() for future in futures]
    finally:
        sample_stop.set();sample_thread.join(timeout=2)
    records=sorted([record for group in groups for record in group],key=lambda row:row['started'])
    overlap=[record for record in records if record['started']<writer_finished and record['ended']>writer_started]
    result={'scope':'Direct QueryService reads during real build/publication; no HTTP or production capacity claim',
        'startedAt':experiment_started,'finishedAt':datetime.now(timezone.utc).isoformat(),'oldRevision':old_revision,'newRevision':new_revision,
        'oldCounts':before['counts'],'workers':workers,'cases':cases,'writerExitCode':process.returncode,
        'writerProcessSeconds':writer_finished-writer_started,'timedOut':timed_out,'terminationReason':termination_reason,
        'readersHitBudget':limited,'summary':summarize(records),'overlapSummary':summarize(overlap),
        'readerCaseSummaries':{name:summarize([r for r in records if r['case']==name]) for name in sorted({r['case'] for r in records})},
        'sampledReaderPeakRssBytes':max(samples) if samples else None,'records':records,'httpVerified':False,'productionCapacityVerified':False}
    result['oldSnapshotUnchanged']=(digest_file(settings.snapshots/old_revision/'catalog.db')==old_database_hash and
        digest_file(settings.snapshots/old_revision/'manifest.json')==old_manifest_hash)
    # A writer can fail after an atomic pointer change. Inspect authoritative
    # state for every exit code rather than inferring that no publish occurred.
    after=None
    try:
        after=QueryService(settings).store.current()
        result['currentRevisionAfter']=after['revision']
    except ServiceError as failure:
        result['currentInspectionError']=failure.code
    if process.returncode==0 and not termination_reason and after is not None:
        _verify_snapshot(settings.snapshots/new_revision,new_revision,DEFAULT_MAX_DATABASE_BYTES)
        result['newCounts']=after['counts']
        result['canonicalInputUnchanged']=before['inputDigest']==after['inputDigest']
        result['newSnapshot']={'manifestSha256':digest_file(settings.snapshots/new_revision/'manifest.json'),
            'databaseSha256':digest_file(settings.snapshots/new_revision/'catalog.db')}
        result['defaultQueryRevisionAfter']=QueryService(settings).packages(Query(limit=1))['revision']
        if initial['nextCursor']:
            continued=QueryService(settings).packages(Query(sort='name',limit=1),cursor=initial['nextCursor'])
            result['oldCursorRevisionAfter']=continued['revision']
        else:result['oldCursorRevisionAfter']=None
    result['passed']=(process.returncode==0 and not termination_reason and bool(overlap) and not any(limited) and
        result['summary']['errors']==0 and result['oldSnapshotUnchanged'] and result.get('canonicalInputUnchanged') is True and
        result.get('currentRevisionAfter')==new_revision and result.get('newCounts')==before['counts'] and result.get('defaultQueryRevisionAfter')==new_revision and
        result.get('oldCursorRevisionAfter') in (None,old_revision))
    atomic_json(output/'report.json',result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='A new isolated experiment root, never a live runtime')
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--expected-backup-sha256')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--max-seconds',type=int,default=900)
    parser.add_argument('--max-per-worker',type=int,default=5000)
    args=parser.parse_args(argv)
    try:
        output=args.output.absolute();catalog=args.catalog.resolve()
        if output.exists() or output!=output.resolve() or catalog.is_relative_to(output) or output.is_relative_to(catalog/'sources'):
            raise ValueError('output must be a new isolated directory outside canonical sources')
        if bool(args.backup)!=bool(args.expected_backup_sha256):raise ValueError('backup requires a trusted descriptor checksum')
        if not 1<=args.workers<=32 or not 1<=args.max_seconds<=3600 or not 1<=args.max_per_worker<=10000:raise ValueError('invalid limits')
        # Leave space for two public snapshots, private state and staging; the
        # check is an admission guard, not an estimate of every possible dataset.
        parent=output.parent
        while not parent.exists():parent=parent.parent
        if shutil.disk_usage(parent).free<2*1024**3:raise ValueError('insufficient free space for isolated publication experiment')
        settings=Settings(catalog,output/'runtime');output.mkdir(parents=True)
        if args.backup:restore_state_backup(settings,args.backup,expected_backup_sha256=args.expected_backup_sha256)
        base=build_and_publish(settings,revision='overlap-base');atomic_json(output/'base-manifest.json',base)
        result=run_overlap(settings,output/'run',new_revision='overlap-next',workers=args.workers,
            max_seconds=args.max_seconds,max_per_worker=args.max_per_worker)
        print(json.dumps({'output':str(output),'passed':result['passed'],'writerExitCode':result['writerExitCode'],
            'readRequests':result['summary']['requests'],'readErrors':result['summary']['errors']},ensure_ascii=False))
        return 0 if result['passed'] else 1
    except (OSError,ValueError,ServiceError,threading.BrokenBarrierError) as error:
        print(json.dumps({'error':error.code if isinstance(error,ServiceError) else type(error).__name__,'scope':'isolated publication overlap'},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
