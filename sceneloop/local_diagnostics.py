"""Whitelist-only host failure evidence; never serialize exception text/paths."""
from pathlib import Path

STAGES = {'fork_candidate', 'verify_source', 'register_baseline', 'baseline_complete'}


def annotate(error, *, stage, candidate_generated):
    if stage in STAGES:
        error.local_execution = {'stage': stage, 'candidate_generated': candidate_generated is True if candidate_generated is not None else None}


def describe(error):
    result = {'exception_type': type(error).__name__ if type(error).__module__ == 'builtins' else 'ProjectException'}
    for key in ('errno', 'winerror'):
        value = getattr(error, key, None)
        if type(value) is int: result[key] = value
    local = getattr(error, 'local_execution', {})
    if local.get('stage') in STAGES:
        # A fork that did not return may still have created files. Unknown is
        # deliberately distinct from an affirmative no-side-effects claim.
        result.update(stage=local['stage'], candidate_generated=local.get('candidate_generated')
                      if type(local.get('candidate_generated')) is bool else None)
    persistence = getattr(error, 'json_persistence', {})
    if persistence.get('phase') in {'create_directory', 'serialize', 'write_temporary', 'replace'}:
        result['persistence'] = {key: persistence[key] for key in ('phase', 'replace_attempts', 'target') if key in persistence}
        result['persistence']['replacement_confirmed'] = False
    if isinstance(error, OSError):
        result['category'] = ('sharing_violation' if result.get('winerror') in {32, 33}
            else 'access_denied_or_sharing' if result.get('winerror') == 5
            else 'disk_full' if result.get('errno') == 28 or result.get('winerror') in {39, 112}
            else 'path_unavailable' if result.get('errno') in {2, 20} else 'local_io_error')
    frames = []
    trace = error.__traceback__
    root = Path(__file__).resolve().parent
    while trace:
        code = trace.tb_frame.f_code
        path = Path(code.co_filename).resolve()
        if path.parent == root and code.co_name.isidentifier():
            frames.append({'module': path.stem, 'function': code.co_name, 'line': trace.tb_lineno})
        trace = trace.tb_next
    result['frames'] = frames[-6:]
    return result
