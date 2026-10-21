"""Resume only the next unsent Judge request at a fully returned boundary."""
import copy
import uuid

from .director_continuation import Continuation, public_assistant
from .director_contract import digest, reject
from .evaluation_input import EvaluationInputBuilder, validate_evaluation_inputs
from .providers import _agent_history
from .runtime import AppError


class JudgeResourceWait(Exception):
    def __init__(self, continuation=None):
        super().__init__('Judge资源不足；当前评审等待原Run追加。')
        self.continuation = continuation


class JudgeContinuationUnavailable(AppError):
    def __init__(self):
        super().__init__('裁判续接记录无法安全保存；已有调用与证据保留，未形成质量结论，不会自动重发。',
                         'judge_continuation_unavailable', 409)


def _private(manager, run, identity):
    return Continuation(manager.run_directory(run['id']) / 'judge-continuations' / identity,
                        run['project_id'], run['id'])


def capture(judge, builder, messages, *, evidence_ids, correction_count, corrected_error):
    _agent_history(messages)
    if any(row['state'] != 'returned' for row in builder.requests):
        reject('评审仍有未知请求，不能追加后重发。', 'resource_call_outcome_unknown', 409)
    if validate_evaluation_inputs(builder.items):
        reject('原评审输入已变化。', 'evaluation_input_integrity', 409)
    # Keep Provider private fields encrypted. Large image bytes are rehydrated
    # only from their original immutable input rows, never from new observations.
    def encode(value):
        if isinstance(value, dict):
            if value.get('type') == 'image_url':
                key = builder._image_objects.get(id(value))
                if key is None:
                    reject('原评审图片身份缺失。', 'evaluation_input_integrity', 409)
                return {'_judge_image_input': key}
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [encode(item) for item in value]
        return value
    builder.persist(judge.manager._projects().state_store(judge.run['project_id']))
    identity = uuid.uuid4().hex
    payload = {'messages': encode(messages), 'evidence_ids': sorted(evidence_ids),
               'correction_count': correction_count, 'corrected_error': corrected_error,
               'view_count': judge._view_count, 'geometry_count': judge._geometry_count,
               'issued_views': sorted(judge.issued_views),
               'geometry_evidence': copy.deepcopy(judge.geometry_evidence)}
    assistant = {'role': 'assistant', 'content': 'Judge continuation', '_judge_resume': payload}
    decision = {'parent_id': judge.identifier, 'decision_id': digest({
        'input_id': builder.evaluation_id, 'input_digest': builder.input_digest, 'checkpoint': identity}),
        'event_id': identity, 'assistant': public_assistant(assistant)}
    try:
        reference = _private(judge.manager, judge.run, identity).save(decision, assistant, 'exchange')
    except AppError as error:
        # Only the protected local save failure has known zero new execution.
        # Earlier unknown/input-integrity/SQL failures keep their own protection.
        if error.code != 'director_continuation_unavailable' or getattr(error, 'outcome_unknown', False):
            raise
        raise JudgeContinuationUnavailable() from None
    return {'id': identity, 'input_id': builder.evaluation_id, 'input_digest': builder.input_digest,
            'decision': decision, 'reference': reference}


def verify(manager, run, reference):
    state = manager._projects().state_store(run['project_id'])
    row = state.lookup_evaluation_input(reference['input_id'])
    if not row or row['state'] != 'open' or row['input_digest'] != reference['input_digest']:
        reject('原评审输入或目标已变化，不能继续。', 'evaluation_input_conflict', 409)
    manifest = row['manifest']
    if (manifest['run_id'] != run['id'] or manifest['project_id'] != run['project_id']
            or any(r['state'] != 'returned' for r in manifest['requests'])
            or validate_evaluation_inputs(manifest['items'])):
        reject('原评审调用或证据不可验证。', 'evaluation_input_integrity', 409)
    assistant = _private(manager, run, reference['id']).load(
        reference['reference'], reference['decision'])
    return manifest, assistant


def restore(judge, reference, expected_context):
    manifest, assistant = verify(judge.manager, judge.run, reference)
    actual = copy.deepcopy(expected_context)
    frozen = manifest['context']
    # The same attempt may have appended diagnostic images. Reuse the shared
    # subset check: original files must still match; extra renders are allowed.
    for field in ('current_files', 'before_files'):
        before, after = frozen['target'].get(field), actual['target'].get(field)
        if before is None and after is None:
            continue
        if not judge.manager._snapshots_match(before, after):
            reject('原评审文件已变化，不能继续。', 'evaluation_input_integrity', 409)
        actual['target'][field] = copy.deepcopy(before)
    if frozen != actual:
        reject('原评审目标已变化，不能继续。', 'evaluation_input_conflict', 409)
    payload = assistant['_judge_resume']
    builder = EvaluationInputBuilder(**{k: manifest[k] for k in
        ('evaluation_id', 'run_id', 'attempt_id', 'kind', 'project_id')})
    builder.items = copy.deepcopy(manifest['items'])
    builder.requests = copy.deepcopy(manifest['requests'])
    builder.context = copy.deepcopy(manifest['context'])
    builder._sequence = max((row['sequence'] for row in builder.items), default=0)
    builder._persisted_digest = manifest['input_digest']
    def decode(value):
        if isinstance(value, dict):
            if set(value) == {'_judge_image_input'}:
                item = next(r for r in builder.items if r['input_id'] == value['_judge_image_input'])
                content = judge.providers.image_content(item['path'])
                builder.bind_image_payload(content, item['input_id'])
                return content
            return {key: decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decode(item) for item in value]
        return value
    payload['messages'] = decode(payload['messages'])
    _agent_history(payload['messages'])
    judge.attempt_id = manifest['attempt_id']
    judge.target = copy.deepcopy(frozen['target'])
    judge.issued_views = set(payload['issued_views'])
    judge.geometry_evidence = payload['geometry_evidence']
    judge._view_count, judge._geometry_count = payload['view_count'], payload['geometry_count']
    return builder, payload
