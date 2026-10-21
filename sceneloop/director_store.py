"""Narrow Director records in the existing project SQLite authority."""
import copy
import json
import re
import uuid
from .director_contract import digest, reject
from . import director_ledger as ledger

def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

class DirectorStore:
    def __init__(self, state, *, cost_source=None, mock_provider=None):
        self.state = state
        self.cost_source = cost_source
        self.mock_provider = mock_provider
        self.run_states = {}  # Bound by the owning Runtime; never model supplied.
        self.run_directories = {}
        self.worker_states = {}
        with state._write() as c:
            for statement in (
                'CREATE TABLE IF NOT EXISTS director_tasks (slot INTEGER PRIMARY KEY CHECK(slot=1), task_id TEXT UNIQUE NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_parents (parent_id TEXT PRIMARY KEY, submission_key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, task_id TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_delegations (delegation_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, call_id TEXT NOT NULL, child_id TEXT UNIQUE NOT NULL, document TEXT NOT NULL, UNIQUE(parent_id,call_id))',
                'CREATE TABLE IF NOT EXISTS director_events (event_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, document TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)',
                'CREATE TABLE IF NOT EXISTS director_sessions (parent_id TEXT PRIMARY KEY, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_decisions (decision_id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_actions (action_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_attempts (attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_cost_evidence (evidence_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, request_digest TEXT NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_metadata (metadata_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, decision_id TEXT NOT NULL, call_id TEXT NOT NULL, request_digest TEXT NOT NULL, adopted INTEGER NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS director_progress_results (scope_digest TEXT NOT NULL, result_id TEXT NOT NULL, PRIMARY KEY(scope_digest,result_id))'):
                c.execute(statement)
    def task(self):
        with self.state._connection() as c: row = c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()
        return json.loads(row[0]) if row else None
    def readonly(self, parent):
        from .director_readonly import enabled
        return enabled(self,parent)
    def register(self, parent, fingerprint):
        with self.state._write() as c:
            row = c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()
            task = json.loads(row[0]) if row else {'task_id':uuid.uuid4().hex,'policy_version':1,
                'policy':parent['director_policy'],'authorization':{'source':
                    'protected_readonly_request' if parent.get('conversation_mode')=='read_only' else 'protected_user_request',
                    'parent_run_id':parent['id']},
                'pause':None,'decisions':[],'revision':1,'goal_ref':None,'progress':{},
                'render_policy':{'storage_limit_bytes':128*1024*1024}}
            if task['policy'] != parent['director_policy']: reject('持续任务政策不能通过新请求静默重置。','director_policy_conflict',409)
            from .director_creation import register
            register(task,parent)
            c.execute('INSERT OR IGNORE INTO director_tasks VALUES (1,?,?)',(task['task_id'],encoded(task)))
            c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
            c.execute('INSERT INTO director_parents VALUES (?,?,?,?)',
                (parent['id'],parent['idempotency_key'],fingerprint,task['task_id']))
            event_id=digest({'parent_id':parent['id'],'kind':'user_input'})
            c.execute('INSERT INTO director_sessions VALUES (?,?)',(parent['id'],encoded({
                'parent_id':parent['id'],'run':copy.deepcopy(parent),'exchange':[],
                'event_id':event_id,'state':'prepared','result':None,'turn_pending':False})))
            c.execute('INSERT INTO director_events VALUES (?,?,?,0)',(event_id,parent['id'],encoded({
                'kind':'user_input','source':'interactive','state':'prepared','result':None})))
        return task
    def parent(self, rid):
        with self.state._connection() as c: row=c.execute('SELECT * FROM director_parents WHERE parent_id=?',(rid,)).fetchone()
        return dict(row) if row else None
    def parent_by_key(self, key):
        with self.state._connection() as c: row=c.execute('SELECT * FROM director_parents WHERE submission_key=?',(key,)).fetchone()
        return dict(row) if row else None
    def bindings(self, parent=None):
        with self.state._connection() as c:
            rows=c.execute('SELECT document FROM director_delegations'+(' WHERE parent_id=?' if parent else ''),
                           (parent,) if parent else ()).fetchall()
        return [json.loads(row[0]) for row in rows]
    def child(self, rid):
        with self.state._connection() as c: row=c.execute('SELECT document FROM director_delegations WHERE child_id=?',(rid,)).fetchone()
        return json.loads(row[0]) if row else None
    def confirmed_not_created(self, record):
        """A rejection label alone cannot erase an actual child or SQL attempt."""
        if (record.get('state') != 'rejected' or record.get('rejection', {}).get('execution') != 'confirmed_not_created'
                or not re.fullmatch(r'[0-9a-f]{32}', str(record.get('child_id', '')))
                or (record.get('request') or {}).get('project_id') != self.state.project_id
                or record.get('child_snapshot') or record.get('result') or record.get('child_id') in self.run_states):
            return False
        from .project_runtime import is_link
        directory = self.state.metadata / 'runs' / record['child_id']
        if is_link(directory) or directory.exists(): return False
        with self.state._connection() as c:
            row = c.execute('SELECT * FROM director_delegations WHERE child_id=?', (record['child_id'],)).fetchone()
            if (not row or json.loads(row['document']) != record or any(record.get(key) != row[key]
                    for key in ('delegation_id', 'parent_id', 'call_id', 'child_id'))): return False
            parent = c.execute('SELECT task_id FROM director_parents WHERE parent_id=?', (record['parent_id'],)).fetchone()
            if not parent or parent[0] != record['task_id']: return False
            for table in ('operations', 'receipts', 'local_rounds', 'asset_run_capabilities',
                          'asset_run_requests', 'evaluation_inputs', 'evaluation_receipts'):
                if c.execute('SELECT 1 FROM ' + table + ' WHERE run_id=? LIMIT 1', (record['child_id'],)).fetchone():
                    return False
            if any(json.loads(row[0]).get('run_id') == record['child_id']
                   for row in c.execute('SELECT document FROM director_attempts')): return False
        return True
    def reserve(self, parent, call_id, brief):
        if self.readonly(parent['id']):reject('只读请求不能委派施工。','director_readonly_violation',409)
        with self.state._write() as c:
            row=c.execute('SELECT document FROM director_delegations WHERE parent_id=? AND call_id=?',(parent['id'],call_id)).fetchone()
            if row:
                existing=json.loads(row[0])
                if digest(existing['brief'])!=digest(brief): reject('委派身份内容冲突。','director_delegation_conflict',409)
                return existing
            active=[json.loads(row[0]) for row in c.execute('SELECT document FROM director_delegations')]
            if any(row['state'] in {'reserved','prepared','started','waiting'} for row in active): reject('同项目只允许一个Builder写入者。','busy',409)
            record={'delegation_id':uuid.uuid4().hex,'child_id':uuid.uuid4().hex,'parent_id':parent['id'],
                'task_id':self.parent(parent['id'])['task_id'],'call_id':call_id,'state':'reserved','brief':brief,'result':None}
            c.execute('INSERT INTO director_delegations VALUES (?,?,?,?,?)',
                (record['delegation_id'],record['parent_id'],call_id,record['child_id'],encoded(record)))
        return record
    def update(self, record):
        with self.state._write() as c:
            c.execute('UPDATE director_delegations SET document=? WHERE delegation_id=?',
                      (encoded(record),record['delegation_id']))
    def finish(self, record, result):
        event_id=digest({'delegation':record['delegation_id'],'result':digest(result)})
        with self.state._write() as c:
            stored=json.loads(c.execute('SELECT document FROM director_delegations WHERE delegation_id=?',
                (record['delegation_id'],)).fetchone()[0])
            if stored['result'] is not None:
                if digest(stored['result'])!=digest(result): reject('结果事实不能覆盖。','director_result_conflict',409)
                return event_id
            stored.update(state='completed',result=result)
            c.execute('UPDATE director_delegations SET document=? WHERE delegation_id=?',(encoded(stored),record['delegation_id']))
            c.execute('INSERT OR IGNORE INTO director_events VALUES (?,?,?,0)',
                (event_id,record['parent_id'],encoded({'kind':'child_finished','source':'autonomous','state':'prepared','result':result})))
        return event_id
    def consume(self, event_id):
        with self.state._write() as c:
            changed=c.execute('UPDATE director_events SET consumed=1 WHERE event_id=? AND consumed=0',(event_id,)).rowcount
        return bool(changed)
    def pause(self, reason, evidence):
        task=self.task()
        task['revision']=task.get('revision',1)+1
        scope='all_models' if reason in {'unknown_action','ledger_write_unknown','price_upper_bound_missing','decision_outcome_unknown','provider_continuation_unavailable','cost_evidence_conflict','cost_exposure_unknown','cost_limit_exhausted','cost_limit_exceeded'} else 'autonomous'
        if (task.get('pause') or {}).get('scope')=='all_models' and scope!='all_models': return
        if (task.get('pause') or {}).get('reason')=='repeated_no_progress' and reason=='child_requires_review': return
        task['pause']={'pause_id':uuid.uuid4().hex,'reason':reason,'scope':scope,'evidence':evidence,
            'version':task['revision'],'resume_condition':'human review with original claim/version and changed basis; never replay started/unknown attempts'}
        self._task_write(task)
    def _task_write(self, task):
        with self.state._write() as c: c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
    def legacy_boundary_origin(self, c, task, current_parent=None):
        """Prove one historical, returned-but-unexecuted legacy boundary in SQL.

        This never consumes it, loads Provider reasoning, or clears its pause.
        Old pauses lacked a parent ID, so ambiguous origins fail closed.
        """
        pause=task.get('pause') or {}; evidence=pause.get('evidence') or {}
        if pause.get('scope')!='autonomous' or pause.get('reason')!='decision_boundary_invalid':return None
        count=evidence.get('call_count')
        if type(count) is not int or count<2:return None
        def documents(table):return [json.loads(row[0]) for row in c.execute('SELECT document FROM '+table)]
        sessions={row['parent_id']:row for row in documents('director_sessions')}
        decisions=documents('director_decisions');bindings=documents('director_delegations')
        attempts=ledger.rows(c,task['task_id']);by_attempt={row['attempt_id']:row for row in attempts}
        owned={current_parent}|{r['child_id'] for r in bindings if r['parent_id']==current_parent}
        for row in attempts:
            if (row.get('cost_conflict') or row.get('cost_anomaly') or row['state']=='unknown'
                or row['state']!='settled' and row['run_id'] not in owned):return None
        if any(d['state']=='unknown' or d['state']=='started' and d['parent_id'] not in owned for d in decisions):return None
        if any(r['state'] in {'reserved','prepared','started','waiting'} and r['parent_id']!=current_parent for r in bindings):return None
        if c.execute('SELECT 1 FROM reconciliation LIMIT 1').fetchone():return None
        if c.execute('SELECT 1 FROM run_counters WHERE needs_reconciliation!=0 LIMIT 1').fetchone():return None
        if any(r['state']=='unknown' or r['state']=='prepared' and r['run_id'] not in owned
               for r in c.execute('SELECT run_id,state FROM operations')):return None
        if any(r['status']=='paused' or r['run_id'] not in owned
               for r in c.execute("SELECT run_id,status FROM local_rounds WHERE status IN ('active','paused')")):return None
        if any(r[0] not in owned for r in c.execute('SELECT run_id FROM asset_run_requests WHERE closed=0')):return None
        for job in documents('asset_generation_jobs'):
            if (job.get('remote') not in {'succeeded','failed','banned','expired','cancelled'}
                or job.get('dispatch') in {'submitting','outcome_unknown'}
                or job.get('cost') not in {'consumed','released','no_charge_confirmed'}):return None
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        observations=documents('director_observe_claims') if 'director_observe_claims' in tables else []
        if any(r['state']!='returned' for r in observations):return None
        if task.get('consecutive_observation_repeats',0)>=2 or task.get('metadata_progress',{}).get('consecutive_no_change',0)>=2:return None
        for run in self.run_states.values():
            if (run.get('project_id')==self.state.project_id and run['id'] not in owned
                and run.get('status') not in {'completed','failed','cancelled','interrupted','awaiting_resume','queued'}):return None
        candidates=[]
        for decision in decisions:
            rid=decision['parent_id'];session=sessions.get(rid,{});accepted=session.get('run',{})
            run=self.run_states.get(rid,{});calls=(decision.get('assistant') or {}).get('tool_calls',[])
            reference=decision.get('private_ref') or {};attempt=by_attempt.get(decision.get('attempt_id'),{})
            if (accepted.get('request_protocol')=='natural_v1' or session.get('natural_recovery')
                or decision['state']!='returned' or 'batch' in decision or len(calls)!=count
                or session.get('event_id')!=decision['event_id'] or session.get('exchange')
                or session.get('decision') or session.get('observations') or session.get('metadata_results')
                or run.get('status') not in {'failed','awaiting_resume'} or run.get('cancel_requested')
                or (run.get('error') or {}).get('code')!='director_boundary_invalid'
                or run.get('director_ledger_unknown')):continue
            parent=c.execute('SELECT task_id FROM director_parents WHERE parent_id=?',(rid,)).fetchone()
            event=c.execute('SELECT parent_id,consumed FROM director_events WHERE event_id=?',(decision['event_id'],)).fetchone()
            if (not parent or parent[0]!=task['task_id'] or not event or event['parent_id']!=rid or event['consumed']
                or accepted.get('id')!=rid or accepted.get('project_id')!=self.state.project_id
                or any(reference.get(k)!=v for k,v in {'project_id':self.state.project_id,'parent_id':rid,
                    'decision_id':decision['decision_id'],'event_id':decision['event_id'],'slot':'response'}.items())
                or not isinstance(reference.get('cipher_sha256'),str) or len(reference['cipher_sha256'])!=64
                or attempt.get('state')!='settled' or attempt.get('run_id')!=rid or attempt.get('role')!='director'
                or attempt.get('call_index')!=decision['call_index'] or attempt.get('settlement',{}).get('outcome')!='returned'):continue
            if (any(r['parent_id']==rid for r in bindings) or any(r['parent_id']==rid for r in observations)
                or c.execute('SELECT 1 FROM director_metadata WHERE parent_id=?',(rid,)).fetchone()
                or c.execute('SELECT 1 FROM operations WHERE run_id=?',(rid,)).fetchone()
                or c.execute('SELECT 1 FROM receipts WHERE run_id=?',(rid,)).fetchone()):continue
            if evidence.get('parent_run_id',rid)!=rid or evidence.get('decision_id',decision['decision_id'])!=decision['decision_id']:continue
            from .director_continuation import Continuation
            directory=self.run_directories.get(rid)
            if not directory or not Continuation(directory,self.state.project_id,rid).verified_response(reference,decision):continue
            candidates.append(rid)
        return candidates[0] if len(candidates)==1 else None
    def pause_blocks(self, task, accepted, scope, *, c=None):
        from .director_request_scope import pause_blocks,finite_authorization,managed_authorization
        if not pause_blocks(task,accepted,scope):return False
        closed_stop=(task.get('pause') or {}).get('reason')=='user_cancelled'
        if c is None:
            with self.state._connection() as connection:return self.pause_blocks(task,accepted,scope,c=connection)
        session=c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(accepted['id'],)).fetchone()
        if not (finite_authorization(accepted,scope) or closed_stop and managed_authorization(accepted,scope,
            session=json.loads(session[0]) if session else None,connection=c)):return True
        parent=c.execute('SELECT task_id FROM director_parents WHERE parent_id=?',(accepted['id'],)).fetchone()
        if not parent or parent[0]!=task['task_id']:return True
        if (task.get('pause') or {}).get('reason')=='user_cancelled':
            return not self.closed_finite_stop(c,task,accepted['id'])
        try:origin=self.legacy_boundary_origin(c,task,accepted['id'])
        except (KeyError,TypeError,ValueError,AttributeError):return True
        return origin is None or origin==accepted['id']
    def closed_finite_stop(self,c,task,parent):
        """A new finite message cannot revoke an ongoing/unknown or continuous stop."""
        return self.finite_stop_blocker(c,task,parent) is None

    def historical_candidate(self,c,operation,parent,*,allow_superseded_initial=False,
                             allow_same_working=False,finishing_run=None):
        """Prove an unpublished candidate is separated from the authoritative head.

        It stays unknown/unreplayable. This only answers whether its isolated
        files can stop a different request on a verified descendant Working.
        """
        def selection(value):
            return (value.get('run_id'),value.get('revision_id'),value.get('scene_id')) if value else None
        try:
            rid=operation['run_id'];kind=operation['kind'];identity=json.loads(operation['identity'])
            if (rid==parent or kind not in {'draft_edit','builder','initial'} or operation['project_id']!=self.state.project_id
                or any(identity.get(k)!=v for k,v in {'project_id':self.state.project_id,'run_id':rid,'kind':kind}.items())):return False
            run=self.run_states.get(rid,{})
            if run.get('status') not in {'completed','failed','cancelled','interrupted'}:return False
            worker=self.worker_states.get(rid)
            if worker and worker.is_alive():
                import threading
                if finishing_run!=rid or worker is not threading.current_thread():return False
            if operation['state']=='unknown' and kind!='draft_edit':return False
            if c.execute('SELECT 1 FROM receipts WHERE operation_id=?',(operation['operation_id'],)).fetchone():return False
            rounds=[json.loads(r[0]) for r in c.execute('SELECT document FROM local_rounds WHERE run_id=?',(rid,))]
            document=next((r for r in rounds if r['id']==identity.get('round_id')),None) if kind=='draft_edit' else next(
                (r for r in rounds if r.get('commit_operation_id')==operation['operation_id']),None)
            source=identity.get('source_selection');version=identity.get('expected_working_version')
            if kind in {'draft_edit','builder'}:
                if not document or document.get('status') not in {'ended','committed'} or document.get('project_id')!=self.state.project_id:return False
                authority=c.execute('SELECT status FROM local_rounds WHERE round_id=? AND run_id=?',(document['id'],rid)).fetchone()
                if not authority or authority[0]!=document['status']:return False
                start=document['start'];version=start['working_version']
                if selection(start['selection'])!=selection(source) or start['project_revision']!=identity.get('expected_project_revision'):return False
                if document['status']=='ended':
                    request={'end_id':document.get('end_id'),'round_id':document['id'],
                        'expected_round_version':document['version']-1,'reason':document.get('end_reason')}
                    if not request['end_id'] or not request['reason'] or digest(request)!=document.get('end_digest'):return False
                else:
                    row=c.execute('SELECT receipt FROM receipts WHERE operation_id=?',(document.get('receipt_operation_id'),)).fetchone()
                    if not row or json.loads(row[0]).get('local_round',{}).get('round_id')!=document['id']:return False
                if kind=='builder' and (document.get('commit_identity')!=identity or operation['state']!='prepared'):return False
                if kind=='draft_edit':
                    matches=[s for s in document.get('steps',[]) if s.get('operation_id')==operation['operation_id']]
                    if len(matches)!=1 or matches[0].get('base_revision')!=identity.get('base_revision'):return False
                    candidate={'run_id':rid,'revision_id':matches[0]['revision_id'],'scene_id':(source or {}).get('scene_id')}
                    if any(selection(json.loads(r[0]).get('target_selection'))==selection(candidate) for r in c.execute('SELECT receipt FROM receipts')):return False
            if type(version) is not int or version<0:return False
            project=json.loads(self.state._read_row(c)['manifest']);head=project.get('current_scene');head_version=project['workspace']['working_version']
            if type(head_version) is not int or head_version<version:return False
            same_head=head_version==version
            if same_head and (not allow_same_working or kind not in {'draft_edit','builder'} or not source
                or selection(head)!=selection(source) or document['status']!='ended'):return False
            if same_head and kind=='builder':
                # begin_local_round reserves a CAS identity before any edit or
                # save is attempted. A legally ended, unused reservation is
                # not an uncertain Working write. Never accept unknown here.
                if (operation['state']!='prepared' or document.get('receipt_operation_id')
                    or c.execute('SELECT 1 FROM edit_attempts WHERE operation_id=?',(operation['operation_id'],)).fetchone()):return False
                candidate={'run_id':rid,'revision_id':document.get('selected_revision'),'scene_id':source.get('scene_id')}
                if not candidate['revision_id'] or any(selection(json.loads(r[0]).get('target_selection'))==selection(candidate)
                    for r in c.execute('SELECT receipt FROM receipts')):return False
            receipts=[]
            for row in c.execute('SELECT r.receipt,o.identity,o.state FROM receipts r JOIN operations o ON o.operation_id=r.operation_id'):
                receipt=json.loads(row[0]);origin=json.loads(row[1])
                if (row[2]!='committed' or receipt.get('kind') not in {'initial','builder'}
                    or receipt.get('project_id')!=self.state.project_id or origin.get('project_id')!=self.state.project_id
                    or receipt.get('working_version')!=origin.get('expected_working_version',-2)+1
                    or receipt.get('project_revision')!=origin.get('expected_project_revision',-2)+1
                    or selection(receipt.get('source_selection'))!=selection(origin.get('source_selection'))):continue
                receipts.append(receipt)
            # Follow actual receipt source/target links, not dates or merely a
            # larger version number. A missing/adopted/ambiguous head is blocked.
            current=selection(head);remaining=head_version;chain=[]
            while remaining>version:
                matches=[r for r in receipts if r['working_version']==remaining and selection(r.get('target_selection'))==current]
                if len(matches)!=1:return False
                receipt=matches[0];chain.append(receipt);current=selection(receipt.get('source_selection'));remaining-=1
            if current!=selection(source):return False
            if same_head:
                # No synthetic descendant: the exact unchanged Working still
                # has its own unique committed receipt; the ended local draft
                # is private to this terminal Run and was never published.
                matches=[r for r in receipts if r['working_version']==head_version and selection(r.get('target_selection'))==selection(head)]
                if len(matches)!=1:return False
                head_receipt=matches[0]
                for row in c.execute('SELECT run_id,document FROM local_rounds WHERE run_id!=?',(rid,)):
                    other=json.loads(row['document'])
                    if selection((other.get('start') or {}).get('selection'))==selection(candidate):return False
            else:head_receipt=chain[0]
            mapping=c.execute('SELECT selection,artifacts,availability FROM scene_revisions WHERE revision_id=?',(head_receipt.get('global_revision_id'),)).fetchone()
            if (not mapping or mapping['availability']!='available' or selection(json.loads(mapping['selection']))!=selection(head)
                or json.loads(mapping['artifacts'])!=head_receipt.get('artifacts')):return False
            if kind=='initial' and not any(r['run_id']==rid and r['kind']=='initial' for r in chain):
                # Goal continuity may prove an old, unpublished Working0
                # reservation was superseded by a different formal initial.
                # Keep every old record and default resume semantics unchanged.
                if (not allow_superseded_initial or operation['state']!='prepared'
                    or source is not None or version!=0 or run.get('director_ledger_unknown')
                    or any(r[0] not in {'succeeded','failed'} for r in c.execute(
                        'SELECT status FROM edit_attempts WHERE operation_id=?',(operation['operation_id'],)))
                    or chain[-1]['kind']!='initial' or chain[-1].get('source_selection') is not None):return False
            return True
        except (ValueError,TypeError,KeyError,AttributeError):return False

    def finite_stop_blocker(self,c,task,parent):
        """Whitelist-only diagnosis, never exposes raw SQL identities or paths."""
        origin=((task.get('pause') or {}).get('evidence') or {}).get('parent_run_id')
        def block(category,reason,source=None):
            import re
            rid=source or origin
            return {'category':category,'source_run_id':rid if isinstance(rid,str) and re.fullmatch(r'[0-9a-f]{32}',rid) else None,'reason':reason}
        try:
            pause=task.get('pause') or {};origin=(pause.get('evidence') or {}).get('parent_run_id')
            if pause.get('scope')!='autonomous' or origin==parent:return block('stop_origin_unverified','旧请求停止仍适用于原请求。')
            row=c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(origin,)).fetchone()
            if not row:return block('stop_origin_unverified','旧请求的停止身份缺少可核验记录。')
            old=json.loads(row[0]);run=self.run_states.get(origin,{})
            from .director_request_scope import finite_authorization,managed_authorization
            if (not (finite_authorization(old['run'],old.get('request_scope')) or managed_authorization(old['run'],old.get('request_scope'),session=old,connection=c)) or not run.get('cancel_requested')
                or run.get('status')!='cancelled' or run.get('director_ledger_unknown')):return block('stop_origin_unverified','旧请求尚未确认安全结束，或属于持续停止。')
            bound=c.execute('SELECT task_id FROM director_parents WHERE parent_id=?',(origin,)).fetchone()
            if not bound or bound[0]!=task['task_id']:return block('stop_origin_unverified','旧请求不属于当前任务的可信停止记录。')
            bindings=[json.loads(r[0]) for r in c.execute('SELECT document FROM director_delegations')]
            owned={parent}|{r['child_id'] for r in bindings if r['parent_id']==parent}
            stopped={origin}|{r['child_id'] for r in bindings if r['parent_id']==origin}
            for rid in stopped:
                child=self.run_states.get(rid,{})
                worker=self.worker_states.get(rid)
                if child.get('status') not in {'completed','failed','cancelled','interrupted'} or worker and worker.is_alive():return block('active_writer','旧请求或子执行仍在收尾。',rid)
            if any(r['state'] in {'reserved','prepared','started','waiting'} and r['parent_id']!=parent for r in bindings):return block('active_writer','项目仍有未结束的委派写入。')
            attempts=ledger.rows(c,task['task_id'])
            if any(r.get('cost_conflict') or r.get('cost_anomaly') or r['state']=='unknown'
                   or r['state']!='settled' and r['run_id'] not in owned for r in attempts):return block('model_or_cost_unresolved','项目存在待核实的模型或费用结果。')
            for row in c.execute('SELECT document FROM director_decisions'):
                decision=json.loads(row[0])
                if decision['state']=='unknown' or decision['state']=='started' and decision['parent_id'] not in owned:return block('model_or_cost_unresolved','原模型决策结果尚未核实。',decision['parent_id'])
            if c.execute('SELECT 1 FROM reconciliation LIMIT 1').fetchone():return block('working_reconciliation','Working状态仍需核对。')
            if c.execute('SELECT 1 FROM run_counters WHERE needs_reconciliation!=0 LIMIT 1').fetchone():return block('working_reconciliation','正式保存计数仍需核对。')
            for operation in c.execute("SELECT * FROM operations WHERE state IN ('prepared','unknown')"):
                if operation['state']=='prepared' and operation['run_id'] in owned:continue
                if operation['run_id'] in owned or not self.historical_candidate(c,operation,parent):
                    category='working_commit_unresolved' if operation['kind'] in {'builder','initial'} else 'historical_operation_unverified'
                    return block(category,'历史操作尚不能证明与当前Working隔离；原记录保留待核实。',operation['run_id'])
            if any(r[0] not in owned for r in c.execute("SELECT run_id FROM local_rounds WHERE status IN ('active','paused')")):return block('active_writer','项目仍有未结束的草稿轮。')
            if any(r[0] not in owned for r in c.execute('SELECT run_id FROM asset_run_requests WHERE closed=0')):return block('external_asset_pending','项目仍有待处理的资产请求。')
            for row in c.execute('SELECT document FROM asset_generation_jobs'):
                job=json.loads(row[0])
                if (job.get('remote') not in {'succeeded','failed','cancelled','banned','expired'}
                    or job.get('dispatch') in {'submitting','outcome_unknown'}
                    or job.get('cost') not in {'consumed','released','no_charge_confirmed'}):return block('external_asset_pending','资产创建或费用状态尚未核实。')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'director_observe_claims' in tables:
                if any(json.loads(r[0])['state']!='returned' for r in c.execute('SELECT document FROM director_observe_claims')):return block('observation_unresolved','原观察操作结果尚未核实。')
            for run in self.run_states.values():
                if (run.get('project_id')==self.state.project_id and run['id'] not in owned
                    and run.get('operation')!='conversation' and run.get('status') not in {'completed','failed','cancelled','interrupted'}):return block('active_writer','项目仍有活动施工。',run['id'])
            return None
        except (ValueError,TypeError,KeyError,AttributeError):return block('stop_origin_unverified','旧请求的停止或历史证据不完整。')
    def decision(self, parent_id, value):
        task=self.task(); task['decisions']=(task['decisions']+[{'parent_id':parent_id,**copy.deepcopy(value)}])[-8:]
        self._task_write(task)
    def attempt(self, rid, role, index, profile, source, *, event_id=None):
        if role not in {'director','builder','judge'} or source not in {'interactive','autonomous'}:
            reject('费用调用角色/触发来源无效。','director_cost_evidence_invalid',409)
        attempt_id=digest({'run_id':rid,'role':role,'call_index':index})
        task=self.task()
        self.admission(source)
        if task['pause'] and source=='autonomous':
            reject('持续任务暂停，不再自动调用模型。','director_autonomy_paused',409)
        if task['pause'] and task['pause'].get('scope')=='all_models':
            reject('费用或请求结果未知，保留只读状态。','director_economy_paused',409)
        try:
            price=ledger.quote(self.cost_source,profile,role,self.mock_provider)
        except Exception:
            self.pause('cost_evidence_conflict',{'role':role,'run_id':rid,'reason':'invalid trusted quote'})
            reject('可信价格来源/适用范围不可验证，停止新调用。','director_economy_paused',409)
        binding=self.parent(rid) or self.child(rid)
        if not binding or binding['task_id']!=task['task_id']: reject('费用调用缺少原task/Run关联。','director_unbound',409)
        record={'attempt_id':attempt_id,'task_id':task['task_id'],'policy_version':task['policy_version'],
            'run_id':rid,'role':role,'call_index':index,'source':source,
            'profile_id':profile.get('id'),'profile_revision':profile.get('revision'),
            'provider_id':profile.get('provider_id'),'model':profile.get('model'),'price':price,
            'state':'pending','confidence':'estimated' if price and price['estimate_amount'] is not None else 'reserved',
            'currency':price['currency'] if price else None,'amount':price['estimate_amount'] if price else None,
            'price_version':price['version'] if price else None,
            'cost_basis_source':price['source'] if price else None,
            'input_tokens':None,'completion_tokens':None,'cache_hit_tokens':None,'cache_miss_tokens':None,
            'max_output_tokens':None,'image_count':None,
            'duration_ms':None,'amount_limit_guaranteed':bool(price and price['hard_upper_bound'] is not None),
            'upper_bound_scope':'controlled_mock_only' if price and price['hard_upper_bound'] is not None else None}
        blocked=None
        with self.state._write() as c:
            current=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            if current['policy_version']!=task['policy_version'] or current.get('revision',1)!=task.get('revision',1):
                reject('费用准入期间政策/暂停版本已变化。','director_action_stale',409)
            decision=None
            if event_id:
                row=c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()
                decision=json.loads(row[0]) if row else None
                if not decision or decision['state']!='prepared': reject('原决策已开始或缺失，不能重发。','director_decision_not_replayable',409)
            if c.execute('SELECT 1 FROM director_attempts WHERE attempt_id=?',(attempt_id,)).fetchone():
                reject('模型attempt已登记，不能重发。','director_attempt_exists',409)
            blocked=ledger.strict_check(current,ledger.rows(c,task['task_id']),price)
            if blocked:
                ledger.cost_pause(c,current,blocked,{'role':role,'run_id':rid,'attempt_id':attempt_id})
            else:
                c.execute('INSERT INTO director_attempts VALUES (?,?,?)',(attempt_id,task['task_id'],encoded(record)))
                if decision:
                    decision.update(state='started',attempt_id=attempt_id)
                    c.execute('UPDATE director_decisions SET document=? WHERE event_id=?',(encoded(decision),event_id))
        if blocked: reject('严格经济准入缺上界、未知占用或额度不足。','director_economy_paused',409)
        return record
    def settle(self, record, usage, error, duration):
        with self.state._write() as c:
            row=c.execute('SELECT document FROM director_attempts WHERE attempt_id=? AND task_id=?',
                          (record['attempt_id'],record['task_id'])).fetchone()
            if not row: reject('原费用attempt缺失。','director_unbound',409)
            # A fully received, metered response can fail protocol validation
            # without making the transport/charge outcome unknown. Preserve
            # that failure separately; missing response proof stays unknown.
            returned_error=(error is not None and getattr(error,'outcome_unknown',None) is False
                and isinstance(usage,dict) and usage.get('state')=='error'
                and usage.get('input_source')=='response.usage.prompt_tokens'
                and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','completion_tokens')))
            record=ledger.settled(json.loads(row[0]),usage,None if returned_error else error,duration)
            if returned_error:
                code=getattr(error,'code',None)
                record['response_failure']={'classification':'returned_protocol_error',
                    'code':code if isinstance(code,str) and re.fullmatch(r'[a-z0-9_]{1,80}',code) else 'protocol_error'}
            c.execute('UPDATE director_attempts SET document=? WHERE attempt_id=?',(encoded(record),record['attempt_id']))
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            if ledger.anomaly(task,ledger.rows(c,task['task_id'])):
                # Consume the already admitted response and commit first; the
                # next model gate enforces its newly confirmed exposure.
                record['cost_anomaly']='returned cost exceeds current strict limit'
                c.execute('UPDATE director_attempts SET document=? WHERE attempt_id=?',(encoded(record),record['attempt_id']))
        if error and getattr(error,'outcome_unknown',False): self.pause('unknown_action',{'attempt_id':record['attempt_id']})
        receiver=getattr(self.cost_source,'receipt',None)
        if receiver:
            fact=receiver(copy.deepcopy(record),ledger.token_usage(usage),error)
            if fact is not None: self.cost_receipt(fact,authority=self.cost_source)
        return record
    def cost_receipt(self, fact, *, authority):
        """Internal-only reconciliation; never called with model/tool free text."""
        if authority is None or authority is not self.cost_source:
            reject('成本回执需要后端可信来源。','director_cost_evidence_invalid',409)
        if not isinstance(fact,dict) or not isinstance(fact.get('attempt_id'),str):
            reject('成本回执身份无效。','director_cost_evidence_invalid',409)
        conflict=False; result=None
        with self.state._write() as c:
            row=c.execute('SELECT document FROM director_attempts WHERE attempt_id=?',(fact['attempt_id'],)).fetchone()
            if not row: reject('原成本attempt不存在。','director_unbound',409)
            result=json.loads(row[0]); task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            if result['task_id']!=task['task_id']: reject('成本task身份冲突。','director_unbound',409)
            try:
                ledger.validate_receipt(fact,result)
                key=ledger.evidence_key(fact); fingerprint=digest(fact)
                prior=c.execute('SELECT * FROM director_cost_evidence WHERE evidence_key=?',(key,)).fetchone()
                if prior:
                    if prior['attempt_id']!=result['attempt_id'] or prior['request_digest']!=fingerprint:
                        reject('相同回执身份内容冲突。','director_cost_evidence_invalid',409)
                    return result
                if fact['supersedes']!=result.get('cost_receipt_id'):
                    reject('晚到回执必须绑定当前原回执，不能用新序号覆盖。','director_cost_evidence_invalid',409)
            except Exception:
                conflict=True
                result['cost_conflict']={'evidence_digest':digest(fact),'reason':'receipt identity/unit/version/basis conflict'}
                ledger.cost_pause(c,task,'cost_evidence_conflict',{'attempt_id':result['attempt_id']})
            if not conflict:
                c.execute('INSERT INTO director_cost_evidence VALUES (?,?,?,?)',
                          (key,result['attempt_id'],fingerprint,encoded(fact)))
                result.update(amount=fact['amount'],currency=fact['currency'],confidence='confirmed',
                              cost_receipt_id=fact['receipt_id'],cost_basis_source=fact['source'])
            c.execute('UPDATE director_attempts SET document=? WHERE attempt_id=?',(encoded(result),result['attempt_id']))
            if not conflict and ledger.anomaly(task,ledger.rows(c,task['task_id'])):
                result['cost_anomaly']='confirmed cost exceeds current strict limit'
                c.execute('UPDATE director_attempts SET document=? WHERE attempt_id=?',(encoded(result),result['attempt_id']))
        if conflict: reject('成本证据冲突，保留原金额并暂停。','director_cost_evidence_invalid',409)
        return result
    def cost_summary(self, expected_task_id=None):
        with self.state._connection() as c:
            row=c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()
            task=json.loads(row[0]) if row else None
            if not task or expected_task_id is not None and task['task_id']!=expected_task_id:
                return ledger.unbound_summary(expected_task_id)
            return ledger.summary(ledger.rows(c,task['task_id']))
    def economic_dispatch_admission(self, profile, *, source='autonomous'):
        """Gate the next child; this is not an already-admitted tool commit."""
        self.admission(source)
        if self.task()['policy']['mode']!='strict': return
        price=ledger.quote(self.cost_source,profile,'builder',self.mock_provider)
        with self.state._write() as c:
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            blocked=ledger.strict_check(task,ledger.rows(c,task['task_id']),price)
            if blocked: ledger.cost_pause(c,task,blocked,{'stage':'new child dispatch'})
        if blocked: reject('经济授权不足，保留已返回Director结果，不启动新子任务。','director_economy_paused',409)
    def detail(self, parent):
        with self.state._connection() as c:
            attempts=ledger.rows(c,self.parent(parent)['task_id'])
            events=[{'event_id':row['event_id'],'consumed':bool(row['consumed'])}
                for row in c.execute('SELECT event_id,consumed FROM director_events WHERE parent_id=?',(parent,))]
        return {'task':self.task(),'delegations':self.bindings(parent)[-16:],'attempts':attempts[-64:],
            'events':events[-16:],'recovery':self.recovery_view(parent),'cost_summary':ledger.summary(attempts)}
    def recovery_view(self, parent):
        session=self.session(parent)
        if not session: return {'state':'legacy_m1','requires_human_review':True}
        decision=self.decision_for(session['event_id'])
        view = {k:copy.deepcopy(session.get(k)) for k in ('state','event_id','child_id','turn_pending')} | {
            'decision_id':(decision or {}).get('decision_id'),'decision_state':(decision or {}).get('state','prepared'),
            'native_exchange_digest':digest(session['exchange'])}
        if any(self.confirmed_not_created(record) for record in self.bindings(parent)):
            view['state'] = 'child_creation_rejected'
        return view

    def session(self, parent):
        with self.state._connection() as c:
            row=c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()
        return json.loads(row[0]) if row else None
    def sessions(self):
        with self.state._connection() as c: rows=c.execute('SELECT document FROM director_sessions').fetchall()
        return [json.loads(row[0]) for row in rows]
    def activate_queue(self, parent, project):
        """Rebind only known unstarted work, preserving the original accepted Turn."""
        with self.state._write() as c:
            session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()[0])
            if (session.get('queue') or {}).get('state')!='accepted':reject('原队列不再待调度。','director_queue_stale',409)
            prior=c.execute('SELECT document FROM director_decisions WHERE event_id=?',(session['event_id'],)).fetchone()
            claim=json.loads(prior[0]) if prior else None
            if claim and claim['state'] in {'started','unknown','returned'}:reject('队列已有待核实模型响应，不能重新绑定。','director_decision_not_replayable',409)
            event_id=digest({'parent':parent,'queue_ready':session['request_scope']['id'],'working_version':project['workspace']['working_version']})
            if event_id!=session['event_id']:
                c.execute('UPDATE director_events SET consumed=1 WHERE event_id=?',(session['event_id'],))
            c.execute('INSERT OR IGNORE INTO director_events VALUES (?,?,?,0)',(event_id,parent,
                encoded({'kind':'queued_request_ready','source':'interactive','state':'prepared','result':session.get('result')})))
            session['queue'].update(state='dispatching',rebinding_required=False)
            if session['queue'].get('reinterpret_at_idle'):
                session['queue']['original_interpretation']=copy.deepcopy(session.pop('request_scope'))
                session['queue']['original_candidate_status']=session.pop('creative_continuation',None)
                session['queue']['interpretation_required']=True
            else:session['request_scope'].update(state='classified',baseline=project.get('current_scene'),working_version=project['workspace']['working_version'])
            session.update(event_id=event_id,decision=None,turn_pending=False,state='queued_request_ready')
            c.execute('UPDATE director_sessions SET document=? WHERE parent_id=?',(encoded(session),parent))
    def save_session(self, value):
        with self.state._write() as c: c.execute('UPDATE director_sessions SET document=? WHERE parent_id=?',
            (encoded(value),value['parent_id']))
    def event(self, event_id):
        with self.state._connection() as c:
            row=c.execute('SELECT * FROM director_events WHERE event_id=?',(event_id,)).fetchone()
        if not row: return None
        return {'event_id':event_id,'parent_id':row['parent_id'],'consumed':bool(row['consumed']),**json.loads(row['document'])}
    def prepare_decision(self, parent, event_id, facts, *, call_index, report_pause_id=None):
        decision_id=digest({'parent_id':parent,'event_id':event_id})
        with self.state._write() as c:
            row=c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()
            if row: return json.loads(row[0])
            value={'decision_id':decision_id,'parent_id':parent,'event_id':event_id,'state':'prepared','call_index':call_index,
                'input':{'task_id':facts['task_id'],'working':{k:copy.deepcopy(facts['working'].get(k))
                    for k in ('selection','working_version','blend_sha256','plan_sha256','images')},
                    'result_id':(facts.get('result') or {}).get('result_id')},'assistant':None,'decision':None}
            if facts.get('capability',{}).get('mode')=='read_only':
                value['input']['readonly_snapshot']={key:copy.deepcopy(facts[key]) for key in ('working','as_of','progress','capability')}
            if report_pause_id is not None:value['input']['report_pause_id']=report_pause_id
            c.execute('INSERT INTO director_decisions VALUES (?,?,?)',(decision_id,event_id,encoded(value)))
        return value
    def decision_for(self, event_id):
        with self.state._connection() as c: row=c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()
        return json.loads(row[0]) if row else None
    def decision_started(self, event_id, attempt_id):
        with self.state._write() as c:
            row=c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()
            if not row: reject('缺少原决策claim。','director_claim_missing',409)
            value=json.loads(row[0])
            if value['state']!='prepared': reject('原决策已经开始或结果未知，不能重发。','director_decision_not_replayable',409)
            value.update(state='started',attempt_id=attempt_id)
            c.execute('UPDATE director_decisions SET document=? WHERE decision_id=?',(encoded(value),value['decision_id']))
    def response_confirmed(self, event_id, assistant, *, private_ref):
        from .director_continuation import public_assistant
        safe=public_assistant(assistant)
        with self.state._write() as c:
            value=json.loads(c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()[0])
            if value['state']!='started': reject('决策响应身份冲突。','director_decision_conflict',409)
            value.update(state='returned',assistant=safe,private_ref=copy.deepcopy(private_ref))
            c.execute('UPDATE director_decisions SET document=? WHERE decision_id=?',(encoded(value),value['decision_id']))
    def batch_call(self, event_id, calls, index=None, *, outcome=None):
        """Original returned decision owns each call cursor; no external work in SQL."""
        with self.state._write() as c:
            value=json.loads(c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()[0])
            if value['state']!='returned':reject('仅已确认原响应可逐调用处理。','director_decision_conflict',409)
            ids=[{'id':q['id'],'digest':digest(q)} for q in calls]
            batch=value.setdefault('batch',{'calls':ids,'cursor':0,'outcomes':[],'started':None})
            if batch['calls']!=ids:reject('原批次调用内容变化。','director_decision_conflict',409)
            if index is not None:
                if index!=batch['cursor']:reject('原批次游标不匹配。','director_decision_conflict',409)
                if outcome is None:batch['started']=index
                else:
                    batch['outcomes'].append(copy.deepcopy(outcome));batch['cursor']+=1;batch['started']=None
            c.execute('UPDATE director_decisions SET document=? WHERE event_id=?',(encoded(value),event_id))
        return copy.deepcopy(batch)
    def apply_batch(self, parent, event_id, exchange, outcomes, private_ref, *, source='autonomous'):
        from .providers import _agent_history
        from .director_continuation import public_assistant
        _agent_history([{'role':'user','content':'original native exchange'}]+exchange,director_arguments=True)
        safe=[public_assistant(exchange[0])]+copy.deepcopy(exchange[1:])
        with self.state._write() as c:
            value=json.loads(c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()[0])
            if value['state']=='applied':return
            if value['state']!='returned' or value['batch']['cursor']!=len(exchange)-1:reject('批次尚未完整配对。','director_decision_conflict',409)
            session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()[0])
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            decision=next((r['decision'] for r in outcomes if r.get('decision')),None)
            child=next((r['child_id'] for r in outcomes if r.get('child_id')),None)
            session.update(exchange=safe,private_ref=copy.deepcopy(private_ref),child_id=child,decision=decision,
                           turn_pending=bool(decision),state='waiting_child' if child else 'reply_confirmed' if decision else 'tools_ready')
            for row in outcomes:
                if row.get('observation'):
                    observed=row['observation'];session['observations']=(session.get('observations',[])+[observed])[-2:]
                    same=task.get('last_observation_identity')==observed['identity']
                    task['consecutive_observation_repeats']=task.get('consecutive_observation_repeats',0)+1 if same else 0
                    task['last_observation_identity']=observed['identity']
                if row.get('metadata'):
                    from .director_goal_plan import metadata_progress
                    pause=metadata_progress(c,task,row['metadata'],value['decision_id'])
                    session['metadata_results']=(session.get('metadata_results',[])+[row['metadata']])[-4:]
                    if pause and not task.get('pause'):
                        task['revision']=task.get('revision',1)+1
                        task['pause']={'pause_id':uuid.uuid4().hex,'reason':pause['reason'],'scope':'autonomous',
                                       'evidence':pause,'version':task['revision']}
            if task.get('consecutive_observation_repeats',0)>=2 and not task.get('pause'):
                task['revision']=task.get('revision',1)+1
                task['pause']={'pause_id':uuid.uuid4().hex,'reason':'repeated_observation_no_new_evidence','scope':'autonomous',
                               'evidence':{},'version':task['revision']}
            if decision:task['decisions']=(task['decisions']+[{'parent_id':parent,**copy.deepcopy(decision)}])[-8:]
            if not child and not decision:
                next_event=digest({'decision_id':value['decision_id'],'batch_complete':True})
                c.execute('INSERT OR IGNORE INTO director_events VALUES (?,?,?,0)',
                          (next_event,parent,encoded({'kind':'tools_ready','source':source,'state':'prepared','result':session.get('result')})))
                session['event_id']=next_event
            value.update(state='applied',decision=decision,child_id=child)
            c.execute('UPDATE director_sessions SET document=? WHERE parent_id=?',(encoded(session),parent))
            c.execute('UPDATE director_decisions SET document=? WHERE event_id=?',(encoded(value),event_id))
            c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
            c.execute('UPDATE director_events SET consumed=1 WHERE event_id=?',(event_id,))
    def apply_decision(self, parent, event_id, exchange, *, decision=None, child_id=None, private_ref, observation=None, metadata=None):
        from .providers import _agent_history
        from .director_continuation import public_assistant
        _agent_history([{'role':'user','content':'original native exchange'}]+exchange)
        safe_exchange=[public_assistant(exchange[0])]+[{k:copy.deepcopy(row[k]) for k in
            ('role','tool_call_id','content') if k in row} for row in exchange[1:]]
        with self.state._write() as c:
            value=json.loads(c.execute('SELECT document FROM director_decisions WHERE event_id=?',(event_id,)).fetchone()[0])
            if value['state']=='applied': return value
            if value['state']!='returned': reject('仅已确认响应可应用。','director_decision_conflict',409)
            session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()[0])
            readonly=session['run'].get('conversation_mode')=='read_only'
            if readonly and (child_id or observation or metadata):
                reject('只读请求不能应用施工/渲染/目标写入。','director_readonly_violation',409)
            session.update(exchange=safe_exchange,private_ref=copy.deepcopy(private_ref),state='waiting_child' if child_id else 'reply_confirmed',
                child_id=child_id,turn_pending=bool(decision),decision=decision)
            value.update(state='applied',decision=decision,child_id=child_id)
            if observation:
                session['observations']=(session.get('observations',[])+[copy.deepcopy(observation)])[-2:]
                next_event=digest({'decision_id':value['decision_id'],'observation':observation['identity']})
                c.execute('INSERT OR IGNORE INTO director_events VALUES (?,?,?,0)',
                    (next_event,parent,encoded({'kind':'observation_ready','source':'autonomous','state':'prepared','result':session.get('result')})))
                session.update(event_id=next_event,state='observation_ready')
                task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
                repeat=task.get('last_observation_identity')==observation['identity']
                task['consecutive_observation_repeats']=task.get('consecutive_observation_repeats',0)+1 if repeat else 0
                task['last_observation_identity']=observation['identity']
                c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
            if decision and not readonly:
                task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
                task['decisions']=(task['decisions']+[{'parent_id':parent,**copy.deepcopy(decision)}])[-8:]
                c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
            if metadata:
                from .director_goal_plan import metadata_progress
                task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
                pause=metadata_progress(c,task,metadata,value['decision_id'])
                next_event=digest({'decision_id':value['decision_id'],'metadata_id':metadata['metadata_id']})
                c.execute('INSERT OR IGNORE INTO director_events VALUES (?,?,?,0)',
                    (next_event,parent,encoded({'kind':'goal_plan_updated','source':'autonomous','state':'prepared','result':session.get('result')})))
                session.update(event_id=next_event,state='metadata_ready',
                    metadata_results=(session.get('metadata_results',[])+[copy.deepcopy(metadata)])[-4:])
                if pause and not task.get('pause'):
                    task['revision']=task.get('revision',1)+1
                    task['pause']={'pause_id':uuid.uuid4().hex,'reason':pause['reason'],'scope':'autonomous',
                        'evidence':pause,'version':task['revision'],
                        'resume_condition':'protected human CAS with changed basis; ordinary questions do not resume'}
                c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
            c.execute('UPDATE director_sessions SET document=? WHERE parent_id=?',(encoded(session),parent))
            c.execute('UPDATE director_decisions SET document=? WHERE event_id=?',(encoded(value),event_id))
            c.execute('UPDATE director_events SET consumed=1 WHERE event_id=?',(event_id,))
        return value
    def readonly_status(self, parent, decision):
        """A Runtime explanation is explicitly separate from a model response."""
        if not self.readonly(parent):reject('缺少SQL只读能力。','director_unbound',409)
        with self.state._write() as c:
            session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()[0])
            if session.get('decision'):return
            session.update(decision=copy.deepcopy(decision),turn_pending=True,state='runtime_status')
            c.execute('UPDATE director_sessions SET document=? WHERE parent_id=?',(encoded(session),parent))
            c.execute('UPDATE director_events SET consumed=1 WHERE event_id=?',(session['event_id'],))
    def attach_result(self, parent, event_id, result):
        session=self.session(parent)
        if session['event_id']==event_id: return session
        session.update(event_id=event_id,result=result,state='result_prepared')
        self.save_session(session)
        return session
    def metadata_receipt(self, parent, decision, call, body):
        op_id=digest({'parent':parent,'decision':decision['decision_id'],'call':call['id']})
        with self.state._connection() as c:
            row=c.execute('SELECT request_digest,document FROM director_metadata WHERE metadata_id=?',(op_id,)).fetchone()
        if not row: return None
        if row['request_digest']!=digest(body): reject('原元数据身份内容冲突。','director_metadata_conflict',409)
        return json.loads(row['document'])
    def remember_goal(self, goal):
        task=self.task(); reference={key:goal[key] for key in ('id','version','content_digest')}
        if task.get('goal_ref') and task['goal_ref']!=reference: reject('持续任务全局Goal已改变。','director_stale',409)
        task['goal_ref']=reference; self._task_write(task)
    def claim_child(self, child):
        with self.state._write() as c:
            row=c.execute('SELECT document FROM director_delegations WHERE child_id=?',(child,)).fetchone()
            value=json.loads(row[0]) if row else None
            if not value or value['state']!='prepared': return False
            value['state']='started'
            c.execute('UPDATE director_delegations SET document=? WHERE child_id=?',(encoded(value),child))
        return True
    def progress(self, record, result):
        with self.state._write() as c:
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            key=digest({'goal':task.get('progress_guard_ref') or record['brief']['goal_ref'],
                        'objects':sorted(record['brief']['object_ids'])})
            progress=task.setdefault('progress',{}).setdefault(key,{'failures':0,'seen':[],'consecutive_failures':0})
            # Old counters stay conservative until a new confirmed real save;
            # already-established pauses are never cleared by progress.
            progress.setdefault('consecutive_failures',progress['failures'])
            if result['result_id'] in progress['seen']: return
            if not c.execute('INSERT OR IGNORE INTO director_progress_results VALUES (?,?)',
                             (key,result['result_id'])).rowcount: return
            progress['seen']=(progress['seen']+[result['result_id']])[-16:]
            changed=any(row.get('changed_fields') and any(
                row.get('before',{}).get(field)!=row.get('after',{}).get(field)
                for field in row['changed_fields']) for row in result['net_changes'])
            initial=False
            commit=result.get('initial_working_commit') or {}
            if commit and result['status']=='completed' and not result.get('error'):
                row=c.execute('SELECT r.receipt FROM receipts r JOIN operations o ON r.operation_id=o.operation_id '
                    "WHERE r.operation_id=? AND r.run_id=? AND o.kind='initial' AND o.state='committed'",
                    (commit.get('operation_id'),record['child_id'])).fetchone()
                manifest=c.execute('SELECT manifest FROM project_state WHERE singleton=1').fetchone()
                if row and manifest:
                    receipt=json.loads(row[0]);project=json.loads(manifest[0]);base=record['brief']['baseline']
                    initial=(base['selection'] is None and base['working_version']==0
                        and receipt['kind']=='initial' and receipt['source_selection'] is None
                        and receipt['target_selection']==result['working']['selection']==project['current_scene']
                        and receipt['working_version']==commit.get('working_version')==result['working']['working_version']==project['workspace']['working_version']
                        and receipt['working_version']>0 and receipt['project_id']==self.state.project_id)
            if result['saved_edit_rounds'] and changed or initial:
                progress.update(last_progress=result['result_id'],consecutive_failures=0)
            else:
                progress['failures']+=1
                progress['consecutive_failures']+=1
            c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
        pause=task.get('pause') or {}
        if progress['consecutive_failures']>=2 and pause.get('reason')!='repeated_no_progress' and pause.get('scope')!='all_models':
            self.pause('repeated_no_progress',{'scope_digest':key,'failures':progress['failures'],
                'consecutive_failures':progress['consecutive_failures'],'result_id':result['result_id']})
    def execution_budget(self):
        task=self.task(); limit=task['policy'].get('total_saved_limit')
        original=(task.get('creative_authorization') or {}).get('approved',{}).get('total_saved_limit')
        if original is not None:limit=min(limit,original) if limit is not None else original
        from .agent_resources import task_limit
        limit=task_limit(self.state,task['task_id'],'saved_rounds',limit)
        with self.state._connection() as c:
            counters=c.execute('SELECT r.saved_edit_rounds,r.needs_reconciliation FROM run_counters r '
                'JOIN director_delegations d ON d.child_id=r.run_id JOIN director_parents p ON p.parent_id=d.parent_id WHERE p.task_id=?',(task['task_id'],)).fetchall()
            if any(r[0] is None or r[1] for r in counters):reject('task正式保存计数待核实。','director_execution_pool_unknown',409)
            used=sum(r[0] for r in counters)
        return used,limit
    def execution_used(self):return self.execution_budget()[0]
    def execution_remaining(self):
        used,limit=self.execution_budget()
        return None if limit is None else max(0,limit-used)
    def execution_admission(self, saved_target, *, requestable=False):
        used,limit=self.execution_budget()
        if limit is None:return
        if used+saved_target>limit:
            if not requestable:self.pause('execution_authorization_exhausted',{'saved_limit':limit,'used_saves':used,'requested_saves':saved_target})
            reject('显式上层总保存授权不足；换父请求/模型不清零。','director_execution_pool_exhausted',409)
    def admission(self, source):
        task=self.task(); pause=task.get('pause')
        if task['policy']['mode']=='strict' and self.cost_source is None:
            self.pause('price_upper_bound_missing',{'missing':'trusted charge upper bound'})
            reject('缺少可信费用上界。','director_economy_paused',409)
        if pause and (source=='autonomous' or pause.get('scope')=='all_models'):
            reject('持续任务暂停；普通提问不解锁自主施工。',
                'director_autonomy_paused' if source=='autonomous' else 'director_economy_paused',409)
        if source=='autonomous' and not task['policy']['autonomous']:
            reject('未授权持续自主调用。','director_autonomy_paused',409)
    def human_action(self, parent, action, body, *, natural_execution=None):
        """CAS on the original task/pause; policy and cost history never reset."""
        request={'parent':parent,'action':action,'body':body}; request_hash=digest(request)
        with self.state._write() as c:
            prior=c.execute('SELECT * FROM director_actions WHERE action_id=?',(body['action_id'],)).fetchone()
            if prior:
                if prior['request_digest']!=request_hash: reject('人类动作身份内容冲突。','director_action_conflict',409)
                return json.loads(prior['document'])
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            pause=task.get('pause')
            if (body['task_id']!=task['task_id'] or body['expected_task_version']!=task.get('revision',1)
                or body['expected_policy_version']!=task['policy_version']
                or body['expected_pause_id']!=(pause or {}).get('pause_id')):
                reject('任务/政策/pause claim版本已变化。','director_action_stale',409)
            if action in {'resume','policy'}:
                if body.get('execution_policy_version')==2:
                    session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()[0])
                    if not natural_execution or digest(session)!=natural_execution['session_digest']:
                        reject('原范围/native状态已变化，不能迁移。','director_action_stale',409)
                    event_id=digest({'parent':parent,'natural_execution_action':body['action_id']})
                    proof={**copy.deepcopy(natural_execution),'action_id':body['action_id'],'event_id':event_id,'source':'protected_human_resume'}
                    session['natural_execution']=proof
                    session['request_scope'].update(state='active',max_children=None)
                    session.update(event_id=event_id,decision=None,turn_pending=False,state='natural_execution_ready')
                    c.execute('INSERT INTO director_events VALUES (?,?,?,0)',(event_id,parent,
                        encoded({'kind':'natural_execution_resumed','source':'interactive','state':'prepared','result':session.get('result')})))
                    c.execute('UPDATE director_sessions SET document=? WHERE parent_id=?',(encoded(session),parent))
                if pause and pause['reason'] in {'unknown_action','ledger_write_unknown','decision_outcome_unknown','child_dispatch_unknown','cost_evidence_conflict','cost_exposure_unknown'}:
                    reject('原未知请求/费用需先核实，不能用resume重发。','director_unknown_requires_reconciliation',409)
                if body['basis']==task.get('last_resume_basis'): reject('相同依据不能反复解锁。','director_resume_basis_unchanged',409)
                if action=='policy':
                    if body['policy'].get('continuous_scope'):
                        session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()[0])
                        scope=session.get('request_scope') or {};reference=body['policy']['continuous_scope']
                        if (scope.get('intent')!='continuous' or scope.get('proposal_id')!=reference['proposal_id']
                            or scope.get('scope_digest')!=reference['scope_digest']
                            or body['policy'].get('total_saved_limit')!=scope['stop_conditions']['total_saved_limit']):
                            reject('持续范围须匹配原提案及绝对保存上限。','director_scope_invalid',409)
                    task['policy']=copy.deepcopy(body['policy']); task['policy_version']+=1
                if task['policy']['mode']=='strict':
                    session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent,)).fetchone()[0])
                    price=ledger.quote(self.cost_source,session['run']['profiles']['brain'],'director',self.mock_provider)
                    if ledger.strict_check(task,ledger.rows(c,task['task_id']),price):
                        reject('可信上界/剩余经济授权仍不足。','director_economy_paused',409)
                task.update(pause=None,last_resume_basis=body['basis'])
                task['revision']=task.get('revision',1)+1
                task['authorization']={'source':'protected_human_'+action,'parent_run_id':parent,'action_id':body['action_id']}
                if action=='policy' and body['policy'].get('continuous_scope'):
                    task['authorization']['continuous']={**copy.deepcopy(scope),'source':'protected_human_policy','action_id':body['action_id']}
                c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
            response={'action_id':body['action_id'],'task_id':task['task_id'],'task_version':task.get('revision',1),
                'policy_version':task['policy_version'],'policy':copy.deepcopy(task['policy']),
                'basis':body['basis'],'source':'protected_human_'+action,'accepted':True,
                'project_id':self.state.project_id,'parent_id':parent,'action':action,'receipt_state':'accepted'}
            if natural_execution:response['execution_policy_version']=2
            c.execute('INSERT INTO director_actions VALUES (?,?,?)',(body['action_id'],request_hash,encoded(response)))
        return response

    def action_receipt(self, parent, action_id, *, project_id, task_id, action, body=None):
        """No execution or projection: verify one original SQL human receipt."""
        binding = self.parent(parent)
        if project_id != self.state.project_id or not binding or binding['task_id'] != task_id:
            reject('原动作项目/父请求/任务身份不一致。', 'director_action_conflict', 409)
        identity = {'action_id':action_id,'project_id':project_id,'parent_id':parent,'task_id':task_id,'action':action}
        with self.state._connection() as c:
            row = c.execute('SELECT * FROM director_actions WHERE action_id=?', (action_id,)).fetchone()
        missing = {**identity,'receipt_state':'not_found','accepted':None,'source':None,
                   'task_version':None,'policy_version':None}
        if not row: return missing
        receipt = json.loads(row['document'])
        if (receipt.get('action_id') != action_id or receipt.get('task_id') != task_id
                or receipt.get('source') != 'protected_human_' + action
                or body is not None and row['request_digest'] != digest({'parent':parent,'action':action,'body':body})):
            reject('原动作身份或内容冲突。', 'director_action_conflict', 409)
        keys = {'project_id','parent_id','action'}
        if any(key in receipt and receipt[key] != identity[key] for key in keys):
            reject('原动作回执绑定冲突。', 'director_action_conflict', 409)
        if not keys <= receipt.keys():
            return {**missing,'receipt_state':'legacy_unverifiable'}
        if (any(receipt.get(key) != value for key,value in identity.items()) or receipt.get('accepted') is not True
                or receipt.get('receipt_state') != 'accepted'
                or any(type(receipt.get(key)) is not int or receipt[key] < 1 for key in ('task_version','policy_version'))):
            reject('原动作回执绑定不可确认。', 'director_action_conflict', 409)
        if body is not None: return copy.deepcopy(receipt)
        return {key:copy.deepcopy(receipt[key]) for key in
                (*identity, 'receipt_state', 'accepted', 'source', 'task_version', 'policy_version')}
