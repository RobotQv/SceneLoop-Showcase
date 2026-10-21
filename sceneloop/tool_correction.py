"""Trusted same-Builder JSON correction receipts; no native reasoning persistence."""
import copy
import hashlib
import json


class UnexecutedToolRejected(Exception):
    """Trusted validation boundary, never inferred from a code or HTTP status."""


def rejection_receipt(error, call_id, name):
    if not isinstance(error, UnexecutedToolRejected):
        raise TypeError('A trusted validation rejection is required')
    details = copy.deepcopy(getattr(error, 'details', None) or {})
    details.update(execution='confirmed_not_executed', execution_scope='current_call',
                   tool_call_id=call_id, tool_name=name, prior_successes_preserved=True)
    return {'ok': False, 'error': {'code': error.code, 'message': str(error), 'details': details}}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode(errors='surrogatepass')).hexdigest()


def exchange_digest(assistant, replies):
    return digest({'calls':assistant.get('tool_calls') or [],'replies':[
        {'id':r['tool_call_id'],'body':json.loads(r['content'])} for r in replies]})


class CorrectionCancelled(Exception):
    """Caller must use its existing cancellation/finalization path."""


class BuilderCorrection:
    LIMIT=2

    def __init__(self, manager, run_id):
        self.manager,self.run_id=manager,run_id
        run=manager.get(run_id)
        self.data=copy.deepcopy(run['builder_tool_correction'] if 'builder_tool_correction' in run else {
            'schema_version':1,'run_id':run_id,'correction_calls':0,'requests':[],
            'batches':[],'seen_ids':[],'pending':None})
        d=self.data
        if (not isinstance(d,dict) or d.get('schema_version')!=1 or d.get('run_id')!=run_id
                or type(d.get('correction_calls')) is not int or not 0<=d['correction_calls']<=self.LIMIT
                or not isinstance(d.get('requests'),list) or len(d['requests'])!=d['correction_calls']
                or not isinstance(d.get('seen_ids'),list) or any(type(x) is not str for x in d['seen_ids'])
                or len(set(d['seen_ids']))!=len(d['seen_ids'])
                or not isinstance(d.get('batches'),list) or len(d['batches'])>d['correction_calls']+1):
            self.fail('tool_correction_state_unknown','纠错累计身份不可确认，不能补发调用。',unknown=True)
        self.received=None;self.last_exception=None

    @property
    def seen_ids(self):return set(self.data['seen_ids'])

    def capture(self, assistant, failures):
        # Only Provider's fully validated outer response enters this transient slot.
        for failure in failures:
            # Diagnostic retention is independent of later monetary settlement.
            # No history permit or execution authority is issued here.
            failure['diagnostics']=self.manager._response_diagnostics(self.run_id,failure['error'])
            self.last_exception=failure['error']
        self.received=(digest(assistant.get('tool_calls') or []),failures)

    def permits_history(self, assistant, replies):
        try:
            proof=exchange_digest(assistant,replies)
            return any(b['execution']=='not_executed' and b['exchange_digest']==proof for b in self.data['batches'])
        except (KeyError,ValueError,TypeError):return False

    def persist(self):
        from .providers import ProviderError
        try:
            with self.manager.lock:
                run=self.manager._get(self.run_id)
                run['builder_tool_correction']=copy.deepcopy(self.data)
                self.manager._save(run)
                from .project_runtime import is_link
                path=self.manager.run_directory(self.run_id)/'run.json'
                if is_link(path) or json.loads(path.read_text(encoding='utf-8')).get('builder_tool_correction')!=self.data:
                    raise ValueError('receipt readback mismatch')
        except Exception as error:
            raise ProviderError('tool_correction_persistence_unknown','纠错消耗/回执落盘不可确认，停止且不补发。',outcome_unknown=True) from error

    def fail(self, code, message, *, unknown=False):
        from .providers import ProviderError
        raise ProviderError(code,message,diagnostics=((self.data.get('last_error') or {}).get('diagnostics') if isinstance(self.data,dict) else None),
                            response_snapshot=getattr(getattr(self,'last_exception',None),'response_snapshot',None),outcome_unknown=unknown)

    def guard(self):
        run=self.manager.get(self.run_id)
        if run.get('cancel_requested'):raise CorrectionCancelled('已取消，不继续纠错。')
        if run.get('director_ledger_unknown'):self.fail('builder_ledger_unknown','调用费用账本未核实，停止纠错/施工。',unknown=True)
        if (run.get('agent') or {}).get('uncertain_write'):
            self.fail('write_result_unknown','已有未知写入，不能纠错重放。',unknown=True)
        if run.get('project_id'):
            state=self.manager._projects().state_store(run['project_id'])
            self.guard_asset(state)
            with state._connection() as c:
                if (c.execute("SELECT 1 FROM operations WHERE run_id=? AND state='unknown'",(self.run_id,)).fetchone()
                        or c.execute("SELECT 1 FROM edit_attempts a JOIN operations o ON a.operation_id=o.operation_id WHERE o.run_id=? AND a.status IN ('scheduled','unknown')",(self.run_id,)).fetchone()):
                    self.fail('write_result_unknown','原SQL写入状态未知，不能继续纠错。',unknown=True)
            if state.run_counter(self.run_id)['needs_reconciliation']:
                self.fail('write_result_unknown','原SQL计数待核实，不能继续纠错。',unknown=True)

    def guard_asset(self, state):
        from .asset_run_store import AssetRunStore
        from .asset_job_store import AssetJobStore
        from .providers import ProviderError
        try:
            store=AssetRunStore(state);doc=store.request(self.run_id)
            if doc is None:return
            blocked=(doc.get('driver_error') or doc['phase'] in {'importing','import_unknown','cost_anomaly'}
                or doc.get('import_intent') and not doc.get('import_receipt'))
            if doc.get('job_id'):
                job=AssetJobStore(state).read(doc['job_id'])
                from .asset_local_authorization import confirmed_local_resource_job
                from .local_asset_failure import confirmed
                known_local_failure=bool(doc.get('failure_feedback') and confirmed(state,doc))
                cost_known = job['cost'] in {'consumed','no_charge_confirmed','released'} or confirmed_local_resource_job(state,doc,job)
                blocked=blocked or (not known_local_failure and (job['dispatch']!='accepted' or job['remote']!='succeeded'
                    or job['preparation']!='ready' or job['safe_error'] is not None
                    or not cost_known))
            elif not doc['closed']:blocked=True
            if not doc['closed'] and doc['phase'] not in {'ready','builder_continuing','pending_review','continuing'}:
                blocked=True
            continuation=store.continuation(doc['id']) if doc.get('builder_mode') else None
            if continuation and continuation['state']=='unknown':blocked=True
            if continuation and continuation['state']=='started':
                worker=self.manager.workers.get(self.run_id)
                if not worker or not worker.is_alive():blocked=True
            if blocked:self.fail('tool_correction_asset_blocked','原资产创建、导入或费用仍待核实，不能继续纠错。',unknown=True)
        except ProviderError:raise
        except Exception:
            self.fail('tool_correction_asset_blocked','原SQL资产证据不可确认，不能继续纠错。',unknown=True)

    def check_request(self, history):
        if not self.data['pending']:return
        self.guard()
        if self.data['correction_calls']>=self.LIMIT:
            self.fail('tool_arguments_correction_exhausted','工具JSON语法纠错两次已耗尽；原错误和回执保留。')
        from .builder_context import latest_exchange
        group=latest_exchange(history)
        if not group or not self.permits_history(group[0],group[1:]):
            self.fail('tool_correction_continuation_unavailable','原未执行native配对续接不可确认，不切换协议或重发。')

    def reserve_request(self):
        if not self.data['pending']:return
        self.data['correction_calls']+=1
        self.data['requests'].append({'index':self.data['correction_calls'],
            'brain_call':self.manager.get(self.run_id)['usage']['brain_calls'],'status':'reserved'})
        self.persist()

    def request_failed(self, error):
        if self.data['requests'] and self.data['requests'][-1]['status']=='reserved':
            cancelled=getattr(error,'reason',None)=='cancelled'
            status=('cancelled' if cancelled else 'blocked_after_response' if self.received is not None else
                    'outcome_unknown' if getattr(error,'outcome_unknown',False) else 'failed')
            if self.received is not None:
                self.data['requests'][-1]['response_status']='returned_invalid' if self.received[1] else 'returned_valid'
            self.data['requests'][-1].update(status=status,
                                           error_code='cancelled' if cancelled else getattr(error,'code','execution_failed'))
            self.persist()

    def accept(self, assistant):
        calls=assistant.get('tool_calls') or []
        if self.received is None or self.received[0]!=digest(calls):
            self.fail('tool_correction_response_unbound','Provider纠错响应身份不可确认。',unknown=True)
        failures=self.received[1];self.received=None
        self.data['seen_ids']+= [c['id'] for c in calls]
        if self.data['requests'] and self.data['requests'][-1]['status']=='reserved':
            self.data['requests'][-1]['status']='returned_invalid' if failures else 'returned_valid'
        if not failures:
            self.data['pending']=None;self.persist();return []
        replies=[];errors={f['id']:f['error'] for f in failures}
        summaries=[]
        for call in calls:
            name=call['function']['name'];error=errors.get(call['id'])
            if error:
                diagnostics=next(f['diagnostics'] for f in failures if f['id']==call['id'])
                parsed=diagnostics.get('response_parse') or {}
                summary={k:copy.deepcopy(parsed[k]) for k in ('layer','reason','tool_index','tool_name','position') if k in parsed}
                summaries.append(summary);self.data['last_error']={'code':'invalid_json','diagnostics':diagnostics}
                feedback={'code':'tool_arguments_json_syntax','message':'工具参数不是有效JSON对象；请重新生成完整参数。本批未执行，之前已完成的施工保持，不要重放。',
                    'tool_call_id':call['id'],'tool_name':name,'execution':'not_executed','execution_scope':'current_batch',
                    'reason':summary.get('reason','unknown'),'prior_successes_preserved':True,
                    'position':summary.get('position',{'status':'unknown'}),'field_path':'unknown'}
            else:
                feedback={'code':'batch_not_executed','message':'同批存在非法JSON，当前调用也未执行；请在新批次重新决定。',
                          'tool_name':name,'execution':'not_executed'}
            replies.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps({'ok':False,'error':feedback},ensure_ascii=False)})
        batch={'execution':'not_executed','brain_call':self.manager.get(self.run_id)['usage']['brain_calls'],
            'calls':[{'id':c['id'],'name':c['function']['name'],'arguments_sha256':hashlib.sha256(c['function']['arguments'].encode(errors='surrogatepass')).hexdigest()} for c in calls],
            'errors':summaries,'exchange_digest':exchange_digest(assistant,replies)}
        self.data['batches'].append(batch);self.data['pending']=batch['exchange_digest'];self.persist()
        return replies

    def paired(self):
        if self.data['correction_calls']>=self.LIMIT:
            self.fail('tool_arguments_correction_exhausted','工具JSON语法纠错两次已耗尽；三次返回及未执行回执保留。')

    def stopped(self, reason):
        self.data['stop_reason']=reason;self.persist()
        self.fail('tool_correction_stopped','Builder纠错后未能完成有效施工，已按原停止条件结束；原工具错误及尝试保留。')
