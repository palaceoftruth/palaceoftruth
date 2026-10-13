"""Bounded readers: no proxy, no redirect, explicit TLS context retained."""
import time
import urllib.request
import urllib.error
import base64
import io
import json
import os
import select
import signal
from scripts.smoke_control import ControlError

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise urllib.error.URLError('redirect refused')

class Budget:
    def __init__(self, seconds: float=120, max_bytes=8*1024*1024):
        self.deadline=time.monotonic()+seconds
        self.remaining=max_bytes
    def timeout(self, requested):
        left=self.deadline-time.monotonic()
        if left<=0 or self.remaining<=0:raise ControlError('reader total budget exhausted')
        return min(requested,left,10)
    def read(self,response,limit=1048576):
        chunks=[];total=0
        while True:
            timeout=self.timeout(10)
            sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
            if sock is not None:sock.settimeout(timeout)
            # read1 returns one underlying read, preventing an unbounded slow drip.
            read=getattr(response,'read1',None) or response.read
            data=read(min(65536,limit-total+1,self.remaining+1))
            if not data:break
            total+=len(data)
            if not getattr(response,'_precharged',False):self.remaining-=len(data)
            if total>limit or self.remaining<0:raise ControlError('reader byte budget exceeded')
            chunks.append(data)
        self.timeout(10)
        return b''.join(chunks)

def strict_open(request, *, timeout, context=None, budget=None, body_limit=1048576):
    budget=budget or Budget(seconds=timeout)
    budget.timeout(timeout)
    handlers=[urllib.request.ProxyHandler({}),NoRedirect()]
    if context is not None:handlers.append(urllib.request.HTTPSHandler(context=context))
    opener=urllib.request.build_opener(*handlers)
    return _cancellable_exchange(opener,request,timeout,budget,body_limit)

class BufferedResponse(io.BytesIO):
    """The child has already completed opening, headers and bounded body reads."""
    def __init__(self,status,body):
        super().__init__(body)
        self.status=status
        self._precharged=True

def _exchange_worker(opener,request,timeout,budget,write_fd,body_limit):
    # The TLS context/opener is inherited unchanged; no credential files,
    # proxies, retry loop or environment-based token refresh are introduced.
    initial_remaining=budget.remaining
    try:
        response=opener.open(request,timeout=budget.timeout(timeout))
        if response is None:
            result={'none':True}
        else:
            with response:
                body=budget.read(response,body_limit)
                result={'status':response.status,'body':base64.b64encode(body).decode('ascii')}
    except urllib.error.HTTPError as exc:
        result={'http_error':exc.code}
    except BaseException:
        # No exception text, request headers or server error body crosses IPC.
        result={'error':True}
    result['consumed_bytes']=initial_remaining-budget.remaining
    try:
        data=json.dumps(result,separators=(',',':')).encode()
        while data:
            count=os.write(write_fd,data)
            data=data[count:]
    finally:
        os.close(write_fd)
        os._exit(0)

def _cancellable_exchange(opener,request,timeout,budget,body_limit):
    """POSIX-only helper. Parent enforces one deadline including response headers.

    An interrupted POST is ambiguous, not retryable. Admission remains owned by
    run_reserved, which has already durably consumed its permit before this call.
    """
    if not hasattr(os,'fork'):raise ControlError('cancellable transport unavailable')
    if type(body_limit) is not int or not 0<body_limit<=1048576:raise ControlError('invalid exchange body cap')
    read_fd,write_fd=os.pipe()
    pid=None
    try:
        budget.timeout(timeout)
        pid=os.fork()
        if pid==0:
            os.close(read_fd)
            _exchange_worker(opener,request,timeout,budget,write_fd,body_limit)
        os.close(write_fd);write_fd=None
        data=bytearray()
        while True:
            wait=budget.timeout(timeout)
            ready,_,_=select.select([read_fd],[],[],wait)
            if not ready:raise ControlError('exchange deadline exceeded; outcome ambiguous')
            chunk=os.read(read_fd,65536)
            if not chunk:break
            data.extend(chunk)
            if len(data)>1500000:raise ControlError('exchange IPC byte cap exceeded')
        budget.timeout(timeout)
        result=json.loads(data)
        consumed=result.get('consumed_bytes')
        if type(consumed) is not int or consumed<0:raise ControlError('invalid bounded exchange accounting')
        budget.remaining-=consumed
        if result.get('http_error') is not None:
            raise urllib.error.HTTPError(request.full_url,result['http_error'],'request refused',{},None)
        if result.get('error'):raise urllib.error.URLError('bounded exchange failed')
        if result.get('none'):return None
        body=base64.b64decode(result['body'],validate=True)
        if len(body)>body_limit or len(body)!=consumed or budget.remaining<=0:raise ControlError('exchange byte cap exceeded')
        return BufferedResponse(result['status'],body)
    finally:
        os.close(read_fd)
        if write_fd is not None:os.close(write_fd)
        if pid:
            completed,_=os.waitpid(pid,os.WNOHANG)
            if completed==0:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
                os.waitpid(pid,0)
