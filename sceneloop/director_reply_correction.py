"""Read-only final-report correction; count original SQL decisions, not a resettable counter."""
import json

REPLY_TOOLS = {'director_reply', 'director_clarify'}
DENIED_TOOLS = {'delegate_task', 'director_observe', 'director_goal_plan', 'director_request_scope', 'request_resources'}
ERRORS = {'director_result_reference_invalid', 'director_invalid', 'invalid_arguments_json', 'invalid_arguments_structure'}


def known_result(store, parent, facts):
    result = facts.get('result') or {}
    pause = store.task().get('pause') or {}
    return (not parent.get('cancel_requested') and not parent.get('director_ledger_unknown')
        and pause.get('reason') == 'child_requires_review' and pause.get('scope') == 'autonomous'
        and pause.get('evidence', {}).get('result_id') == result.get('result_id')
        and result.get('status') in {'failed', 'completed'}
        and (not result.get('error') or result['error'].get('outcome_unknown') is False)
        and not any(row.get('unknown') for row in result.get('evaluations', [])))


def window(store, parent, facts):
    if not known_result(store, parent, facts): return None
    with store.state._connection() as connection:
        records = [json.loads(row[0]) for row in connection.execute('SELECT document FROM director_decisions')]
    records = sorted((row for row in records if row.get('parent_id') == parent['id']
        and row.get('input', {}).get('result_id') == facts['result']['result_id']
        and row.get('input', {}).get('report_pause_id') == store.task()['pause']['pause_id']
        and row.get('state') == 'applied'), key=lambda row: row['call_index'])
    start = None
    for index, row in enumerate(records):
        outcomes = row.get('batch', {}).get('outcomes', [])
        assistant = row.get('assistant') or {}
        calls = assistant.get('tool_calls') or []
        if any(outcome.get('decision') or outcome.get('child_id') or outcome.get('resource_request')
               or outcome.get('metadata') or outcome.get('observation') for outcome in outcomes): return None
        if start is None:
            for call, outcome in zip(calls, outcomes):
                feedback = json.loads(outcome.get('tool', {}).get('content', '{}'))
                if call.get('function', {}).get('name') in REPLY_TOOLS and (feedback.get('error') or {}).get('code') in ERRORS:
                    start = index
                    break
    if start is None: return None
    # First malformed report is the original call, followed by at most two new calls.
    return {'attempts': len(records) - start - 1, 'remaining': max(0, 3 - (len(records) - start))}
