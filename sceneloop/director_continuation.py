"""Two bounded, OS-user protected native continuation slots per Director parent.

Only ciphertext reaches disk. SQL holds public projections and claim references;
decrypted Provider fields are used solely for the original native continuation.
This calls the existing protector without opening any credential repository.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import uuid

from .credentials import SecretProtector, MAX_KEY_LENGTH
from .director_contract import digest, reject

MAX_PAYLOAD = 256 * 1024
MAX_FILE = 4 * 1024 * 1024
MAX_CHUNKS = (4 * ((MAX_PAYLOAD + 2) // 3) + MAX_KEY_LENGTH - 1) // MAX_KEY_LENGTH


def public_assistant(assistant):
    value = {k: copy.deepcopy(assistant[k]) for k in ('role', 'content') if k in assistant}
    if 'tool_calls' in assistant:
        value['tool_calls'] = [
            {k: copy.deepcopy(call[k]) for k in ('id', 'type') if k in call} |
            {'function': {k: copy.deepcopy(call['function'][k]) for k in ('name', 'arguments')
                          if k in call['function']}}
            for call in assistant['tool_calls']]
    return value


class Continuation:
    def __init__(self, directory, project_id, parent_id):
        self.directory = Path(directory) / 'director-private'
        self.project_id = project_id
        self.parent_id = parent_id
        self.protector = SecretProtector()

    def _path(self, slot):
        if slot not in {'response', 'exchange'}:
            raise ValueError('invalid slot')
        # Reject filesystem redirection, including Windows junctions.
        for p in (self.directory, *self.directory.parents):
            if p.is_symlink() or getattr(p, 'is_junction', lambda: False)():
                raise ValueError('redirected checkpoint')
        path = self.directory / (slot + '.json')
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('redirected checkpoint')
        return path

    def _binding(self, decision):
        if decision['parent_id'] != self.parent_id:
            raise ValueError('wrong parent claim')
        return {'project_id': self.project_id, 'parent_id': self.parent_id,
                'decision_id': decision['decision_id'], 'event_id': decision['event_id']}

    def save(self, decision, assistant, slot, *, tools=None):
        temporary = None
        try:
            binding = self._binding(decision)
            payload = json.dumps({'version': 1, 'binding': binding, 'assistant': assistant, 'tools': tools or []},
                                 ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(payload) > MAX_PAYLOAD:
                raise ValueError('checkpoint capacity')
            text = base64.b64encode(payload).decode('ascii')
            chunks = [self.protector.protect(text[i:i + MAX_KEY_LENGTH])
                      for i in range(0, len(text), MAX_KEY_LENGTH)]
            cipher = json.dumps({'version': 1, 'chunks': chunks}, separators=(',', ':')).encode('ascii')
            if len(cipher) > MAX_FILE:
                raise ValueError('cipher capacity')
            path = self._path(slot)
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.directory / (uuid.uuid4().hex + '.tmp')
            with temporary.open('xb') as output:
                output.write(cipher)
                output.flush()
                import os
                os.fsync(output.fileno())
            temporary.replace(path)
            return {**binding, 'slot': slot, 'cipher_sha256': hashlib.sha256(cipher).hexdigest()}
        except Exception:
            reject('原Provider续接状态无法保护；保留原claim并暂停，不能重发。',
                   'director_continuation_unavailable', 409)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def load(self, reference, decision, *, tools=None):
        try:
            binding = self._binding(decision)
            if not isinstance(reference, dict) or any(reference.get(k) != v for k, v in binding.items()):
                raise ValueError('wrong checkpoint scope')
            path = self._path(reference['slot'])
            with path.open('rb') as source:
                cipher = source.read(MAX_FILE + 1)
            if len(cipher) > MAX_FILE or hashlib.sha256(cipher).hexdigest() != reference['cipher_sha256']:
                raise ValueError('changed checkpoint')
            envelope = json.loads(cipher)
            chunks = envelope['chunks']
            if envelope['version'] != 1 or not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_CHUNKS:
                raise ValueError('invalid checkpoint')
            raw = base64.b64decode(''.join(self.protector.unprotect(chunk) for chunk in chunks), validate=True)
            if len(raw) > MAX_PAYLOAD:
                raise ValueError('checkpoint capacity')
            value = json.loads(raw)
            if value['version'] != 1 or value['binding'] != binding:
                raise ValueError('wrong protected identity')
            if value['tools'] != (tools or []):
                raise ValueError('changed native tool pairing')
            assistant = value['assistant']
            if digest(public_assistant(assistant)) != digest(decision['assistant']):
                raise ValueError('wrong public projection')
            return assistant
        except Exception:
            reject('原Provider续接状态缺失或不可验证；保留原claim并暂停，不能重发。',
                   'director_continuation_unavailable', 409)

    def verified_response(self, reference, decision):
        """Check original protected-file integrity without decrypting Provider data."""
        try:
            binding=self._binding(decision)
            if (not isinstance(reference,dict) or reference.get('slot')!='response'
                or any(reference.get(k)!=v for k,v in binding.items())):return False
            with self._path('response').open('rb') as source:cipher=source.read(MAX_FILE+1)
            return bool(cipher and len(cipher)<=MAX_FILE and hashlib.sha256(cipher).hexdigest()==reference.get('cipher_sha256'))
        except (OSError,ValueError,KeyError,TypeError):return False
