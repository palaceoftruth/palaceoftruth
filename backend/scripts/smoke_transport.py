"""Bounded readers: no proxy, no redirect, explicit TLS context retained."""
import time
import urllib.request
import urllib.error
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
            total+=len(data);self.remaining-=len(data)
            if total>limit or self.remaining<0:raise ControlError('reader byte budget exceeded')
            chunks.append(data)
        self.timeout(10)
        return b''.join(chunks)

def strict_open(request, *, timeout, context=None, budget=None):
    budget=budget or Budget(seconds=timeout)
    handlers=[urllib.request.ProxyHandler({}),NoRedirect()]
    if context is not None:handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(request,timeout=budget.timeout(timeout))
