"""Internal trusted cost evidence, exact task totals, and conservative admission.

No network/price discovery. The backend may inject quote()/receipt() authority;
normal profile fields or Provider free text are never a cost authority. Production
defaults to no authority. Hard bounds are supported only for an explicitly bound
controlled Mock Provider, not for real supplier billing.
"""
import copy
from decimal import Decimal, localcontext
import json
import re
import time
from .director_contract import digest, reject

SOURCES = {'backend_config','human_verified','provider_verified','controlled_mock'}
UNITS = {'USD','CNY','credits'}
PROFILE = ('id','revision','provider_id','model')
COUNTS = ('input_tokens','completion_tokens','cache_hit_tokens','cache_miss_tokens','max_output_tokens','image_count')

def money(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,35})(?:\.[0-9]{1,18})?',value):
        reject('成本数值必须是有界、非负、精确十进制。','director_cost_evidence_invalid',409)
    return Decimal(value)

def text(value):
    result=format(value,'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result

def identity(profile): return {k:profile.get(k) for k in PROFILE}
def unbound_summary(task_id):
    return {'task_id':task_id,'status':'ledger_unbound','attempt_count':None,
            'unknown':True,'units':{},'conversion':None}
def token_usage(usage):
    safe={k:(v if type(v:= (usage or {}).get(k)) is int and 0<=v<=2**31-1 else None) for k in COUNTS}
    safe['input_source']='response.usage.prompt_tokens' if (usage or {}).get('input_source')=='response.usage.prompt_tokens' else 'unknown'
    return safe

def quote(authority, profile, role, mock_provider):
    if authority is None: return None
    value=authority.quote(profile,role)
    if value is None: return None
    fields={'source','evidence_id','version','profile','role','currency','valid_from','valid_until',
            'unit_tokens','rates','estimate_amount','hard_upper_bound'}
    if (not isinstance(value,dict) or set(value)!=fields or value['source'] not in SOURCES
        or value['currency'] not in UNITS or value['profile']!=identity(profile) or value['role']!=role
        or any(not isinstance(value[k],str) or not 1<=len(value[k])<=128 for k in ('version','evidence_id'))
        or any(type(value[k]) is not int for k in ('valid_from','valid_until','unit_tokens'))
        or not value['valid_from']<=int(time.time())<value['valid_until'] or value['unit_tokens'] not in {10**i for i in range(10)}
        or not isinstance(value['rates'],dict) or set(value['rates'])!={'input','cache_hit','output'}):
        reject('可信价格的来源、版本、profile/model或适用范围不匹配。','director_cost_evidence_invalid',409)
    for rate in value['rates'].values():
        if money(rate)>Decimal(10**18): reject('价格率超出精确计量范围。','director_cost_evidence_invalid',409)
    if value['estimate_amount'] is not None: money(value['estimate_amount'])
    if value['hard_upper_bound'] is not None:
        money(value['hard_upper_bound'])
        if (value['source']!='controlled_mock' or getattr(authority,'mock_only',False) is not True
            or mock_provider is None or getattr(authority,'mock_provider',None) is not mock_provider):
            reject('本阶段只有绑定实际受控Mock Provider的上界可用于测试。','director_cost_evidence_invalid',409)
    return copy.deepcopy(value)

def calculate(price, usage):
    if not price: return None
    inputs=usage['input_tokens']; output=usage['completion_tokens']; hits=usage['cache_hit_tokens']
    rates=price['rates']
    if inputs is None or output is None or usage['input_source']!='response.usage.prompt_tokens': return None
    if hits is None:
        if rates['input']!=rates['cache_hit']: return None
        hits=0
    if hits>inputs or (usage['cache_miss_tokens'] is not None and usage['cache_miss_tokens']!=inputs-hits):
        reject('缓存usage与包含缓存的input总量冲突。','director_cost_evidence_invalid',409)
    with localcontext() as ctx:
        ctx.prec=80
        return text(((inputs-hits)*money(rates['input'])+hits*money(rates['cache_hit'])+output*money(rates['output']))/price['unit_tokens'])

def rows(c, task_id):
    return [json.loads(row[0]) for row in c.execute('SELECT document FROM director_attempts WHERE task_id=? ORDER BY rowid',(task_id,))]

def summary(attempts):
    units={}; roles={}; sources={}; unknown=0; pending=0
    with localcontext() as ctx:
        ctx.prec=80
        for row in attempts:
            unit=row.get('currency') or 'unpriced'
            bucket=units.setdefault(unit,{k:{'amount':None if k=='unknown' else '0','attempts':0}
                for k in ('confirmed','calculated','estimated','reserved_pending','unknown')})
            confidence=row.get('confidence','unknown')
            category=confidence if confidence in {'confirmed','calculated','estimated'} and row.get('amount') is not None else 'unknown'
            bucket[category]['attempts']+=1
            if category=='unknown': unknown+=1
            else: bucket[category]['amount']=text(Decimal(bucket[category]['amount'])+money(row['amount']))
            bound=(row.get('price') or {}).get('hard_upper_bound')
            if row.get('state')=='pending' or category not in {'confirmed','calculated'}:
                pending+=1
                if bound is not None:
                    target=bucket['reserved_pending']; target['attempts']+=1
                    target['amount']=text(Decimal(target['amount'])+money(bound))
            role=row['role']; source=row['source']
            roles[role]=roles.get(role,0)+1; sources[source]=sources.get(source,0)+1
    return {'attempt_count':len(attempts),'unknown_count':unknown,'unsettled_count':pending,
            'anomaly_count':sum(bool(row.get('cost_anomaly') or row.get('cost_conflict')) for row in attempts),
            'mock_attempts':sum(row.get('cost_basis_source')=='controlled_mock' for row in attempts),
            'units':units,'roles':roles,'sources':sources,'conversion':None}

def strict_check(task, attempts, price):
    """Upper bounds and remaining exposure; no call-count admission rule."""
    if task['policy']['mode']!='strict': return None
    unit=task['policy']['currency']; limit=Decimal(str(task['policy']['limit']))
    if not price or price['currency']!=unit or price.get('hard_upper_bound') is None:
        return 'price_upper_bound_missing'
    used=Decimal(0)
    with localcontext() as ctx:
        ctx.prec=80
        for row in attempts:
            if row.get('cost_conflict') or row['state']=='unknown': return 'unknown_action'
            if row.get('currency')!=unit: return 'cost_exposure_unknown'
            if row['state']=='pending':
                bound=(row.get('price') or {}).get('hard_upper_bound')
                if bound is None: return 'cost_exposure_unknown'
                confirmed=money(row['amount']) if row.get('confidence')=='confirmed' else Decimal(0)
                used+=max(money(bound),confirmed)
            elif row.get('confidence') in {'confirmed','calculated'} and row.get('amount') is not None:
                used+=money(row['amount'])
            else:
                bound=(row.get('price') or {}).get('hard_upper_bound')
                if bound is None: return 'cost_exposure_unknown'
                used+=money(bound)
        if used>limit: return 'cost_limit_exceeded'
        if used+money(price['hard_upper_bound'])>limit: return 'cost_limit_exhausted'
    return None

def settled(record, usage, error, duration):
    result=copy.deepcopy(record); safe=token_usage(usage)
    settlement={'usage':safe,'outcome':'unknown' if error else 'returned'}
    if result.get('settlement'):
        if result['settlement']!=settlement:
            reject('原调用结算usage/结果冲突。','director_cost_evidence_invalid',409)
        return result
    result.update(**safe,settlement=settlement,state='unknown' if error else 'settled',duration_ms=max(0,int(duration*1000)))
    amount=None if error else calculate(result.get('price'),safe)
    if result.get('cost_receipt_id') and result['confidence']=='confirmed':
        result['calculated_basis_amount']=amount
        return result
    if amount is not None: result.update(amount=amount,confidence='calculated')
    elif error: result.update(amount=None,confidence='unknown')
    else:
        estimate=(result.get('price') or {}).get('estimate_amount')
        result.update(amount=estimate,confidence='estimated' if estimate is not None else 'unknown')
    return result

def validate_receipt(value, record):
    required={'receipt_id','attempt_id','task_id','run_id','role','profile','price_version','source','evidence_id','currency','amount','supersedes'}
    expected={'id':record.get('profile_id'),'revision':record.get('profile_revision'),
              'provider_id':record.get('provider_id'),'model':record.get('model')}
    if (not isinstance(value,dict) or set(value)!=required or value['source'] not in SOURCES
        or value['currency'] not in UNITS or value['profile']!=expected
        or any(value[k]!=record[k] for k in ('attempt_id','task_id','run_id','role','price_version'))
        or (record.get('currency') is not None and value['currency']!=record['currency'])
        or any(not isinstance(value[k],str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value[k]) for k in ('receipt_id','evidence_id'))
        or (value['supersedes'] is not None and not isinstance(value['supersedes'],str))):
        reject('成本回执身份、币种、来源或价格版本冲突。','director_cost_evidence_invalid',409)
    money(value['amount'])

def cost_pause(c, task, reason, evidence):
    import uuid
    task=copy.deepcopy(task); task['revision']=task.get('revision',1)+1
    task['pause']={'pause_id':uuid.uuid4().hex,'reason':reason,'scope':'all_models','evidence':evidence,
                   'version':task['revision'],'resume_condition':'human review of original cost/claim and policy CAS; never replay unknown actions'}
    c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task,ensure_ascii=False),))

def anomaly(task, attempts):
    if task['policy']['mode']!='strict': return False
    unit=task['policy']['currency']; total=Decimal(0)
    with localcontext() as ctx:
        ctx.prec=80
        for row in attempts:
            if row.get('currency')==unit and row.get('confidence') in {'confirmed','calculated'}:
                total+=money(row['amount'])
    return total>Decimal(str(task['policy']['limit']))

def evidence_key(value): return digest({'source':value['source'],'receipt_id':value['receipt_id']})
