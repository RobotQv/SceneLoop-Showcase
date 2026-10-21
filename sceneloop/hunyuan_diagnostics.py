"""Safe local loading failure facts; no messages, tensors, locals or full paths."""
import math
from pathlib import Path

STAGES={'validating_input','loading_model','importing_libraries','constructing_pipeline','model_loaded','inference','exporting_raw'}
TYPES={'AssertionError','RuntimeError','ValueError','TypeError','KeyError','ImportError','ModuleNotFoundError','OSError','MemoryError','OutOfMemoryError'}


def public(value):
    if not isinstance(value,dict):return {}
    result={}
    if value.get('stage') in STAGES:result['stage']=value['stage']
    result['exception_type']=value.get('exception_type') if value.get('exception_type') in TYPES else 'unknown'
    seconds=value.get('elapsed_seconds')
    if type(seconds) in (int,float) and math.isfinite(seconds) and 0<=seconds<86400:result['elapsed_seconds']=round(seconds,3)
    frames=[]
    for frame in value.get('frames',[])[:8]:
        if (isinstance(frame,dict) and all(isinstance(frame.get(k),str) and frame[k].isidentifier() and len(frame[k])<=80 for k in ('module','function'))
                and type(frame.get('line')) is int and 0<frame['line']<1000000):
            frames.append({k:frame[k] for k in ('module','function','line')})
    result['frames']=frames
    return result


def describe(error,stage,elapsed,roots):
    frames=[];trace=error.__traceback__
    roots=[Path(p).resolve() for p in roots]
    while trace:
        code=trace.tb_frame.f_code;path=Path(code.co_filename).resolve()
        if any(path.is_relative_to(root) for root in roots):
            frames.append({'module':path.stem,'function':code.co_name,'line':trace.tb_lineno})
        trace=trace.tb_next
    return public({'stage':stage,'elapsed_seconds':elapsed,'exception_type':type(error).__name__,'frames':frames[-8:]})
