"""Fixed trusted offline Hunyuan 2mini image runner. Not model-generated code."""
import argparse
import hashlib
import math
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sceneloop.asset_provider import digest
from sceneloop.hunyuan_process import atomic_json, classify, gpu_slot, runner_birth, read_json, resources


def resource_admission(snapshot):
    """Compatibility advisory code; never an execution veto after host admission."""
    ram, commit, gpu = snapshot.get("ram"), snapshot.get("commit"), snapshot.get("gpu")
    if not ram or not commit or not gpu:
        return "local_resource_monitor_unavailable"
    if ram["available"] < 6 * 2**30 or commit["commit_limit"] - commit["commit_total"] < 8 * 2**30:
        return "local_system_memory_insufficient"
    try:
        first = gpu["memory_mib"].splitlines()[0].split(",")
        total, available = [float(item.strip()) for item in first[:2]]
        if not all(math.isfinite(item) for item in (total,available)) or not 0 <= available <= total:
            return "local_resource_monitor_unavailable"
    except (ValueError, IndexError, KeyError):
        return "local_resource_monitor_unavailable"
    if available < 6144:
        return "local_gpu_memory_insufficient"
    # WDDM lists desktop/browser PIDs and may redact names. That is not proof
    # of another heavy model. Free VRAM and the shared GPU lease govern admission;
    # the process list remains evidence with an explicit monitoring limitation.
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = read_json(manifest_path)
    output = manifest_path.parent
    if Path(manifest["output"]).resolve() != output:
        raise ValueError("manifest output differs")
    from sceneloop.hunyuan_provider import MODEL
    repo, weights, python = [Path(manifest[name]) for name in ("repo", "weights", "python")]
    if (not all(path.is_absolute() for path in (repo, weights, python))
            or Path(sys.executable).resolve() != python.resolve()
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != manifest["runner_sha256"]):
        raise ValueError("trusted environment identity differs")
    binding = {"nonce": manifest["nonce"], "manifest_digest": digest(manifest)}
    from types import SimpleNamespace
    from sceneloop.local_resource_warning import Admission, validate
    admitted = manifest.get('resource_admission') or {}
    admitted_profile = admitted.get('binding') or {}
    validate(Admission(admitted), SimpleNamespace(**manifest['identity']),
             {'id': admitted_profile.get('profile_id'), 'revision': admitted_profile.get('profile_revision')},
             manifest['wire'])
    atomic_json(output / "running.json", {**binding, **runner_birth()})
    finish_lock = threading.Lock()
    def terminal(status, **details):
        with finish_lock:
            if not (output / "terminal.json").exists():
                atomic_json(output / "terminal.json", {**binding, "status": status, **details})
    def deadline():
        terminal("failed", category="local_job_timeout", elapsed=time.monotonic() - started)
        os._exit(124)  # Only this dedicated job process; never a monitor-selected PID.
    started = time.monotonic()
    timeout = manifest["timeout_seconds"]
    if type(timeout) is not int or not 30 <= timeout <= 7200:
        raise ValueError("invalid job window")
    timer = threading.Timer(timeout, deadline)
    timer.daemon = True
    timer.start()
    stage='validating_input'
    try:
        def cancelled():
            path=output/'cancel-request.json'
            if not path.exists():return False
            value=read_json(path)
            if value.get('nonce')!=binding['nonce'] or value.get('manifest_digest')!=binding['manifest_digest']:
                raise ValueError('cancel identity differs')
            terminal('cancelled',category=value.get('reason') if value.get('reason') in
                     {'human_cancelled','local_job_timeout'} else 'human_cancelled',inference_started=False)
            return True
        if cancelled():return 0
        source = Path(manifest["input"])
        expected_input = output.parent / "input/source"
        if source.resolve() != expected_input.resolve() or hashlib.sha256(source.read_bytes()).hexdigest() != manifest["input_sha256"]:
            raise ValueError("fixed image changed")
        with gpu_slot():
            before = resources()
            atomic_json(output / "resources-before.json", before)
            if cancelled():return 0
            os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                              OMP_NUM_THREADS="4", MKL_NUM_THREADS="4")
            sys.path.insert(0, str(repo))
            def progress(stage):
                atomic_json(output/'progress.json', {**binding,'stage':stage,
                            'elapsed':time.monotonic()-started})
            stage='loading_model'
            progress('loading_model')
            # These heavyweight imports occur only in the explicitly launched runner.
            stage='importing_libraries'
            import torch
            from PIL import Image
            from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
            torch.set_num_threads(4)
            stage='constructing_pipeline'
            previous = torch.get_default_dtype()
            torch.set_default_dtype(torch.float16)
            try:
                pipe = Hunyuan3DDiTFlowMatchingPipeline.from_single_file(
                    ckpt_path=str(weights / "model.fp16.safetensors"), config_path=str(weights / "config.yaml"),
                    use_safetensors=True, device="cuda", dtype=torch.float16)
            finally:
                torch.set_default_dtype(previous)
            progress('model_loaded')
            stage='model_loaded'
            if cancelled():return 0
            wire = manifest["wire"]
            with Image.open(source) as opened:
                image = opened.convert("RGBA")
            progress('inference')
            stage='inference'
            mesh = pipe(image=image, num_inference_steps=32, guidance_scale=5,
                        octree_resolution=256, num_chunks=4000,
                        generator=torch.Generator(device="cuda").manual_seed(wire["seed"]),
                        output_type="trimesh")[0]
            # Freeze raw BEFORE connected-component filtering, cleanup or decimation.
            progress('exporting_raw')
            stage='exporting_raw'
            raw = mesh.export(file_type="glb")
            with (output / "raw.glb").open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            artifact = {"sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw)}
            atomic_json(output / "raw-facts.json", {**binding, **artifact, "unmodified": True,
                "vertices": len(mesh.vertices), "triangles": len(mesh.faces), "bounds": mesh.bounds.tolist(),
                "components_filtered": False, "cleanup": False, "derived": None,
                "model": MODEL, "wire": wire, "elapsed": time.monotonic() - started,
                "peak_vram_bytes": torch.cuda.max_memory_allocated()})
            from sceneloop.asset_contract import AssetError, parse_glb
            try:
                parse_glb(raw)
            except AssetError:
                # Raw remains byte-for-byte available for review. No automatic
                # component filtering, cleanup or target-ratio decimation.
                terminal('failed',category='local_asset_contract_rejected',artifact=artifact,inference_started=True)
                return 4
            terminal("succeeded", artifact=artifact, inference_started=True)
            return 0
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            terminal("cancelled", category="runner_interrupted")
        else:
            from sceneloop.hunyuan_diagnostics import describe
            diagnostic=describe(error,stage,time.monotonic()-started,
                [Path(__file__).parent,repo,Path(sys.prefix)/'Lib/site-packages'])
            terminal("failed", category=getattr(error, "code", None) or classify(error),
                     error_type=diagnostic['exception_type'],diagnostic=diagnostic)
        return 1
    finally:
        timer.cancel()
        try:
            atomic_json(output / "resources-after.json", resources())
        except Exception:
            pass  # Monitoring cannot change the confirmed terminal outcome.


if __name__ == "__main__":
    raise SystemExit(main())
