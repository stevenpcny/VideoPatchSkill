"""Opt-in process-local frame ownership prototypes, measured on frozen P1.

Raw buffers recycle only inside merge processing, never for the public/default
Reader. Encoded-input buffers recycle only after FrameWriter's write completes.
"""
import contextlib
import queue
import threading

import numpy as np
import flow_patch as fp


@contextlib.contextmanager
def candidate(name):
    assert name in ('raw','output','both')
    if hasattr(fp,'_FramePool'):
        raise RuntimeError('Run this prototype on the fixed P1 worktree (b585a1e); production already contains frame pools')
    old_reader,old_writer,old_process=fp.Reader,fp.FrameWriter,fp.process
    local=threading.local()
    class Reader(old_reader):
        def __init__(self,path,size,color=None,*a,**kw):
            count=size[0]*size[1]*3
            self.recycle=(name in ('raw','both') and getattr(local,'processing',False)
                          and color is not None and not color['rgb_input']
                          and 1024*1024 <= count and count*(fp.PREFETCH+2)<=128*1024*1024)
            self.free=queue.Queue();self.owned=[];self.current=None
            super().__init__(path,size,color,*a,**kw)

        def _read_ahead(self):
            if not self.recycle:
                return super()._read_ahead()
            try:
                while not self.stopping.is_set():
                    try:blocks=self.free.get_nowait()
                    except queue.Empty:
                        if len(self.owned)<fp.PREFETCH+2:
                            blocks=bytearray(self.w*self.h*3);self.owned.append(blocks)
                        else:
                            try:blocks=self.free.get(timeout=.1)
                            except queue.Empty:continue
                    view,done=memoryview(blocks),0
                    while done<len(blocks):
                        n=self.p.stdout.readinto(view[done:])
                        if not n:break
                        done+=n
                    if done<len(blocks):break
                    self._put(blocks)
            except (OSError,ValueError):
                pass
            self._put(None)

        def advance(self,index):
            if not self.recycle:return super().advance(index)
            fp.require(index>=self.index,'Non-monotone mapping is unsupported')
            while self.index<index:
                if self.current is not None:
                    previous=self.current;self.current=None;self.raw=None;self.last=None
                    self.free.put(previous)
                blocks=self.frames.get() if self.thread.is_alive() or not self.frames.empty() else None
                if blocks is None:
                    self.thread.join();raise ValueError(f'Decoder ended before frame {index}')
                self.current=blocks
                self.raw=np.frombuffer(blocks,np.uint8).reshape(3,self.h,self.w)
                self.last=None;self.index+=1

        def passthrough420(self,index):
            if name not in ('output','both') or self.color['rgb_input'] or not getattr(local,'processing',False):
                return super().passthrough420(index)
            self.advance(index)
            writer=getattr(local,'writer',None)
            if writer is None:return super().passthrough420(index)
            buf=writer.acquire(self.w*self.h*3//2)
            buf,Y,U,V=fp.planes420(buf,self.w,self.h)
            y,u,v=self.raw;k=fp.kernels()
            def work(a,b):
                Y[a:b]=y[a:b]
                for src,dst in ((u,U),(v,V)):
                    if k:k.chroma_avg(src[a:b],dst[a//2:b//2]);continue
                    c=src[a:b].astype(np.uint16)
                    s=c[0::2,0::2]+c[1::2,0::2]+c[0::2,1::2]+c[1::2,1::2]
                    q,r=s>>2,s&3
                    dst[a//2:b//2]=q+((r>2)|((r==2)&(q&1==1)))
            fp.in_bands(self.h,work)
            return buf,Y,U,V

    class Writer(old_writer):
        def __init__(self,pipe):
            self.available=queue.Queue();self.owned={};self.frame_size=None
            super().__init__(pipe)
            if getattr(local,'processing',False):local.writer=self

        def acquire(self,size):
            if size<1024*1024 or size*(fp.PREFETCH+2)>64*1024*1024:
                return bytearray(size)
            if self.frame_size is None:self.frame_size=size
            assert self.frame_size==size
            try:return self.available.get_nowait()
            except queue.Empty:
                if len(self.owned)<fp.PREFETCH+2:
                    buf=bytearray(size);self.owned[id(buf)]=buf;return buf
                return self.available.get()

        def _run(self):
            while (item:=self.items.get()) is not None:
                try:
                    if self.error is None:
                        try:self.pipe.write(item)
                        except (OSError,ValueError) as e:self.error=e
                finally:
                    if id(item) in self.owned:self.available.put(item)

    def process(*a,**kw):
        previous=getattr(local,'processing',False);writer=getattr(local,'writer',None)
        local.processing=(a[2] if len(a)>2 else kw.get('mode'))=='merge';local.writer=None
        try:return old_process(*a,**kw)
        finally:local.processing=previous;local.writer=writer

    fp.Reader,fp.FrameWriter,fp.process=Reader,Writer,process
    try:yield
    finally:fp.Reader,fp.FrameWriter,fp.process=old_reader,old_writer,old_process
