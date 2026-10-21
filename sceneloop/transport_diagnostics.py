"""Bounded transport facts only; never inspect exception text or response bytes."""
import errno
import http.client
import math
import re
import socket
import ssl
import urllib.error

CATEGORIES = frozenset({'timeout','dns','tls_certificate','tls','connection_reset',
    'remote_disconnected','response_incomplete','connection_refused','network_unreachable','unknown'})
PHASES = frozenset({'request_open','response_read','response_close','unknown'})
_HEX32 = re.compile(r'[0-9a-f]{32}')
_MAX_MILLISECONDS = 86_400_000
_MAX_SECONDS = 86_400


def category(error):
    """Only typed URLError reasons, with bounded traversal and cycle protection."""
    seen = set()
    for _ in range(5):
        if id(error) in seen: return 'unknown'
        seen.add(id(error))
        if isinstance(error, urllib.error.URLError):
            error = error.reason
            if not isinstance(error, BaseException): return 'unknown'
            continue
        # RemoteDisconnected also inherits ConnectionResetError; TLS errors
        # and DNS errors also inherit OSError. Specific types come first.
        if isinstance(error, http.client.RemoteDisconnected): return 'remote_disconnected'
        if isinstance(error, http.client.IncompleteRead): return 'response_incomplete'
        if isinstance(error, ssl.SSLCertVerificationError): return 'tls_certificate'
        if isinstance(error, ssl.SSLError): return 'tls'
        if isinstance(error, socket.gaierror): return 'dns'
        if isinstance(error, TimeoutError): return 'timeout'
        if isinstance(error, ConnectionResetError): return 'connection_reset'
        if isinstance(error, ConnectionRefusedError): return 'connection_refused'
        if isinstance(error, OSError):
            code = error.errno
            win = getattr(error,'winerror',None)
            if type(code) is int:
                known = {errno.ETIMEDOUT:'timeout',errno.ECONNRESET:'connection_reset',
                         errno.ECONNREFUSED:'connection_refused',errno.ENETUNREACH:'network_unreachable',
                         errno.EHOSTUNREACH:'network_unreachable'}
                if code in known: return known[code]
            if type(win) is int:
                return {10060:'timeout',10054:'connection_reset',10061:'connection_refused',
                        10051:'network_unreachable',10065:'network_unreachable'}.get(win,'unknown')
        return 'unknown'
    return 'unknown'


def _number(value, maximum, *, integer=False):
    if type(value) not in ({int} if integer else {int,float}): return None
    try:
        return value if math.isfinite(value) and 0 <= value <= maximum else None
    except (ValueError, OverflowError):
        return None


def sanitize(value):
    if type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        return None
    label, phase = value.get('category'), value.get('phase')
    if type(label) is not str or label not in CATEGORIES or type(phase) is not str or phase not in PHASES:
        return None
    result = {'schema_version':1,'category':label,'phase':phase,
        'duration_ms':_number(value.get('duration_ms'),_MAX_MILLISECONDS,integer=True),
        'timeout_seconds':_number(value.get('timeout_seconds'),_MAX_SECONDS)}
    for key in ('run_id','input_id'):
        item = value.get(key)
        if type(item) is str and _HEX32.fullmatch(item): result[key] = item
    role = value.get('role')
    if type(role) is str and role in {'builder','judge','director'}: result['role'] = role
    index = _number(value.get('call_index'),10000,integer=True)
    if index is not None and index >= 1: result['call_index'] = index
    return result


def failure(error, phase, elapsed_seconds, timeout):
    label = 'unknown'
    try: label = category(error)
    except Exception: pass
    elapsed = _number(elapsed_seconds,_MAX_SECONDS)
    return sanitize({'schema_version':1,'category':label,'phase':phase,
        'duration_ms':int(elapsed*1000) if elapsed is not None else None,'timeout_seconds':timeout})


def bind(value, run_id, role, call_index, *, input_id=None):
    value = sanitize(value)
    if value is None: return None
    # Identity belongs to the executing host, never the provider/exception.
    for key in ('run_id','role','call_index','input_id'): value.pop(key,None)
    value.update(run_id=run_id,role=role,call_index=call_index)
    if input_id is not None: value['input_id'] = input_id
    return sanitize(value)


def summary(value):
    value = sanitize(value)
    if value is None: return ''
    labels = {'timeout':'超时','dns':'域名解析失败','tls_certificate':'TLS证书校验失败',
        'tls':'TLS传输失败','connection_reset':'连接重置','remote_disconnected':'远端提前断开',
        'response_incomplete':'响应读取不完整','connection_refused':'连接被拒绝',
        'network_unreachable':'网络不可达','unknown':'无法准确分类'}
    phases = {'request_open':'连接/发送/等待响应头','response_read':'读取响应',
              'response_close':'关闭响应','unknown':'阶段未知'}
    duration = str(value['duration_ms'])+'毫秒' if value['duration_ms'] is not None else '未知'
    timeout = str(value['timeout_seconds'])+'秒' if value['timeout_seconds'] is not None else '未知'
    return f"传输诊断：{labels[value['category']]}；阶段：{phases[value['phase']]}；耗时：{duration}；配置超时：{timeout}。结果未知，未自动重试。"
