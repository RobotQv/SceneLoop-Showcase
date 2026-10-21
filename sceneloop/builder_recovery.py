"""Same-Run, metered continuation after a fully received zero-tool length stop.

No raw reasoning is stored here. A trusted Provider adapter may hold an opaque
native continuation in memory; ordinary chat/completions always uses facts.
"""
import copy

from .providers import ProviderError


class BuilderRecovery:
    LIMIT = 2

    def __init__(self, agent):
        self.agent = agent
        self.native = None
        self.data = agent.state.setdefault('builder_recovery', {
            'schema_version': 1, 'run_id': agent.identifier, 'episode': 0,
            'pending': False, 'recovery_calls': 0, 'attempts': [], 'truncations': 0,
            'mode': 'trusted_state', 'last_error': None})
        d = self.data
        if (not isinstance(d, dict) or d.get('schema_version') != 1 or d.get('run_id') != agent.identifier
                or type(d.get('episode')) is not int or d['episode'] < 0
                or type(d.get('pending')) is not bool or type(d.get('recovery_calls')) is not int
                or not 0 <= d['recovery_calls'] <= self.LIMIT
                or not isinstance(d.get('attempts'), list) or len(d['attempts']) != d['recovery_calls']
                or d.get('mode') not in {'trusted_state', 'trusted_state_after_restart', 'native'}
                or type(d.get('truncations')) is not int or d['truncations'] < 0):
            self.fail('builder_recovery_state_unknown', '原Builder截断恢复身份不可核实。', unknown=True)
        for index, attempt in enumerate(d['attempts'], 1):
            if (not isinstance(attempt, dict) or attempt.get('index') != index
                    or type(attempt.get('brain_call')) is not int
                    or not 1 <= attempt['brain_call'] <= agent.run['usage']['brain_calls']
                    or attempt.get('status') not in {'reserved', 'returned', 'failed', 'outcome_unknown'}):
                self.fail('builder_recovery_state_unknown', '原Builder恢复调用记录不可核实。', unknown=True)
        if d['pending'] and (not isinstance(d.get('last_error'), dict)
                or d['last_error'].get('execution') != 'confirmed_zero_tools'
                or d['last_error'].get('diagnostics', {}).get('response_state') != 'length_zero_tools'):
            self.fail('builder_recovery_state_unknown', '原Builder缺少完整零工具截断证据。', unknown=True)
        if d['pending'] and d['mode'] == 'native':
            # Native truncated state is not fabricated from the public manifest.
            # It intentionally expires across a process boundary, which is
            # disclosed in the next decision's context.
            d['mode'] = 'trusted_state_after_restart'

    def persist(self):
        from .project_runtime import is_link
        import json
        try:
            with self.agent.manager.lock:
                run = self.agent.manager._get(self.agent.identifier)
                run['agent'] = copy.deepcopy(self.agent.state)
                self.agent.manager._save(run)
                path = self.agent.root / 'run.json'
                if is_link(path) or json.loads(path.read_text(encoding='utf-8'))['agent']['builder_recovery'] != self.data:
                    raise ValueError('readback mismatch')
        except Exception as error:
            raise ProviderError('builder_recovery_persistence_unknown', '恢复计数未可靠落盘；停止后续调用。', outcome_unknown=True) from error

    def fail(self, code, message, *, unknown=False):
        raise ProviderError(code, message, outcome_unknown=unknown,
                            diagnostics=((self.data.get('last_error') or {}).get('diagnostics') if isinstance(self.data, dict) else None))

    def guard(self):
        from .tool_correction import BuilderCorrection, CorrectionCancelled
        if self.agent.manager._cancelled(self.agent.identifier):
            raise CorrectionCancelled('已取消原Builder。')
        BuilderCorrection(self.agent.manager, self.agent.identifier).guard()
        if self.agent.run.get('project_id'):
            state = self.agent.manager._projects().state_store(self.agent.run['project_id'])
            with state._connection() as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='director_attempts'").fetchone():
                    import json
                    task_id = (self.agent.run.get('director_task_id') or
                               self.agent.run.get('director_delegation', {}).get('task_id'))
                    for row in connection.execute('SELECT document FROM director_attempts WHERE task_id=?', (task_id,)):
                        if json.loads(row[0]).get('state') in {'pending', 'unknown'}:
                            self.fail('builder_ledger_unknown', '原任务有未结或未知调用费用，不能自动收束重试。', unknown=True)
        self.agent.manager._assert_refinement_goal(self.agent.manager.get(self.agent.identifier))
        self.agent.manager._verify_project_baseline(self.agent.manager.get(self.agent.identifier))
        for identifier in {self.agent.state['selected_revision'], self.agent.state['pending_revision']} - {None}:
            self.agent._verify_revision(self.agent._revision(identifier))

    def before_request(self):
        if not self.data['pending']:
            return
        self.guard()
        if self.data['recovery_calls'] >= self.LIMIT:
            self.fail('builder_length_recovery_exhausted', '单次思考触顶后两次原Builder收束调用仍无执行进展；交回Director决策。')

    def reserve_request(self):
        if not self.data['pending']:
            return
        self.data['recovery_calls'] += 1
        self.data['attempts'].append({'index': self.data['recovery_calls'],
            'brain_call': self.agent.manager.get(self.agent.identifier)['usage']['brain_calls'], 'status': 'reserved'})
        self.persist()

    def response(self, error=None):
        if error is None and self.native is not None:
            # Opaque continuation tickets are one-shot. A complete reply now
            # supplies the normal native exchange for any following request.
            self.native = None
            self.data['mode'] = 'trusted_state'
        if self.data['attempts'] and self.data['attempts'][-1]['status'] == 'reserved':
            self.data['attempts'][-1]['status'] = ('outcome_unknown' if getattr(error, 'outcome_unknown', False)
                else 'failed' if error else 'returned')
            if error:
                self.data['attempts'][-1]['error_code'] = getattr(error, 'code', 'unknown')
            self.persist()

    def length(self, error):
        from .providers import OutputLengthExhausted
        if not isinstance(error, OutputLengthExhausted) or error.outcome_unknown:
            raise error
        usage = error.diagnostics.get('usage') or {}
        if type(usage.get('prompt_tokens')) is not int or type(usage.get('completion_tokens')) is not int:
            # Legacy Builders do not own a Director ledger; absence of that
            # table must not turn missing usage into permission to spend again.
            raise ProviderError('builder_usage_unknown', '截断响应未给出可信输入/输出用量；不能自动追加模型调用。',
                                outcome_unknown=True, diagnostics=error.diagnostics)
        self.guard()
        if not self.data['pending']:
            self.data.update(episode=self.data['episode'] + 1, pending=True, recovery_calls=0, attempts=[],
                             baseline_revisions=[r['id'] for r in self.agent.state['revisions']],
                             baseline_commit_count=len(self.agent.manager.get(self.agent.identifier).get('commits', [])))
        self.data['truncations'] += 1
        self.native = error.native
        self.data['mode'] = 'native' if self.native else 'trusted_state'
        self.data['last_error'] = {'code': error.code, 'diagnostics': copy.deepcopy(error.diagnostics),
            'response_sha256': error.response_sha256, 'execution': 'confirmed_zero_tools',
            'brain_call': self.agent.manager.get(self.agent.identifier)['usage']['brain_calls']}
        self.persist()
        self.agent._trace('builder_length', self.data['last_error'])
        if self.data['recovery_calls'] >= self.LIMIT:
            self.fail('builder_length_recovery_exhausted', '单次思考连续触顶，原Builder两次收束机会已用完；未执行残缺输出。')

    def prepare(self, payload, adapter):
        """Trusted code-only native protocol, never assistant-message invention."""
        if self.native is None:
            return payload
        previous_adapter, state = self.native
        if adapter is not previous_adapter:
            self.fail('builder_native_continuation_unavailable', '原Provider原生续接能力不匹配；不重发或伪造续接。')
        try:
            result = adapter.prepare(copy.deepcopy(payload), state)
            # The adapter owns opaque wire extension only, not facts, model,
            # tools, or configured output capacity.
            if (not isinstance(result, dict) or any(result.get(k) != payload.get(k) for k in payload)):
                raise ValueError('changed trusted request')
            return result
        except Exception:
            self.fail('builder_native_continuation_unavailable', '原生续接包无法构造；没有重新发送原请求。')

    def progress(self, name, result, *, reused=False):
        if not self.data['pending'] or reused or not result.get('ok'):
            return
        if name not in {'create_scene', 'apply_edit', 'import_asset', 'transform_asset', 'commit_local_round'}:
            return
        body = result.get('result') or {}
        revision = body.get('revision') or {}
        identifier = revision.get('id') or body.get('revision_id')
        if not identifier or not any(r['id'] == identifier for r in self.agent.state['revisions']):
            return
        if identifier in self.data.get('baseline_revisions', []):
            if name != 'commit_local_round' or len(self.agent.manager.get(self.agent.identifier).get('commits', [])) <= self.data.get('baseline_commit_count', 0):
                return
        self.agent._verify_revision(self.agent._revision(identifier))
        if name != 'create_scene' and not (revision.get('operations') or name in {'import_asset', 'transform_asset', 'commit_local_round'}):
            return
        parent_id = self.agent._revision(identifier).get('parent_id')
        if parent_id and name != 'commit_local_round':
            # Reuse the existing save contract's net authored-state comparison,
            # not a fresh visual-quality heuristic or .blend byte comparison.
            import json
            from .refinement_round import scene_semantics
            def semantics(rid):
                workspace = self.agent._workspace(self.agent._revision(rid))
                return scene_semantics(json.loads((workspace / 'plan.json').read_text(encoding='utf-8')),
                                       json.loads((workspace / 'inspection.json').read_text(encoding='utf-8')))
            if semantics(parent_id) == semantics(identifier):
                return
        self.agent._trace('builder_recovery_completed', {**copy.deepcopy(self.data), 'progress_revision': identifier, 'tool': name})
        self.data.update(pending=False, recovery_calls=0, attempts=[], mode='trusted_state',
                         last_progress={'revision_id': identifier, 'tool': name})
        self.native = None
        self.persist()


def context(state):
    value = state.get('builder_recovery')
    if not value or not value.get('pending'):
        return None
    return {'episode': value['episode'], 'used': value['recovery_calls'], 'limit': BuilderRecovery.LIMIT,
        'mode': value['mode'], 'reasoning_recovered': value['mode'] == 'native',
        'instruction': '此前单次推理未形成可执行输出。基于当前已确认事实和你主动写下的工作记忆，聚焦下一项能实际执行并验证的操作；不必一次规划整个场景。无原生续接时没有恢复被截断的私有思考。记忆整理不重置此恢复额度。'}
