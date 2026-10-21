"""Candidate-only checkpoints. Immutable files precede SQL publication."""
import copy
import json
import uuid

from .agent_resource_store import ResourceStore
from .director_contract import digest, reject
from .project_state_store import _write_json
from .project_runtime import file_sha256


def store_for(manager, run):
    return ResourceStore(manager._projects().state_store(run['project_id']))


def guard(manager, run):
    if run.get('cancel_requested') or run.get('director_ledger_unknown') or (run.get('error') or {}).get('outcome_unknown'):
        reject('取消或未知调用不能通过追加额度恢复。', 'resource_continuation_blocked', 409)
    # Reuse the actual SQL/asset/reconciliation checks. Unpriced but settled
    # statistics_only calls are not unknown request outcomes.
    from .tool_correction import BuilderCorrection
    checker = BuilderCorrection(manager, run['id'])
    checker.guard()
    from .resource_parent_recovery import guard_parent
    guard_parent(manager, run)
    state = manager._projects().state_store(run['project_id'])
    with state._connection() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='director_attempts'").fetchone():
            for row in c.execute('SELECT document FROM director_attempts WHERE task_id=?',
                                 (run.get('director_task_id') or run.get('director_delegation', {}).get('task_id'),)):
                attempt = json.loads(row[0])
                if attempt.get('state') in {'pending', 'unknown'}:
                    reject('任务存在未结或未知调用；追加额度不解除保护。', 'resource_call_outcome_unknown', 409)
    manager._verify_project_baseline(run)
    manager._assert_refinement_goal(run)
    from .creative_continuation import current, verify
    host=manager._director_host()
    if run.get('operation')=='conversation':
        continuation=current(host,run)
        if continuation:verify(host,run,continuation)
    else:
        continuation=run.get('director_delegation',{}).get('brief',{}).get('creative_continuation')
        if continuation and not run.get('initial_working_commit'):
            verify(host,manager.runs[run['director_delegation']['parent_id']],continuation,child_id=run['id'])


def capture(agent, title, *, allow_empty=False):
    run = agent.manager.get(agent.identifier)
    guard(agent.manager, run)
    selected, pending = agent.state['selected_revision'], agent.state['pending_revision']
    if not (selected or pending) and not allow_empty:
        reject('尚无候选可保存检查点。', 'resource_candidate_missing', 409)
    revisions = {r['id'] for r in agent.state['revisions']}
    files = []
    plans = {}
    for revision in agent.state['revisions']:
        agent._verify_revision(revision)
        path = agent._workspace(revision) / 'plan.json'
        plans[revision['id']] = json.loads(path.read_text(encoding='utf-8'))
    for item in run['artifacts']:
        if item.get('revision_id') not in revisions or item['kind'] not in {'blend', 'plan', 'inspection', 'render'}: continue
        path = (agent.root / item['path']).resolve()
        if not path.is_relative_to(agent.root) or file_sha256(path) != item['sha256']:
            reject('候选文件已变化。', 'resource_checkpoint_changed', 409)
        files.append({k: copy.deepcopy(item[k]) for k in ('id', 'path', 'sha256', 'kind', 'revision_id')})
    state = copy.deepcopy(agent.state)
    project = agent.manager._projects().get(run['project_id'], verify_artifacts=False)
    document = {'id': uuid.uuid4().hex, 'project_id': run['project_id'], 'run_id': run['id'],
        'title': title, 'revision_id': pending or selected, 'selected_revision': selected, 'pending_revision': pending,
        'verified': True, 'files': files, 'plans': plans, 'agent_state': state,
        'usage': copy.deepcopy(run['usage']), 'correction': copy.deepcopy(run.get('builder_tool_correction')),
        'tool_results': copy.deepcopy(agent.tool_results), 'seen': sorted(agent.seen),
        'seen_revisions': sorted(agent.seen_revisions), 'base_digest': run['agent_resource_base']['digest'],
        'scene_identity': {'selection': copy.deepcopy(project.get('current_scene')),
                           'working_version': project['workspace']['working_version'],
                           'project_revision': project['revision']},
        'expected_project_revision': run['expected_project_revision']}
    # Raw Provider-private fields never enter this manifest.
    path = agent.root / 'candidate-checkpoints' / document['id'] / 'manifest.json'
    from .project_runtime import is_link
    if any(is_link(p) for p in path.parents if p.is_relative_to(agent.root)):
        reject('候选检查点目录不能重定向。', 'resource_checkpoint_changed', 409)
    path.parent.mkdir(parents=True, exist_ok=False)
    _write_json(path, document)
    document['manifest'] = {'path': path.relative_to(agent.root).as_posix(), 'sha256': file_sha256(path)}
    return document


def verify(manager, run, checkpoint):
    guard(manager, run)
    if (not checkpoint or checkpoint['run_id'] != run['id'] or checkpoint['project_id'] != run['project_id']
            or checkpoint['base_digest'] != run['agent_resource_base']['digest']
            or checkpoint['usage'] != run['usage'] or checkpoint['correction'] != run.get('builder_tool_correction')
            or checkpoint['expected_project_revision'] != run['expected_project_revision']):
        reject('恢复检查点与原Run累计身份不一致。', 'resource_checkpoint_changed', 409)
    if checkpoint.get('role') == 'director':
        project = manager.get_project(run['project_id'])
        if checkpoint['scene_identity'] != {'selection': project.get('current_scene'), 'project_revision': project['revision']}:
            reject('Director原Working基线已变化。', 'resource_checkpoint_changed', 409)
        if project.get('current_scene'):
            manager.scene_version(project['current_scene']['run_id'], project['current_scene']['revision_id'])
    root = manager.run_directory(run['id'])
    for item in [checkpoint['manifest'], *checkpoint['files']]:
        path = (root / item['path']).resolve()
        from .project_runtime import is_link
        if (not path.is_relative_to(root) or any(is_link(p) for p in [path, *path.parents] if p.is_relative_to(root))
                or not path.is_file() or file_sha256(path) != item['sha256']):
            reject('原候选或检查点已变化，不能续做。', 'resource_checkpoint_changed', 409)
    manifest = json.loads((root / checkpoint['manifest']['path']).read_text(encoding='utf-8'))
    if manifest != {k: v for k, v in checkpoint.items() if k not in {'manifest', 'native'}}:
        reject('SQL检查点与原落盘状态不一致。', 'resource_checkpoint_changed', 409)
    pending = (checkpoint.get('agent_state') or {}).get('_pending_judge') or {}
    if pending.get('continuation'):
        from .judge_continuation import verify as verify_judge
        verify_judge(manager, run, pending['continuation'])
    if checkpoint.get('role') != 'director' and checkpoint.get('scene_identity'):
        project = manager._projects().get(run['project_id'], verify_artifacts=False)
        if checkpoint['scene_identity'] != {'selection': project.get('current_scene'),
                'working_version': project['workspace']['working_version'], 'project_revision': project['revision']}:
            reject('等待期间Working身份已改变，不能追加后继续旧任务。', 'resource_checkpoint_changed', 409)
    if pending.get('mode') == 'continue':
        from .project_runtime import _prepare_judge_checkpoint
        from .judge import JudgeAgent
        prior, _ = _prepare_judge_checkpoint(manager, run['id'], checkpoint['agent_state'],
                                            pending['trigger'], JudgeAgent)
        if (not prior or prior['status'] != 'completed' or prior.get('input_id') != pending['completed_input_id']
                or (prior.get('judgement') or {}).get('verdict') != 'continue'):
            reject('已完成评审不再适用于当前版本。', 'evaluation_input_conflict', 409)
    return checkpoint
