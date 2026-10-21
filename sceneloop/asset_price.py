"""Trusted catalog admission, not a supplier price guarantee or fee quote API."""
from dataclasses import dataclass
from decimal import Decimal
import math
from urllib.parse import urlsplit

from .asset_provider import AssetJobError, digest
from .tripo_transport import json_body

ENDPOINTS = {"https://openapi.tripo3d.ai/v3", "https://openapi.tripo3d.com/v3"}


def default_catalog():
    # Trusted, dated source from the authorized TASK; expiry is not refreshed on restart.
    from datetime import datetime, timezone
    expires = datetime(2026,10,18,tzinfo=timezone.utc).timestamp()
    shape = {'model':'v3.1-20260211','texture':False,'pbr':False,
             'geometry_quality':'standard','face_limit':20000}
    return PriceCatalog([PriceRecord('h31-text-shape-20261011','2026-10-11',
        ('https://developers.tripo3d.com/en/models/v3-1','https://developers.tripo3d.com/en/docs/billing'),
        endpoint,shape,10,expires) for endpoint in sorted(ENDPOINTS)])


@dataclass(frozen=True)
class PriceRecord:
    version: str
    checked_at: str
    sources: tuple
    endpoint: str
    shape: dict
    credits: float
    expires_at: float
    task: str = "text_to_model"


@dataclass(frozen=True)
class PriceApproval:
    grant_id: str
    human_origin: str
    binding: dict
    policy: str
    max_credits: float | None
    expires_at: float
    acknowledged_no_hard_cap: bool = False
    revoked: bool = False

    def validate(self, binding, now):
        if (not self.human_origin or not self.grant_id or self.binding != binding
                or self.revoked is not False or not finite(self.expires_at)
                or now >= self.expires_at):
            raise AssetJobError("asset_price_approval_conflict")
        if self.policy == "actual_usage":
            if self.max_credits is not None or self.acknowledged_no_hard_cap is not True:
                raise AssetJobError("asset_price_acknowledgement_required")
            return
        if not finite(self.max_credits) or self.max_credits < 0:
            raise AssetJobError("asset_price_approval_conflict")
        if self.policy == "supplier_hard_cap":
            raise AssetJobError("price_guarantee_unavailable")
        if self.policy != "catalog_estimate" or self.acknowledged_no_hard_cap is not True:
            raise AssetJobError("asset_price_acknowledgement_required")


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def request_policy(doc):
    # An existing amount grant always wins over a request's preferred policy.
    # Missing historical fields never confer actual-usage authority.
    approval = doc.get('price_approval')
    return approval['policy'] if approval else doc.get('cost_policy', 'catalog_estimate')


def public_approval(doc):
    return {'policy': request_policy(doc),
            'state': 'closed' if doc['closed'] else 'approved' if doc['grant_id'] else 'pending',
            'approval_id': doc['grant_id'],
            'max_credits': doc['price_approval']['max_credits'] if doc.get('price_approval') else None}


def public_price(doc):
    receipt = doc.get('price_check')
    if not receipt:
        return None
    keys = ('policy', 'supplier_hard_cap') if receipt['policy'] == 'actual_usage' else (
        'catalog_credits', 'policy', 'supplier_hard_cap', 'balance_status')
    return {k: receipt[k] for k in keys}


def actual_usage_estimate():
    return {'status': 'not_provided', 'credits': None, 'unit': 'credits',
            'policy': 'actual_usage', 'supplier_hard_cap': False}


class PriceCatalog:
    def __init__(self, records):
        # Copy at the trusted host boundary; caller-owned dictionaries cannot drift.
        import copy
        self.records = tuple(copy.deepcopy(records))

    def lookup(self, wire, endpoint, now):
        shape = {k: v for k, v in wire.items() if k != "prompt"}
        if (endpoint not in ENDPOINTS
                or wire.get("model") != "v3.1-20260211" or wire.get("texture") is not False
                or wire.get("pbr") is not False or wire.get("geometry_quality") != "standard"
                or type(wire.get("face_limit")) is not int
                or not 1 <= wire["face_limit"] <= 100000
                or set(wire) - {"model", "prompt", "texture", "pbr", "geometry_quality",
                                "face_limit", "model_seed", "negative_prompt"}):
            raise AssetJobError("asset_price_combination_unknown")
        matches = [r for r in self.records if isinstance(r, PriceRecord)
                   and r.endpoint == endpoint and r.task == "text_to_model" and r.shape == shape]
        if len(matches) != 1:
            raise AssetJobError("asset_price_combination_unknown")
        record = matches[0]
        if (not record.version or not record.checked_at or not record.sources
                or any(not isinstance(s, str) or urlsplit(s).scheme != 'https'
                       or urlsplit(s).hostname not in {'developers.tripo3d.com','developers.tripo3d.ai'}
                       for s in record.sources)
                or not finite(record.credits) or record.credits < 0
                or not finite(record.expires_at) or now >= record.expires_at):
            raise AssetJobError("asset_price_catalog_invalid_or_expired")
        return record


def check_price(catalog, approval, binding, wire, endpoint, credential, transport, *, now):
    if not isinstance(approval, PriceApproval):
        raise AssetJobError("asset_trusted_price_approval_required")
    approval.validate(binding, now)
    if binding.get("wire_digest") != digest(wire):
        raise AssetJobError("asset_price_wire_conflict")
    if approval.policy == 'actual_usage':
        # The SQL once-only gate still binds the exact request and wire. No quote
        # or account balance participates in this explicitly uncapped approval.
        return {'schema_version': 1, 'wire_digest': digest(wire),
                'binding_digest': digest(binding), 'approval_id': approval.grant_id,
                'approval_expires_at': approval.expires_at, 'approval_cap': None,
                'policy': 'actual_usage', 'supplier_hard_cap': False, 'checked_time': now}
    record = catalog.lookup(wire, endpoint, now)
    if record.credits > approval.max_credits:
        raise AssetJobError("asset_price_above_approval")
    try:
        with transport.open("GET", endpoint + "/account/balance",
                            headers={"Authorization": "Bearer " + credential,
                                     "Accept": "application/json"}, timeout=60) as response:
            if response.status != 200:
                raise ValueError
            document = json_body(response)
        data = document.get("data")
        if type(document.get("code")) is not int or document["code"] != 0 or not isinstance(data, dict):
            raise ValueError
        values = [data.get(k) for k in ("balance", "frozen")]
        if any(type(v) not in (int, Decimal) or not Decimal(v).is_finite() or v < 0 for v in values):
            raise ValueError
        # Billing balance is already available credit; frozen is reported separately.
        sufficient = Decimal(values[0]) >= Decimal(str(record.credits))
    except Exception:
        # Neither raw response, balance nor credential is returned or logged.
        raise AssetJobError("asset_balance_unknown") from None
    if not sufficient:
        raise AssetJobError("asset_balance_insufficient")
    return {"schema_version": 1, "catalog_version": record.version,
            "checked_at": record.checked_at, "sources": list(record.sources),
            "catalog_expires_at": record.expires_at, "catalog_credits": record.credits,
            "wire_digest": digest(wire), "binding_digest": digest(binding),
            "approval_id": approval.grant_id, "approval_expires_at": approval.expires_at,
            "approval_cap": approval.max_credits, "policy": approval.policy,
            "supplier_hard_cap": False, "balance_status": "satisfied", "checked_time": now}
