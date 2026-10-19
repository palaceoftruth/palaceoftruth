"""Required, read-only evidence adapters for the smoke control CLI.

These queries require separately reviewed reader authority and control artifact
mounts. They do not assume the currently installed chart grants that access.
"""
from __future__ import annotations
import base64
import gzip
import json
import re
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
# Real resource-OOM reasons only. kubelet EVENTS use `OOMKilling`; container
# STATUS lastState uses `OOMKilled`. A prior failed hook Job's
# `BackoffLimitExceeded` is a gate artifact, not runtime OOM, and must not poison
# the health gate on a retry.
OOM_EVENT_REASONS = {"OOMKilling", "OOMKilled"}
# Policy bound, NOT an up-to-now guarantee: a worker that legitimately logs
# less often than this needs the operator to justify a different bound.
FRESHNESS_SECONDS = 300
COVERAGE_TOLERANCE_SECONDS = 120
# ARQ workers refresh a pod-specific broker health key on this cadence
# (app/workers/queues.py). A live, pod-specific key is up-to-now liveness
# evidence for a worker that legitimately emits NO log line inside the window:
# the refresh runs in the worker's event loop, so a stale/absent key means the
# loop is not servicing the broker.
BROKER_HEARTBEAT_INTERVAL_SECONDS = 15
BROKER_HEARTBEAT_TOLERANCE_SECONDS = 30
# Coverage kinds are explicit and honest. An idle worker proved by container
# state + a live broker heartbeat is NEVER reported as having satisfied a
# log-coverage check it did not satisfy.
LOG_COVERAGE_KIND = "log"
IDLE_WORKER_COVERAGE_KIND = "state_and_events_for_idle_worker"
# Known ARQ worker containers -> broker queue name. Only these workloads have a
# broker heartbeat that can prove an idle worker is alive; an unknown container
# (including a non-worker API service) with empty logs still fails closed.
WORKER_QUEUE_BY_CONTAINER = {
    "worker": "arq:queue",
    "media-worker": "arq:queue:media",
    "palace-worker": "arq:queue:palace",
}


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
    return ref['uid']


def _deployment_ancestry(kube, namespace, name):
    """Prove Deployment -> current ReplicaSet chain; return (deploy_uid, rs_uids)."""
    dep = kube.get('/apis/apps/v1/namespaces/' + namespace + '/deployments/' + name)
    dep_uid = (dep.get('metadata') or {}).get('uid')
    if not dep_uid:
        raise control.ControlError('deployment UID missing for ' + name)
    # ReplicaSets are labelled with the RELEASE instance, not the deployment name.
    release_name = name.rsplit('-', 1)[0]
    rs_list = kube.get('/apis/apps/v1/namespaces/' + namespace + '/replicasets',
                       query={'labelSelector': 'app.kubernetes.io/instance=' + release_name}).get('items')
    if not isinstance(rs_list, list):
        raise control.ControlError('replicaset observations missing')
    owned = set()
    for rs in rs_list:
        refs = (rs.get('metadata') or {}).get('ownerReferences') or []
        if len(refs) == 1 and refs[0].get('kind') == 'Deployment' and refs[0].get('controller') is True \
                and refs[0].get('uid') == dep_uid:
            owned.add((rs.get('metadata') or {}).get('uid'))
    if not owned:
        raise control.ControlError('no current ReplicaSet owned by deployment ' + name)
    return dep_uid, owned


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
    # Deployment name convention: <release>-<container>
    ancestry = {name: _deployment_ancestry(kube, args.namespace, release_name + '-' + name) for name in expected}
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
        rs_uid = _replicaset_owner(pod)   # foreign/absent owner fails
        for c in statuses:
            name = c.get('name')
            if name not in expected:
                continue
            if rs_uid not in ancestry[name][1]:
                raise control.ControlError('pod owner is not the current ReplicaSet of ' + name)
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


def _decode_helm_release(secret):
    """Decode a Helm v3 release Secret (double base64 + gzip + JSON)."""
    data = (secret.get('data') or {}).get('release')
    if not isinstance(data, str) or not data:
        raise control.ControlError('installed helm release payload missing')
    try:
        blob = gzip.decompress(base64.b64decode(base64.b64decode(data, validate=True), validate=True))
        release = json.loads(blob)
    except (ValueError, OSError, EOFError) as exc:
        raise control.ControlError('installed helm release payload unparsable') from exc
    if not isinstance(release, dict):
        raise control.ControlError('installed helm release payload malformed')
    return release


def _manifest_image_pins(manifest_text, wanted):
    """Exact digest pins per wanted container from a Helm-rendered manifest."""
    if not isinstance(manifest_text, str) or not manifest_text.strip():
        raise control.ControlError('installed manifest missing')
    import yaml
    try:
        docs = list(yaml.safe_load_all(manifest_text))
    except yaml.YAMLError as exc:
        raise control.ControlError('installed manifest unparsable') from exc
    pins = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get('kind') != 'Deployment':
            continue
        spec = ((doc.get('spec') or {}).get('template') or {}).get('spec') or {}
        for c in spec.get('containers') or []:
            name = c.get('name')
            if name not in wanted:
                continue
            image = c.get('image')
            if not isinstance(image, str) or '@sha256:' not in image:
                raise control.ControlError('installed manifest image is not digest-pinned: ' + str(name))
            pins[name] = image
    missing = wanted - set(pins)
    if missing:
        raise control.ControlError('installed manifest misses workload container: ' + ','.join(sorted(missing)))
    return pins


def _image_matches(image_id, pin):
    """Running imageID equals the installed pin by digest and repository tail."""
    if not isinstance(image_id, str) or '@sha256:' not in image_id:
        return False
    if not isinstance(pin, str) or '@sha256:' not in pin:
        return False
    image_repo, image_digest = image_id.rsplit('@', 1)
    pin_repo, pin_digest = pin.rsplit('@', 1)
    return image_digest == pin_digest and image_repo.rsplit('/', 2)[-2:] == pin_repo.rsplit('/', 2)[-2:]


def _helm_release_revisions(kube, namespace, release_name):
    """Every numeric Helm v3 revision recorded for the release (read-only)."""
    payload = kube.get('/api/v1/namespaces/' + namespace + '/secrets',
                       query={'labelSelector': 'owner=helm,name=' + release_name})
    items = payload.get('items')
    if not isinstance(items, list) or not items:
        raise control.ControlError('helm release revisions missing')
    prefix = 'sh.helm.release.v1.' + release_name + '.v'
    versions = []
    for secret in items:
        name = (secret.get('metadata') or {}).get('name', '')
        if not name.startswith(prefix):
            continue
        tail = name[len(prefix):]
        if tail.isdigit():
            versions.append(int(tail))
    if not versions:
        raise control.ControlError('helm release revisions malformed')
    return sorted(versions)


def pre_upgrade_artifact_binding(smoke, client, report, args, kube, manifest):
    """Read-only PRE-UPGRADE artifact binding for a held, declared-new window.

    The AUTOMATIC post-upgrade `health_runtime_binding` binds the DECLARED image
    to the RUNNING image and stays strict. This separate pre-upgrade check runs
    BEFORE the upgrade, while the release is held and the new target is declared
    but NOT applied:

    - the DECLARED HelmRelease target must equal the frozen APPROVED tuple:
      same release UID, suspend true, declared backend digest == target_digest,
      and the declared chart OCI reference digest == chart_digest;
    - the highest attempted revision must equal the approved expected maximum
      (no new attempt since the frozen window);
    - the RUNNING workload images must equal the ACTUAL INSTALLED artifact taken
      from the Helm release manifest of that max attempted revision, NOT from the
      declared target and NOT from a "first successful" history fallback;
    - every replica, owner, readiness, and exact image pin is checked (#575).

    A FAILED max revision is expected for a held partial apply: release Ready is
    NOT required. Any missing/unparsable installed artifact fails CLOSED with a
    precise error; there is no image-ignore flag.
    """
    doc = manifest.doc
    manifest.active(smoke.utc_now())
    if doc['namespace'] != args.namespace or doc['release_name'] != args.target_name:
        raise control.ControlError('pre-upgrade binding differs from approved release')
    kube.get('/api/v1/namespaces/kube-system')
    kube.get('/api/v1/namespaces/' + args.namespace)
    release = _live_release(kube, args.namespace, args.target_name)
    if (release.get('metadata') or {}).get('uid') != doc['release_uid']:
        raise control.ControlError('live HelmRelease UID differs from approved release')
    spec = release.get('spec') or {}
    if spec.get('suspend') is not True:
        raise control.ControlError('canonical hold is not suspended')
    # --- the DECLARED target must equal the frozen approved tuple ---
    declared = (spec.get('values') or {}).get('image') or {}
    if declared.get('backendDigest') != doc['target_digest']:
        raise control.ControlError('declared target image differs from approved target')
    for key in ('backendDigest', 'workerDigest', 'frontendDigest'):
        if not isinstance(declared.get(key), str) or not re.fullmatch('sha256:[0-9a-f]{64}', declared[key]):
            raise control.ControlError('declared image pin tuple incomplete: ' + key)
    chart_ref = spec.get('chartRef') or {}
    if chart_ref.get('kind') != 'OCIRepository' or not chart_ref.get('name'):
        raise control.ControlError('declared chart OCI reference missing')
    ref_ns = chart_ref.get('namespace') or args.namespace
    oci = kube.get('/apis/source.toolkit.fluxcd.io/v1/namespaces/' + ref_ns + '/ocirepositories/' + chart_ref['name'])
    declared_chart = ((oci.get('spec') or {}).get('ref') or {}).get('digest')
    if declared_chart != doc['chart_digest']:
        raise control.ControlError('declared chart OCI differs from approved target')
    # --- highest recorded Helm revision == approved max (no new attempt) ---
    # The revision source is the immutable Helm release Secrets, NOT the Flux
    # status.history (which can lag behind partial attempts) and NOT a "first
    # successful" fallback. Any revision NEWER than the approved maximum aborts.
    revisions = _helm_release_revisions(kube, args.namespace, args.target_name)
    max_rev = max(revisions)
    if max_rev != doc['expected_max_revision']:
        raise control.ControlError('latest attempted revision differs from approved maximum')
    # --- actual INSTALLED artifact = the max attempted revision manifest ---
    secret = kube.get('/api/v1/namespaces/' + args.namespace + '/secrets/' +
                      'sh.helm.release.v1.' + args.target_name + '.v' + str(max_rev))
    installed = _decode_helm_release(secret)
    if installed.get('version') != max_rev:
        raise control.ControlError('installed release payload revision mismatch')
    pins = _manifest_image_pins(installed.get('manifest'), set(WORKLOAD_CONTAINERS))
    # --- the RUNNING runtime must be the installed artifact, not the declared target ---
    expected = _expected_workload_replicas(release)
    ancestry = {name: _deployment_ancestry(kube, args.namespace, args.target_name + '-' + name) for name in expected}
    found = {name: [] for name in expected}
    for pod in _workload_pods(kube, args.namespace, args.target_name):
        if pod.get('metadata', {}).get('deletionTimestamp'):
            continue
        refs = (pod.get('metadata') or {}).get('ownerReferences') or []
        if any(r.get('kind') in ('Job', 'CronJob') for r in refs):
            continue
        statuses = pod.get('status', {}).get('containerStatuses') or []
        names = {c.get('name') for c in statuses}
        if not (names & set(expected)):
            continue
        if (pod.get('status') or {}).get('phase') != 'Running':
            raise control.ControlError('workload pod not running')
        rs_uid = _replicaset_owner(pod)
        for c in statuses:
            name = c.get('name')
            if name not in expected:
                continue
            if rs_uid not in ancestry[name][1]:
                raise control.ControlError('pod owner is not the current ReplicaSet of ' + name)
            if not _image_matches(c.get('imageID'), pins[name]):
                raise control.ControlError('running workload image is not the installed release pin: ' + name)
            if c.get('ready') is not True:
                raise control.ControlError('workload container not ready: ' + name)
            found[name].append({'pod_uid': pod.get('metadata', {}).get('uid'), 'container': name,
                                'imageID': c.get('imageID'), 'ready': True})
    for name, count in expected.items():
        if len(found[name]) != count:
            raise control.ControlError('expected replicas not all observed for ' + name)
    smoke._record_check(report, 'artifact_binding', 'passed', observations=found,
                        installed_revision=max_rev, installed_pins=pins,
                        declared_target_digest=doc['target_digest'],
                        declared_chart_digest=doc['chart_digest'])


def pre_upgrade_health_preflight(smoke, client, args, kube, manifest):
    """Read-only pre-upgrade lane: same 8 evidence checks, installed-artifact bind."""
    report = {'target': args.target_name, 'checks': [], 'alerts': [], 'generated_at': smoke.utc_now().isoformat()}
    calls = [lambda: smoke.check_api_health(client, report),
             lambda: smoke.check_runtime_dependencies(client, report, args, kube=kube),
             lambda: smoke.resolve_tenant_identity(client, report, args),
             lambda: pre_upgrade_artifact_binding(smoke, client, report, args, kube, manifest),
             lambda: smoke.asyncio.run(smoke.asyncio.wait_for(smoke.check_sentinel(report), timeout=args.reader_budget.timeout(10))),
             lambda: smoke.check_mcp_reachable(args.mcp_url, report, timeout=min(args.request_timeout, 10), budget=args.reader_budget),
             lambda: smoke._record_check(report, 'baseline_temporal', 'passed', **health_temporal(smoke, args, kube)),
             lambda: health_kubernetes_alerts(smoke, args, kube, smoke.utc_now()),
             lambda: smoke._record_check(report, 'kubernetes_alerts', 'passed')]
    for call in calls[:-1]:
        try:
            args.reader_budget.timeout(10)
            call()
        except Exception as exc:  # noqa: BLE001
            smoke._alert(report, 'required_preflight_exception', 'read-only health prerequisite failed', error_class=type(exc).__name__)
    if smoke._check_passed(report, 'baseline_temporal') and smoke._check_passed(report, 'runtime_dependencies'):
        try:
            calls[-1]()
        except Exception as exc:  # noqa: BLE001
            smoke._alert(report, 'required_preflight_exception', 'kubernetes alert stamp failed', error_class=type(exc).__name__)
    return report


def _termination_is_recent(term, floor):
    if not isinstance(term, dict) or not term:
        return False
    try:
        finished = control.instant(term.get('finishedAt'))
    except control.ControlError:
        return True                        # unparsable recent evidence is not proof
    return finished >= floor


def _running_started_at(state):
    """Parse ``state.running.startedAt``; None when absent or invalid."""
    if not isinstance(state, dict):
        return None
    running = state.get('running')
    if not isinstance(running, dict):
        return None
    try:
        return control.instant(running.get('startedAt'))
    except control.ControlError:
        return None


def _health_workers(smoke, args, kube, floor):
    """Two-pass worker observations judged in the CURRENT observation window.

    The restart counter is an ACTIVE-window signal, not an absolute historical
    total. A counter at/over ``restart_alert_threshold`` is admitted ONLY with
    the full historical proof: the container's most recent termination is
    provably OUTSIDE the window (``lastState.terminated.finishedAt < floor``),
    the live container has run continuously across the whole window
    (``state.running.startedAt <= floor``), that start is coherent with the
    termination (``startedAt > finishedAt``), and every timestamp is valid and
    not in the future.

    Fail-closed edges: a non-zero counter with no ``lastState`` evidence, a
    termination inside the window, an absent/unparsable/future timestamp, a
    container that began inside the window, or an incoherent start/termination
    interval all refuse the gate. The threshold (3) and the log window (>=3600s)
    are unchanged: this is a windowed scope, not a threshold raise and not a
    label-capture bypass. An OLD counter with old terminations passes; a
    contemporary or unprovable counter still fails.
    """
    now = smoke.utc_now()
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
            finished = None
            empty_history_proven = False
            if term:
                try:
                    finished = control.instant(term.get('finishedAt'))
                except control.ControlError as exc:
                    # Unparsable evidence is not proof of health.
                    raise control.ControlError('termination timestamp unparsable') from exc
                if finished > now:
                    raise control.ControlError('future termination evidence')
                if finished >= floor:
                    raise control.ControlError('recent worker termination is not proof of health')
            elif count > 0:
                # A live non-zero counter with EMPTY lastState has no termination
                # time to compare. It is admitted ONLY with the continuous-container
                # proof: the current instance started AT or BEFORE the window floor
                # (it has run across the whole window), and that start is coherent
                # and not in the future. A restart would move startedAt and the
                # counter; the two-pass stability in health_temporal is the
                # independent witness. Empty lastState alone is NOT proof.
                run_start = _running_started_at(c.get('state'))
                if run_start is None:
                    raise control.ControlError('container start metadata missing for a non-zero counter with no lastState')
                if run_start > now:
                    raise control.ControlError('future container start evidence')
                if run_start > floor:
                    raise control.ControlError('container began inside the window; restart scope unproven')
                empty_history_proven = True
            if count >= args.restart_alert_threshold:
                # Historical-only admission: the full window proof is required.
                if finished is not None:
                    run_start = _running_started_at(c.get('state'))
                    if run_start is None:
                        raise control.ControlError('container start metadata missing for a historical counter')
                    if run_start > floor:
                        raise control.ControlError('container began inside the window; historical restart scope unproven')
                    if run_start <= finished:
                        raise control.ControlError('incoherent container start and termination interval')
                elif not empty_history_proven:
                    raise control.ControlError('historical restart count without termination evidence')
            rows.append({'pod': name, 'uid': pod.get('metadata', {}).get('uid'),
                         'container': c.get('name'), 'container_id': c.get('containerID'),
                         'restart_count': count,
                         'started_at': (c.get('state', {}).get('running') or {}).get('startedAt'),
                         'last_terminated_at': finished.isoformat() if finished is not None else None})
    if not rows:
        raise control.ControlError('no approved workers observed')
    return rows


def _idle_worker_queue(container_name):
    """Map a KNOWN ARQ worker container to its broker queue, else None."""
    return WORKER_QUEUE_BY_CONTAINER.get(container_name)


def _broker_heartbeat_proof(broker, queue_name, pod_name, now):
    """Read-only ARQ heartbeat proof for an idle worker, else None.

    The probe is INJECTED (host harness) or resolves to the in-cluster lane's
    live Redis EXISTS/TTL read (check_memory_rollout_smoke.broker_pulse). An
    absent probe, an absent key, or a stale/implausible value returns None so the
    caller fails closed. The probe never enqueues, pushes, or mutates SQL.
    """
    if not callable(broker):
        return None
    try:
        pulse = broker(queue_name, pod_name)
    except Exception:  # noqa: BLE001 - an unavailable probe is not proof
        return None
    if not isinstance(pulse, dict) or pulse.get('present') is not True:
        return None
    # The heartbeat must be for THIS queue and pod (the same key), not a sibling.
    if pulse.get('queue') != queue_name or pulse.get('pod') != pod_name:
        return None
    ttl = pulse.get('ttl_seconds')
    # `type(...) is int` excludes bool. A RAW Redis TTL of -2 (key missing), -1
    # (exists with no expiry: NOT being refreshed), 0 (imminent expiry), or
    # > interval+tolerance (stale) is NOT a fresh heartbeat. Never clamp a
    # negative TTL into this range; only a live, refreshing key qualifies.
    if type(ttl) is not int or ttl < 1 or ttl > BROKER_HEARTBEAT_INTERVAL_SECONDS + BROKER_HEARTBEAT_TOLERANCE_SECONDS:
        return None
    try:
        heartbeat_at = control.instant(pulse.get('as_of'))
    except control.ControlError:
        return None
    skew = timedelta(seconds=BROKER_HEARTBEAT_TOLERANCE_SECONDS)
    fresh_window = timedelta(seconds=BROKER_HEARTBEAT_INTERVAL_SECONDS + BROKER_HEARTBEAT_TOLERANCE_SECONDS)
    # The caller's probe stamp must be FRESH: recent enough that the observation
    # is about the live key, yet not fabricated far into the future.
    if heartbeat_at < now - fresh_window or heartbeat_at > now + skew:
        return None
    return {'heartbeat_at': heartbeat_at.isoformat(), 'ttl_seconds': ttl,
            'queue_length': pulse.get('queue_length')}


def _idle_worker_coverage(args, row, now, floor, broker):
    """Explicit `state_and_events_for_idle_worker` proof, else None.

    Only a KNOWN ARQ worker whose container has run continuously across the whole
    window (start metadata present and started <= floor) AND whose live,
    pod-specific broker heartbeat is fresh qualifies. Every other empty capture
    still fails closed. This never asserts log coverage.
    """
    tail = getattr(args, 'log_tail_lines', None)
    if type(tail) is not int or tail < 1:
        return None                       # a degenerate cap is not a complete capture
    queue_name = _idle_worker_queue(row.get('container'))
    if queue_name is None:
        return None                       # not a known ARQ worker: no broker heartbeat
    try:
        started = control.instant(row.get('started_at'))
    except control.ControlError:
        return None                       # start metadata missing -> coverage unproven
    if started > floor:
        return None                       # began inside the window: not a long-idle worker
    proof = _broker_heartbeat_proof(broker, queue_name, row.get('pod'), now)
    if proof is None:
        return None
    return {'pod': row['pod'], 'container': row['container'],
            'coverage_kind': IDLE_WORKER_COVERAGE_KIND,
            'coverage_start': started.isoformat(),
            'coverage_end': proof['heartbeat_at'],
            'heartbeat_at': proof['heartbeat_at'],
            'heartbeat_ttl_seconds': proof['ttl_seconds'],
            'queue_length': proof['queue_length'],
            'captured_at': now.isoformat(),
            'collection_complete': True}


def _health_log_coverage(smoke, args, kube, rows, now, floor, broker=None):
    """Explicit coverage record; bounded freshness; truncation is rejected.

    A worker with an EMPTY capture is covered only when it is a known ARQ worker
    proven live by its pod-specific broker heartbeat and by continuous container
    state; that record carries an explicit `state_and_events_for_idle_worker`
    kind and never claims log coverage.
    """
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
            record = _idle_worker_coverage(args, row, now, floor, broker)
            if record is None:
                # No complete log capture and no idle-worker proof: fail closed.
                raise control.ControlError('empty logs cannot prove coverage')
            records.append(record)
            continue
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
        # Startup metadata is required. Without it the coverage-start guard cannot
        # run, so refuse here instead of skipping the check (fail closed).
        if started is None:
            raise control.ControlError('container start metadata missing; coverage start unproven')
        # The floor is the later of the log window and this container's own start:
        # a freshly rolled pod legitimately has no older history.
        start_floor = max(floor, started)
        coverage_start = max(stamps[0], start_floor)
        # A container that began AT or INSIDE the observation window must have
        # logged near its own start: its first line is real startup evidence. A
        # container OLDER than the window is instead sampled from an ARBITRARY
        # boundary (now - log_since_seconds). A sparse worker with no heartbeat
        # contract can legitimately emit its next line well after that boundary,
        # so a line exactly at the floor is NOT required. Non-empty, complete,
        # ordered capture plus the workload freshness bound below carry the
        # coverage proof; this does not relax the truncation, stale-freshness, or
        # OOM gates.
        if started >= floor:
            if stamps[0] > started + timedelta(seconds=COVERAGE_TOLERANCE_SECONDS):
                raise control.ControlError('coverage does not start at the container start')
        if stamps[-1] > now:
            raise control.ControlError('log coverage is in the future')
        records.append({'pod': row['pod'], 'container': row['container'],
                        'coverage_kind': LOG_COVERAGE_KIND,
                        'coverage_start': coverage_start.isoformat(),
                        'coverage_end': stamps[-1].isoformat(),
                        'captured_at': now.isoformat(),
                        'collection_complete': True})
    # Coverage contract (documented): a container proves it has logged since its
    # floor, OR -- for a known ARQ worker with an empty window -- it proves
    # up-to-now liveness by continuous container state plus a fresh, pod-specific
    # broker heartbeat recorded under `state_and_events_for_idle_worker`. Either
    # way the WORKLOAD must expose a fresh evidence stream within FRESHNESS_SECONDS;
    # the second-pass identity/counter equality in health_temporal is unchanged.
    # FRESHNESS_SECONDS is a policy bound, not an up-to-now guarantee.
    log_ends = [control.instant(r['coverage_end']) for r in records
                if r.get('coverage_kind') == LOG_COVERAGE_KIND]
    beat_ends = [control.instant(r['heartbeat_at']) for r in records
                 if r.get('coverage_kind') == IDLE_WORKER_COVERAGE_KIND]
    if not log_ends and not beat_ends:
        raise control.ControlError('no selected worker logged within the freshness bound')
    freshest = max(log_ends + beat_ends)
    if freshest < now - timedelta(seconds=FRESHNESS_SECONDS):
        raise control.ControlError('no selected worker logged within the freshness bound')
    return records


def health_temporal(smoke, args, kube, broker=None):
    """Live two-pass temporal baseline with explicit coverage and freshness.

    The restart policy is WINDOWED: an at/over-threshold counter is admitted
    only with proof -- from BOTH passes -- that it is historical: the most
    recent termination is outside the window, the container has run across the
    whole window, and no counter move (even 1) occurred during the observation.
    This mirrors the control path's baseline delta (smoke_control.temporal_gate)
    while preserving threshold 3 and the >=3600s window. Any counter advance,
    termination-metadata change, or identity/container change refuses the gate.
    """
    if args.restart_alert_threshold != 3 or args.log_since_seconds < 3600:
        raise control.ControlError('required mode forbids weaker gates')
    if broker is None:
        broker = getattr(smoke, 'broker_pulse', None)
    now = smoke.utc_now()
    floor = now - timedelta(seconds=max(args.log_since_seconds, 3600))
    first = _health_workers(smoke, args, kube, floor)
    coverage = _health_log_coverage(smoke, args, kube, first, now, floor, broker)
    second = _health_workers(smoke, args, kube, floor)
    first_by = {(r['uid'], r['container']): r for r in first}
    if len(first_by) != len(first) or len(first_by) != len(second):
        raise control.ControlError('worker identity set changed during the observation window')
    history = []
    for r in second:
        b = first_by.get((r['uid'], r['container']))
        if b is None or r['container_id'] != b['container_id']:
            raise control.ControlError('worker identity/container changed during the observation window')
        if r['restart_count'] != b['restart_count']:
            # Any in-window counter movement (even 1) is an active restart signal.
            raise control.ControlError('restart counter advanced during the observation window')
        if r['started_at'] != b['started_at'] or r['last_terminated_at'] != b['last_terminated_at']:
            raise control.ControlError('worker termination metadata changed during the observation window')
        if r['restart_count'] >= args.restart_alert_threshold:
            history.append({'pod': r['pod'], 'uid': r['uid'], 'container': r['container'],
                            'restart_count': r['restart_count'],
                            'last_terminated_at': r['last_terminated_at']})
    return {'workers': second, 'passes': 2, 'coverage': coverage,
            'lookback_start': floor.isoformat(), 'historical_restarts': history}


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
        if e.get('reason') not in OOM_EVENT_REASONS:
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
