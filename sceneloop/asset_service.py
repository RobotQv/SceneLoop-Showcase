"""Explicitly started, serial Run asset ticks; construction/GET are local only."""
import threading
from time import monotonic

from .asset_approval import StoredApprovals, approve
from .asset_provider import AssetJobError


class AssetService:
    def __init__(self, manager, host, *, interval=1.0):
        self.manager, self.host, self.interval = manager,host,max(.01,interval)
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.resource_checks = {}
        host.approvals = StoredApprovals(manager)

    def approve(self, run_id, body):
        return approve(self.manager,run_id,body)

    def resource_continue(self, run_id, body, *, source=None):
        from .local_resource_warning import approve as confirm
        with self.lock:
            return confirm(self.manager, run_id, body, source=source)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive(): return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop,name='sceneloop-asset-driver',daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(2)

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            self.tick()

    def tick(self):
        if not self.lock.acquire(blocking=False): return
        try:
            for run in list(self.manager.runs.values()):
                if self.stop_event.is_set(): break
                if not run.get('asset_capability'): continue
                try:
                    doc = self.host.store(self.manager,run['id']).request(run['id'])
                    if not doc: continue
                    cap = self.host.store(self.manager,run["id"]).capability(run["id"])
                    if (doc['phase'] in {'generation_blocked','failure_ready'} and not doc['closed']
                            and cap and cap['profile']['provider_id']=='hunyuan'):
                        from .local_asset_failure import mark_ready
                        if mark_ready(self.host,self.manager,run['id']):
                            self.host.schedule_builder(self.manager,run['id'])
                        continue
                    if cap and cap["profile"]["provider_id"] == "hunyuan" and not doc["closed"]:
                        if not doc["grant_id"]:
                            self.host._authorize_local(self.manager,run["id"])
                            doc = self.host.store(self.manager,run["id"]).request(run["id"])
                        if (doc.get("resource_retry_after") or 0) > self.host.clock():
                            continue
                        if doc.get('driver_error') in {'asset_create_outcome_unknown','asset_sql_outcome_unknown'} and doc['job_id']:
                            try:
                                original=self.host.reconcile_local_create(self.manager,run['id'])
                            except AssetJobError:
                                # Evidence may arrive later. Keep the original
                                # visible unknown; no submit/resume/grant here.
                                continue
                            if original['dispatch']!='accepted':continue
                            doc=self.host.store(self.manager,run['id']).update(doc,driver_error=None,phase='generation')
                            self.host.sync(self.manager,run['id'])
                    if (doc.get('builder_mode') and doc['import_intent'] and not doc['import_receipt']
                            and (doc['closed'] or doc['phase'] in {'importing','import_unknown'})
                            and not doc.get('driver_error')):
                        worker = self.manager.workers.get(run['id'])
                        if not worker or not worker.is_alive():
                            # This verifier only reads immutable local receipts and
                            # finalizes SQL. It cannot call an importer or provider.
                            self.host._recover_import(self.manager,run['id'],doc)
                        continue
                    if doc['closed'] or not doc['grant_id'] or doc.get('driver_error') not in {None,"asset_local_resource_wait","asset_local_resource_warning"}: continue
                    if doc['phase'] in {'approval','generation'}:
                        if doc.get('driver_error') in {'asset_local_resource_wait', 'asset_local_resource_warning'}:
                            previous = self.resource_checks.get(run['id'])
                            if previous and previous[0] == doc['version'] and monotonic() < previous[1]:
                                continue
                        self.host.advance(self.manager,run['id'])
                        current = self.host.store(self.manager,run['id']).request(run['id'])
                        if current.get('driver_error') in {'asset_local_resource_wait', 'asset_local_resource_warning'}:
                            self.resource_checks[run['id']] = (current['version'], monotonic() + 15)
                        else:
                            self.resource_checks.pop(run['id'], None)
                    doc = self.host.store(self.manager,run['id']).request(run['id'])
                    if doc['job_id'] and doc['phase'] == 'generation':
                        job = self.host.local_coordinator(self.manager,run['id']).store.read(doc['job_id'])
                        if job['dispatch'] in {'outcome_unknown','rejected'} or job['remote'] in {'failed','cancelled','banned','expired'}:
                            self.host.store(self.manager,run['id']).update(doc,phase='generation_blocked',
                                driver_error='asset_create_outcome_unknown' if job['dispatch']=='outcome_unknown' else 'asset_generation_failed')
                            self.host.sync(self.manager,run['id'])
                            continue
                    if doc.get('builder_mode') and doc['phase'] == 'ready' and not doc['closed']:
                        continuation = self.host.store(self.manager,run['id']).continuation(doc['id'])
                        if not continuation or continuation['state'] == 'accepted':
                            self.host.schedule_builder(self.manager,run['id'])
                except AssetJobError as error:
                    # Visible bounded local failure, never automatic POST retry.
                    store = self.host.store(self.manager,run['id'])
                    current = store.request(run['id'])
                    if current and current.get('driver_error') != error.code:
                        store.update(current,driver_error=error.code)
                        self.host.sync(self.manager,run['id'])
                except Exception:
                    # Preserve started intents; an explicit read exposes unknown claims.
                    continue
        finally:
            self.lock.release()


def install(manager):
    from .asset_run import AssetRunHost
    from .asset_price import default_catalog
    from .asset_routing import AssetProviderRouter
    from .tripo_transport import PinnedTransport
    host = AssetRunHost(manager.profile_repository,None,
        lambda state:AssetProviderRouter(state.project_root),None,default_catalog(),PinnedTransport())
    manager.asset_host = host
    manager.asset_service = AssetService(manager,host)
    return manager.asset_service
