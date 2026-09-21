"""Actual parsing/search/planning measurements; no synthetic LLM timings."""
import time
import os
import tracemalloc
from .common import DATA,write,read
from .catalog import load_catalog
from .documents import parse,coalesce_groups
from .search import Index

def rss():
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        import ctypes
        class Mem(ctypes.Structure):
            _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong),('peak',ctypes.c_size_t),('rss',ctypes.c_size_t),('pq',ctypes.c_size_t),('q',ctypes.c_size_t),('pnp',ctypes.c_size_t),('np',ctypes.c_size_t),('pf',ctypes.c_size_t),('ppf',ctypes.c_size_t)]
        m=Mem();m.cb=ctypes.sizeof(m)
        current=ctypes.windll.kernel32.GetCurrentProcess;current.restype=ctypes.c_void_p
        get=ctypes.windll.psapi.GetProcessMemoryInfo;get.argtypes=[ctypes.c_void_p,ctypes.POINTER(Mem),ctypes.c_ulong]
        if not get(current(),ctypes.byref(m),m.cb):return None
        return m.rss

def main():
    cat=load_catalog();path=DATA/'fixtures/scale/unique-scale.docx';start=time.time();doc=parse(path,cat);parsed=time.time();index=Index([doc]);indexed=time.time();groups=coalesce_groups(doc);planned=time.time();returned=sum(len(g) for g in groups);expected=sum(bool(b['text'].strip()) and not b.get('toc') for b in doc['blocks']);assert returned==expected
    result={'parse_seconds':parsed-start,'index_seconds':indexed-parsed,'structural_planning_seconds':planned-indexed,'rss_bytes':rss(),'characters':sum(len(b['text']) for b in doc['blocks']),'blocks':len(doc['blocks']),'sections':len(doc['headings']),'tables':len(doc['tables']),'structural_groups':len(groups),'covered_blocks':returned,'expected_blocks':expected,'unique_source':True,'llm_run':False,'token_budget_check':'Exact final payload checks execute in Engine.enqueue_bounded; this measurement covers CPU stages only'}
    write(DATA/'scale-measurements.json',result);print(result)

if __name__=='__main__':main()
