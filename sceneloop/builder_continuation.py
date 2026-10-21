"""Bounded protected native exchange, never a public reasoning transcript."""
import copy

from .builder_context import latest_exchange
from .director_contract import digest, reject
from .director_continuation import Continuation, public_assistant
from .providers import _agent_history


def _private(manager, run, checkpoint):
    # An immutable checkpoint owns its own protected slot. Later checkpoints
    # cannot overwrite a pending request's original native identity.
    return Continuation(manager.run_directory(run['id']) / 'candidate-checkpoints' / checkpoint['id'],
                        run['project_id'], run['id'])


def _empty_edit_initialization(run, checkpoint):
    """Prove no native exchange or candidate exists before an edit's first fork."""
    state = checkpoint.get('agent_state', {})
    marker = {'kind': 'working_edit', 'base_scene': run.get('base_scene')}
    counters = ('image_calls', 'brain_calls', 'judge_calls', 'judge_tool_calls', 'asset3d_calls', 'retries')
    if (run.get('operation') != 'edit' or not run.get('agent_resource_base') or not run.get('base_scene')
            or run.get('bootstrap') or run.get('director_delegation', {}).get('brief', {}).get('creative_continuation')
            or state.get('_pending_initialization') != marker or run.get('artifacts')
            or any(checkpoint.get('usage', {}).get(k) != 0 or run.get('usage', {}).get(k) != 0 for k in counters)
            or state.get('used') != {'edit_rounds': 0, 'tool_calls': 0, 'observations': 0}
            or state.get('selected_revision') is not None or state.get('pending_revision') is not None
            or checkpoint.get('revision_id') is not None
            or any(state.get(k) for k in ('revisions', 'observations', 'geometry', 'actions'))
            or any(checkpoint.get(k) for k in ('files', 'plans', 'tool_results', 'seen', 'seen_revisions'))):
        reject('编辑初始化的零执行边界不可确认。', 'builder_continuation_unavailable', 409)
    return digest(marker)


def seal(agent, checkpoint):
    group = latest_exchange(agent.history)
    if not group:
        if checkpoint.get('agent_state', {}).get('_pending_initialization'):
            return {'kind': 'empty_before_edit_initialization', 'run_id': agent.identifier,
                    'checkpoint_id': checkpoint['id'],
                    'initialization_digest': _empty_edit_initialization(agent.manager.get(agent.identifier), checkpoint)}
        # A fully received first length response may precede any assistant.
        # Seal the truthful empty baseline, never fabricate assistant/tool rows.
        recovery = checkpoint.get('agent_state', {}).get('builder_recovery') or {}
        if (recovery.get('pending') and recovery.get('last_error', {}).get('execution') == 'confirmed_zero_tools'
                and checkpoint['usage']['brain_calls'] >= 1 and not checkpoint.get('tool_results')):
            return {'kind': 'empty_after_length', 'run_id': agent.identifier,
                    'checkpoint_id': checkpoint['id'], 'recovery_digest': digest(recovery)}
        reject('没有完整原生工具配对，不可恢复。', 'builder_continuation_unavailable', 409)
    _agent_history(group, builder_correction=agent.correction)
    assistant, tools = group[0], group[1:]
    decision = {'parent_id': agent.identifier, 'decision_id': digest({'checkpoint': checkpoint['id'], 'usage': checkpoint['usage']}),
                'event_id': checkpoint['id'], 'assistant': public_assistant(assistant)}
    reference = _private(agent.manager, agent.run, checkpoint).save(decision, assistant, 'exchange', tools=tools)
    return {'decision': decision, 'tools': copy.deepcopy(tools), 'reference': reference}


def load(manager, run, checkpoint):
    native = checkpoint.get('native')
    if not native: reject('原Provider续接包缺失。', 'builder_continuation_unavailable', 409)
    if native.get('kind') == 'empty_before_edit_initialization':
        if (native.get('run_id') != run['id'] or native.get('checkpoint_id') != checkpoint['id']
                or native.get('initialization_digest') != _empty_edit_initialization(run, checkpoint)):
            reject('编辑初始化恢复身份不一致。', 'builder_continuation_unavailable', 409)
        return []
    if native.get('kind') == 'empty_after_length':
        recovery = checkpoint.get('agent_state', {}).get('builder_recovery') or {}
        if (native.get('run_id') != run['id'] or native.get('checkpoint_id') != checkpoint['id']
                or native.get('recovery_digest') != digest(recovery) or not recovery.get('pending')
                or recovery.get('last_error', {}).get('execution') != 'confirmed_zero_tools'
                or checkpoint.get('tool_results')):
            reject('空原生恢复基线身份不一致。', 'builder_continuation_unavailable', 409)
        return []
    assistant = _private(manager, run, checkpoint).load(native['reference'], native['decision'], tools=native['tools'])
    group = [assistant, *copy.deepcopy(native['tools'])]
    from .tool_correction import BuilderCorrection
    _agent_history(group, builder_correction=BuilderCorrection(manager, run['id']))
    return group
