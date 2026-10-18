"""Compare real QueryService connection costs on one verified immutable snapshot.

This measures direct Python/SQLite calls, not HTTP or production capacity. The
reuse variant is an experiment pinned to one revision and one owning thread.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import sys
import threading
import time

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_forge_service.bundles import _verify_snapshot,DEFAULT_MAX_DATABASE_BYTES
from agent_forge_service.errors import ServiceError
from agent_forge_service.query import QueryService,Query
from agent_forge_service.settings import Settings
from agent_forge_service.storage import read_snapshot,encode_json


class ReusedConnectionExperiment(QueryService):
    """No cross-thread use, revision switching, or production pooling contract."""
    def __init__(self,settings,revision):
        super().__init__(settings)
        self.pinned=revision
        self.connection=None

    @contextmanager
    def _connection(self,revision):
        if revision!=self.pinned:
            raise ValueError('connection experiment is bound to one immutable revision')
        if self.connection is None:
            self.connection=read_snapshot(self.store.database(revision),self.settings.query_timeout_seconds)
        connection=self.connection
        deadline=time.monotonic()+self.settings.query_timeout_seconds
        connection.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
        failed=False
        try:
            yield connection
        except sqlite3.OperationalError as error:
            failed=True
            if 'interrupted' in str(error).lower():
                raise ServiceError('query_timeout','query exceeded its execution budget',503) from error
            raise
        finally:
            connection.set_progress_handler(None,0)
            if failed:
                connection.close();self.connection=None

    def close(self):
        if self.connection is not None:
            self.connection.close();self.connection=None


def signature(value):
    # Dynamic snapshot ages/freshness change between calls; identities and query
    # policy/cursor must remain identical throughout the experiment.
    if 'items' in value:
        stable={'ids':[item.get('id') for item in value['items']], 'nextCursor':value.get('nextCursor'),
            'sort':value.get('sort'),'scope':value.get('scope'),'policy':value.get('sortPolicyVersion')}
    else:
        stable={'id':value.get('id'),'canonical':value.get('canonicalRecord'),'latest':value.get('canonicalLatest')}
    return hashlib.sha256(encode_json(stable).encode()).hexdigest()


def execute(service,case,revision):
    if case['method']=='detail':
        return service.package(case['id'],version=case['version'],revision=revision)
    return service.packages(Query(**case['query']),cursor=case.get('cursor'),revision=revision)


def workload(service,manifest,depth=100):
    revision=manifest['revision']
    cases=[]
    def add(name,**query):cases.append({'name':name,'method':'packages','query':{'limit':20,**query}})
    for sort in ('canonical_updated','first_seen','name'):
        add(sort,sort=sort)
    add('english_common',q='mcp',sort='relevance')
    add('chinese_short',q='数据库',sort='relevance')
    if 'dsh' in manifest.get('agents',[]):add('agent_type',agent='dsh',type='plugin',sort='name')
    for sort in ('stars','star_growth','repo_pushed'):
        if manifest['schemaVersion']>=2:add(sort,sort=sort)
    for sort in ('downloads','download_growth'):
        if manifest['schemaVersion']>=3:add(sort,sort=sort,metric_source='npmjs.org')
    if manifest.get('features',{}).get('releaseSort'):add('latest_release',sort='latest_release')
    if manifest.get('features',{}).get('recommendations'):
        sources=service.recommendation_sources(revision=revision,limit=1)['items']
        if sources:add('source_filter',sort='name',recommendation_source=sources[0]['id'])
    cursor=None;pages=0
    for _ in range(depth):
        page=service.packages(Query(sort='name',limit=20),cursor=cursor,revision=revision)
        if page['nextCursor'] is None:break
        cursor=page['nextCursor'];pages+=1
    if cursor:cases.append({'name':'name_after_'+str(pages*20),'method':'packages','query':{'sort':'name','limit':20},'cursor':cursor})
    page=service.packages(Query(sort='name',limit=20),revision=revision)
    for item in page['items']:
        if item.get('canonicalLatest'):
            cases.append({'name':'exact_detail','method':'detail','id':item['id'],'version':item['canonicalLatest']});break
    for case in cases:
        value=execute(service,case,revision)
        case['expectedSignature']=signature(value)
        case['expectedItems']=len(value['items']) if 'items' in value else 1
    return cases


def rss_bytes():
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
                ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        value=Counters();value.cb=ctypes.sizeof(value)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.GetCurrentProcess.restype=wintypes.HANDLE
        psapi=ctypes.WinDLL('psapi',use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(value),value.cb):return value.WorkingSetSize
    elif sys.platform.startswith('linux'):
        try:return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        except (OSError,ValueError,IndexError):pass
    return None


def percentile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None


def summarize(records):
    times=[r['durationMs'] for r in records]
    return {'requests':len(records),'successes':sum(r['error'] is None for r in records),
        'errors':sum(r['error'] is not None for r in records),'p50Ms':percentile(times,.5),
        'p95Ms':percentile(times,.95),'p99Ms':percentile(times,.99),'maxMs':max(times) if times else None}


def run_phase(settings,revision,cases,*,workers,requests,strategy):
    if not 1<=workers<=64 or not workers<=requests<=100000 or strategy not in ('per_request','per_worker_reuse'):
        raise ValueError('invalid benchmark phase')
    steady_started=[None]
    gate=threading.Barrier(workers+1,action=lambda:steady_started.__setitem__(0,time.perf_counter()))
    memory=[];stop=threading.Event()
    def sample():
        while not stop.is_set():
            value=rss_bytes()
            if value is not None:memory.append(value)
            stop.wait(.05)
    sampler=threading.Thread(target=sample,daemon=True);sampler.start()
    def worker(number):
        service=QueryService(settings) if strategy=='per_request' else ReusedConnectionExperiment(settings,revision)
        records=[];preparation=time.perf_counter()
        try:
            service.store.load(revision)
            service.packages(Query(limit=20),revision=revision)
            prepared_ms=(time.perf_counter()-preparation)*1000
            gate.wait(timeout=90)
            for index in range(number,requests,workers):
                case=cases[index%len(cases)];start=time.perf_counter();error=None;items=None
                try:
                    value=execute(service,case,revision)
                    items=len(value['items']) if 'items' in value else 1
                    if signature(value)!=case['expectedSignature']:error='result_mismatch'
                except ServiceError as failure:error=failure.code
                except Exception as failure:error=type(failure).__name__
                records.append({'index':index,'case':case['name'],'durationMs':(time.perf_counter()-start)*1000,'items':items,'error':error})
            return records,prepared_ms
        except BaseException:
            gate.abort();raise
        finally:
            if isinstance(service,ReusedConnectionExperiment):service.close()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures=[pool.submit(worker,number) for number in range(workers)]
            gate.wait(timeout=90);start=steady_started[0]
            results=[future.result() for future in futures]
            elapsed=time.perf_counter()-start
    finally:
        stop.set();sampler.join(timeout=2)
    records=sorted([item for group,_ in results for item in group],key=lambda item:item['index'])
    by_case={name:summarize([r for r in records if r['case']==name]) for name in sorted({r['case'] for r in records})}
    return {'strategy':strategy,'workers':workers,'steadyStateSeconds':elapsed,'throughputPerSecond':len(records)/elapsed,
        'preparationPerWorkerMs':[ms for _,ms in results],'sampledPeakRssBytes':max(memory) if memory else None,
        'summary':summarize(records),'cases':by_case,'records':records}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--catalog',type=Path,default=Path.cwd())
    parser.add_argument('--revision',required=True)
    parser.add_argument('--workers',type=int,nargs='+',default=[1,20])
    parser.add_argument('--requests',type=int,default=280)
    parser.add_argument('--cursor-depth',type=int,default=100)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        settings=Settings(args.catalog,args.runtime)
        target=args.output.absolute()
        if target.exists() or target!=target.resolve() or target.is_relative_to(settings.runtime_root) or target.is_relative_to(settings.catalog_root/'sources'):
            raise ValueError('report output must be a new file outside runtime and canonical sources')
        if not args.workers or any(not 1<=w<=64 or w>args.requests for w in args.workers) or not 1<=args.requests<=100000 or not 0<=args.cursor_depth<=10000:
            raise ValueError('invalid request/worker/cursor-depth limits')
        service=QueryService(settings)
        # Validate all public tables before measuring and bind the exact bytes.
        service.store.load(args.revision)
        manifest,manifest_hash,database_hash=_verify_snapshot(settings.snapshots/args.revision,args.revision,DEFAULT_MAX_DATABASE_BYTES)
        cases=workload(service,manifest,args.cursor_depth)
        phases=[run_phase(settings,args.revision,cases,workers=workers,requests=args.requests,strategy=strategy)
            for workers in args.workers for strategy in ('per_request','per_worker_reuse')]
        output={'scope':'direct QueryService/SQLite; no HTTP, network, production pool or cold OS cache proof',
            'revision':args.revision,'manifestSha256':manifest_hash,'databaseSha256':database_hash,
            'counts':manifest['counts'],'features':manifest['features'],'databaseBytes':manifest['database']['bytes'],
            'cacheConditions':'Public bundle verification reads the database before tests; OS cache is not flushed. Workers validate and make one initial query before the timed phase.',
            'host':{'platform':platform.platform(),'python':sys.version,'sqlite':sqlite3.sqlite_version,'logicalCpus':os.cpu_count(),'processor':platform.processor()},
            'queryTimeoutSeconds':settings.query_timeout_seconds,'cases':cases,'phases':phases,
            'productionCapacityVerified':False}
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('x',encoding='utf-8') as stream:json.dump(output,stream,ensure_ascii=False,indent=2);stream.write('\n')
        print(json.dumps({'output':str(target),'revision':args.revision,'counts':manifest['counts'],
            'phases':[{'strategy':p['strategy'],'workers':p['workers'],**p['summary']} for p in phases]},ensure_ascii=False))
        return 1 if any(phase['summary']['errors'] for phase in phases) else 0
    except (OSError,ValueError,ServiceError,threading.BrokenBarrierError) as error:
        print(json.dumps({'error':error.code if isinstance(error,ServiceError) else type(error).__name__,'scope':'benchmark setup or execution'},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
