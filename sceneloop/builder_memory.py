"""Bounded Builder-authored notes over the existing immutable business audit.

Notes are opinions, never authority. Only this Run's registered identities can
be referenced; retrieval cannot read a model-supplied path.
"""
import copy
import json
import re

from . import builder_context

SOURCE = 'model_authored_unverified'
MAX_NOTES = 12
INSTRUCTIONS = '''You own concise working notes through manage_builder_context. Record conclusions, choices,
failed approaches and next actions, not private reasoning or copies of authoritative facts. Notes are
unverified model opinions. Archive redundant notes without deleting their audit originals; list/read
the current Run's audit to retrieve old evidence. Never claim notes change permissions, Working,
Judge decisions or resource usage. Current Harness facts and tool receipts remain authoritative.
update replaces summary and active notes using expected_version from builder_working_memory;
archive removes selected note ids and/or suppresses referenced audit records from optional recent
history. It never removes the latest complete native exchange or current facts. list returns bounded
registered references and a next_cursor; read accepts those exact references. Put important audit
references in active notes to retain their focus, and read again when original detail is needed.'''

_REFERENCE = {'type': 'object', 'properties': {
    'kind': {'type': 'string', 'enum': ['audit', 'object', 'artifact']},
    'project_id': {'type': ['string', 'null']}, 'run_id': {'type': 'string'},
    'sequence': {'type': 'integer'}, 'sha256': {'type': 'string'},
    'revision_id': {'type': 'string'}, 'object_id': {'type': 'string'}, 'artifact_id': {'type': 'string'}},
    'required': ['kind', 'project_id', 'run_id'], 'additionalProperties': False,
    'description': 'audit requires sequence+sha256 copied from list; object requires revision_id+object_id; artifact requires artifact_id+sha256. Only current project/Run registered identities.'}

TOOL = {'type': 'function', 'function': {'name': 'manage_builder_context',
    'description': 'Maintain bounded model-authored working notes; archive/list/read this Run business audit. No scene write or permission changes.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['update', 'archive', 'list', 'read']},
        'expected_version': {'type': 'integer', 'minimum': 0},
        'summary': {'type': 'string', 'maxLength': 1600},
        'notes': {'type': 'array', 'maxItems': MAX_NOTES, 'items': {'type': 'object',
            'properties': {'id': {'type': 'string'}, 'text': {'type': 'string', 'maxLength': 800},
                           'references': {'type': 'array', 'maxItems': 4, 'items': _REFERENCE}},
            'required': ['id', 'text', 'references'], 'additionalProperties': False}},
        'ids': {'type': 'array', 'maxItems': MAX_NOTES, 'items': {'type': 'string'}},
        'references': {'type': 'array', 'minItems': 1, 'maxItems': 4, 'items': _REFERENCE},
        'cursor': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
        'required': ['action'], 'additionalProperties': False}}}


def _bad(message):
    from .visual_agent import ToolInputError
    raise ToolInputError(message)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _bad('工作记忆字段不合法；不能写入Harness事实或权限。')


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _bad('工作记忆应是有界的简明结论。')
    return value.strip()


def current(agent):
    if 'builder_memory' in agent.state:
        return copy.deepcopy(agent.state['builder_memory'])
    return {
        'version': 0, 'source': SOURCE, 'project_id': agent.run.get('project_id'),
        'run_id': agent.identifier, 'summary': '', 'notes': [], 'archived_sequences': [], 'audit': None}


def validate(agent):
    memory = current(agent)
    if (not isinstance(memory, dict) or memory.get('source') != SOURCE or memory.get('run_id') != agent.identifier
            or memory.get('project_id') != agent.run.get('project_id')
            or type(memory.get('version')) is not int or memory['version'] < 0
            or not isinstance(memory.get('notes'), list) or len(memory['notes']) > MAX_NOTES):
        _bad('工作记忆身份不属于当前Run。')
    if memory['version']:
        record = agent.audit.read(memory['audit'])
        if record.get('kind') != 'builder_memory' or record.get('value') != {k: v for k, v in memory.items() if k != 'audit'}:
            _bad('工作记忆与原审计版本不一致。')
    return memory


def _bound(agent, ref):
    if (not isinstance(ref, dict) or ref.get('run_id') != agent.identifier
            or ref.get('project_id') != agent.run.get('project_id')):
        _bad('历史引用必须属于当前项目和Run。')


def _catalog(agent):
    """Use original append-only trace receipts, never bless a file's new SHA."""
    from .project_runtime import is_link
    path = agent.root / 'agent-trace.jsonl'
    if not path.exists():
        return {}
    if is_link(path):
        _bad('审计登记不可重定向。')
    rows = {}
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            ref = row.get('audit') or {}
            if type(ref.get('sequence')) is int and row.get('kind') in {'tool', 'builder_memory'}:
                if ref['sequence'] in rows and rows[ref['sequence']][1] != ref:
                    _bad('审计登记身份冲突。')
                rows[ref['sequence']] = (row['kind'], ref)
    return rows


def _audit_reference(agent, sequence):
    if type(sequence) is not int or not 1 <= sequence <= 99999999:
        _bad('审计序号无效。')
    path = agent.audit.directory / f'{sequence:08d}.json'
    # BusinessAudit.read repeats the path/symlink and hash checks before use.
    from .project_runtime import is_link
    if is_link(path) or is_link(agent.audit.directory):
        _bad('审计引用不可重定向。')
    registered = _catalog(agent).get(sequence)
    if not registered:
        _bad('审计序号没有原始登记回执。')
    ref = registered[1]
    if ref.get('path') != 'builder-audit/' + path.name or path.stat().st_size > 16 * 1024 * 1024:
        _bad('审计引用身份无效或超出单次检索容量；使用当前场景观察工具。')
    doc = agent.audit.read(ref)
    if doc.get('kind') != registered[0] or doc.get('sequence') != sequence:
        _bad('审计内容与原登记回执不一致。')
    return copy.deepcopy(ref)


def reference(agent, ref):
    _bound(agent, ref)
    common = {'kind', 'project_id', 'run_id'}
    kind = ref.get('kind')
    if kind == 'audit':
        _keys(ref, common | {'sequence', 'sha256'})
        internal = _audit_reference(agent, ref['sequence'])
        if internal['sha256'] != ref['sha256']:
            _bad('审计SHA不匹配。')
        document = agent.audit.read(internal)
        return {'reference': copy.deepcopy(ref), 'applicability': 'historical_not_current_truth', 'document': document}
    if kind == 'object':
        _keys(ref, common | {'revision_id', 'object_id'})
        revision = agent._revision(ref['revision_id'])
        agent._verify_revision(revision)
        workspace = agent._workspace(revision)
        plan = json.loads((workspace / 'plan.json').read_text(encoding='utf-8'))
        inspection = json.loads((workspace / 'inspection.json').read_text(encoding='utf-8'))
        from .scene_evidence import known_object_ids
        if ref['object_id'] not in known_object_ids(plan, inspection):
            _bad('对象不在所引用的真实场景中。')
        return {'reference': copy.deepcopy(ref), 'applicability': 'current_candidate' if revision['id'] in {
            agent.state['selected_revision'], agent.state['pending_revision']} else 'historical_revision',
            'object_id': ref['object_id'], 'revision_id': revision['id']}
    if kind == 'artifact':
        _keys(ref, common | {'artifact_id', 'sha256'})
        item = next((r for r in agent.manager.get(agent.identifier)['artifacts'] if r['id'] == ref['artifact_id']), None)
        if not item or item['sha256'] != ref['sha256']:
            _bad('产物身份或SHA无效。')
        from .project_runtime import file_sha256, is_link
        path = agent.root / item['path']
        if (not path.resolve().is_relative_to(agent.root.resolve())
                or any(is_link(p) for p in [path, *path.parents] if p.is_relative_to(agent.root))
                or file_sha256(path) != item['sha256']):
            _bad('产物已变化或越出当前Run。')
        return {'reference': copy.deepcopy(ref), 'kind': item['kind'], 'revision_id': item.get('revision_id'),
                'applicability': 'current_candidate' if item.get('revision_id') in {
                    agent.state['selected_revision'], agent.state['pending_revision']} else 'historical_artifact'}
    _bad('不支持的引用类型；不能按文件路径读取。')


def _refs(agent, values, *, minimum=0):
    if not isinstance(values, list) or not minimum <= len(values) <= 4:
        _bad('每次只能引用最多4条已登记证据。')
    return [reference(agent, value) for value in values]


def _listing(agent, cursor, limit):
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 20:
        _bad('审计分页无效。')
    rows = []
    for sequence in sorted(_catalog(agent)):
        if sequence <= cursor:
            continue
        internal = _audit_reference(agent, sequence)
        doc = agent.audit.read(internal)
        if doc['kind'] not in {'tool', 'builder_memory'}:
            continue
        rows.append({'reference': {'kind': 'audit', 'project_id': agent.run.get('project_id'),
            'run_id': agent.identifier, 'sequence': internal['sequence'], 'sha256': internal['sha256']},
            'kind': doc['kind'], 'tool': doc['value'].get('name'),
            'memory_version': doc['value'].get('version') if doc['kind'] == 'builder_memory' else None})
        if len(rows) > limit:
            break
    return {'entries': rows[:limit], 'has_more': len(rows) > limit,
            'next_cursor': rows[limit-1]['reference']['sequence'] if len(rows) >= limit else (rows[-1]['reference']['sequence'] if rows else cursor)}


def manage(agent, arguments):
    try:
        return _manage(agent, arguments)
    except (OSError, KeyError, ValueError, TypeError) as error:
        from .visual_agent import ToolInputError
        if isinstance(error, ToolInputError):
            raise
        _bad('工作记忆或引用不可核实；请重新读取当前事实。')


def _manage(agent, arguments):
    _keys(arguments, {'action'}, {'expected_version', 'summary', 'notes', 'ids', 'references', 'cursor', 'limit'})
    action = arguments['action']
    old = validate(agent)
    if action == 'list':
        _keys(arguments, {'action'}, {'cursor', 'limit'})
        return _listing(agent, arguments.get('cursor', 0), arguments.get('limit', 10))
    if action == 'read':
        _keys(arguments, {'action', 'references'})
        rows = _refs(agent, arguments['references'], minimum=1)
        for row in rows:
            if len(builder_context.encoded(row).encode()) > 24000:
                doc = row.pop('document', {})
                row.update(truncated=True, document_kind=doc.get('kind'),
                           instruction='原件仍保留；大型历史场景请用inspect_scene/inspect_geometry重新获取当前证据。')
        return {'source': 'immutable_business_audit', 'records': rows}
    if action not in {'update', 'archive'}:
        _bad('未知工作记忆动作。')
    if type(arguments.get('expected_version')) is not int or arguments['expected_version'] != old['version']:
        _bad('工作记忆版本冲突；读取当前版本后再提交。')
    new = {k: copy.deepcopy(v) for k, v in old.items() if k != 'audit'}
    if action == 'update':
        _keys(arguments, {'action', 'expected_version', 'summary', 'notes'})
        new['summary'] = _text(arguments['summary'], 1600)
        values = arguments['notes']
        if not isinstance(values, list) or len(values) > MAX_NOTES:
            _bad('热工作记忆最多12条；先归档冗余结论。')
        notes = []
        for note in values:
            _keys(note, {'id', 'text', 'references'})
            if not isinstance(note['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', note['id']):
                _bad('记忆条目标识无效。')
            _refs(agent, note['references'])
            notes.append({'id': note['id'], 'text': _text(note['text'], 800), 'references': copy.deepcopy(note['references'])})
        if len({n['id'] for n in notes}) != len(notes):
            _bad('记忆条目标识重复。')
        new['notes'] = sorted(notes, key=lambda n: n['id'])
    else:
        _keys(arguments, {'action', 'expected_version'}, {'ids', 'references'})
        ids = arguments.get('ids', [])
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_NOTES or any(type(i) is not str for i in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= {n['id'] for n in old['notes']}):
            if ids or not arguments.get('references'):
                _bad('只能归档当前已有的记忆条目。')
        refs = arguments.get('references', [])
        _refs(agent, refs)
        if any(r['kind'] != 'audit' for r in refs):
            _bad('只能选择归档已登记审计信息；当前事实不支持归档。')
        sequences = sorted(set(new['archived_sequences']) | {r['sequence'] for r in refs})
        if len(sequences) > 512:
            _bad('选择性归档索引已达容量；原始历史仍可检索。')
        new['archived_sequences'] = sequences
        new['notes'] = [n for n in new['notes'] if n['id'] not in ids]
    new['version'] += 1
    ref = agent._trace('builder_memory', new)
    if not ref:
        _bad('工作记忆原件未可靠登记，未更新热记忆。')
    new['audit'] = ref
    agent.state['builder_memory'] = new
    return {'memory': copy.deepcopy(new), 'authority': 'opinions_only', 'originals_retained': True}


def view(state):
    value = state.get('builder_memory')
    return copy.deepcopy(value) if value else {'version': 0, 'source': SOURCE, 'summary': '', 'notes': []}


def omitted(state):
    memory = state.get('builder_memory') or {}
    pinned = {r['sequence'] for n in memory.get('notes', []) for r in n['references'] if r['kind'] == 'audit'}
    return set(memory.get('archived_sequences', [])) - pinned
