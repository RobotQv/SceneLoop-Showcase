"""Trusted image-to-shape local process adapter. Never auto-replays a claimed launch."""
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .asset_provider import Asset3DProvider, AssetJobError, Capability, LocalArtifact, ProviderReply, digest
from .asset_jobs import controlled_path, file_facts
from .hunyuan_process import atomic_json, read_json, process_identity, same_process, terminate_original

MODEL = "hunyuan3d-2mini"
PARAMETERS = {"seed": {"type": "integer", "min": 0, "max": 2**31-1}}


class HunyuanProvider(Asset3DProvider):
    def __init__(self, project_root, *, runner=None, python=None, job_timeout=1800, monitor=None):
        self.root = Path(project_root)
        self.runner = Path(runner) if runner else Path(__file__).with_name("hunyuan_runner.py")
        self.python = Path(python) if python else None
        self.test_runner = runner is not None
        self.timeout = job_timeout
        self.monitor = monitor

    def capabilities(self, profile):
        if (profile.get("provider_id") != "hunyuan" or profile.get("model") != MODEL
                or profile.get("mode") != "local_process" or profile.get("credential_mode") != "none"):
            raise AssetJobError("unsupported_asset_capability")
        if not self.test_runner:
            for name in ("python_path", "model_dir", "repo_dir"):
                value = profile.get(name)
                if (not isinstance(value, str) or not Path(value).is_absolute()
                        or value.startswith(("\\\\", "//")) or ".." in Path(value).parts):
                    raise AssetJobError("asset_local_configuration_invalid")
        return Capability("hunyuan", MODEL, "local_process", ("image",), PARAMETERS,
                          "local-mini-shape-v1", "fixed installed 2mini standard entry", "2026-10-17",
                          "mock-tested", True)

    def validate(self, profile, input_descriptor, parameters):
        cap = self.capabilities(profile)
        cap.validate_parameters(parameters)
        if input_descriptor.get("kind") != "image":
            raise AssetJobError("asset_local_image_required")
        return {"model": MODEL, "input_kind": "image", "input_sha256": input_descriptor["sha256"],
                "steps": 32, "guidance": 5, "octree": 256, "chunks": 4000,
                "seed": parameters.get("seed", 48193), "dtype": "float16", "device": "cuda",
                "cpu_threads": 4}

    def directory(self, identity):
        return controlled_path(self.root, f".sceneloop/asset-jobs/{identity.job_id}/local")

    def upload_input(self, identity, profile, credential, input_path):
        raise AssetJobError("asset_local_upload_forbidden")

    def preflight(self):
        from .hunyuan_process import gpu_slot, resources
        from .hunyuan_runner import resource_admission
        try:
            with gpu_slot():
                snapshot = resources()
        except AssetJobError as error:
            return {"monitor": "unavailable", "code":error.code}, error.code
        return snapshot, resource_admission(snapshot)

    def submit(self, identity, profile, credential, wire, *, admission=None):
        self.capabilities(profile)
        if credential is not None or identity.stage != "create" or not 30 <= self.timeout <= 7200:
            raise AssetJobError("asset_local_configuration_invalid")
        directory = self.directory(identity)
        if directory.exists():
            raise AssetJobError("asset_local_launch_already_claimed")
        from .local_resource_warning import normal, validate
        admission = validate(admission if admission is not None else normal(self, identity, profile, wire),
                             identity, profile, wire)
        source = controlled_path(self.root, f".sceneloop/asset-jobs/{identity.job_id}/input/source")
        raw, facts = file_facts(source, 8*1024*1024)
        if facts["sha256"] != wire["input_sha256"] or wire.get("model") != MODEL:
            raise AssetJobError("asset_local_input_changed")
        directory.mkdir(parents=True, exist_ok=False)
        nonce = uuid.uuid4().hex
        python = self.python or Path(profile["python_path"])
        repo, weights = Path(profile["repo_dir"]), Path(profile["model_dir"])
        if not self.test_runner and (not python.is_file() or not (repo / "hy3dgen/shapegen").is_dir()
                or not (weights / "model.fp16.safetensors").is_file() or not (weights / "config.yaml").is_file()):
            raise AssetJobError("asset_local_environment_missing")
        manifest = {"schema": 1, "identity": asdict(identity), "nonce": nonce, "wire": wire,
                    "resource_admission": admission,
                    "input": str(source.resolve()), "input_sha256": facts["sha256"],
                    "repo": str(repo), "weights": str(weights), "python": str(python),
                    "runner_sha256": hashlib.sha256(self.runner.read_bytes()).hexdigest(),
                    "host_process": process_identity(os.getpid()),
                    "timeout_seconds": self.timeout, "output": str(directory.resolve()),
                    "created_at": time.time()}
        atomic_json(directory / "manifest.json", manifest)
        # Claim directory is never removed, even if process creation/ACK fails.
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
                                          "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMDRIVE"}}
        environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        with (directory / "process.log").open("xb") as log:
            child = subprocess.Popen([str(python), "-I", "-B", str(self.runner), "--manifest",
                                      str((directory / "manifest.json").resolve())],
                                     cwd=str(directory), env=environment, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=0x08000000 if os.name == "nt" else 0)
        birth = process_identity(child.pid)
        if birth is None:
            raise AssetJobError("asset_local_launch_outcome_unknown")
        atomic_json(directory / "launched.json", {"nonce": nonce, "process": birth,
                                                "manifest_digest": digest(manifest)})
        return ProviderReply(identity, task_id=identity.job_id, model=MODEL, input_kind="image")

    def _records(self, identity):
        directory = self.directory(identity)
        manifest = read_json(directory / "manifest.json")
        frozen = manifest["identity"]
        if any(frozen.get(key) != getattr(identity, key) for key in ("project_id", "job_id", "semantic_digest", "wire_digest")):
            raise AssetJobError("asset_local_identity_conflict")
        if identity.stage != "create" and identity.task_id != identity.job_id:
            raise AssetJobError("asset_local_identity_conflict")
        launch_path = directory / "launched.json"
        if launch_path.exists():
            launch = read_json(launch_path)
        else:
            # Parent may have died after spawn and before its ACK was saved.
            # Adopt only the runner's original nonce/birth evidence, never spawn.
            start = self._runner(directory,manifest,None)
            if (directory/'terminal.json').exists():
                self._terminal(directory,manifest)
            else:
                actual = process_identity(start['process']['pid'])
                if not same_process(actual,start['process']) or not actual['alive']:
                    raise AssetJobError('asset_local_process_outcome_unknown')
            candidates=[start['process'],*start['ancestors']]
            launcher=next(item for item in candidates if Path(item['executable']).resolve()==Path(manifest['python']).resolve())
            launch = {'process':launcher,'nonce':manifest['nonce'],'manifest_digest':digest(manifest),
                      'recovered_from':'runner_birth'}
            atomic_json(launch_path,launch)
        if launch["nonce"] != manifest["nonce"] or launch["manifest_digest"] != digest(manifest):
            raise AssetJobError("asset_local_identity_conflict")
        if same_process(launch['process'],manifest.get('host_process',{})):
            raise AssetJobError('asset_local_identity_conflict')
        return directory, manifest, launch

    @staticmethod
    def _runner(directory,manifest,launch):
        start=read_json(directory/'running.json')
        if start.get('nonce')!=manifest['nonce'] or start.get('manifest_digest')!=digest(manifest):
            raise AssetJobError('asset_local_identity_conflict')
        chain=[start['process'],*start.get('ancestors',[])]
        if same_process(start['process'],manifest['host_process']) or (launch and same_process(launch['process'],manifest['host_process'])):
            raise AssetJobError('asset_local_identity_conflict')
        expected=launch['process'] if launch else manifest['host_process']
        index=next((i for i,item in enumerate(chain) if same_process(item,expected)),None)
        limit=(index+1 if launch else index) if index is not None else 0
        if index is None or not any(Path(item['executable']).resolve()==Path(manifest['python']).resolve()
                                    for item in chain[:limit]):
            raise AssetJobError('asset_local_identity_conflict')
        return start

    @staticmethod
    def _terminal(directory, manifest):
        state = read_json(directory / 'terminal.json')
        if (state.get('nonce') != manifest['nonce'] or state.get('manifest_digest') != digest(manifest)
                or state.get('status') not in {'succeeded','failed','cancelled'}):
            raise AssetJobError('asset_local_identity_conflict')
        return state

    def resolve(self, reference, identity):
        """Trusted receipt for the ORIGINAL launch; no spawn or new attempt."""
        if reference != "local:" + identity.job_id or identity.stage != "create":
            raise AssetJobError("asset_receipt_unavailable")
        directory, manifest, launch = self._records(identity)
        if manifest["identity"] != asdict(identity):
            raise AssetJobError("asset_local_identity_conflict")
        expected=launch['process']
        running = directory / "running.json"
        if running.exists():
            expected=self._runner(directory,manifest,launch)['process']
        if (directory/'terminal.json').exists():
            self._terminal(directory,manifest)
            return ProviderReply(identity,task_id=identity.job_id,model=MODEL,input_kind='image')
        actual = process_identity(expected['pid'])
        if not same_process(actual, expected):
            if not (directory / "terminal.json").exists():
                raise AssetJobError("asset_local_process_outcome_unknown")
            self._terminal(directory,manifest)
        return ProviderReply(identity, task_id=identity.job_id, model=MODEL, input_kind="image")

    def query(self, identity, profile, credential):
        self.capabilities(profile)
        directory, manifest, launch = self._records(identity)
        terminal = directory / "terminal.json"
        if terminal.exists():
            state = self._terminal(directory,manifest)
            status = state["status"]
            if status not in {"succeeded", "failed", "cancelled"}:
                raise AssetJobError("asset_local_terminal_invalid")
            output = None
            if status == "succeeded":
                raw, facts = file_facts(controlled_path(self.root, f".sceneloop/asset-jobs/{identity.job_id}/local/raw.glb"),
                                        32*1024*1024)
                if facts != state["artifact"]:
                    raise AssetJobError("asset_local_artifact_changed")
                output = {"artifact_id": identity.job_id, "format":"glb"}
            return ProviderReply(identity, task_id=identity.task_id, remote_status=status,
                                 output=output, output_availability="available" if output else "unknown",
                                 model=MODEL, input_kind="image")
        expected=(self._runner(directory,manifest,launch)['process']
                  if (directory/'running.json').exists() else launch['process'])
        actual = process_identity(expected['pid'])
        if not same_process(actual, expected) or not actual["alive"]:
            raise AssetJobError("asset_local_process_outcome_unknown")
        if time.time() > manifest["created_at"] + manifest["timeout_seconds"]:
            return self._cancel(identity, profile, directory, manifest, launch, reason="local_job_timeout")
        # Monitoring errors are observations, never a reason to terminate.
        if self.monitor is not None:
            try:
                self.monitor()
            except Exception:
                pass
        return ProviderReply(identity, task_id=identity.task_id, remote_status="running",
                             model=MODEL, input_kind="image")

    def _cancel(self, identity, profile, directory, manifest, launch, *, reason):
        atomic_json(directory/'cancel-request.json',{'nonce':manifest['nonce'],
                    'manifest_digest':digest(manifest),'reason':reason})
        until=time.monotonic()+5
        while not (directory/'running.json').exists() and time.monotonic()<until:
            if (directory/'terminal.json').exists():return self.query(identity,profile,None)
            time.sleep(.02)
        if not (directory/'running.json').exists():
            raise AssetJobError('asset_local_process_outcome_unknown')
        runner=self._runner(directory,manifest,launch)['process']
        actual=process_identity(runner['pid'])
        if not same_process(actual,runner) or not actual['alive']:
            if (directory/'terminal.json').exists():return self.query(identity,profile,None)
            raise AssetJobError('asset_local_process_outcome_unknown')
        terminate_original(runner)
        if not same_process(runner,launch['process']):
            # The launcher often exits by itself immediately after the child.
            # The same-handle birth guard handles that race without a bare kill.
            terminate_original(launch['process'])
        if (directory / "terminal.json").exists():
            return self.query(identity, profile, None)
        atomic_json(directory / "terminal.json", {"nonce": manifest["nonce"],
                    "manifest_digest": digest(manifest), "status": "cancelled", "reason": reason})
        return ProviderReply(identity, task_id=identity.task_id, remote_status="cancelled",
                             model=MODEL, input_kind="image")

    def cancel(self, identity, profile, credential):
        self.capabilities(profile)
        directory, manifest, launch = self._records(identity)
        if (directory / "terminal.json").exists():
            return self.query(identity, profile, credential)
        return self._cancel(identity, profile, directory, manifest, launch, reason="human_cancelled")

    def fetch_result(self, identity, profile, credential, output):
        directory, manifest, launch = self._records(identity)
        terminal = self._terminal(directory,manifest)
        if terminal.get("status") != "succeeded" or terminal.get("nonce") != manifest["nonce"]:
            raise AssetJobError("asset_local_artifact_unavailable")
        path = f".sceneloop/asset-jobs/{identity.job_id}/local/raw.glb"
        raw, facts = file_facts(controlled_path(self.root, path), 32*1024*1024)
        if facts != terminal.get("artifact") or output != {"artifact_id":identity.job_id,"format":"glb"}:
            raise AssetJobError("asset_local_artifact_changed")
        incoming = f".sceneloop/asset-jobs/{identity.job_id}/incoming/raw.glb"
        target = controlled_path(self.root, incoming)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_facts(target, 32*1024*1024)[1] != facts:
                raise AssetJobError("asset_local_artifact_changed")
        else:
            with target.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return LocalArtifact(identity, output["artifact_id"], incoming)

    def progress(self, job):
        """Read only bound runner facts. No probe, spawn, adoption or fake percent."""
        if job['dispatch'] == 'outcome_unknown':
            return {'stage':'outcome_unknown','evidence':'job_sql'}
        if job['dispatch'] == 'prepared':
            return {'stage':'queued','evidence':'job_sql'}
        if job['preparation'] == 'ready':
            return {'stage':'ready','evidence':'job_sql'}
        result = {'stage':'starting' if job['dispatch']=='submitting' else 'processing','evidence':'job_sql'}
        directory = controlled_path(self.root,f".sceneloop/asset-jobs/{job['job_id']}/local")
        try:
            manifest = read_json(directory/'manifest.json')
            if any(manifest['identity'].get(key) != job[value] for key,value in
                   (('project_id','project_id'),('job_id','job_id'),('semantic_digest','semantic_digest'),('wire_digest','final_wire_digest'))):
                raise AssetJobError('asset_local_identity_conflict')
            if (directory/'terminal.json').exists():
                state = self._terminal(directory,manifest)
                result = {'stage':{'succeeded':'completed','failed':'failed','cancelled':'cancelled'}[state['status']],
                          'evidence':'runner_terminal'}
                category = state.get('category') or state.get('reason')
                safe_categories = {'local_job_timeout','human_cancelled','runner_interrupted','cuda_oom',
                    'cpu_memory_failure','system_resource_failure','local_generation_failed','local_gpu_busy',
                    'local_resource_monitor_unavailable','local_system_memory_insufficient','local_gpu_memory_insufficient',
                    'local_asset_contract_rejected'}
                if category in safe_categories: result['reason_code'] = category
                if state.get('diagnostic'):
                    from .hunyuan_diagnostics import public
                    result['diagnostic']=public(state['diagnostic'])
                elif state.get('error_type'):
                    from .hunyuan_diagnostics import public
                    result['diagnostic']=public({'exception_type':state['error_type']})
            elif (directory/'progress.json').exists():
                state = read_json(directory/'progress.json')
                if (state.get('nonce') != manifest['nonce'] or state.get('manifest_digest') != digest(manifest)
                        or state.get('stage') not in {'loading_model','model_loaded','inference','exporting_raw'}):
                    raise AssetJobError('asset_local_identity_conflict')
                result = {'stage':state['stage'],'evidence':'runner_progress'}
                if type(state.get('elapsed')) in (int,float) and 0 <= state['elapsed'] < 86400:
                    result['elapsed_seconds'] = state['elapsed']
        except (OSError,ValueError,KeyError,TypeError):
            result['monitoring'] = 'unavailable'
        return result
