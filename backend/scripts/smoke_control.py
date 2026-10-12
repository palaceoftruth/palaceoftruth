"""Conservative, single-control-host smoke admission; no application initialization.

A durable fence is consumed before the sole POST. Ambiguity is read-only recovery,
not retry. Local disk durability/ownership is an explicit deployment prerequisite.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
import fcntl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REQUIRED_CHECKS = ('api_health','runtime_dependencies','tenant_identity','artifact_binding',
                   'baseline_temporal','budget_manifest','sentinel_valkey','mcp_health','kubernetes_alerts')
class ControlError(RuntimeError):pass
class SimulatedCrash(BaseException):pass

def canonical(value):
    try:return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    except (ValueError,TypeError) as exc:raise ControlError('invalid canonical data') from exc

def control_host_id():
    value=Path('/etc/machine-id').read_text().strip()
    if not re.fullmatch('[0-9a-f]{32}',value):raise ControlError('stable control host identity unavailable')
    return value

def sha256_json(value):return hashlib.sha256(canonical(value)).hexdigest()
def instant(value):
    if not isinstance(value,str):raise ControlError('timestamp missing')
    try:t=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError as exc:raise ControlError('invalid timestamp') from exc
    if t.tzinfo is None:raise ControlError('timezone required')
    return t.astimezone(timezone.utc)

def literal_uuid(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',value):
        raise ControlError('literal UUID required')
    return value

def slot_key(doc):return sha256_json({'campaign_id':doc['campaign_id'],'phase':doc['phase']})

def reject_credentials(value):
    if isinstance(value,dict):
        for key,child in value.items():
            if any(word in str(key).lower() for word in ['secret','password','token','authorization','credential_value']):raise ControlError('credential data forbidden in control artifact')
            reject_credentials(child)
    elif isinstance(value,list):
        for child in value:reject_credentials(child)

class Approval:
    def __init__(self,doc,expected_sha256,*,now):
        reject_credentials(doc)
        self._bytes=canonical(doc);self.digest=hashlib.sha256(self._bytes).hexdigest()
        if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256) or self.digest!=expected_sha256:raise ControlError('approval digest mismatch')
        d=self.doc
        required={'schema_version','approval_id','campaign_id','phase','api_base_url','cluster_uid','namespace','namespace_uid','release_name','release_uid','target_digest','chart_digest','tenant_id','principal','scope','starts_at','expires_at','baseline','baseline_sha256','store_binding','max_POST_invocations','max_new_records','transport_retries','payload'}
        if set(d)-required-{'writer_digest','hold_git_revision','canonical_hold','workloads'} or required-set(d) or d['schema_version']!=1:raise ControlError('manifest schema mismatch')
        if 'writer_digest' in d and not re.fullmatch('sha256:[0-9a-f]{64}',str(d['writer_digest'])):raise ControlError('writer digest invalid')
        if 'hold_git_revision' in d and not re.fullmatch('main@sha1:[0-9a-f]{40}',str(d['hold_git_revision'])):raise ControlError('canonical hold revision invalid')
        for k in ['approval_id','campaign_id','cluster_uid','namespace','namespace_uid','release_name','release_uid','tenant_id']:
            if not isinstance(d[k],str) or not d[k] or len(d[k])>256 or any(ord(c)<32 for c in d[k]):raise ControlError('invalid manifest binding')
        if d['phase'] not in ['forward','rollback']:raise ControlError('explicit phase required')
        if type(d['max_POST_invocations']) is not int or d['max_POST_invocations']!=1 or type(d['max_new_records']) is not int or d['max_new_records']!=1 or type(d['transport_retries']) is not int or d['transport_retries']!=0:raise ControlError('one-invocation policy required')
        from urllib.parse import urlsplit
        url=urlsplit(d['api_base_url'])
        if url.scheme!='https' or not url.netloc or url.username or url.password or url.path!='/api/v1' or url.query or url.fragment:raise ControlError('approved HTTPS API origin required')
        if any(not isinstance(d[k],str) or not re.fullmatch('sha256:[0-9a-f]{64}',d[k]) for k in ['target_digest','chart_digest']):raise ControlError('immutable image/chart required')
        p=d['principal']
        if not isinstance(p,dict) or p.get('mode') not in ['api_key','mcp_oauth']:raise ControlError('server principal required')
        if set(p)!=({'mode','id','client_key'} if p['mode']=='mcp_oauth' else {'mode','id'}):raise ControlError('unexpected principal fields')
        literal_uuid(p.get('id'))
        if p['mode']=='mcp_oauth' and (not isinstance(p.get('client_key'),str) or not p['client_key']):raise ControlError('client identity required')
        scope=d['scope']
        if not isinstance(scope,dict) or set(scope)!={'type','key'} or scope.get('type')!='workspace' or not isinstance(scope.get('key'),str) or not scope['key']:raise ControlError('explicit smoke workspace required')
        base=d['baseline']
        if not isinstance(base,dict) or set(base)!= {'captured_at','workers'} or sha256_json(base)!=d['baseline_sha256'] or not isinstance(base['workers'],list) or not base['workers']:raise ControlError('baseline missing or tampered')
        for row in base['workers']:validate_worker_schema(row)
        start=instant(d['starts_at']);end=instant(d['expires_at']);capture=instant(base['captured_at'])
        if not start-timedelta(seconds=300)<=capture<=start or not start<end<=start+timedelta(seconds=3600):raise ControlError('bounded fresh pre-action baseline required')
        payload=d['payload']
        from app.schemas.memory import MemoryEntryRequest
        if not isinstance(payload,dict) or set(payload)-set(MemoryEntryRequest.model_fields):raise ControlError('unexpected payload fields')
        try:MemoryEntryRequest.model_validate(payload)
        except Exception as exc:raise ControlError('invalid actual memory request') from exc
        if not isinstance(payload,dict) or payload.get('tenant_id')!=d['tenant_id'] or payload.get('scope')!=scope or not isinstance(payload.get('body'),str) or not payload['body'] or len(payload['body'].encode())>65536:raise ControlError('frozen payload binding invalid')
        if instant(payload.get('created_at'))>start or payload.get('enable_ai_enrichment') is not False or payload.get('relationship_policy')!='deferred' or payload.get('source')!='palace-rollout-smoke':raise ControlError('safe frozen payload required')
        self.slot=slot_key(d)
        if payload.get('idempotency_key')!=self.slot or payload.get('tags')!=['smoke-slot-'+self.slot]:raise ControlError('stable campaign-phase key required')
        binding=d['store_binding']
        if not isinstance(binding,dict) or set(binding)!={'host_id','store_id','epoch','root','device','inode','inventory_device','inventory_inode','anchor_sha256'}:raise ControlError('store binding schema mismatch')
        for key in ['host_id','store_id','epoch','root','anchor_sha256']:
            if not isinstance(binding[key],str) or not binding[key]:raise ControlError('invalid store identity')
        for key in ['device','inode','inventory_device','inventory_inode']:
            if type(binding[key]) is not int or binding[key]<0:raise ControlError('invalid store inode')
        if 'canonical_hold' in d:
            hold=d['canonical_hold']
            if not isinstance(hold,dict) or set(hold)!={'revision','path','sha256','blob','parent_verified'} or hold['parent_verified'] is not True or hold['revision']!=d.get('hold_git_revision') or not isinstance(hold['path'],str) or hold['path'].startswith('/') or '..' in hold['path'].split('/') or not isinstance(hold['blob'],str):raise ControlError('canonical delivery schema mismatch')
            if hashlib.sha256(hold['blob'].encode()).hexdigest()!=hold['sha256']:raise ControlError('canonical blob integrity mismatch')
        if 'workloads' in d:
            workloads=d['workloads']
            if not isinstance(workloads,dict) or set(workloads)!={'backend','mcp'}:raise ControlError('workload schema mismatch')
            for w in workloads.values():
                if not isinstance(w,dict) or set(w)!={'replicas','replicaset_uids'} or type(w['replicas']) is not int or w['replicas']<1 or not isinstance(w['replicaset_uids'],list) or not w['replicaset_uids'] or any(not isinstance(uid,str) or not uid for uid in w['replicaset_uids']):raise ControlError('workload owner evidence missing')
        self.active(now)
    @property
    def doc(self):return json.loads(self._bytes)
    @property
    def payload(self):return self.doc['payload']
    def active(self,now):
        d=self.doc
        if not instant(d['starts_at'])<=now<instant(d['expires_at']):raise ControlError('manifest not active')
    @classmethod
    def load(cls,path,expected_sha256,*,now):
        try:
            p=Path(path)
            return cls(json.loads(read_private(p,262144)),expected_sha256,now=now)
        except (OSError,ValueError) as exc:raise ControlError('manifest unreadable') from exc

def validate_worker_schema(row):
    required={'pod','uid','container','container_id','restart_count','ready','phase','last_terminated'}
    if not isinstance(row,dict) or required-set(row) or set(row)-required-{'waiting','terminated','master_error_fingerprint'}:raise ControlError('worker schema mismatch')
    term=row['last_terminated']
    if not isinstance(term,dict) or set(term)-{'reason','exitCode','finishedAt','startedAt','containerID','signal','message'}:raise ControlError('termination schema mismatch')
    if term and (not isinstance(term.get('reason'),str) or type(term.get('exitCode')) is not int):raise ControlError('termination evidence incomplete')
    if 'master_error_fingerprint' in row:
        fp=row['master_error_fingerprint']
        if not isinstance(fp,dict) or set(fp)!={'count','sha256'} or type(fp['count']) is not int or not re.fullmatch('[0-9a-f]{64}',str(fp['sha256'])):raise ControlError('error fingerprint schema mismatch')

def temporal_gate(approval,workers,logs,*,now,threshold=3):
    approval.active(now)
    if threshold!=3:raise ControlError('restart delta threshold must remain three')
    baseline=approval.doc['baseline']['workers']
    old={ (r.get('uid'),r.get('container')):r for r in baseline }
    current={ (r.get('uid'),r.get('container')):r for r in workers }
    if len(old)!=len(baseline) or len(current)!=len(workers) or set(old)!=set(current):raise ControlError('worker identity set changed')
    history=[];observations=[];window=min(instant(approval.doc['starts_at']),now-timedelta(seconds=3600))
    for key,row in current.items():
        b=old[key]
        for r in [b,row]:
            validate_worker_schema(r)
            if not all(isinstance(r.get(k),str) and r[k] for k in ['pod','uid','container','container_id']) or type(r.get('restart_count')) is not int or r['restart_count']<0:raise ControlError('worker observation incomplete')
            if r.get('phase')!='Running' or r.get('ready') is not True or r.get('waiting') or r.get('terminated'):raise ControlError('worker not healthy')
            term=r['last_terminated']
            if term:
                end=instant(term.get('finishedAt'))
                bound=instant(approval.doc['baseline']['captured_at']) if r is b else now
                if end>bound:raise ControlError('future termination evidence')
                if 'startedAt' in term and instant(term['startedAt'])>end:raise ControlError('incoherent termination interval')
            elif r['restart_count']>0:raise ControlError('restart history absent')
        if any(row[k]!=b[k] for k in ['pod','container_id']):raise ControlError('container/pod replacement requires new explicit approval')
        delta=row['restart_count']-b['restart_count']
        if delta<0 or delta>=threshold:raise ControlError('restart counter regression/spike')
        if delta>0:raise ControlError('restart transition lacks approved container mapping')
        # A changed lastState is itself evidence of a new termination, even below threshold.
        term=row.get('last_terminated') or {}
        if term!= (b.get('last_terminated') or {}):
            if not term or not term.get('finishedAt'):raise ControlError('termination temporal evidence missing')
            if instant(term['finishedAt'])>=instant(approval.doc['baseline']['captured_at']):raise ControlError('new termination since baseline')
        if term.get('reason')=='OOMKilled' or term.get('exitCode')==137:
            t=instant(term.get('finishedAt'))
            if t>now+timedelta(seconds=5) or t>=window:raise ControlError('recent or future OOM evidence')
            history.append({'pod':row['pod'],'restart_count':row['restart_count'],'last_terminated':term})
        evidence=logs.get(key)
        if not isinstance(evidence,dict) or evidence.get('readable') is not True or not isinstance(evidence.get('text'),str):raise ControlError('required logs missing')
        observed=instant(evidence.get('observed_at'))
        if not now-timedelta(seconds=30)<=observed<=now+timedelta(seconds=5) or instant(evidence.get('covers_since'))>window:raise ControlError('log temporal coverage insufficient')
        text=evidence['text']
        if any(s in text for s in ['OOMKilled','Out of memory','out of memory','exitCode=137']):raise ControlError('recent OOM log evidence')
        errors=[line.strip() for line in text.splitlines() if 'MasterNotFoundError' in line or 'No master found' in line]
        if errors:
            fingerprint={'count':len(errors),'sha256':sha256_json(errors)}
            reconciliation=evidence.get('master_reconciliation',{})
            if b.get('master_error_fingerprint')!=fingerprint or reconciliation.get('worker_recheck_stable') is not True or reconciliation.get('sentinel_ensemble_stable') is not True or reconciliation.get('sentinel_healthy') is not True:raise ControlError('unresolved recent worker error; immutable baseline and bounded read proof required')
        observations.append({'pod':row['pod'],'uid':row['uid'],'container':row['container'],'restart_count':row['restart_count'],'restart_delta':delta})
    return {'workers':observations,'historical_oom':history,'lookback_start':window.isoformat()}

def _private(path,*,directory=False):
    p=Path(path)
    try:s=p.lstat()
    except OSError as exc:raise ControlError('durable control path missing') from exc
    if stat.S_ISLNK(s.st_mode) or (not stat.S_ISDIR(s.st_mode) if directory else not stat.S_ISREG(s.st_mode)) or s.st_uid!=os.getuid() or s.st_mode&0o077:raise ControlError('unsafe durable control ownership/path')
    if not directory and s.st_nlink!=1:raise ControlError('hardlinked control artifact')
    if p.absolute()!=p.resolve():raise ControlError('symlink/traversal control path')
    return s

def open_private(path):
    before=_private(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        after=os.fstat(fd);current=_private(path)
        if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or (current.st_dev,current.st_ino)!=(after.st_dev,after.st_ino) or after.st_nlink!=1 or after.st_uid!=os.getuid() or not stat.S_ISREG(after.st_mode):raise ControlError('artifact changed during open')
        return fd
    except BaseException:os.close(fd);raise

def read_private(path,limit):
    fd=open_private(path)
    try:
        with os.fdopen(fd,'rb',closefd=False) as f:data=f.read(limit+1)
        if len(data)>limit:raise ControlError('artifact size bound exceeded')
        current=_private(path);s=os.fstat(fd)
        if (current.st_dev,current.st_ino)!=(s.st_dev,s.st_ino) or s.st_nlink!=1:raise ControlError('artifact replaced while reading')
        return data
    finally:os.close(fd)

def _fsync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def _new_private_file(path,data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb',closefd=False) as f:f.write(data);f.flush();os.fsync(fd)
    finally:os.close(fd)
    _fsync_directory(Path(path).parent)

def initialize_store(root,*,host_id,store_id):
    """Explicit bootstrap only. Never called by a runner or read-only hook."""
    root=Path(root)
    if root.exists():raise ControlError('store already exists; no reset or quota replenishment')
    if root.absolute()!=root.resolve():raise ControlError('unsafe store path')
    root.mkdir(mode=0o700);_private(root,directory=True)
    (root/'reservations').mkdir(mode=0o700)
    _new_private_file(root/'consumed.json',canonical({}))
    _new_private_file(root/'control.lock',b'')
    db=root/'journal.sqlite3';_new_private_file(db,b'')
    epoch=str(uuid.uuid4())
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('CREATE TABLE metadata (host TEXT, store TEXT, epoch TEXT)')
        conn.execute('INSERT INTO metadata VALUES (?,?,?)',(host_id,store_id,epoch))
        conn.execute('CREATE TABLE slots (slot TEXT PRIMARY KEY, manifest TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, job_id TEXT, receipt TEXT)')
    st=db.stat();inventory=(root/'consumed.json').stat();anchor={'host_id':host_id,'store_id':store_id,'epoch':epoch,'root':str(root),'device':st.st_dev,'inode':st.st_ino,'inventory_device':inventory.st_dev,'inventory_inode':inventory.st_ino}
    _new_private_file(root/'anchor.json',canonical(anchor));_fsync_directory(root)
    return {**anchor,'anchor_sha256':sha256_json(anchor)}

class Journal:
    def __init__(self,root,approval,*,host_id):
        self.root=Path(root);self.binding=approval.doc['store_binding'];self.host=host_id
        fd=open_private(self.root/'control.lock')
        try:
            deadline=time.monotonic()+2.5
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise ControlError('control observation busy')
                    time.sleep(.005)
            self._check()
        finally:os.close(fd)
    def _check(self):
        _private(self.root,directory=True);_private(self.root/'reservations',directory=True)
        a=self.root/'anchor.json';_private(a);s=_private(self.root/'journal.sqlite3')
        try:anchor=json.loads(read_private(a,65536))
        except (OSError,ValueError) as exc:raise ControlError('store anchor corrupt') from exc
        b=self.binding
        if anchor!={k:v for k,v in b.items() if k!='anchor_sha256'} or sha256_json(anchor)!=b.get('anchor_sha256') or anchor.get('host_id')!=self.host or anchor.get('root')!=str(self.root) or anchor.get('inode')!=s.st_ino or anchor.get('device')!=s.st_dev:raise ControlError('control domain/store identity mismatch')
        inventory_stat=_private(self.root/'consumed.json')
        if (inventory_stat.st_dev,inventory_stat.st_ino)!=(b['inventory_device'],b['inventory_inode']):raise ControlError('consumed inventory replaced')
        inventory=json.loads(read_private(self.root/'consumed.json',1048576))
        if not isinstance(inventory,dict):raise ControlError('inventory corrupt')
        fences={f.stem:json.loads(read_private(f,65536)) for f in (self.root/'reservations').iterdir()}
        if set(fences)!=set(inventory) or any(fences[k]!={'slot':k,'manifest_sha256':v} for k,v in inventory.items()):raise ControlError('consumed inventory/fence mismatch')
        try:
            with sqlite3.connect(f'file:{self.root / "journal.sqlite3"}?mode=rw',uri=True,timeout=2.5) as c:
                if c.execute('PRAGMA integrity_check').fetchone()!=('ok',):raise ControlError('journal integrity failed')
                if c.execute('SELECT host,store,epoch FROM metadata').fetchall()!=[(anchor['host_id'],anchor['store_id'],anchor['epoch'])]:raise ControlError('journal metadata mismatch')
                rows=c.execute('SELECT slot,manifest,digest,state,job_id,receipt FROM slots').fetchall()
                for slot,manifest,digest,state,job,receipt in rows:
                    if inventory.get(slot)!=digest:raise ControlError('consumed inventory/row mismatch')
                    if hashlib.sha256(manifest.encode()).hexdigest()!=digest or state not in ['reserved','verified'] or slot_key(json.loads(manifest))!=slot:raise ControlError('journal row corruption')
                    fence=self.root/'reservations'/(slot+'.json');_private(fence)
                    if json.loads(read_private(fence,65536))!={'slot':slot,'manifest_sha256':digest}:raise ControlError('fence corruption')
                # Orphan tombstones consume quota after a pre-commit crash. Never bootstrap rows.
                if not {r[0] for r in rows}.issubset(inventory):raise ControlError('inventory missing history')
                current=_private(self.root/'journal.sqlite3')
                if (current.st_dev,current.st_ino)!=(s.st_dev,s.st_ino):raise ControlError('journal replaced during observation')
        except (sqlite3.Error,OSError,ValueError) as exc:raise ControlError('journal unavailable/corrupt') from exc
    def _connect(self):
        self._check();fd=open_private(self.root/'journal.sqlite3')
        try:
            c=sqlite3.connect(f'file:{self.root / "journal.sqlite3"}?mode=rw',uri=True,timeout=2.5)
            current=_private(self.root/'journal.sqlite3');pinned=os.fstat(fd)
            if (current.st_dev,current.st_ino)!=(pinned.st_dev,pinned.st_ino):c.close();raise ControlError('journal connection inode changed')
            c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL');return c
        finally:os.close(fd)
    def inspect(self,slot):
        try:
            with self._connect() as c:r=c.execute('SELECT * FROM slots WHERE slot=?',(slot,)).fetchone()
        except sqlite3.Error as exc:raise ControlError('journal read failure') from exc
        return dict(r) if r else None
    def reserve(self,a,*,crash=lambda point:None):
        fd=open_private(self.root/'control.lock')
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return self._reserve_locked(a,crash=crash)
        except BlockingIOError as exc:raise ControlError('control admission busy') from exc
        finally:os.close(fd)
    def _reserve_locked(self,a,*,crash):
        self._check();f=self.root/'reservations'/(a.slot+'.json')
        try:_new_private_file(f,canonical({'slot':a.slot,'manifest_sha256':a.digest}))
        except FileExistsError:
            row=self.inspect(a.slot)
            if not row:raise ControlError('orphan reservation consumed; no POST recovery')
            if row['digest']!=a.digest or row['manifest']!=a._bytes.decode():raise ControlError('same campaign/phase manifest conflict')
            return False
        crash('after_fence')
        inventory=json.loads(read_private(self.root/'consumed.json',1048576))
        if a.slot in inventory:raise ControlError('consumed slot cannot replenish')
        inventory[a.slot]=a.digest
        fd=os.open(self.root/'consumed.json',os.O_WRONLY|os.O_NOFOLLOW)
        try:
            expected=_private(self.root/'consumed.json');actual=os.fstat(fd)
            if (expected.st_dev,expected.st_ino)!=(actual.st_dev,actual.st_ino):raise ControlError('inventory race')
            data=canonical(inventory);os.ftruncate(fd,0);os.write(fd,data);os.fsync(fd)
        finally:os.close(fd)
        try:
            with self._connect() as c:
                c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO slots VALUES (?,?,?,\'reserved\',NULL,NULL)',(a.slot,a._bytes.decode(),a.digest));c.commit()
            fd=os.open(self.root/'journal.sqlite3',os.O_RDONLY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
            _fsync_directory(self.root)
            saved=self.inspect(a.slot)
            if saved is None or saved['digest']!=a.digest:raise ControlError('reservation readback mismatch')
        except (sqlite3.Error,OSError) as exc:raise ControlError('reservation consumed but not confirmed; no POST') from exc
        crash('after_reservation');return True
    def update(self,a,*,job_id=None,receipt=None):
        try:
            with self._connect() as c:
                row=c.execute('SELECT digest FROM slots WHERE slot=?',(a.slot,)).fetchone()
                if not row or row['digest']!=a.digest:raise ControlError('reserved slot missing/conflicting')
                if job_id:c.execute('UPDATE slots SET job_id=? WHERE slot=?',(literal_uuid(job_id),a.slot))
                if receipt:c.execute('UPDATE slots SET state=\'verified\',receipt=? WHERE slot=?',(canonical(receipt).decode(),a.slot))
                c.commit()
        except sqlite3.Error as exc:raise ControlError('receipt journal update failed; permit remains consumed') from exc

def require_checks(report):
    if not isinstance(report,dict) or report.get('alerts'):raise ControlError('prerequisite alerts or unavailable report')
    checks=report.get('checks')
    if not isinstance(checks,list):raise ControlError('prerequisite checks missing')
    for name in REQUIRED_CHECKS:
        rows=[r for r in checks if isinstance(r,dict) and r.get('name')==name]
        if not rows or any(r.get('status')!='passed' for r in rows):raise ControlError('required gate failed: '+name)

def verify_receipt(a,client,job_id,*,entry_id=None):
    literal_uuid(job_id)
    response=client.request('GET',f'/memory/jobs/{job_id}/checkpoint-receipt')
    r=response.payload;d=a.doc;p=a.payload
    if response.status!=200 or not isinstance(r,dict) or r.get('durable') is not True:raise ControlError('canonical durable receipt unavailable')
    expected={'tenant_id':d['tenant_id'],'scope':d['scope'],'job_id':job_id,'idempotency_key':a.slot,'body_sha256':hashlib.sha256(p['body'].encode()).hexdigest()}
    if any(r.get(k)!=v for k,v in expected.items()):raise ControlError('canonical receipt binding/hash mismatch')
    literal_uuid(r.get('source_item_id'))
    if entry_id:literal_uuid(entry_id)
    return {'manifest_sha256':a.digest,'payload_sha256':sha256_json(p),'entry_id':entry_id,'receipt':{k:r[k] for k in ['durable','tenant_id','scope','job_id','source_item_id','idempotency_key','body_sha256']}}

def recover_receipt(a,client,*,known_job_id=None):
    response=client.request('GET','/memory/entries',query={'scope_type':a.doc['scope']['type'],'scope_key':a.doc['scope']['key'],'tags':'smoke-slot-'+a.slot,'limit':2})
    d=response.payload
    if response.status!=200 or not isinstance(d,dict) or type(d.get('total')) is not int or d['total']!=1 or d.get('next_cursor') is not None or not isinstance(d.get('entries'),list) or len(d['entries'])!=1:raise ControlError('consumed invocation unresolved/ambiguous; no POST retry')
    row=d['entries'][0]
    if not isinstance(row,dict) or row.get('scope')!=a.doc['scope']:raise ControlError('entry scope mismatch')
    job=literal_uuid(row.get('job_id'));entry=literal_uuid(row.get('entry_id'));item=literal_uuid(row.get('source_item_id'))
    if known_job_id and job!=known_job_id:raise ControlError('job pointer mismatch')
    envelope=verify_receipt(a,client,job,entry_id=entry)
    if envelope['receipt']['source_item_id']!=item:raise ControlError('item pointer mismatch')
    return envelope

def run_reserved(a,journal,client,preflight,postflight,*,now=None,clock=None,crash=lambda point:None):
    current=clock or (lambda:now if now is not None else datetime.now(timezone.utc))
    a.active(current())
    if getattr(client,'base_url',None)!=a.doc['api_base_url']:raise ControlError('transport retarget forbidden')
    try:require_checks(preflight())
    except Exception as exc:raise ControlError('read-only prerequisites failed') from exc
    a.active(current())
    fresh=journal.reserve(a,crash=crash)
    if fresh:
        a.active(current())
        try:
            response=client.request('POST','/memory/entries',body=a.payload)
            crash('after_post')
            if response.status not in [200,201,202] or not isinstance(response.payload,dict):raise ControlError('admission response unavailable; permit consumed')
            job=literal_uuid(response.payload.get('job_id'));journal.update(a,job_id=job)
        except Exception as exc:raise ControlError('ambiguous or failed invocation consumed; only read-only recovery allowed') from exc
    row=journal.inspect(a.slot)
    if not row:raise ControlError('reservation unavailable')
    a.active(current());envelope=recover_receipt(a,client,known_job_id=row['job_id'])
    try:require_checks(postflight())
    except Exception as exc:raise ControlError('postflight failed; permit consumed') from exc
    a.active(current());journal.update(a,receipt=envelope)
    # Separate fresh readback is mandatory before success.
    saved=journal.inspect(a.slot)
    if saved['state']!='verified' or json.loads(saved['receipt'])!=envelope:raise ControlError('durable local receipt readback failed')
    return envelope

def migration_name(release_name,runtime_chart_version,backend_image):
    return (release_name+'-migrate-'+hashlib.sha256((runtime_chart_version+'-'+backend_image).encode()).hexdigest()[:10])[:63].rstrip('-')
def helm319_hooks(hooks,run):
    for weight,name in sorted(hooks,key=lambda h:(h[0],h[1])):
        if not run(name):raise ControlError('hook failed; later same-weight hooks not executed')
