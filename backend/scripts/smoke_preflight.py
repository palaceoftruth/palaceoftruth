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
# queue, sentinel/valkey, MCP, Kubernetes, and a live temporal baseline) and
# fails closed when any required evidence is missing. It never loads a writer
# Approval and never demands a campaign receipt.
# ---------------------------------------------------------------------------

def _health_workers(smoke, args, kube):
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
            count = c.get('restartCount')
            if type(count) is not int or count < 0 or count >= args.restart_alert_threshold:
                raise control.ControlError('restart counter at/over threshold')
            rows.append({'pod': name, 'uid': pod.get('metadata', {}).get('uid'),
                         'container': c.get('name'), 'container_id': c.get('containerID'),
                         'restart_count': count,
                         'last_terminated': c.get('lastState', {}).get('terminated', {})})
    if not rows:
        raise control.ControlError('no approved workers observed')
    return rows


def _health_log_coverage(smoke, args, kube, rows, now):
    seconds = max(args.log_since_seconds, 3600)
    seen = 0
    for row in rows:
        text = kube.get_text('/api/v1/namespaces/' + args.namespace + '/pods/' + row['pod'] + '/log',
                             query={'container': row['container'], 'sinceSeconds': seconds + 60,
                                    'tailLines': args.log_tail_lines, 'timestamps': 'true'})
        if not isinstance(text, str) or not text.splitlines():
            raise control.ControlError('empty logs do not prove coverage')
        if len(text.encode()) > 1048576 or len(text.splitlines()) >= args.log_tail_lines:
            raise control.ControlError('bounded log coverage truncated/unavailable')
        stamps = []
        for line in text.splitlines():
            stamp, sep, _ = line.partition(' ')
            if not sep:
                raise control.ControlError('log timestamp missing')
            stamps.append(control.instant(stamp))
        window = now - timedelta(seconds=seconds)
        if stamps[0] > window or stamps[-1] < now - timedelta(seconds=30) or stamps[-1] > now or stamps != sorted(stamps):
            raise control.ControlError('actual log interval does not cover observation window')
        seen += 1
    return seen


def health_runtime_binding(smoke, client, report, args, kube):
    """Live-only artifact binding for the read-only lane (no approval manifest)."""
    release_name = args.target_name
    kube.get('/api/v1/namespaces/kube-system')
    kube.get('/api/v1/namespaces/' + args.namespace)
    release = kube.get('/apis/helm.toolkit.fluxcd.io/v2/namespaces/' + args.namespace + '/helmreleases/' + release_name)
    if release.get('spec', {}).get('suspend') is not True:
        raise control.ControlError('canonical hold is not suspended')
    history = release.get('status', {}).get('history', [])
    digest = release.get('spec', {}).get('values', {}).get('image', {}).get('backendDigest')
    if not history or not history[0].get('ociDigest') or not digest:
        raise control.ControlError('live immutable chart/artifact evidence missing')
    pods = kube.get('/api/v1/namespaces/' + args.namespace + '/pods',
                    query={'labelSelector': 'app.kubernetes.io/instance=' + release_name}).get('items')
    if not isinstance(pods, list) or not pods:
        raise control.ControlError('live artifact pod observations missing')
    observed = []
    for pod in pods:
        for c in pod.get('status', {}).get('containerStatuses', []) or []:
            image = c.get('imageID')
            if isinstance(image, str) and image.endswith('@' + digest):
                observed.append({'pod_uid': pod.get('metadata', {}).get('uid'),
                                 'container': c.get('name'), 'imageID': image, 'ready': c.get('ready')})
    if not observed or any(o['ready'] is not True for o in observed):
        raise control.ControlError('running pods do not match the live backend digest')
    smoke._record_check(report, 'artifact_binding', 'passed', observations=observed, phase_digest=digest)


def health_temporal(smoke, args, kube):
    """Live two-pass temporal baseline: the same run re-reads and must agree."""
    if args.restart_alert_threshold != 3 or args.log_since_seconds < 3600:
        raise control.ControlError('required mode forbids weaker gates')
    first = _health_workers(smoke, args, kube)
    _health_log_coverage(smoke, args, kube, first, smoke.utc_now())
    second = _health_workers(smoke, args, kube)
    keys = lambda rows: sorted((r['uid'], r['container'], r['container_id'], r['restart_count']) for r in rows)
    if keys(first) != keys(second):
        raise control.ControlError('worker identity/counter changed during the observation window')
    return {'workers': second, 'passes': 2}


def health_preflight(smoke, client, args, kube):
    """Read-only health preflight. No Approval manifest, no writer receipt."""
    report = {'target': args.target_name, 'checks': [], 'alerts': [], 'generated_at': smoke.utc_now().isoformat()}
    calls = [lambda: smoke.check_api_health(client, report),
             lambda: smoke.check_runtime_dependencies(client, report, args, kube=kube),
             lambda: smoke.resolve_tenant_identity(client, report, args),
             lambda: health_runtime_binding(smoke, client, report, args, kube),
             lambda: smoke.asyncio.run(smoke.asyncio.wait_for(smoke.check_sentinel(report), timeout=args.reader_budget.timeout(10))),
             lambda: smoke.check_mcp_reachable(args.mcp_url, report, timeout=min(args.request_timeout, 10), budget=args.reader_budget),
             lambda: smoke._record_check(report, 'baseline_temporal', 'passed', **health_temporal(smoke, args, kube))]
    for call in calls:
        try:
            args.reader_budget.timeout(10)
            call()
        except Exception as exc:
            smoke._alert(report, 'required_preflight_exception', 'read-only health prerequisite failed', error_class=type(exc).__name__)
    if smoke._check_passed(report, 'baseline_temporal') and smoke._check_passed(report, 'runtime_dependencies'):
        smoke._record_check(report, 'kubernetes_alerts', 'passed')
    return report
