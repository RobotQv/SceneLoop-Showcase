"""Protected human admission persisted in the same project request transaction."""
import copy
from dataclasses import asdict
import uuid

from .asset_provider import ApprovalSource, AssetJobError, HumanGrant, digest, identifier
from .asset_price import PriceApproval, finite


class StoredApprovals(ApprovalSource):
    def __init__(self, manager):
        self.manager = manager

    def record(self, reference):
        identifier(reference)
        for run in list(self.manager.runs.values()):
            if not run.get('asset_capability'): continue
            doc = self.manager._asset_host().store(self.manager,run['id']).request(run['id'])
            if doc and doc.get('human_approval',{}).get('grant',{}).get('id') == reference:
                return copy.deepcopy(doc['human_approval'])
        raise AssetJobError('asset_approval_unavailable')

    def resolve(self, reference):
        from .asset_local_authorization import LocalAssetAuthority
        for run in list(self.manager.runs.values()):
            if not run.get("asset_capability"):
                continue
            host = self.manager._asset_host()
            cap = host.store(self.manager, run["id"]).capability(run["id"])
            if not cap or cap["profile"]["provider_id"] != "hunyuan":
                continue
            try:
                return LocalAssetAuthority(self.manager._projects().state_store(run["project_id"])).resolve(reference)
            except AssetJobError as error:
                if error.code != "asset_approval_unavailable":
                    raise
        return HumanGrant(**self.record(reference)['grant'])

    def resolve_price(self, reference):
        return PriceApproval(**self.record(reference)['price'])


def approve(manager, run_id, body):
    common = {'request_id','expected_request_version','approval_key','acknowledged_no_hard_cap'}
    actual = isinstance(body,dict) and set(body) == common | {'cost_policy'} and body['cost_policy'] == 'actual_usage'
    legacy = (isinstance(body,dict) and set(body) == common | {'max_credits'}
              and finite(body['max_credits']) and body['max_credits'] >= 0)
    if (not (actual or legacy) or type(body['expected_request_version']) is not int
            or body['acknowledged_no_hard_cap'] is not True):
        raise AssetJobError('invalid_asset_approval')
    identifier(body['approval_key']); identifier(body['request_id'])
    host = manager._asset_host()
    store = host.store(manager,run_id)
    doc = store.request(run_id)
    if not doc or doc['id'] != body['request_id']:
        raise AssetJobError('asset_request_binding_conflict')
    # A prior exact request can recover its ACK without issuing new authority.
    fingerprint = digest(body)
    prior = doc.get('human_approval')
    cap = host._profile(manager,run_id)
    if cap['profile']['provider_id'] == 'hunyuan':
        raise AssetJobError('asset_local_task_authorization_required')
    inspected,_ = host.coordinator(manager,run_id).inspect_request(doc['request'])
    if inspected['credential_binding'] != cap['credential_binding']:
        raise AssetJobError('asset_credential_binding_conflict')
    if prior:
        if prior['key'] != body['approval_key'] or prior['digest'] != fingerprint:
            raise AssetJobError('asset_approval_identity_conflict')
        PriceApproval(**prior['price']).validate(host.approval_binding(doc),host.clock())
        return host.public(manager,run_id)
    host.verify(manager,run_id,doc)
    if actual:
        policy, cap_credits, expires = 'actual_usage', None, host.clock()+3600
    else:
        record = host.prices.lookup(doc['semantic']['wire_template'],doc['endpoint'],host.clock())
        if record.credits > body['max_credits']:
            raise AssetJobError('asset_price_above_approval')
        policy, cap_credits = 'catalog_estimate', body['max_credits']
        expires = min(host.clock()+3600,record.expires_at)
    reference = uuid.uuid5(uuid.UUID(doc['id']),'human-approval:'+body['approval_key']).hex
    grant = HumanGrant(reference,doc['project_id'],doc['semantic_digest'],doc['profile_digest'],
        'generation','server:human',policy+': no supplier hard cap acknowledged',expires)
    price = PriceApproval(reference,'server:human',host.approval_binding(doc),policy,cap_credits,expires,True)
    approval = {'key':body['approval_key'],'digest':fingerprint,'grant':asdict(grant),'price':asdict(price)}
    with store.project._write() as c:
        current = store.request(run_id,connection=c)
        old = current.get('human_approval')
        if old:
            if old['key'] != approval['key'] or old['digest'] != fingerprint:
                raise AssetJobError('asset_approval_identity_conflict')
        else:
            store.check_checkpoint(c,current)
            if current['closed'] or current['version'] != body['expected_request_version'] or current['grant_id']:
                raise AssetJobError('asset_request_version_conflict')
            current.update(human_approval=approval,grant_id=reference,price_approval=asdict(price),cost_policy=policy)
            store._put(c,current,current['version'])
    host._after_step('human_approval_fixed',doc)
    host.sync(manager,run_id)
    return host.public(manager,run_id)
