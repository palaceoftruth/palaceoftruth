"""Required, read-only evidence adapters for the smoke control CLI.

These queries require separately reviewed reader authority and control artifact
mounts. They do not assume the currently installed chart grants that access.
"""
from __future__ import annotations
import json
from datetime import timedelta
from pathlib import Path
from scripts import smoke_control as control

class ReadOnlyTransport:
    def __init__(self,client):self.client=client;self.base_url=client.base_url
    def request(self,method,path,**kw):
        if method!='GET':raise control.ControlError('automatic/preflight transport permits GET only')
        return self.client.request(method,path,**kw)

def runtime_binding(smoke,client,report,args,kube,a):
    d=a.doc
    selector='app.kubernetes.io/instance='+d['release_name']
    if args.pod_label_selector!=selector:raise control.ControlError('selector is not approved release instance')
    if args.namespace!=d['namespace'] or client.base_url!=d['api_base_url'] or args.scope_key!=d['scope']['key']:raise control.ControlError('CLI binding differs from approved manifest')
    ident=report.get('identity')
    if not isinstance(ident,dict):raise control.ControlError('fresh server identity missing')
    principal=d['principal'];credential=ident.get('credential_id') or (ident.get('mcp_client_id') if principal['mode']=='mcp_oauth' else None)
    if ident.get('tenant_id')!=d['tenant_id'] or ident.get('auth_mode')!=principal['mode'] or credential!=principal['id'] or (principal['mode']=='mcp_oauth' and ident.get('mcp_client_key')!=principal['client_key']):raise control.ControlError('server tenant/principal identity mismatch')
    grants=ident.get('allowed_scopes')
    required={'read'} if args.mode=='read-only' else {'read','write','write:workspace'}
    if not isinstance(grants,list) or not required.issubset(grants):raise control.ControlError('required existing grants not established; never widen them')
    cluster=kube.get('/api/v1/namespaces/kube-system');namespace=kube.get('/api/v1/namespaces/'+args.namespace)
    release=kube.get('/apis/helm.toolkit.fluxcd.io/v2/namespaces/'+args.namespace+'/helmreleases/'+d['release_name'])
    if cluster.get('metadata',{}).get('uid')!=d['cluster_uid'] or namespace.get('metadata',{}).get('uid')!=d['namespace_uid'] or release.get('metadata',{}).get('uid')!=d['release_uid']:raise control.ControlError('cluster/namespace/release UID mismatch')
    digest=d.get('writer_digest') if args.mode=='bounded' else d['target_digest']
    if not digest:raise control.ControlError('bounded writer artifact must be explicitly approved')
    history=release.get('status',{}).get('history',[])
    if not history or history[0].get('ociDigest')!=d['chart_digest']:raise control.ControlError('actual immutable chart missing/mismatched')
    values=release.get('spec',{}).get('values',{})
    effective=values.get('image',{}).get('backendDigest')
    if effective is not None and effective!=digest:raise control.ControlError('selected artifact differs from approved phase')
    replicas=values.get('highAvailability',{}).get('replicas',{})
    expected={name:replicas.get(name) for name in ['backend','mcp']}
    owners=d.get('workloads')
    if not owners or any(owners[k]['replicas']!=expected[k] for k in expected):raise control.ControlError('manifest-bound replica owners missing')
    if any(type(n) is not int or n<1 for n in expected.values()) or args.expected_sentinel_pods!=replicas.get('valkeySentinels'):raise control.ControlError('explicit expected runtime ensemble missing/mismatched')
    pods=kube.get('/api/v1/namespaces/'+args.namespace+'/pods',query={'labelSelector':args.pod_label_selector}).get('items')
    if not isinstance(pods,list):raise control.ControlError('artifact pod observations missing')
    found={name:[] for name in expected}
    for p in pods:
        if p.get('metadata',{}).get('deletionTimestamp'):continue
        for c in p.get('status',{}).get('containerStatuses',[]):
            if c.get('name') in found:
                meta=p.get('metadata',{})
                refs=meta.get('ownerReferences',[])
                if meta.get('labels',{}).get('app.kubernetes.io/instance')!=d['release_name'] or len(refs)!=1 or refs[0].get('kind')!='ReplicaSet' or refs[0].get('controller') is not True or refs[0].get('uid') not in owners[c['name']]['replicaset_uids']:raise control.ControlError('pod release owner mismatch')
                if c.get('ready') is not True or not isinstance(c.get('imageID'),str) or not c['imageID'].endswith('@'+digest):raise control.ControlError('actual image ID/readiness mismatch')
                found[c['name']].append({'pod_uid':p.get('metadata',{}).get('uid'),'imageID':c['imageID']})
    if any(len(found[k])!=expected[k] or not all(p['pod_uid'] for p in found[k]) for k in expected):raise control.ControlError('artifact replica evidence incomplete')
    if args.mode=='bounded':
        revision=d.get('hold_git_revision')
        if not revision or release['spec'].get('suspend') is not True:raise control.ControlError('bounded operator write requires explicit canonical hold')
        ks=kube.get('/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/k3s-lab-apps')
        git=kube.get('/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/flux-control')
        if ks.get('status',{}).get('lastAppliedRevision')!=revision or git.get('status',{}).get('artifact',{}).get('revision')!=revision:raise control.ControlError('canonical/applied revision mismatch')
        for resource in [ks,git]:
            generation=resource.get('metadata',{}).get('generation')
            conditions=resource.get('status',{}).get('conditions',[])
            ready=[r for r in conditions if r.get('type')=='Ready']
            if type(generation) is not int or len(ready)!=1 or ready[0].get('status')!='True' or ready[0].get('observedGeneration')!=generation or resource.get('status',{}).get('observedGeneration')!=generation:raise control.ControlError('source readiness is stale or failed')
        hold=d.get('canonical_hold')
        if not hold or hold['revision']!=revision or hold['parent_verified'] is not True:raise control.ControlError('parent-verified canonical artifact delivery required')
        # No artifact reader authority is assumed. Parent verification is external,
        # and the integrity pin cannot manufacture that authorization.
        import yaml
        canonical=yaml.safe_load(hold['blob'])
        if not isinstance(canonical,dict) or canonical.get('kind')!='HelmRelease' or canonical.get('metadata',{}).get('name')!=d['release_name'] or canonical.get('metadata',{}).get('namespace')!=d['namespace'] or canonical.get('spec',{}).get('suspend') is not True or canonical.get('spec',{}).get('values',{}).get('image',{}).get('backendDigest')!=digest:raise control.ControlError('canonical same-release hold not proven')
    smoke._record_check(report,'artifact_binding','passed',observations=found,release_uid=d['release_uid'],phase_digest=digest)

def temporal_observations(smoke,args,kube,a):
    now=smoke.utc_now();seconds=max(args.log_since_seconds,3600)
    selector='app.kubernetes.io/instance='+a.doc['release_name']
    payload=kube.get('/api/v1/namespaces/'+args.namespace+'/pods',query={'labelSelector':selector})
    workers=[];logs={}
    if not isinstance(payload.get('items'),list):raise control.ControlError('worker observations missing')
    for p in payload['items']:
        name=p.get('metadata',{}).get('name','');label=p.get('metadata',{}).get('labels',{}).get('app','')
        if args.worker_name_fragment not in name and args.worker_name_fragment not in label:continue
        if p.get('metadata',{}).get('deletionTimestamp'):raise control.ControlError('approved worker terminating; no unrelated counter subtraction')
        statuses=p.get('status',{}).get('containerStatuses')
        if not isinstance(statuses,list) or not statuses:raise control.ControlError('worker container evidence missing')
        for c in statuses:
            row={'pod':name,'uid':p.get('metadata',{}).get('uid'),'container':c.get('name'),'container_id':c.get('containerID'),'restart_count':c.get('restartCount'),'ready':c.get('ready'),'phase':p.get('status',{}).get('phase'),'last_terminated':c.get('lastState',{}).get('terminated',{}),'waiting':c.get('state',{}).get('waiting'),'terminated':c.get('state',{}).get('terminated')}
            workers.append(row)
            text=kube.get_text('/api/v1/namespaces/'+args.namespace+'/pods/'+name+'/log',query={'container':c.get('name'),'sinceSeconds':seconds+60,'tailLines':args.log_tail_lines,'timestamps':'true'})
            if not isinstance(text,str) or len(text.encode())>1048576 or len(text.splitlines())>=args.log_tail_lines:raise control.ControlError('bounded log coverage truncated/unavailable')
            lines=text.splitlines()
            if not lines:raise control.ControlError('empty logs do not prove coverage')
            timestamps=[]
            for line in lines:
                stamp,sep,_=line.partition(' ')
                if not sep:raise control.ControlError('log timestamp missing')
                timestamps.append(control.instant(stamp))
            started=control.instant(c.get('state',{}).get('running',{}).get('startedAt'))
            window=now-timedelta(seconds=seconds)
            if started>window or timestamps[0]>window or timestamps[-1]<now-timedelta(seconds=30) or timestamps[-1]>now or timestamps!=sorted(timestamps):raise control.ControlError('actual log/container interval does not cover observation window')
            logs[(row['uid'],row['container'])]={'readable':True,'covers_since':timestamps[0].isoformat(),'observed_at':smoke.utc_now().isoformat(),'text':text}
    # Immutable baseline fingerprints must agree independently; successful POST is irrelevant.
    if any('MasterNotFoundError' in e['text'] or 'No master found' in e['text'] for e in logs.values()):
        initial=smoke._worker_startup_state(kube,args)
        reconciled=smoke._reconcile_worker_master_discovery_errors(initial,kube,args,baseline_worker_state=initial)
        ensemble=reconciled.get('sentinel_pods',{})
        proof={'worker_recheck_stable':reconciled.get('worker_recheck_stable') is True,'sentinel_ensemble_stable':ensemble.get('ready') is True and len(ensemble.get('selected',[]))==args.expected_sentinel_pods,'sentinel_healthy':reconciled.get('sentinel_reconciliation',{}).get('healthy') is True}
        for e in logs.values():e['master_reconciliation']=proof
    return control.temporal_gate(a,workers,logs,now=smoke.utc_now(),threshold=args.restart_alert_threshold)

def preflight(smoke,client,args,kube,a):
    report={'target':args.target_name,'checks':[],'alerts':[],'generated_at':smoke.utc_now().isoformat()}
    a.active(smoke.utc_now())
    smoke._record_check(report,'budget_manifest','passed',manifest_sha256=a.digest,max_POST_invocations=1,max_new_records=1)
    calls=[lambda:smoke.check_api_health(client,report),
           lambda:smoke.check_runtime_dependencies(client,report,args,kube=kube),
           lambda:smoke.resolve_tenant_identity(client,report,args),
           lambda:runtime_binding(smoke,client,report,args,kube,a),
           lambda:smoke.asyncio.run(smoke.asyncio.wait_for(smoke.check_sentinel(report),timeout=args.reader_budget.timeout(10))),
           lambda:smoke.check_mcp_reachable(args.mcp_url,report,timeout=min(args.request_timeout,10),budget=args.reader_budget),
           lambda:smoke._record_check(report,'baseline_temporal','passed',**temporal_observations(smoke,args,kube,a))]
    for call in calls:
        try:
            args.reader_budget.timeout(10)
            call()
        except Exception as exc:smoke._alert(report,'required_preflight_exception','read-only prerequisite failed',error_class=type(exc).__name__)
    # Temporal and runtime checks include the required Kubernetes/log/ensemble evidence.
    if smoke._check_passed(report,'baseline_temporal') and smoke._check_passed(report,'runtime_dependencies'):smoke._record_check(report,'kubernetes_alerts','passed')
    return report

def readonly_receipt(smoke,client,args,a):
    if not args.receipt_file:raise control.ControlError('approved campaign receipt delivery missing')
    p=Path(args.receipt_file)
    try:envelope=json.loads(control.read_private(p,65536))
    except (OSError,ValueError) as exc:raise control.ControlError('receipt delivery corrupt') from exc
    if not isinstance(envelope,dict) or envelope.get('manifest_sha256')!=a.digest or envelope.get('payload_sha256')!=control.sha256_json(a.payload):raise control.ControlError('receipt envelope differs from approval')
    job=control.literal_uuid(envelope.get('receipt',{}).get('job_id'))
    fresh=control.recover_receipt(a,client,known_job_id=job)
    if envelope!=fresh:raise control.ControlError('fresh canonical receipt differs from delivered receipt')
    return fresh


# ---------------------------------------------------------------------------
# Autonomous READ-ONLY health lane.
#
# The automatic smoke Job supplies no approval manifest and no receipt file.
# This lane therefore validates LIVE bindings only (identity, dependencies,
# queue, sentinel/valkey, MCP, Kubernetes alerts, and a live temporal baseline)
# and fails closed when any required evidence is missing. It never loads a
# writer Approval and never demands a campaign receipt.
#
# Strictness (incident476 review C1-C5):
# - the workload binding derives the EXPECTED replica counts from the live
#   HelmRelease, evaluates ALL workload-replica pods (a wrong image cannot hide),
#   and pins ownership to the ReplicaSet controller;
# - the temporal evidence is an explicit {coverage_start, coverage_end,
#   collection_complete} record with a bounded freshness -- an old line alone is
#   not up-to-now proof;
# - kubernetes_alerts QUERIES real evidence (recent OOM terminations and
#   namespace warning events) instead of being derived.
# ---------------------------------------------------------------------------

WORKLOAD_CONTAINERS = ("backend", "mcp")
OOM_REASONS = {"OOMKilled", "BackoffLimitExceeded"}
FRESHNESS_SECONDS = 300
COVERAGE_TOLERANCE_SECONDS = 120


def _live_release(kube, namespace, release_name):
    return kube.get('/apis/helm.toolkit.fluxcd.io/v2/namespaces/' + namespace + '/helmreleases/' + release_name)


def _expected_workload_replicas(release):
    """Exact expected replica counts from the live HelmRelease, else fail."""
    values = (release.get('spec') or {}).get('values') or {}
    replicas = (values.get('highAvailability') or {}).get('replicas') or {}
    expected = {}
    for name in WORKLOAD_CONTAINERS:
        count = replicas.get(name)
        if type(count) is not int or count < 1:
            raise control.ControlError('desired replica count missing for ' + name)
        expected[name] = count
    return expected


def _workload_pods(kube, namespace, release_name):
    payload = kube.get('/api/v1/namespaces/' + namespace + '/pods',
                       query={'labelSelector': 'app.kubernetes.io/instance=' + release_name})
    items = payload.get('items')
    if not isinstance(items, list):
        raise control.ControlError('workload pod observations missing')
    return items


def _replicaset_owner(pod):
    """Return the owning ReplicaSet uid, or raise (no/foreign owner)."""
    refs = (pod.get('metadata') or {}).get('ownerReferences') or []
    if len(refs) != 1:
        raise control.ControlError('pod has no single owner')
    ref = refs[0]
    if ref.get('kind') != 'ReplicaSet' or ref.get('controller') is not True or not ref.get('uid'):
        raise control.ControlError('pod owner is not a controlled ReplicaSet')


def health_runtime_binding(smoke, client, report, args, kube):
    """Live-only artifact binding: expected counts, ALL pods, ReplicaSet owner."""
    release_name = args.target_name
    kube.get('/api/v1/namespaces/kube-system')
    kube.get('/api/v1/namespaces/' + args.namespace)
    release = _live_release(kube, args.namespace, release_name)
    if release.get('spec', {}).get('suspend') is not True:
        raise control.ControlError('canonical hold is not suspended')
    history = release.get('status', {}).get('history', [])
    digest = (release.get('spec', {}).get('values', {}).get('image') or {}).get('backendDigest')
    if not history or not history[0].get('ociDigest') or not digest:
        raise control.ControlError('live immutable chart/artifact evidence missing')
    expected = _expected_workload_replicas(release)
    found = {name: [] for name in expected}
    for pod in _workload_pods(kube, args.namespace, release_name):
        if pod.get('metadata', {}).get('deletionTimestamp'):
            continue
        refs = (pod.get('metadata') or {}).get('ownerReferences') or []
        if any(r.get('kind') in ('Job', 'CronJob') for r in refs):
            continue                      # hook/Job pods are not workload replicas
        statuses = pod.get('status', {}).get('containerStatuses') or []
        names = {c.get('name') for c in statuses}
        if not (names & set(expected)):
            continue                      # not a workload replica of interest
        if (pod.get('status') or {}).get('phase') != 'Running':
            raise control.ControlError('workload pod not running')
        _replicaset_owner(pod)            # foreign/absent owner fails
        for c in statuses:
            name = c.get('name')
            if name not in expected:
                continue
            image = c.get('imageID')
            # EVERY selected workload container must match the canonical image:
            # a wrong digest FAILS instead of being filtered out.
            if not isinstance(image, str) or not image.endswith('@' + digest):
                raise control.ControlError('workload container image digest mismatch: ' + str(name))
            if c.get('ready') is not True:
                raise control.ControlError('workload container not ready: ' + str(name))
            found[name].append({'pod_uid': pod.get('metadata', {}).get('uid'), 'container': name,
                                'imageID': image, 'ready': True})
    for name, count in expected.items():
        if len(found[name]) != count:
            raise control.ControlError('expected replicas not all observed for ' + name)
    smoke._record_check(report, 'artifact_binding', 'passed', observations=found, phase_digest=digest)


def _termination_is_recent(term, floor):
    if not isinstance(term, dict) or not term:
        return False
    try:
        finished = control.instant(term.get('finishedAt'))
    except control.ControlError:
        return True                        # unparsable recent evidence is not proof
    return finished >= floor


def _health_workers(smoke, args, kube, floor):
    """Two-pass worker observations; recent OOM or a restart since the floor fails."""
    selector = 'app.kubernetes.io/instance=' + args.target_name
    payload = kube.get('/api/v1/namespaces/' + args.namespace + '/pods', query={'labelSelector': selector})
    items = payload.get('items')
    if not isinstance(items, list) or not items:
        raise control.ControlError('health worker observations missing')
    rows = []
    for pod in items:
        name = pod.get('metadata', {}).get('name', '')
        label = pod.get('metadata', {}).get('labels', {}).get('app', '')
        if args.worker_name_fragment not in name and args.worker_name_fragment not in label:
            continue
        if pod.get('metadata', {}).get('deletionTimestamp'):
            raise control.ControlError('approved worker terminating')
        statuses = pod.get('status', {}).get('containerStatuses')
        if not isinstance(statuses, list) or not statuses:
            raise control.ControlError('worker container evidence missing')
        if pod.get('status', {}).get('phase') != 'Running':
            raise control.ControlError('worker not running')
        for c in statuses:
            if c.get('ready') is not True or c.get('state', {}).get('waiting') or c.get('state', {}).get('terminated'):
                raise control.ControlError('worker not healthy')
            term = (c.get('lastState') or {}).get('terminated') or {}
            count = c.get('restartCount')
            if type(count) is not int or count < 0:
                raise control.ControlError('restart counter malformed')
            if count >= args.restart_alert_threshold:
                raise control.ControlError('restart counter at/over threshold')
            if term and (_termination_is_recent(term, floor)):
                raise control.ControlError('recent worker termination is not proof of health')
            rows.append({'pod': name, 'uid': pod.get('metadata', {}).get('uid'),
                         'container': c.get('name'), 'container_id': c.get('containerID'),
                         'restart_count': count,
                         'started_at': (c.get('state', {}).get('running') or {}).get('startedAt')})
    if not rows:
        raise control.ControlError('no approved workers observed')
    return rows


def _health_log_coverage(smoke, args, kube, rows, now, floor):
    """Explicit coverage record; bounded freshness; truncation is rejected."""
    seconds = max(args.log_since_seconds, 3600)
    records = []
    for row in rows:
        text = kube.get_text('/api/v1/namespaces/' + args.namespace + '/pods/' + row['pod'] + '/log',
                             query={'container': row['container'], 'sinceSeconds': seconds + 60,
                                    'tailLines': args.log_tail_lines, 'timestamps': 'true'})
        if not isinstance(text, str):
            raise control.ControlError('log read failed')
        lines = text.splitlines()
        if not lines:
            # A newly started container with no lines cannot prove coverage.
            raise control.ControlError('empty logs cannot prove coverage')
        if len(text.encode()) > 1048576 or len(lines) >= args.log_tail_lines:
            raise control.ControlError('log capture truncated (cap/overflow)')
        stamps = []
        for line in lines:
            stamp, sep, _ = line.partition(' ')
            if not sep:
                raise control.ControlError('log timestamp missing')
            stamps.append(control.instant(stamp))
        if stamps != sorted(stamps):
            raise control.ControlError('log timestamps out of order')
        try:
            started = control.instant(row.get('started_at'))
        except control.ControlError:
            started = None
        # The floor is the later of the log window and this container's own
        # start: a freshly rolled pod legitimately has no older history.
        start_floor = floor if started is None else max(floor, started)
        coverage_start = max(stamps[0], start_floor)
        if stamps[0] > start_floor + timedelta(seconds=COVERAGE_TOLERANCE_SECONDS):
            raise control.ControlError('coverage does not start at the floor')
        # Bounded freshness: an old last line alone is NOT up-to-now proof.
        if stamps[-1] < now - timedelta(seconds=FRESHNESS_SECONDS):
            raise control.ControlError('log coverage is not fresh up to now')
        if stamps[-1] > now:
            raise control.ControlError('log coverage is in the future')
        records.append({'pod': row['pod'], 'container': row['container'],
                        'coverage_start': coverage_start.isoformat(),
                        'coverage_end': stamps[-1].isoformat(),
                        'collection_complete': True})
    return records


def health_temporal(smoke, args, kube):
    """Live two-pass temporal baseline with explicit coverage and freshness."""
    if args.restart_alert_threshold != 3 or args.log_since_seconds < 3600:
        raise control.ControlError('required mode forbids weaker gates')
    now = smoke.utc_now()
    floor = now - timedelta(seconds=max(args.log_since_seconds, 3600))
    first = _health_workers(smoke, args, kube, floor)
    coverage = _health_log_coverage(smoke, args, kube, first, now, floor)
    second = _health_workers(smoke, args, kube, floor)
    keys = lambda rows: sorted((r['uid'], r['container'], r['container_id'], r['restart_count']) for r in rows)
    if keys(first) != keys(second):
        raise control.ControlError('worker identity/counter changed during the observation window')
    return {'workers': second, 'passes': 2, 'coverage': coverage}


def health_kubernetes_alerts(smoke, args, kube, now):
    """QUERY real evidence: recent OOM terminations and namespace warning events."""
    floor = now - timedelta(seconds=max(args.log_since_seconds, 3600))
    for pod in _workload_pods(kube, args.namespace, args.target_name):
        for c in (pod.get('status') or {}).get('containerStatuses') or []:
            term = (c.get('lastState') or {}).get('terminated') or {}
            if not term:
                continue
            if term.get('reason') in ('OOMKilled',) or term.get('exitCode') == 137:
                if _termination_is_recent(term, floor):
                    raise control.ControlError('recent OOM termination observed')
    try:
        events = kube.get('/api/v1/namespaces/' + args.namespace + '/events').get('items')
    except Exception as exc:  # noqa: BLE001
        raise control.ControlError('event query failed') from exc
    if not isinstance(events, list):
        raise control.ControlError('event evidence missing')
    for e in events:
        if e.get('reason') not in OOM_REASONS:
            continue
        if e.get('involvedObject', {}).get('namespace') != args.namespace:
            continue
        stamp = e.get('lastTimestamp') or e.get('eventTime') or e.get('firstTimestamp')
        if not isinstance(stamp, str):
            raise control.ControlError('event without timestamp is not proof')
        try:
            when = control.instant(stamp)
        except control.ControlError as exc:
            raise control.ControlError('event timestamp unparsable') from exc
        if when >= floor:
            raise control.ControlError('recent Kubernetes alert event: ' + str(e.get('reason')))


def health_preflight(smoke, client, args, kube):
    """Read-only health preflight. No Approval manifest, no writer receipt."""
    report = {'target': args.target_name, 'checks': [], 'alerts': [], 'generated_at': smoke.utc_now().isoformat()}
    calls = [lambda: smoke.check_api_health(client, report),
             lambda: smoke.check_runtime_dependencies(client, report, args, kube=kube),
             lambda: smoke.resolve_tenant_identity(client, report, args),
             lambda: health_runtime_binding(smoke, client, report, args, kube),
             lambda: smoke.asyncio.run(smoke.asyncio.wait_for(smoke.check_sentinel(report), timeout=args.reader_budget.timeout(10))),
             lambda: smoke.check_mcp_reachable(args.mcp_url, report, timeout=min(args.request_timeout, 10), budget=args.reader_budget),
             lambda: smoke._record_check(report, 'baseline_temporal', 'passed', **health_temporal(smoke, args, kube)),
             lambda: health_kubernetes_alerts(smoke, args, kube, smoke.utc_now()),
             lambda: smoke._record_check(report, 'kubernetes_alerts', 'passed')]
    for call in calls[:-1]:
        try:
            args.reader_budget.timeout(10)
            call()
        except Exception as exc:
            smoke._alert(report, 'required_preflight_exception', 'read-only health prerequisite failed', error_class=type(exc).__name__)
    if smoke._check_passed(report, 'baseline_temporal') and smoke._check_passed(report, 'runtime_dependencies'):
        try:
            calls[-1]()
        except Exception as exc:
            smoke._alert(report, 'required_preflight_exception', 'kubernetes alert stamp failed', error_class=type(exc).__name__)
    return report
