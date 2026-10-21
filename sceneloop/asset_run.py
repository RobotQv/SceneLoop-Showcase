"""Trusted host bridge for one text shape in one opt-in local Run.

Public methods expose sanitized SQL projections. Approval, pricing, transport
and provider factories are trusted constructor dependencies, never Run payloads.
"""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import time
import uuid

from .asset_provider import AssetJobError, digest, identifier
from .asset_jobs import AssetJobCoordinator, controlled_path
from .asset_run_store import AssetRunStore
from .asset_price import (PriceApproval, check_price, request_policy, public_approval,
                          public_price, actual_usage_estimate)
from .refinement_budget import budget_progress, edit_limit, resolve_snapshot
from .refinement_round import criteria_snapshot, enabled


class AssetRunHost:
    def __init__(self, profiles, profile_id, provider_factory, approvals, prices, balance_transport,
                 *, clock=time.time, reference_resolver=None):
        self.profiles, self.profile_id, self.provider_factory = profiles, profile_id, provider_factory
        self.approvals, self.prices, self.transport, self.clock = approvals, prices, balance_transport, clock
        self.reference_resolver = reference_resolver

    def store(self, manager, run_id):
        run = manager.runs[run_id]
        return AssetRunStore(manager._projects().state_store(run['project_id']))

    def coordinator(self, manager, run_id):
        state = self.store(manager, run_id).project
        provider = self.provider_factory(state)
        cap = self.store(manager, run_id).capability(run_id)
        if cap and cap["profile"]["provider_id"] == "hunyuan":
            from .asset_local_authorization import LocalAssetAuthority
            binding = cap.get("local_authorization")
            if binding:
                # Revocation blocks admission, not collection/cancellation of
                # an already admitted original process.
                limits = LocalAssetAuthority(state).read(**binding, allow_stopped=True)
                local = getattr(provider, "local", provider)
                local.timeout = limits["job_timeout_seconds"]
        return AssetJobCoordinator(state, self.profiles, provider, self.approvals, receipts=provider if hasattr(provider, "resolve") else None,
            clock=self.clock, before_dispatch=lambda job, profile, key, wire:
            self._price_gate(manager, run_id, job, profile, key, wire))

    def local_coordinator(self, manager, run_id):
        # History/read paths need no provider factory, grant source or credential.
        return AssetJobCoordinator(self.store(manager,run_id).project,None,None,None,clock=self.clock)

    def freeze(self, manager, run):
        if (not (run.get('bootstrap') or enabled(run) and run['operation'] == 'edit') or run['execution']['mode'] != 'headless'
                or run.get('scene_kind','parametric') != 'parametric'):
            raise AssetJobError('asset_run_mode_unsupported')
        profile_id = (run.get('asset_generation') or {}).get('profile_id',self.profile_id)
        profile = next((p for p in self.profiles.list() if p['id'] == profile_id), None)
        local = bool(profile and profile['provider_id'] == 'hunyuan' and profile['mode'] == 'local_process')
        if run.get('bootstrap') and not local: raise AssetJobError('asset_bootstrap_local_only')
        if not profile or not local and (profile['provider_id'] != 'tripo' or profile['model'] != 'v3.1-20260211'):
            raise AssetJobError('asset_configuration_conflict')
        jobs = self.coordinator(manager, run['id'])
        if local:
            _, _, binding = jobs._profile(profile)
            jobs.provider.capabilities(profile).snapshot()
            inspected = {"credential_binding": binding}
        else:
            inspected, _ = jobs.inspect_request({'profile':profile,'input':{'kind':'text','prompt':'host preflight'},
                                                 'parameters':{}})
        self.store(manager, run['id']).freeze(run['id'], {
            'schema_version':1, 'project_id':run['project_id'], 'run_id':run['id'],
            'profile':copy.deepcopy(profile), 'credential_binding':inspected['credential_binding'],
            'upload_limit':0, 'generation_limit':1, 'slot':1,
            **({"local_authorization":copy.deepcopy(run.get("local_asset_authorization")), 'permission_policy':1,
                'bootstrap':bool(run.get('bootstrap'))} if local else {})},
            initial_permission=(run.get('director_delegation',{}).get('brief',{}).get('creation_authorization',{}).get('approved') if local else None))

    @staticmethod
    def approval_binding(doc):
        return {k:copy.deepcopy(doc[k]) for k in ('project_id','run_id','id','request_revision',
            'semantic_digest','profile_digest','credential_binding','wire_digest','model','endpoint')}

    def _profile(self, manager, run_id):
        cap = self.store(manager, run_id).capability(run_id)
        if not cap:
            raise AssetJobError('asset_host_opt_in_required')
        current = next((p for p in self.profiles.list() if p['id'] == cap['profile']['id']), None)
        if current != cap['profile']:
            raise AssetJobError('asset_configuration_conflict')
        return cap

    def request(self, manager, run_id, body, *, agent_state, exchange):
        if not isinstance(body, dict) or set(body) - {'input','parameters','pose','reason','stage_permission','continuation_checkpoint'} or not {'input','parameters'} <= set(body):
            raise AssetJobError('invalid_asset_request')
        cap = self._profile(manager, run_id)
        local = cap["profile"]["provider_id"] == "hunyuan"
        reference = None
        if local:
            source = body["input"]
            if (not isinstance(source, dict) or set(source) != {"kind","reference_id"}
                    or source["kind"] != "image" or self.reference_resolver is None):
                raise AssetJobError("asset_trusted_reference_required")
            reference = self.reference_resolver(manager, run_id, source["reference_id"])
            from .asset_jobs import file_facts, image_facts, MAX_IMAGE_BYTES
            raw, facts = file_facts(controlled_path(self.store(manager,run_id).project.project_root,reference["path"]),MAX_IMAGE_BYTES)
            if (reference["project_id"] != manager.runs[run_id]["project_id"]
                    or reference["reference_id"] != source["reference_id"] or reference["purpose"] != "asset"
                    or any(reference[k] != facts[k] for k in facts)):
                raise AssetJobError("asset_reference_identity_conflict")
            image_facts(raw)
            if cap.get('permission_policy'):
                from .local_asset_permission import authorized
                if not isinstance(body.get('stage_permission'),dict): raise AssetJobError('asset_stage_permission_required')
                authorized(manager, manager.get(run_id), body['stage_permission'])
            source = {"kind":"image","path":reference["path"]}
        else:
            if not isinstance(body['input'], dict) or body['input'].get('kind') != 'text':
                raise AssetJobError('asset_text_shape_only')
            source = body["input"]
        if not isinstance(exchange, dict) or exchange.get('settled') is not True:
            raise AssetJobError('asset_tool_exchange_unsettled')
        native = None
        if exchange.get('mode') == 'builder_v1':
            from .asset_builder import exchange as fixed_exchange
            native = fixed_exchange(exchange.get('native_exchange',[]))
        from .asset_contract import transform
        pose = transform(body.get('pose'))
        store = self.store(manager, run_id)
        request = {'profile':cap['profile'], 'input':source, 'parameters':body['parameters']}
        inspected, _ = self.coordinator(manager, run_id).inspect_request(request)
        if inspected['credential_binding'] != cap['credential_binding']:
            raise AssetJobError('asset_credential_binding_conflict')
        wire = inspected['semantic']['wire_template']
        request_digest = digest({'semantic_digest':inspected['semantic_digest'], 'pose':pose,
                                **({'reason':body['reason']} if native else {})})
        old = store.request(run_id)
        retry=bool(local and old and old.get('failure_feedback'))
        if old and not retry:
            if (old['request_digest'] != request_digest
                    or local and old.get('stage_permission') != body.get('stage_permission')):
                raise AssetJobError('asset_slot_conflict')
            return self.public(manager, run_id)
        with manager.lock:
            run = manager._get(run_id)
            import threading
            worker = manager.workers.get(run_id)
            if worker and worker.is_alive() and worker is not threading.current_thread():
                raise AssetJobError('asset_worker_not_at_exchange_boundary')
            if run.get('bootstrap'):
                from .bootstrap_asset import request_document
                doc = request_document(self, manager, run, body, agent_state, exchange, inspected, request, request_digest, pose, reference, native)
                if retry:
                    from .local_asset_failure import next_document
                    doc=next_document(store.project,old,doc)
                store.pause(doc, None)
                self._after_step('request_paused', doc)
                manager._save(run)
                self._authorize_local(manager,run_id)
                return self.public(manager,run_id)
            _, rd = manager._local_round_state(run)
            if rd['pending_revision'] or any(s['status'] in {'scheduled','unknown','needs_user_review'} for s in rd['steps']):
                raise AssetJobError('asset_pause_not_safe')
            if (agent_state.get('selected_revision') != rd['selected_revision']
                    or agent_state.get('pending_revision') is not None):
                raise AssetJobError('asset_agent_state_conflict')
            artifacts = manager._fixed_scene_artifacts(run, rd['selected_revision'])
            selection = rd['start']['selection']
            working_artifacts = manager._fixed_scene_artifacts(manager.get(selection['run_id']),selection['revision_id'])
            request_id = uuid.uuid5(uuid.UUID(run['project_id']), f"{run_id}:asset-slot:1").hex
            doc = {'schema_version':1,'project_id':run['project_id'],'run_id':run_id,'round_id':rd['id'],
                'slot':1,'id':request_id,'version':1,'request_revision':1,
                'submission_key':uuid.uuid5(uuid.UUID(request_id),'submission').hex,
                'request_digest':request_digest, 'request':copy.deepcopy(request), **inspected,
                'wire_digest':digest(wire),'model':cap['profile']['model'],'endpoint':cap['profile']['base_url'],
                'pose':pose,'local_goal':rd['local_goal'],'focus_items':list(rd['item_ids']),
                'job_id':None,'grant_id':None,'price_approval':None,'price_check':None,
                'cost_policy':'actual_usage', **({"reference":reference,"local_authorization":cap.get("local_authorization"),
                    'stage_permission':copy.deepcopy(body.get('stage_permission'))} if local else {}),
                'phase':'approval','closed':False,'import_intent':None,'import_receipt':None,
                'checkpoint':{'round_id':rd['id'],'round_version':rd['version'],
                    'start':copy.deepcopy(rd['start']),'selected_revision':rd['selected_revision'],
                    'pending_revision':rd['pending_revision'],'goal_id':rd['goal_id'],'goal_digest':rd['goal_digest'],
                    'criteria_digest':rd['criteria_digest'],'artifacts':artifacts,
                    'working_artifacts':working_artifacts,'start_artifacts':copy.deepcopy(rd['start_artifacts']),
                    'agent_state':copy.deepcopy(agent_state),'usage':copy.deepcopy(run['usage']),
                    'limits':resolve_snapshot(run),'exchange_digest':digest(exchange)}}
            rd['agent_state'] = copy.deepcopy(agent_state)
            if native:
                doc.update(builder_mode=True,reason=body['reason'])
                doc['checkpoint'].update(native_exchange=native,native_exchange_digest=digest(native))
            if retry:
                from .local_asset_failure import next_document
                doc=next_document(store.project,old,doc)
            store.pause(doc, rd)
            self._after_step('request_paused', doc)
            manager._save(run)
        if local:
            self._authorize_local(manager,run_id)
        return self.public(manager, run_id)

    def _authorize_local(self, manager, run_id):
        from dataclasses import asdict
        from .asset_local_authorization import LocalAssetAuthority
        doc = self.store(manager,run_id).request(run_id)
        if doc["closed"] or doc["grant_id"]:
            return
        binding = doc.get("local_authorization")
        if not binding:
            return  # Explicit waiting, never derive human authority from tool input.
        grant = LocalAssetAuthority(self.store(manager,run_id).project).claim(binding,doc["id"],doc)
        self.store(manager,run_id).update(doc,grant_id=grant.id,local_grant=asdict(grant))

    def verify(self, manager, run_id, doc=None):
        store = self.store(manager, run_id)
        doc = doc or store.request(run_id)
        if not doc or doc['closed']:
            raise AssetJobError('asset_wait_ended')
        if doc.get('bootstrap'):
            from .bootstrap_asset import verify
            return verify(self, manager, run_id, doc)
        run = manager.get(run_id)
        manager._assert_refinement_goal(run)
        rd = store.project.local_round(run_id, round_id=doc['round_id'])
        cp = doc['checkpoint']
        project = manager._projects().get(doc['project_id'], verify_artifacts=False)
        state = store.project.refinement_state(run_id)
        if (project['revision'] != cp['start']['project_revision']
                or project['workspace']['working_version'] != cp['start']['working_version']
                or project['current_scene'] != cp['start']['selection'] or not rd
                or rd['goal_digest'] != cp['goal_digest'] or rd['criteria_digest'] != cp['criteria_digest']
                or digest(criteria_snapshot(state['plan'], rd['item_ids'])) != cp['criteria_digest']
                or rd['start'] != cp['start'] or resolve_snapshot(run) != cp['limits']
                or any(run['usage'].get(k, 0) < v for k,v in cp['usage'].items()
                       if type(v) in (int,float))):
            raise AssetJobError('asset_checkpoint_drift')
        if doc['phase'] in {'approval','generation','ready','generation_blocked','failure_ready'} and (
                rd['selected_revision'] != cp['selected_revision'] or rd['pending_revision'] is not None
                or rd['agent_state'] != cp['agent_state']):
            raise AssetJobError('asset_checkpoint_drift')
        actual = manager._fixed_scene_artifacts(run, cp['selected_revision'])
        if (actual != cp['artifacts'] or store.project.verify_artifacts(cp['working_artifacts'])
                or store.project.verify_artifacts(cp['start_artifacts'])):
            raise AssetJobError('asset_checkpoint_artifact_drift')
        return doc

    def approve(self, manager, run_id, reference):
        identifier(reference)
        doc = self.verify(manager, run_id)
        self._profile(manager, run_id)
        jobs = self.coordinator(manager, run_id)
        grant = jobs._grant(reference, doc, 'generation')
        if doc["request"]["profile"]["provider_id"] == "hunyuan":
            if not doc.get("local_grant") or doc["local_grant"]["id"] != reference:
                raise AssetJobError("asset_local_authorization_required")
            return self.public(manager,run_id)
        try:
            price = self.approvals.resolve_price(reference)
        except Exception:
            raise AssetJobError('asset_trusted_price_approval_required') from None
        if not isinstance(price, PriceApproval) or price.grant_id != grant['id'] or price.human_origin != grant['origin']:
            raise AssetJobError('asset_price_approval_conflict')
        price.validate(self.approval_binding(doc), self.clock())
        if doc['grant_id']:
            if doc['grant_id'] != reference or doc['price_approval'] != asdict(price):
                raise AssetJobError('asset_request_binding_conflict')
        else:
            self.store(manager, run_id).update(doc, grant_id=reference, price_approval=asdict(price))
        return self.public(manager, run_id)

    def _price_gate(self, manager, run_id, job, profile, key, wire):
        doc = self.verify(manager, run_id)
        self._profile(manager, run_id)
        if profile["provider_id"] == "hunyuan":
            from .asset_local_authorization import LocalAssetAuthority
            auth = LocalAssetAuthority(self.store(manager,run_id).project)
            binding = doc["local_authorization"]
            auth.read(**binding)
            if doc.get('resource_admission'):
                from .local_resource_warning import guard, _permission
                guard(manager, run_id)
                store = self.store(manager, run_id)
                with store.project._connection() as connection:
                    _permission(store, connection, doc, job)
            elif self._profile(manager,run_id).get('permission_policy'):
                from .local_asset_permission import authorized
                authorized(manager,manager.get(run_id),doc.get('stage_permission') or {})
            if (doc["job_id"] != job["job_id"] or doc["wire_digest"] != digest(wire)
                    or auth.resolve(doc["grant_id"]).snapshot() != job["generation_grant"]):
                raise AssetJobError("asset_local_authorization_conflict")
            return {"request_id":doc["id"], "request_version":doc["version"],
                    "job_version":job["version"], "local_authorization":binding,
                    "grant_digest":digest(doc["local_grant"])}
        if (doc['job_id'] != job['job_id'] or doc['wire_digest'] != digest(wire)
                or doc['credential_binding'] != job['credential_binding'] or job['creation_attempts']):
            raise AssetJobError('asset_price_wire_conflict')
        try:
            price = self.approvals.resolve_price(doc['grant_id'])
        except Exception:
            raise AssetJobError('asset_trusted_price_approval_required') from None
        if not isinstance(price, PriceApproval) or asdict(price) != doc['price_approval']:
            raise AssetJobError('asset_price_approval_conflict')
        receipt = check_price(self.prices, price, self.approval_binding(doc), wire,
                              profile['base_url'], key, self.transport, now=self.clock())
        current = self.verify(manager, run_id)
        if current['version'] != doc['version']:
            raise AssetJobError('asset_request_version_conflict')
        self._profile(manager,run_id)
        jobs = self.coordinator(manager,run_id)
        jobs._action_facts(job)
        jobs._grant(doc['grant_id'],job,'generation',frozen=job['generation_grant'])
        fresh = self.approvals.resolve_price(doc['grant_id'])
        if not isinstance(fresh,PriceApproval) or asdict(fresh) != doc['price_approval']:
            raise AssetJobError('asset_price_approval_conflict')
        fresh.validate(self.approval_binding(doc),self.clock())
        if price.policy == 'catalog_estimate':
            price_record = self.prices.lookup(wire,profile['base_url'],self.clock())
            if (price_record.version != receipt['catalog_version']
                    or price_record.credits != receipt['catalog_credits']
                    or price_record.expires_at != receipt['catalog_expires_at']):
                raise AssetJobError('asset_price_catalog_changed')
        checked = self.store(manager, run_id).update(current, price_check=receipt)
        self._after_step('price_checked', current)
        return {'request_id':checked['id'],'request_version':checked['version'],
                'job_version':job['version'],'price_digest':digest(receipt)}

    def advance(self, manager, run_id):
        doc = self.store(manager, run_id).request(run_id)
        if doc is None:
            raise AssetJobError('asset_request_missing')
        jobs = self.coordinator(manager, run_id)
        # Ended waits may collect an already dispatched task's result, but never
        # create/import/publish. No new authority is obtained from saved JSON.
        if not doc['closed']:
            self.verify(manager, run_id, doc)
        if not doc['job_id']:
            if doc['closed'] or not doc['grant_id']:
                return self.public(manager, run_id)
            self.approve(manager, run_id, doc['grant_id'])
            old = jobs.store.by_key(doc['submission_key'])
            if old is None:
                jobs.prepare(doc['submission_key'], doc['request'], generation_approval=doc['grant_id'])
                self._after_step('job_prepared', doc)
                old = jobs.store.by_key(doc['submission_key'])
            self.store(manager, run_id).bind(doc, old)
            self._after_step('job_bound', doc)
        else:
            job = jobs.store.read(doc['job_id'])
            if job['dispatch'] == 'submitting':
                jobs.recover(job['job_id'])
                job = jobs.store.read(doc["job_id"])
            local = job["semantic"]["profile"]["provider_id"] == "hunyuan"
            if local and job["dispatch"] == "outcome_unknown":
                # A verifiable original local launch can reconcile, never respawn.
                jobs.reconcile_create(job["job_id"], "local:"+job["job_id"])
                job = jobs.store.read(doc["job_id"])
            if job['dispatch'] == 'prepared' and not job['creation_attempts'] and not doc['closed']:
                if local:
                    provider = getattr(jobs.provider,"local",jobs.provider)
                    from .local_resource_warning import preflight
                    if not preflight(self, manager, run_id, job, provider):
                        return self.public(manager,run_id)
                jobs.resume(job['job_id'])
                jobs.submit(job['job_id'])
            elif job['dispatch'] == 'accepted' and job['task_id'] and job['remote'] not in {'succeeded','failed','cancelled'}:
                # stop() sets local=stopped. A trusted explicit read tick uses
                # query's read-only stopped-job path, without supplier cancel.
                (jobs.query_recorded if doc['closed'] or local and job['local']!='active' else jobs.query)(job['job_id'])
            elif job['remote'] == 'succeeded' and job['preparation'] != 'ready' and not doc['closed']:
                (jobs.prepare_recorded_artifact if local and job['local']!='active' else jobs.prepare_artifact)(job['job_id'])
            current = self.store(manager, run_id).request(run_id)
            job = jobs.store.read(doc['job_id'])
            if (job['credits'] is not None and current['price_approval']
                    and current['price_approval']['policy'] == 'catalog_estimate'
                    and job['credits'] > current['price_approval']['max_credits']):
                current = manager._close_asset_wait(run_id,phase='cost_anomaly',reason='cost_anomaly')
            elif job['preparation'] == 'ready' and not current['closed'] and current['phase'] == 'generation':
                self.store(manager, run_id).update(current, phase='ready')
        self.sync(manager, run_id)
        return self.public(manager, run_id)

    def reconcile_local_create(self, manager, run_id):
        """Only verify the original local receipt, without resume or dispatch."""
        doc=self.store(manager,run_id).request(run_id)
        if not doc or doc['request']['profile']['provider_id']!='hunyuan' or not doc['job_id']:
            raise AssetJobError('asset_local_identity_conflict')
        jobs=self.coordinator(manager,run_id)
        job=jobs.store.read(doc['job_id'])
        if job['dispatch']=='submitting':
            jobs.recover(job['job_id']);job=jobs.store.read(job['job_id'])
        if job['dispatch']=='outcome_unknown':
            jobs.reconcile_create(job['job_id'],'local:'+job['job_id'])
        return jobs.store.read(job['job_id'])

    def public(self, manager, run_id):
        doc = self.store(manager, run_id).request(run_id)
        if doc is None:
            return None
        result = {k:copy.deepcopy(doc[k]) for k in ('id','project_id','run_id','round_id','slot',
                  'request_revision','version','phase','closed','job_id')}
        local = doc["request"]["profile"]["provider_id"] == "hunyuan"
        result.update(input_kind='image' if local else 'text', model=doc['model'], upload_limit=0, generation_limit=1,
                      draft_import_attempts=int(doc['import_intent'] is not None))
        if doc.get('failure_feedback'):result['failure_feedback']=copy.deepcopy(doc['failure_feedback'])
        if doc.get('attempt_history'):
            result['prior_attempts']=[{'request_id':a['document']['id'],'job_id':a['document']['job_id'],
                'outcome':'confirmed_failed'} for a in doc['attempt_history']]
        if doc['price_check']:
            result['price'] = public_price(doc)
        if doc['job_id']:
            result['job'] = self.local_coordinator(manager, run_id).read(doc['job_id'])
        if doc.get('builder_mode'):
            continuation = self.store(manager,run_id).continuation(doc['id'])
            state = continuation['state'] if continuation else 'blocked' if doc.get('driver_error') else 'waiting'
            if state == 'started':
                worker = manager.workers.get(run_id)
                if not worker or not worker.is_alive(): state = 'unknown'
            if local:
                estimate = {"status":"local_resource_authorization", "credits":None,
                            "unit":"local_generation", "policy":"local_resources"}
            elif request_policy(doc) == 'actual_usage':
                estimate = actual_usage_estimate()
            else:
                try:
                    record = self.prices.lookup(doc['semantic']['wire_template'],doc['endpoint'],self.clock())
                    estimate = {'status':'available','credits':record.credits,'unit':'credits','policy':'catalog_estimate',
                        'supplier_hard_cap':False,'checked_at':record.checked_at,'expires_at':record.expires_at}
                except AssetJobError as error:
                    estimate = {'status':'unavailable','credits':None,'reason_code':error.code}
            result.update(prompt=doc['request']['input'].get('prompt',''),reason=doc['reason'],endpoint=doc['endpoint'],
                profile_id=doc['request']['profile']['id'],profile_revision=doc['request']['profile']['revision'],
                estimate=estimate,approval=public_approval(doc),
                continuation={'state':state,'resume_id':continuation['resume_id'] if continuation else None,
                              'blocked_reason':doc.get('driver_error')},
                actions={'can_approve':not local and not doc['closed'] and not doc['grant_id'] and not doc['job_id']
                                      and not doc.get('driver_error'),
                         'can_end':not doc['closed'],
                         'can_resume':not doc['closed'] and state=='accepted'},
                error={'code':doc['driver_error']} if doc.get('driver_error') else None)
            if doc['job_id']:
                job = self.local_coordinator(manager,run_id).store.read(doc['job_id'])
                if job['preparation'] == 'ready':
                    raw = job['raw_artifact']
                    result['specification'] = {k:copy.deepcopy(raw[k]) for k in
                                              ('sha256','byte_size','contract') if k in raw}
        if local:
            from .asset_local_authorization import LocalAssetAuthority
            from .local_resource_warning import report, public as public_warning
            stored_snapshot = doc.get('resource_snapshot')
            safe_snapshot = report(stored_snapshot) if stored_snapshot is not None else None
            result.update(reference=doc.get("reference"), local_authorization=doc.get("local_authorization"),
                          local_resource_wait=safe_snapshot,
                          error={"code":doc["driver_error"]} if doc.get("driver_error") else None)
            if doc.get('local_authorization'):
                authorization = LocalAssetAuthority(self.store(manager,run_id).project).read(
                    **doc['local_authorization'],allow_stopped=True)
                result['local_resources'] = {key:authorization[key] for key in
                    ('max_generations','effective_max_generations','used_generations','job_timeout_seconds','stopped')}
                result['local_resources']['usage_basis'] = 'reserved_grants_including_unknown; no automatic refund'
            if doc.get('driver_error') == 'asset_local_resource_warning':
                result['local_process'] = {'stage':'resource_warning','evidence':'host_preflight'}
            elif doc.get('driver_error') == 'asset_local_resource_wait':
                result['local_process'] = {'stage':'resource_wait','evidence':'host_preflight'}
            elif not doc['job_id']:
                result['local_process'] = {'stage':'queued' if doc['grant_id'] else 'awaiting_authorization','evidence':'host_sql'}
            else:
                from .hunyuan_provider import HunyuanProvider
                jobs = self.local_coordinator(manager,run_id)
                job = jobs.store.read(doc['job_id'])
                result['local_process'] = HunyuanProvider(jobs.root).progress(job)
                if job['preparation'] == 'ready':
                    from .asset_contract import geometry_facts
                    from .asset_jobs import file_facts
                    raw = job['raw_artifact']
                    content,facts = file_facts(controlled_path(jobs.root,raw['path']),32*1024*1024)
                    if any(facts[k] != raw[k] for k in facts):
                        raise AssetJobError('asset_artifact_integrity_error')
                    result['specification'] = {**facts,**geometry_facts(content),
                        'source_raw_sha256':raw['sha256'],'prepared_strategy':'raw_direct','derived':None,
                        'components_filtered':False,'cleanup':False,'visual_quality':'unreviewed'}
            if doc.get('resource_warning'):
                job = self.local_coordinator(manager, run_id).store.read(doc['job_id'])
                warning = public_warning(doc, job)
                if warning is not None:
                    result['resource_warning'] = warning
        return result

    def cancel_local(self, manager, run_id):
        """Explicit trusted human-stop hook; never create/resume or cancel cloud."""
        doc = self.store(manager,run_id).request(run_id)
        if not doc or doc['request']['profile']['provider_id'] != 'hunyuan' or not doc['job_id']:
            return None
        jobs = self.coordinator(manager,run_id)
        job = jobs.store.read(doc['job_id'])
        if job['dispatch'] == 'submitting':
            jobs.recover(job['job_id'])
            job = jobs.store.read(job['job_id'])
        if job['dispatch'] == 'outcome_unknown':
            # Resolve only the immutable ORIGINAL nonce/birth receipt. This
            # explicit stop path never resumes, grants or submits work.
            jobs.reconcile_create(job['job_id'],'local:'+job['job_id'])
            job = jobs.store.read(job['job_id'])
        if job['dispatch'] != 'accepted':
            return None
        return jobs.cancel_recorded(job['job_id'])

    def sync(self, manager, run_id):
        with manager.lock:
            manager._save(manager.runs[run_id])

    def close(self, manager, run_id, *, reason=None):
        manager._close_asset_wait(run_id,reason=reason)
        return self.public(manager, run_id)

    def schedule_builder(self, manager, run_id, resume_id=None):
        doc = self.verify(manager,run_id)
        if not doc.get('builder_mode') or doc['phase'] not in {'ready','failure_ready'}:
            raise AssetJobError('asset_resume_not_ready')
        if doc.get('failure_feedback'):
            from .local_asset_failure import resume_guard
            resume_guard(manager,self.store(manager,run_id),doc)
        fixed = uuid.uuid5(uuid.UUID(doc['id']),'builder-continuation').hex
        if resume_id is not None:
            stored = self.store(manager,run_id).continuation(doc['id'])
            if (not stored or stored['resume_id'] != resume_id or resume_id != fixed
                    or stored['state'] != 'accepted'):
                raise AssetJobError('asset_resume_identity_conflict')
        state = self.store(manager,run_id).resume(doc,fixed)
        self._after_step('builder_resume_accepted',doc)
        with manager.lock:
            # Persist acceptance even when another project owns the executor.
            if state == 'accepted' and not any(w.is_alive() for w in manager.workers.values()):
                manager._start(manager.runs[run_id],lambda rid:self._resume_builder(manager,rid,fixed))
        return {'asset_request':self.public(manager,run_id),'dispatch_state':state}

    def _resume_builder(self, manager, run_id, resume_id):
        doc = self.verify(manager,run_id)
        store = self.store(manager,run_id)
        if doc.get('failure_feedback'):
            from .local_asset_failure import resume_guard
            resume_guard(manager,store,doc)
        if not store.start_resume(doc,resume_id): return
        try:
            self._after_step('builder_resume_started',doc)
            current = store.request(run_id)
            store.update(current,phase='builder_continuing',driver_error=None)
            manager._update(run_id,'agent_thinking','本地生成已确认失败，同Run交回Builder决定替代方案或申请新尝试。' if doc.get('failure_feedback') else '资产已就绪，同Run交回Builder决定导入及摆放。',status='running')
            from .visual_agent import VisualAgent
            if doc.get('bootstrap'):
                from .agent_resource_store import ResourceStore
                cp = ResourceStore(store.project).checkpoint(doc['checkpoint']['continuation_checkpoint'])
                VisualAgent(manager,run_id,resource_checkpoint=cp,asset_checkpoint=True).execute()
            else:
                VisualAgent(manager,run_id,resume=True).execute()
            current = store.request(run_id)
            rd = store.project.local_round(run_id,round_id=doc['round_id'])
            store.finish_resume(doc['id'],unknown=current['phase']=='import_unknown'
                or any(s['status']=='unknown' for s in (rd or {}).get('steps',[])))
            if doc.get('bootstrap') and manager.get(run_id)['status'] == 'completed':
                from .bootstrap_asset import finish_confirmed
                finish_confirmed(manager, manager.get(run_id))
                self.sync(manager,run_id)
        except Exception as error:
            store.finish_resume(doc['id'],unknown=True)
            manager._fail(run_id,error)


    def resume(self, manager, run_id, resume_id):
        identifier(resume_id)
        doc = self.verify(manager, run_id)
        if doc.get('builder_mode'):
            return self.schedule_builder(manager,run_id,resume_id)
        job = self.coordinator(manager, run_id).store.read(doc['job_id']) if doc['job_id'] else None
        if not job or job['preparation'] != 'ready':
            raise AssetJobError('asset_resume_not_ready')
        from .asset_contract import fixed_glb
        raw = job['raw_artifact']
        _, facts = fixed_glb(controlled_path(self.store(manager, run_id).project.project_root, raw['path']))
        if any(facts[k] != raw[k] for k in facts):
            raise AssetJobError('asset_artifact_integrity_error')
        state = self.store(manager, run_id).resume(doc, resume_id)
        self._after_step('resume_accepted', doc)
        with manager.lock:
            worker = manager.workers.get(run_id)
            if state == 'accepted' and not (worker and worker.is_alive()):
                if any(w.is_alive() for w in manager.workers.values()):
                    raise AssetJobError('asset_resume_busy')
                manager._start(manager.runs[run_id], lambda rid:self._resume(manager, rid, resume_id))
        return {'request':self.public(manager, run_id), 'dispatch_state':state}

    def _resume(self, manager, run_id, resume_id):
        doc = self.verify(manager, run_id)
        if not self.store(manager, run_id).start_resume(doc, resume_id):
            return
        self._after_step('resume_started', doc)
        try:
            self.import_candidate(manager, run_id)
        except Exception as error:
            # Keep the once-only claim and exact candidate; never pretend it is
            # confirmed not-started after an external-write boundary.
            current = self.store(manager,run_id).request(run_id)
            rejected = getattr(error,'outcome',None) == 'rejected'
            if current['import_intent'] and not rejected:
                from .asset_artifacts import import_failure_receipt
                intent = current['import_intent']
                try:
                    failure = import_failure_receipt(self.local_coordinator(manager,run_id),current['job_id'],intent['operation_id'])
                    rejected = bool(failure and failure['instance_id'] == intent['instance_id']
                        and failure['target_workspace'] == intent['target_relative']
                        and failure['source_workspace'] == intent['source_relative']
                        and failure['source']['sha256'] == intent['raw_sha256']
                        and failure['transform'] == intent['pose'])
                except (OSError,ValueError,KeyError,AssetJobError):
                    rejected = False
            self.store(manager, run_id).fail_import(run_id,confirmed_rejected=rejected)
            self.sync(manager, run_id)

    def import_candidate(self, manager, run_id, *, agent_state=None, pose=None):
        from .asset_artifacts import import_ready_asset, write_json_once
        from .asset_contract import fixed_glb
        store = self.store(manager, run_id)
        doc = store.request(run_id)
        if doc and doc.get('builder_mode') and doc['import_intent'] and pose is not None:
            from .asset_contract import transform
            if doc['pose'] != transform(pose):
                raise AssetJobError('asset_import_identity_conflict')
        if doc and doc['closed'] and doc['import_intent']:
            return self._recover_import(manager,run_id,doc)
        doc = self.verify(manager, run_id)
        if doc.get('builder_mode') and not doc['import_intent']:
            from .asset_contract import transform
            doc = store.freeze_import(doc,agent_state,transform(pose))
        jobs = self.coordinator(manager, run_id)
        job = jobs.store.read(doc['job_id'])
        if job['preparation'] != 'ready':
            raise AssetJobError('asset_import_not_ready')
        raw = job['raw_artifact']
        _, raw_facts = fixed_glb(controlled_path(store.project.project_root,raw['path']))
        if any(raw_facts[k] != raw[k] for k in raw_facts):
            raise AssetJobError('asset_artifact_integrity_error')
        if doc['import_intent']:
            return self._recover_import(manager,run_id,doc)
        run = manager.get(run_id)
        rd = store.project.local_round(run_id)
        blocker = (None if doc.get('bootstrap') else edit_limit(budget_progress(run,store.project,round_document=rd)))
        if blocker:
            raise AssetJobError('round_resource_exhausted')
        agent = copy.deepcopy(doc.get('import_agent_state',doc['checkpoint']['agent_state']))
        revision_id = 'r' + str(1 + max(int(r['id'][1:]) for r in agent['revisions'])).zfill(3)
        operation_id = uuid.uuid5(uuid.UUID(doc['id']),'import').hex
        attempt_id = uuid.uuid5(uuid.UUID(operation_id),'attempt:0').hex
        base_revision = agent['selected_revision']
        version = manager.scene_version(run_id,base_revision,allow_active=True)
        source = version['workspace'].relative_to(store.project.project_root).as_posix()
        target = (manager.run_directory(run_id)/('candidates' if doc.get('builder_mode') else 'revisions')/revision_id).relative_to(store.project.project_root).as_posix()
        candidate = controlled_path(store.project.project_root,target)
        if candidate.exists() and any(candidate.iterdir()):
            raise AssetJobError('asset_candidate_output_exists')
        intent = {'project_id':doc['project_id'],'run_id':run_id,'kind':'asset_import','round_id':doc['round_id'],
            'request_id':doc['id'],'base_revision':base_revision,'job_id':job['job_id'],
            'raw_sha256':raw['sha256'],'operation_id':operation_id,'attempt_id':attempt_id,
            'revision_id':revision_id,'source_relative':source,'target_relative':target,
            'instance_id':'asset_' + doc['id'][:16],'pose':copy.deepcopy(doc['pose']),
            'source_artifacts':manager._fixed_scene_artifacts(run,base_revision)}
        step = {'type':'asset_import','id':attempt_id,'operation_id':operation_id,'attempt_id':attempt_id,
            'retry_index':0,'revision_id':revision_id,'base_revision':base_revision,'status':'scheduled',
            'job_id':job['job_id'],'raw_sha256':raw['sha256'],'instance_id':intent['instance_id'],
            'reason':doc['local_goal'],'artifacts':[],'assessment':None,'created_at':manager._timestamp()}
        if doc.get('bootstrap'):
            from .agent_resources import overlay
            limits = overlay({},run['resource_budget'])
            manager._scene_operation(run_id,revision_id,'initial')
        else: limits=resolve_snapshot(run)
        doc, admitted = store.admit_import(doc,step,intent,limits)
        if not admitted:
            return self._recover_import(manager,run_id,doc)
        self._after_step('import_admitted',doc)
        self.verify(manager,run_id)
        result = import_ready_asset(jobs,job['job_id'],manager._adapter_for(run), operation_id=operation_id,
            source_relative=source,target_relative=target,asset_id=intent['instance_id'],pose=intent['pose'])
        self._after_step('blender_imported',doc)
        self._verify_candidate(candidate,result['asset'])
        rendered = manager._adapter_for(run).render(candidate) or {}
        revision = {'id':revision_id,'parent_id':base_revision,'reason':doc['local_goal'],
                    'workspace':candidate.relative_to(manager.run_directory(run_id)).as_posix(),
                    'artifact_ids':[],'status':'pending'}
        artifacts = []
        for kind, name in (('plan','plan.json'),('blend','scene.blend'),('inspection','inspection.json')):
            item = manager._artifact(run_id,candidate/name,kind,revision_id=revision_id)
            artifacts.append(item); revision['artifact_ids'].append(item['id'])
        from .observation_contract import validate_observation_metadata, comparison_identity
        for view in ('main','auxiliary'):
            actual = validate_observation_metadata(rendered.get('observations',{}).get(view,{}),{'preset':view})
            before = next(a for a in run['artifacts'] if a.get('revision_id') == base_revision
                          and a['kind'] == 'render' and a.get('view_id') == view)
            if comparison_identity(before.get('observation',{})) != comparison_identity(actual):
                raise AssetJobError('asset_observation_identity_conflict')
            observation_id = revision_id + '-' + view
            item = manager._artifact(run_id,candidate/(view+'.png'),'render',revision_id=revision_id,
                                     view_id=view,observation_id=observation_id,observation=actual)
            artifacts.append(item); revision['artifact_ids'].append(item['id'])
            agent['observations'].append({'id':observation_id,'revision_id':revision_id,'view_id':view,
                'view':{'preset':view},'comparison':True,'artifact_id':item['id'],'path':item['path'],
                'sha256':item['sha256'],**actual})
        agent['used']['edit_rounds'] += 1
        agent['used']['observations'] += 2
        agent['revisions'].append(revision)
        agent['pending_revision'] = revision_id
        if doc.get('builder_mode'):
            self._builder_revision(agent,revision,intent,rd)
        # Complete evidence is fixed outside SQL. Re-entry verifies this file and
        # the existing import receipt; it never calls an importer without it.
        receipt = {'schema_version':1,'identity':intent,'asset':result['asset'],
                   'run_artifacts':artifacts,'agent_state':agent}
        self._verify_candidate(candidate,receipt['asset'])
        path = candidate/'asset-run-receipt.json'
        write_json_once(path,receipt)
        self._after_step('import_receipt_fixed',doc)
        return self._recover_import(manager,run_id,doc)

    @staticmethod
    def _verify_candidate(candidate,asset):
        from .asset_contract import inspection, registration
        plan = json.loads((candidate/'plan.json').read_text(encoding='utf-8'))
        doc = json.loads((candidate/'inspection.json').read_text(encoding='utf-8'))
        registration(plan.get('assets'),[o['id'] for o in plan.get('objects',[])])
        matched = [a for a in plan['assets']['instances'] if a['instance_id'] == asset['instance_id']]
        measured = [a for a in doc.get('assets',[]) if a['instance_id'] == asset['instance_id']]
        if matched != [asset] or len(measured) != 1:
            raise AssetJobError('asset_import_inspection_conflict')
        inspection(measured[0],asset)

    def _recover_import(self,manager,run_id,doc):
        from .project_runtime import file_sha256
        store = self.store(manager,run_id)
        intent = doc['import_intent']
        candidate = controlled_path(store.project.project_root,intent['target_relative'])
        path = candidate/'asset-run-receipt.json'
        if not path.is_file() or path.stat().st_size > 2*1024*1024:
            raise AssetJobError('asset_import_outcome_unknown')
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if receipt.get('schema_version') != 1 or receipt['identity'] != intent:
            raise AssetJobError('asset_import_receipt_conflict')
        self._verify_import_evidence(manager,run_id,doc,receipt,candidate)
        self._verify_candidate(candidate,receipt['asset'])
        verified = self._read_import_journal(manager,run_id,doc,candidate)
        if verified['asset'] != receipt['asset']:
            raise AssetJobError('asset_import_receipt_conflict')
        for item in receipt['run_artifacts']:
            file = controlled_path(store.project.project_root,
                                  (manager.run_directory(run_id)/item['path']).relative_to(store.project.project_root).as_posix())
            if not file.is_file() or file.stat().st_size != item['size'] or file_sha256(file) != item['sha256']:
                raise AssetJobError('asset_import_receipt_conflict')
        if store.project.verify_artifacts(doc['checkpoint']['artifacts']):
            raise AssetJobError('asset_checkpoint_artifact_drift')
        if not store.request(run_id)['closed']:
            try:
                self.verify(manager,run_id)
            except AssetJobError:
                if not store.request(run_id)['closed']: raise
        # Publish no partial agent/Working state: finalize then rebuild JSON.
        fixed = [{k:item[k] for k in ('kind','size','sha256')}
                 | {'view_id':item.get('view_id'),
                    'path':(manager.run_directory(run_id)/item['path']).relative_to(store.project.project_root).as_posix()}
                 for item in receipt['run_artifacts']]
        store.finalize_import(doc,fixed,receipt['agent_state'],receipt)
        self._after_step('import_sql_finalized',doc)
        self.sync(manager,run_id)
        return self.public(manager,run_id)

    def _read_import_journal(self,manager,run_id,doc,candidate):
        """Verify original immutable receipts without an executable importer."""
        from .asset_jobs import file_facts
        from .asset_provider import artifact_source
        from .asset_contract import fixed_glb
        from .asset_artifacts import check_shape, workspace_facts, materialized_pose
        from .asset_job_store import AssetJobStore
        root = self.store(manager,run_id).project.project_root
        intent = doc['import_intent']
        job = AssetJobStore(self.store(manager,run_id).project).read(doc['job_id'])
        if job['preparation'] != 'ready': raise AssetJobError('asset_import_not_ready')
        raw = job['raw_artifact']
        content, facts = fixed_glb(controlled_path(root,raw['path']))
        check_shape(content)
        if (facts != {k:raw[k] for k in ('sha256','byte_size')}
                or facts['sha256'] != intent['raw_sha256']
                or raw['source'] != artifact_source(job,**facts)):
            raise AssetJobError('asset_import_receipt_conflict')
        directory = f".sceneloop/asset-jobs/{doc['job_id']}/candidates/{intent['operation_id']}"
        pending, journal = [controlled_path(root,directory+'/'+n) for n in ('pending.json','receipt.json')]
        if not pending.is_file() or not journal.is_file():
            raise AssetJobError('asset_import_outcome_unknown')
        original = json.loads(file_facts(pending,1024*1024)[0])
        saved = json.loads(file_facts(journal,1024*1024)[0])
        source = controlled_path(root,intent['source_relative'])
        expected = {'schema':1,'job_id':doc['job_id'],'operation_id':intent['operation_id'],
            'source':raw['source'],'source_workspace':intent['source_relative'],
            'source_files':workspace_facts(source),'target_workspace':intent['target_relative'],
            'instance_id':intent['instance_id'],'transform':intent['pose']}
        if (source == candidate or source in candidate.parents or candidate in source.parents
                or original != expected or saved['request'] != expected
                or saved['candidate_files'] != workspace_facts(candidate)
                or saved['result']['asset']['source'] != facts
                or saved['result']['asset']['instance_id'] != intent['instance_id']
                or saved['result']['asset']['transform'] != materialized_pose(intent['pose'])):
            raise AssetJobError('asset_import_receipt_conflict')
        return saved['result']

    @staticmethod
    def _verify_import_evidence(manager,run_id,doc,receipt,candidate):
        """Require the complete typed result, not only hashes of listed files."""
        from .observation_contract import validate_observation_metadata, comparison_identity
        intent = doc['import_intent']
        artifacts = receipt['run_artifacts']
        names = {('plan',None):'plan.json',('blend',None):'scene.blend',
                 ('inspection',None):'inspection.json',('render','main'):'main.png',
                 ('render','auxiliary'):'auxiliary.png'}
        keyed = {(a['kind'],a.get('view_id')):a for a in artifacts}
        if (len(artifacts) != 5 or set(keyed) != set(names)
                or len({a['id'] for a in artifacts}) != 5):
            raise AssetJobError('asset_import_receipt_conflict')
        for key, name in names.items():
            item = keyed[key]
            expected = (candidate/name).relative_to(manager.run_directory(run_id)).as_posix()
            if item['path'] != expected or item['revision_id'] != intent['revision_id']:
                raise AssetJobError('asset_import_receipt_conflict')
        expected_agent = copy.deepcopy(doc.get('import_agent_state',doc['checkpoint']['agent_state']))
        expected_agent['used']['edit_rounds'] += 1
        expected_agent['used']['observations'] += 2
        expected_agent['pending_revision'] = intent['revision_id']
        expected_agent['revisions'].append({'id':intent['revision_id'],'parent_id':intent['base_revision'],
            'reason':doc['local_goal'],'workspace':candidate.relative_to(manager.run_directory(run_id)).as_posix(),
            'artifact_ids':[a['id'] for a in artifacts],'status':'pending'})
        for view in ('main','auxiliary'):
            item = keyed[('render',view)]
            observation = validate_observation_metadata(item.get('observation',{}),{'preset':view})
            before = next(a for a in doc.get('import_agent_state',doc['checkpoint']['agent_state'])['observations']
                          if a['revision_id'] == intent['base_revision'] and a['view_id'] == view
                          and a.get('comparison') is True)
            if comparison_identity(before) != comparison_identity(observation):
                raise AssetJobError('asset_observation_identity_conflict')
            observation_id = intent['revision_id']+'-'+view
            if item.get('observation_id') != observation_id:
                raise AssetJobError('asset_import_receipt_conflict')
            expected_agent['observations'].append({'id':observation_id,'revision_id':intent['revision_id'],
                'view_id':view,'view':{'preset':view},'comparison':True,'artifact_id':item['id'],
                'path':item['path'],'sha256':item['sha256'],**observation})
        if doc.get('builder_mode'):
            AssetRunHost._builder_revision(expected_agent,expected_agent['revisions'][-1],intent,None)
        if receipt['agent_state'] != expected_agent:
            raise AssetJobError('asset_import_receipt_conflict')

    @staticmethod
    def _builder_revision(agent,revision,intent,rd):
        revision.update(step_id=intent['attempt_id'],round=agent['used']['edit_rounds'],edit_reason=revision['reason'],operations=[])
        agent['actions'].append({'revision_id':revision['id'],'reason':revision['reason'],
            'operations':[],'status':'pending','type':'asset_import','instance_id':intent['instance_id']})

    def _after_step(self, step, doc):
        """Fault-injection boundary. Production does not perform additional work."""
