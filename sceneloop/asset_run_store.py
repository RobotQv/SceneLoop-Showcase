"""Asset continuation identities in the project's existing SQLite authority.

Job status belongs to AssetJobStore. This ledger owns only host intent, bindings,
write barriers and once-only local dispatch. No transaction spans external work.
"""
import copy
import json
import sqlite3
import uuid

from .asset_provider import AssetJobError, canonical_json, digest
from .project_state_store import _now


class AssetRunStore:
    def __init__(self, project):
        self.project = project

    def capability(self, run_id):
        with self.project._connection() as c:
            row = c.execute("SELECT document FROM asset_run_capabilities WHERE run_id=?", (run_id,)).fetchone()
        doc = json.loads(row[0]) if row else None
        if doc and (doc.get('run_id') != run_id or doc.get('project_id') != self.project.project_id
                    or doc.get('generation_limit') != 1 or doc.get('upload_limit') != 0 or doc.get('slot') != 1):
            raise AssetJobError('asset_capability_integrity')
        return doc

    def freeze(self, run_id, document, *, initial_permission=None):
        with self.project._write() as c:
            old = c.execute("SELECT document FROM asset_run_capabilities WHERE run_id=?", (run_id,)).fetchone()
            resources = (initial_permission or {}).get('local_resources') or {}
            if resources.get('current_stage_permission'):
                binding = document.get('local_authorization')
                assigned = [json.loads(row[0]) for row in c.execute('SELECT document FROM asset_run_capabilities ORDER BY rowid')]
                prior = next((cap for cap in assigned if cap.get('local_authorization') == binding), None)
                reference = next((r for r in initial_permission.get('reference_snapshots',[]) if r['purpose']=='asset'), None)
                if reference and (not prior or prior['run_id'] == run_id):
                    document = {**document, 'initial_permission': {'generation_count':1,
                        'reference':{k:reference[k] for k in ('artifact_id','sha256')}, 'source':'protected_user_request'}}
            encoded = canonical_json(document)
            if old and old[0] != encoded:
                raise AssetJobError("asset_capability_conflict")
            c.execute("INSERT OR IGNORE INTO asset_run_capabilities VALUES (?,?)", (run_id, encoded))

    @staticmethod
    def check_checkpoint(c, doc):
        """Compare SQL identities under the same write lock as admission/claim."""
        from .refinement_round import criteria_snapshot
        row = c.execute('SELECT project_id,manifest FROM project_state WHERE singleton=1').fetchone()
        project = json.loads(row['manifest']) if row else None
        cp = doc['checkpoint']
        if doc.get('bootstrap'):
            from .bootstrap_asset import check_checkpoint
            return check_checkpoint(c, doc)
        goal = c.execute('SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1').fetchone()
        plan = c.execute('SELECT document FROM refinement_plans WHERE singleton=1').fetchone()
        goal, plan = json.loads(goal[0]) if goal else None, json.loads(plan[0]) if plan else None
        if (not project or row['project_id'] != doc['project_id']
                or project['revision'] != cp['start']['project_revision']
                or project['workspace']['working_version'] != cp['start']['working_version']
                or project['current_scene'] != cp['start']['selection'] or not goal or not plan
                or goal['id'] != cp['goal_id'] or goal['content_digest'] != cp['goal_digest']
                or digest(criteria_snapshot(plan,doc['focus_items'])) != cp['criteria_digest']):
            raise AssetJobError('asset_checkpoint_drift')

    def request(self, run_id=None, *, request_id=None, connection=None):
        def read(c):
            row = c.execute("SELECT * FROM asset_run_requests WHERE " +
                            ("request_id=?" if request_id else "run_id=?"),
                            (request_id or run_id,)).fetchone()
            if row is None:
                if not request_id:return None
                # Current attempt remains one row per Run. Prior immutable
                # requests retain their full facts in an append-only archive.
                for current in c.execute('SELECT document FROM asset_run_requests'):
                    for archived in json.loads(current[0]).get('attempt_history',[]):
                        document=archived['document']
                        if document['id']==request_id:
                            if digest(document)!=archived['digest']:raise AssetJobError('asset_request_sql_integrity')
                            row={'document':canonical_json(document),'request_id':document['id'],'run_id':document['run_id'],
                                'version':document['version'],'job_id':document['job_id'],'grant_id':document['grant_id'],
                                'closed':document['closed'],'slot':document['slot']}
                            break
                    if row:break
                if row is None:return None
            doc = json.loads(row["document"])
            if (doc["id"] != row["request_id"] or doc["run_id"] != row["run_id"]
                    or doc["version"] != row["version"] or doc["job_id"] != row["job_id"]
                    or doc["grant_id"] != row["grant_id"] or doc["closed"] != bool(row["closed"])
                    or doc["project_id"] != self.project.project_id or doc["slot"] != row["slot"]):
                raise AssetJobError("asset_request_sql_integrity")
            if (doc['job_id'] and not doc['grant_id']) or doc['semantic_digest'] != digest(doc['semantic']):
                raise AssetJobError('asset_request_sql_integrity')
            if doc['job_id']:
                job_row = c.execute('SELECT document FROM asset_generation_jobs WHERE project_id=? AND job_id=?',
                                    (doc['project_id'],doc['job_id'])).fetchone()
                if job_row is None:
                    raise AssetJobError('asset_job_binding_conflict')
                job = json.loads(job_row[0])
                if (job['submission_key'] != doc['submission_key']
                        or job['semantic_digest'] != doc['semantic_digest']
                        or job['profile_digest'] != doc['profile_digest']
                        or job['credential_binding'] != doc['credential_binding']
                        or job['generation_grant']['id'] != doc['grant_id']
                        or (job['creation_attempts'] and not
                            (doc.get('local_grant') if job['semantic']['profile']['provider_id'] == 'hunyuan'
                             else doc['price_check']))
                        or (doc['phase'] == 'ready' and (job['preparation'] != 'ready' or not job['raw_artifact']))):
                    raise AssetJobError('asset_job_binding_conflict')
            return doc
        if connection is not None:
            return read(connection)
        with self.project._connection() as c:
            return read(c)

    def barrier(self, *, except_run=None):
        with self.project._connection() as c:
            rows = c.execute("SELECT run_id FROM asset_run_requests WHERE closed=0").fetchall()
        return any(row[0] != except_run for row in rows)

    def _put(self, c, doc, expected_version):
        encoded = canonical_json(doc)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise AssetJobError("asset_request_size_limit")
        doc["version"] = expected_version + 1
        try:
            changed = c.execute("UPDATE asset_run_requests SET version=?,job_id=?,grant_id=?,closed=?,document=?"
                                " WHERE request_id=? AND version=?",
                                (doc["version"], doc["job_id"], doc["grant_id"], int(doc["closed"]),
                                 canonical_json(doc), doc["id"], expected_version)).rowcount
        except sqlite3.IntegrityError:
            raise AssetJobError("asset_request_binding_conflict") from None
        if changed != 1:
            raise AssetJobError("asset_request_version_conflict")
        return copy.deepcopy(doc)

    def update(self, doc, **changes):
        updated = {**copy.deepcopy(doc), **copy.deepcopy(changes)}
        with self.project._write() as c:
            return self._put(c, updated, doc["version"])

    def close(self, run_id, *, phase='ended', reason=None):
        with self.project._write() as c:
            doc = self.request(run_id,connection=c)
            if not doc or doc['closed']:
                return doc
            row = c.execute('SELECT document FROM local_rounds WHERE round_id=? AND run_id=?',
                            (doc['round_id'],run_id)).fetchone()
            rd = json.loads(row[0]) if row else None
            reason = reason or ('formal_saved' if rd and rd['status'] == 'committed' else 'user_end')
            if reason not in {'formal_saved','user_end','cancelled','cost_anomaly','builder_end'}:
                raise AssetJobError('asset_close_reason_invalid')
            if reason == 'formal_saved' and (not rd or rd['status'] != 'committed'):
                raise AssetJobError('asset_close_reason_invalid')
            if rd and rd['status'] in {'active','paused'}:
                body = {'end_id':uuid.uuid5(uuid.UUID(doc['id']),'end-wait').hex,'round_id':rd['id'],
                        'expected_round_version':rd['version'],'reason':reason}
                rd.update(status='ended',version=rd['version']+1,end_reason=body['reason'],
                          end_id=body['end_id'],end_digest=digest(body))
                c.execute("UPDATE local_rounds SET status='ended',version=?,document=? WHERE round_id=?",
                          (rd['version'],canonical_json(rd),rd['id']))
            doc.update(closed=True,phase=phase,close_reason=reason)
            return self._put(c,doc,doc['version'])

    def pause(self, doc, round_document):
        with self.project._write() as c:
            old = self.request(doc["run_id"], connection=c)
            if old:
                if doc.get('previous_attempt'):
                    from .local_asset_failure import approved_retry, retry_identity, confirmed
                    if old['id']==doc['id']:
                        if old['request_digest']!=doc['request_digest']:raise AssetJobError('asset_slot_conflict')
                        return old
                    approval=approved_retry(self.project,old,connection=c)
                    if (not confirmed(self.project,old) or not approval or doc['retry_approval_id']!=approval['id'] or doc['id']!=retry_identity(old,approval)
                            or doc['previous_attempt']!={'id':old['id'],'digest':digest(old)}
                            or doc['semantic_digest']!=old['semantic_digest'] or doc['round_id']!=old['round_id']):
                        raise AssetJobError('asset_retry_intent_conflict')
                    history=copy.deepcopy(old.get('attempt_history',[]))
                    archived=copy.deepcopy(old);archived.pop('attempt_history',None)
                    # This new complete native batch is the known boundary of
                    # the previous continuation, before any new process launch.
                    archived['builder_completion']='completed'
                    history.append({'document':archived,'digest':digest(archived)})
                    doc['attempt_history']=history
                    if len(canonical_json(doc).encode())>2*1024*1024:raise AssetJobError('asset_request_size_limit')
                elif old["request_digest"] != doc["request_digest"]:
                    raise AssetJobError("asset_slot_conflict")
                else:return old
            if self.capability_in(c, doc["run_id"]) is None:
                raise AssetJobError("asset_host_opt_in_required")
            self.check_checkpoint(c,doc)
            def save():
                if old:
                    # One SQL row is the current attempt; the prior immutable
                    # request is archived in that same atomic replacement.
                    changed=c.execute('UPDATE asset_run_requests SET request_id=?,version=1,job_id=NULL,grant_id=NULL,closed=0,document=? WHERE request_id=? AND version=?',
                        (doc['id'],canonical_json(doc),old['id'],old['version'])).rowcount
                    if changed!=1:raise AssetJobError('asset_request_version_conflict')
                else:
                    c.execute('INSERT INTO asset_run_requests VALUES (?,?,1,1,NULL,NULL,0,?)',
                        (doc['id'],doc['run_id'],canonical_json(doc)))
            if doc.get('bootstrap'):
                save()
                return copy.deepcopy(doc)
            current = c.execute("SELECT version,status FROM local_rounds WHERE round_id=? AND run_id=?",
                                (round_document["id"], doc["run_id"])).fetchone()
            if not current or current[0] != round_document["version"] or current[1] != "active":
                raise AssetJobError("asset_round_conflict")
            paused = copy.deepcopy(round_document)
            paused.update(status="paused", version=paused["version"] + 1)
            paused["checkpoint"] = {"type": "asset_wait", "request_id": doc["id"],
                                    "revision_id": paused["selected_revision"],
                                    "artifacts": doc["checkpoint"]["artifacts"], "created_at": _now()}
            c.execute("UPDATE local_rounds SET status='paused',version=?,document=? WHERE round_id=?",
                      (paused["version"], canonical_json(paused), paused["id"]))
            doc["checkpoint"]["round_version"] = paused["version"]
            try:
                save()
            except sqlite3.IntegrityError:
                raise AssetJobError("asset_slot_conflict") from None
            return copy.deepcopy(doc)

    @staticmethod
    def capability_in(c, run_id):
        row = c.execute("SELECT document FROM asset_run_capabilities WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def bind(self, doc, job):
        if (job["project_id"] != doc["project_id"] or job["submission_key"] != doc["submission_key"]
                or job["semantic_digest"] != doc["semantic_digest"]
                or job["profile_digest"] != doc["profile_digest"]
                or job["credential_binding"] != doc["credential_binding"]
                or job["generation_grant"]["id"] != doc["grant_id"]):
            raise AssetJobError("asset_job_binding_conflict")
        with self.project._write() as c:
            current = self.request(doc["run_id"], connection=c)
            if current["job_id"]:
                if current["job_id"] != job["job_id"]:
                    raise AssetJobError("asset_job_binding_conflict")
                return current
            if current["version"] != doc["version"] or current["closed"] or not current["grant_id"]:
                raise AssetJobError("asset_request_binding_conflict")
            current.update(job_id=job["job_id"], phase="generation")
            return self._put(c, current, doc["version"])

    def resume(self, doc, resume_id):
        request_digest = digest({"request_id": doc["id"], "checkpoint": doc["checkpoint"]})
        with self.project._write() as c:
            self.check_checkpoint(c,doc)
            old = c.execute("SELECT * FROM asset_run_resumes WHERE request_id=?", (doc["id"],)).fetchone()
            if old:
                if old["resume_id"] != resume_id or old["request_digest"] != request_digest:
                    raise AssetJobError("asset_resume_identity_conflict")
                return old["state"]
            current = self.request(doc["run_id"], connection=c)
            if current["closed"] or current["phase"] not in {"ready","failure_ready"}:
                raise AssetJobError("asset_resume_not_ready")
            if current['phase']=='failure_ready':
                from .local_asset_failure import confirmed
                if not confirmed(self.project,current):raise AssetJobError('asset_local_failure_unconfirmed')
            try:
                c.execute("INSERT INTO asset_run_resumes VALUES (?,?,?,'accepted')",
                          (doc["id"], resume_id, request_digest))
            except sqlite3.IntegrityError:
                raise AssetJobError("asset_resume_identity_conflict") from None
            return "accepted"

    def start_resume(self, doc, resume_id):
        with self.project._write() as c:
            current = self.request(doc["run_id"], connection=c)
            if current["closed"]:
                return False
            self.check_checkpoint(c,current)
            if current.get('failure_feedback'):
                from .local_asset_failure import confirmed
                if not confirmed(self.project,current):raise AssetJobError('asset_local_failure_unconfirmed')
            changed = c.execute("UPDATE asset_run_resumes SET state='started'"
                                " WHERE request_id=? AND resume_id=? AND state='accepted'",
                                (doc["id"], resume_id)).rowcount
            if changed:
                if doc.get('bootstrap'): return True
                row = c.execute("SELECT document FROM local_rounds WHERE round_id=?", (doc["round_id"],)).fetchone()
                rd = json.loads(row[0])
                if rd["status"] != "paused" or rd["version"] != doc["checkpoint"]["round_version"]:
                    raise AssetJobError("asset_round_conflict")
                rd.update(status="active", version=rd["version"] + 1)
                c.execute("UPDATE local_rounds SET status='active',version=?,document=? WHERE round_id=?",
                          (rd["version"], canonical_json(rd), rd["id"]))
            return changed == 1

    def continuation(self, request_id):
        with self.project._connection() as c:
            row = c.execute('SELECT resume_id,state FROM asset_run_resumes WHERE request_id=?',(request_id,)).fetchone()
            doc = self.request(request_id=request_id,connection=c)
        return {**dict(row),'state':doc.get('builder_completion',row['state'])} if row else None

    def finish_resume(self, request_id, *, unknown=False):
        with self.project._write() as c:
            doc = self.request(request_id=request_id,connection=c)
            current=self.request(doc['run_id'],connection=c)
            if current['id']!=request_id:
                if unknown:raise AssetJobError('asset_historical_continuation_unknown')
                return  # Already finalized at the new complete pause boundary.
            doc['builder_completion'] = 'unknown' if unknown else 'completed'
            self._put(c,doc,doc['version'])

    def admit_import(self, doc, step, identity, limits):
        with self.project._write() as c:
            current = self.request(doc["run_id"], connection=c)
            self.check_checkpoint(c,current)
            if current["import_intent"]:
                if current["import_intent"] != identity:
                    raise AssetJobError("asset_import_identity_conflict")
                return current, False
            if current["closed"] or current["job_id"] != doc["job_id"] or current["phase"] not in {"ready","builder_continuing"}:
                raise AssetJobError("asset_import_not_admissible")
            dispatch = c.execute("SELECT state FROM asset_run_resumes WHERE request_id=?", (doc["id"],)).fetchone()
            if not dispatch or dispatch[0] != "started":
                raise AssetJobError("asset_import_not_admissible")
            if doc.get('bootstrap'):
                from .bootstrap_asset import admit_import
                return admit_import(self,c,current,step,identity,limits)
            row = c.execute("SELECT document FROM local_rounds WHERE round_id=? AND run_id=?",
                            (doc["round_id"], doc["run_id"])).fetchone()
            rd = json.loads(row[0]) if row else None
            if (not rd or rd["status"] != "active" or rd["selected_revision"] != identity["base_revision"]
                    or rd["pending_revision"] or any(s["status"] in {"scheduled", "unknown"} for s in rd["steps"])):
                raise AssetJobError("asset_round_conflict")
            counts = c.execute("SELECT o.identity,COUNT(*) AS n FROM operations o JOIN edit_attempts a"
                               " ON o.operation_id=a.operation_id WHERE o.run_id=?"
                               " AND o.kind IN ('draft_edit','asset_import') GROUP BY o.operation_id",
                               (doc["run_id"],)).fetchall()
            if (sum(r["n"] for r in counts) >= limits["max_edit_attempts"] or
                    sum(r["n"] for r in counts if json.loads(r["identity"]).get("round_id") == doc["round_id"])
                    >= limits.get("max_edit_attempts_per_round", limits["max_edit_attempts"])):
                raise AssetJobError("round_resource_exhausted")
            c.execute("INSERT INTO operations VALUES (?,?,?,?,?,'prepared',?,?,0)",
                      (step["operation_id"], doc["project_id"], doc["run_id"], "asset_import",
                       canonical_json(identity), _now(), _now()))
            c.execute("INSERT INTO edit_attempts(attempt_id,operation_id,retry_index,status,created_at)"
                      " VALUES (?,?,0,'scheduled',?)", (step["attempt_id"], step["operation_id"], _now()))
            rd["steps"].append(step)
            rd["version"] += 1
            c.execute("UPDATE local_rounds SET version=?,document=? WHERE round_id=?",
                      (rd["version"], canonical_json(rd), rd["id"]))
            current.update(import_intent=identity, phase="importing")
            return self._put(c, current, current["version"]), True

    def freeze_import(self, doc, agent_state, pose):
        with self.project._write() as c:
            current = self.request(doc['run_id'],connection=c)
            if current['import_intent']:
                if current['pose'] != pose: raise AssetJobError('asset_import_identity_conflict')
                return current
            self.check_checkpoint(c,current)
            if doc.get('bootstrap'):
                from .bootstrap_asset import freeze_import
                return freeze_import(self,c,current,agent_state,pose)
            rd = json.loads(c.execute('SELECT document FROM local_rounds WHERE round_id=?',
                                      (doc['round_id'],)).fetchone()[0])
            if (not isinstance(agent_state,dict) or rd['status'] != 'active'
                    or rd['selected_revision'] != agent_state.get('selected_revision')
                    or rd['pending_revision'] or agent_state.get('pending_revision')
                    or current['closed'] or current['phase'] != 'builder_continuing'):
                raise AssetJobError('asset_import_not_admissible')
            if 'import_agent_state' in current and (current['pose'] != pose or current['import_agent_state'] != agent_state):
                raise AssetJobError('asset_import_identity_conflict')
            current.update(pose=copy.deepcopy(pose),import_agent_state=copy.deepcopy(agent_state))
            return self._put(c,current,current['version'])

    def finalize_import(self, doc, artifacts, agent_state, receipt):
        with self.project._write() as c:
            current = self.request(doc["run_id"], connection=c)
            historical = current['closed']
            if not historical:
                self.check_checkpoint(c,current)
            if current.get("import_receipt"):
                if current["import_receipt"] != receipt:
                    raise AssetJobError("asset_import_receipt_conflict")
                return current
            identity = current["import_intent"]
            if identity != doc['import_intent'] or receipt['identity'] != identity:
                raise AssetJobError('asset_import_receipt_conflict')
            if doc.get('bootstrap'):
                from .bootstrap_asset import finalize_import
                return finalize_import(self,c,current,receipt)
            rd = json.loads(c.execute("SELECT document FROM local_rounds WHERE round_id=?",
                                      (doc["round_id"],)).fetchone()[0])
            step = next(s for s in rd["steps"] if s["attempt_id"] == identity["attempt_id"])
            if (step["status"] not in {"scheduled", "unknown"}
                    or rd["status"] != ('ended' if historical else 'active')):
                raise AssetJobError("asset_import_receipt_conflict")
            step.update(status='succeeded' if historical else 'pending', artifacts=copy.deepcopy(artifacts))
            rd['version'] += 1
            if not historical:
                rd.update(pending_revision=step["revision_id"], agent_state=copy.deepcopy(agent_state))
            c.execute("UPDATE edit_attempts SET status='succeeded',retryable=0,failure_category=NULL,detail=NULL"
                      " WHERE attempt_id=?", (identity["attempt_id"],))
            c.execute("UPDATE operations SET state='prepared',updated_at=? WHERE operation_id=?",
                      (_now(), identity["operation_id"]))
            c.execute("UPDATE local_rounds SET version=?,document=? WHERE round_id=?",
                      (rd["version"], canonical_json(rd), rd["id"]))
            current.update(import_receipt=receipt,import_history=historical)
            if not historical:
                current['phase'] = 'pending_review'
            return self._put(c, current, current["version"])

    def fail_import(self, run_id, *, confirmed_rejected=False):
        with self.project._write() as c:
            doc = self.request(run_id, connection=c)
            if not doc or not doc['import_intent'] or doc['import_receipt']:
                return
            intent = doc['import_intent']
            if doc.get('bootstrap'):
                from .bootstrap_asset import fail_import
                return fail_import(self,c,doc,confirmed_rejected)
            rd = json.loads(c.execute('SELECT document FROM local_rounds WHERE round_id=?',
                                      (doc['round_id'],)).fetchone()[0])
            step = next(s for s in rd['steps'] if s['id'] == intent['attempt_id'])
            outcome = 'failed' if confirmed_rejected else 'unknown'
            step['status'] = outcome
            rd['version'] += 1
            c.execute('UPDATE local_rounds SET version=?,document=? WHERE round_id=?',
                      (rd['version'],canonical_json(rd),rd['id']))
            c.execute("UPDATE edit_attempts SET status=?,retryable=0,failure_category=? WHERE attempt_id=?",
                      (outcome,'confirmed_rejected' if confirmed_rejected else 'write_unknown',intent['attempt_id']))
            c.execute("UPDATE operations SET state=?,updated_at=? WHERE operation_id=?",
                      ('prepared' if confirmed_rejected else 'unknown',_now(),intent['operation_id']))
            if not doc['closed']:
                doc['phase'] = 'import_failed' if confirmed_rejected else 'import_unknown'
            self._put(c,doc,doc['version'])
