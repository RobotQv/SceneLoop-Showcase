"""M1 reads registered Working evidence; it does not pretend to be a general cache."""
import copy
import json
import os
import shutil
from pathlib import Path
import threading
import time
from .director_contract import digest, reject
from .observation_contract import normalize_view_spec, validate_observation_metadata, comparison_identity
from .project_runtime import file_sha256
from .observation_dependencies import qualify, verify_reference

_RENDER_LOCK=threading.Lock()

class ObservationCache:
    """Project SQLite receipts for controlled native, self-contained observations."""
    def __init__(self, manager, project_id, adapter):
        self.manager=manager; self.project_id=project_id; self.adapter=adapter
        self.store=manager._director_host().store(project_id); self.state=self.store.state
        with self.state._write() as c:
            c.execute('CREATE TABLE IF NOT EXISTS shared_observations (request_key TEXT PRIMARY KEY, document TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS director_observe_claims (claim_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS observation_consumers (consumer_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS observation_dependency_proofs (proof_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
    def source(self, workspace, view):
        workspace=Path(workspace)
        plan=json.loads((workspace/'plan.json').read_text(encoding='utf-8'))
        # The supported native constructors/edit protocol cannot introduce
        # external resources. Imports/assets and unknown dependency declarations
        # require a later explicit dependency manifest, never guessed provenance.
        self.manager.blender.validate_plan(plan)
        if any(os.environ.get(key) for key in ('OCIO','BLENDER_SYSTEM_DATAFILES','BLENDER_USER_DATAFILES',
                                               'BLENDER_SYSTEM_SCRIPTS','BLENDER_USER_SCRIPTS')):
            reject('外部Blender/色彩资源覆写没有冻结依赖清单，拒绝缓存。','observation_dependencies_unknown',409)
        if plan.get('scene_kind','parametric')!='parametric' or any(
                key in plan for key in ('external_dependencies','libraries','textures','resource_paths')):
            reject('观察缓存需要已证明自包含的原生场景；外部依赖尚未受控。','observation_dependencies_unknown',409)
        discovery=getattr(self.adapter,'discovery',{})
        executable=Path(discovery.get('path') or '')
        if not discovery.get('available') or not discovery.get('version') or not executable.is_file():
            reject('缺少可验证的Blender/renderer版本，不采用缓存。','observation_renderer_unknown',409)
        renderer={'blender_version':discovery['version'],'executable_sha256':file_sha256(executable),
                  'code':{p.name:file_sha256(p) for p in sorted(Path(__file__).parent.glob('*.py'))
                          if p.name.startswith(('blender_','observation_','geometry_','asset_','scene_','imported_','material_'))}}
        proof=qualify(workspace,self.project_id,self.adapter,renderer,state=getattr(self,'state',None))
        return {'schema_version':1,'project_id':self.project_id,'blend_sha256':file_sha256(workspace/'scene.blend'),
                'plan_sha256':file_sha256(workspace/'plan.json'),'assets_digest':digest(plan.get('assets',{})),
                'dependencies':proof,
                'renderer':renderer,'view':normalize_view_spec(view)}
    def resolve(self, record):
        if record['source']['project_id']!=self.project_id: reject('共享观察跨项目。','observation_scope_invalid',409)
        if record['request_key']!=digest(record['source']): reject('共享观察来源摘要无效。','observation_provenance_invalid',409)
        verify_reference(record['source'].get('dependencies'),record['source'],getattr(self,'state',None))
        actual=validate_observation_metadata(record['observation'],record['source']['view'])
        if not actual.get('camera_id') or not actual.get('render_settings_id'):
            reject('旧观察缺少实际camera/settings，不推造provenance。','observation_provenance_invalid',409)
        if record['identity']!=digest({'source':record['source'],'actual':actual,'png_sha256':record['ref']['sha256']}):
            reject('共享观察实际身份无效。','observation_provenance_invalid',409)
        owner=self.manager.get(record['ref']['run_id'])
        if owner.get('project_id')!=self.project_id: reject('观察artifact跨项目。','observation_scope_invalid',409)
        artifact=next((a for a in owner['artifacts'] if a['id']==record['ref']['artifact_id']),None)
        if not artifact or artifact['sha256']!=record['ref']['sha256']:
            reject('共享观察artifact身份失效。','observation_changed',409)
        path=self.manager._checked_artifact(owner,artifact)
        if path.read_bytes()[:8]!=b'\x89PNG\r\n\x1a\n': reject('观察不是PNG。','observation_changed',409)
        return path
    def find(self, source):
        key=digest(source)
        with self.state._connection() as c: row=c.execute('SELECT document FROM shared_observations WHERE request_key=?',(key,)).fetchone()
        if not row: return None
        record=json.loads(row[0]); self.resolve(record)
        if record['source']!=source: reject('共享观察请求来源冲突。','observation_provenance_invalid',409)
        return record
    def record(self, key):
        with self.state._connection() as c: row=c.execute('SELECT document FROM shared_observations WHERE request_key=?',(key,)).fetchone()
        if not row: reject('共享观察记录缺失。','observation_changed',409)
        record=json.loads(row[0]); self.resolve(record); return record
    def publish(self, source, owner, revision, item, observation):
        actual=validate_observation_metadata(observation,source['view'])
        if not actual.get('camera_id') or not actual.get('render_settings_id'):
            reject('未返回实际观察camera/settings。','observation_provenance_invalid',409)
        record={'source':source,'request_key':digest(source),'observation':actual,
            'ref':{'run_id':owner,'revision_id':revision,'artifact_id':item['id'],
                   'sha256':item['sha256'],'url':item['url']}}
        record['identity']=digest({'source':source,'actual':actual,'png_sha256':item['sha256']})
        self.resolve(record)
        with self.state._write() as c:
            c.execute('INSERT OR IGNORE INTO shared_observations VALUES (?,?)',(record['request_key'],json.dumps(record,ensure_ascii=False)))
        return record
    def builder_rendered(self, actor, record, size):
        consumer=digest({'actor':actor['id'],'render_artifact':record['ref']['artifact_id']})
        with self.state._write() as c:
            if not c.execute('INSERT OR IGNORE INTO observation_consumers VALUES (?,?)',
                (consumer,json.dumps({'run_id':actor['id'],'role':'builder','identity':record['identity'],'actual_render':True}))).rowcount: return
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            usage=task.setdefault('observation_usage',{}).setdefault('builder',{'renders':0,'cache_reuses':0,'bytes':0})
            usage['renders']+=1; usage['bytes']+=size
            c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task,ensure_ascii=False),))
    def consume(self, actor, record, *, hit, consumer_tag=None):
        role='director' if actor['operation']=='conversation' else 'builder'
        consumer=digest({'actor':actor['id'],'role':role,'identity':record['identity'],'tag':consumer_tag})
        with self.state._write() as c:
            if not c.execute('INSERT OR IGNORE INTO observation_consumers VALUES (?,?)',
                (consumer,json.dumps({'run_id':actor['id'],'role':role,'identity':record['identity'],'cache_hit':hit}))).rowcount: return
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            usage=task.setdefault('observation_usage',{}).setdefault(role,{'renders':0,'cache_reuses':0,'bytes':0})
            if hit: usage['cache_reuses']+=1
            c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task,ensure_ascii=False),))
    def claim(self, claim_id):
        with self.state._connection() as c: row=c.execute('SELECT document FROM director_observe_claims WHERE claim_id=?',(claim_id,)).fetchone()
        return json.loads(row[0]) if row else None
    def _write_claim(self, value):
        with self.state._write() as c:
            c.execute('INSERT OR REPLACE INTO director_observe_claims VALUES (?,?)',(value['claim_id'],json.dumps(value,ensure_ascii=False)))
    def observe(self, parent, version, view, claim_id, *, admission_source='autonomous'):
        source=self.source(version['workspace'],view); key=digest(source)
        previous=self.claim(claim_id)
        if previous:
            if previous['request_key']!=key: reject('原观察claim基线已变化。','observation_claim_conflict',409)
            if previous['state']=='returned': self.resolve(previous['record']); return previous
            self.store.pause('observation_outcome_unknown',{'claim_id':claim_id})
            reject('已准入观察结果未知，原claim不能重放。','observation_outcome_unknown',409)
        self.store.admission(admission_source)
        cached=self.find(source)
        if cached:
            value={'claim_id':claim_id,'parent_id':parent['id'],'state':'returned','request_key':key,
                   'record':cached,'cache_hit':True,'renders':0,'bytes':0,'seconds':0}
            self._write_claim(value); self.consume(parent,cached,hit=True,consumer_tag=claim_id); return value
        # Native fixed renderer remains 960x640/8-bit PNG. Reserve image plus
        # metadata/host trace room; an exact hit allocates no new files.
        reserve=960*640*4+2*1024*1024
        if shutil.disk_usage(version['workspace']).free<reserve:
            self.store.pause('observation_disk_space',{'requested_reserve_bytes':reserve})
            reject('观察磁盘空间不足，未提交新的render。','observation_storage_limit',409)
        with self.state._write() as c:
            task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            existing=[json.loads(row[0]) for row in c.execute('SELECT document FROM director_observe_claims')]
            occupied=sum(row.get('bytes',0) if row['state']=='returned' else row.get('reserve_bytes',0) for row in existing)
            if occupied+reserve>task['render_policy']['storage_limit_bytes']:
                limited=True
            else:
                limited=False
                value={'claim_id':claim_id,'parent_id':parent['id'],'state':'started','request_key':key,'reserve_bytes':reserve}
                c.execute('INSERT INTO director_observe_claims VALUES (?,?)',(claim_id,json.dumps(value)))
        if limited:
            self.store.pause('observation_storage_limit',{'used_or_reserved_bytes':occupied,'requested_reserve_bytes':reserve})
            reject('冻结观察存储额度不足，保留原结果。','observation_storage_limit',409)
        if not _RENDER_LOCK.acquire(blocking=False):
            self.store.pause('observation_in_progress',{'claim_id':claim_id})
            reject('已有观察在执行，保留claim待核实。','observation_outcome_unknown',409)
        directory=self.manager.run_directory(parent['id'])/'director-observations'/claim_id
        source_directory=Path(version['workspace'])
        before_files={p for p in source_directory.rglob('*') if p.is_file()}
        source_log=source_directory/'blender.log'
        old_log=source_log.read_bytes() if source_log.exists() else None
        start=time.monotonic()
        try:
            directory.mkdir(parents=True,exist_ok=False)
            # Native candidates bind their absolute workspace inside the .blend.
            # Observe the frozen source directly; only new diagnostic filenames
            # are generated. Preserve its previous log and every old artifact.
            result=self.adapter.observe(source_directory,normalize_view_spec(view),claim_id)
            if self.source(source_directory,view)!=source:
                reject('观察期间源文件变化。','observation_source_changed',409)
            actual=validate_observation_metadata(result,source['view'])
            item=self.manager._artifact(version['run']['id'],result.get('image_path',result.get('path')),'render',
                revision_id=version['revision_id'],view_id='director-diagnostic',observation_id=claim_id,observation=actual)
            record=self.publish(source,version['run']['id'],version['revision_id'],item,actual)
            if source_log.exists(): (directory/'blender.log').write_bytes(source_log.read_bytes())
            new_files=[p for p in source_directory.rglob('*') if p.is_file() and p not in before_files]
            size=sum(p.stat().st_size for p in new_files)+sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
            value.update(state='returned',record=record,cache_hit=False,renders=1,bytes=size,seconds=time.monotonic()-start)
            with self.state._write() as c:
                c.execute('UPDATE director_observe_claims SET document=? WHERE claim_id=?',(json.dumps(value),claim_id))
                task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
                usage=task.setdefault('observation_usage',{}).setdefault('director',{'renders':0,'cache_reuses':0,'bytes':0})
                usage['renders']+=1; usage['bytes']+=size
                c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task,ensure_ascii=False),))
            if size>reserve:
                self.store.pause('observation_storage_limit',{'claim_id':claim_id,'actual_bytes':size,'reserve_bytes':reserve})
            return value
        except Exception:
            self.store.pause('observation_outcome_unknown',{'claim_id':claim_id})
            reject('观察结果未确认，已保留原claim与资源占用，等待核实。','observation_outcome_unknown',409)
        finally:
            try:
                if source_log.exists():
                    if directory.exists() and not (directory/'blender.log').exists():
                        (directory/'blender.log').write_bytes(source_log.read_bytes())
                    if old_log is not None: source_log.write_bytes(old_log)
                    else: source_log.unlink()
            except OSError:
                self.store.pause('observation_io_failure',{'claim_id':claim_id})
            finally: _RENDER_LOCK.release()

def shared_baseline(manager, run, adapter):
    if not run.get('director_delegation') or not run.get('base_scene'): return [],[]
    binding=manager._director_host().store(run['project_id']).child(run['id'])
    if not binding: return [],[]
    session=manager._director_host().store(run['project_id']).session(binding['parent_id'])
    if not session or not session.get('observations'): return [],[]
    version=manager.scene_version(**run['base_scene'])
    cache=ObservationCache(manager,run['project_id'],adapter)
    refs=[]; images=[]
    for saved in session.get('observations',[])[-2:]:
        source=cache.source(version['workspace'],saved['source']['view'])
        record=cache.find(source)
        if not record: continue
        cache.consume(run,record,hit=True); refs.append(copy.deepcopy(record))
        images.extend([{'type':'text','text':'SHARED ORIGINAL WORKING OBSERVATION '+json.dumps(record['ref'])+
            ' mode='+record['observation']['observation_mode']},manager.providers.image_content(cache.resolve(record))])
    return refs,images

def director_images(manager,parent,view):
    store=manager._director_host().store(parent['project_id']); session=store.session(parent['id'])
    if not session or not session.get('observations'): return [],[]
    cache=ObservationCache(manager,parent['project_id'],manager.blender.create_adapter(mode='headless'))
    references=[]; content=[]
    version=manager.scene_version(view['selection']['run_id'],view['selection']['revision_id']) if view.get('selection') else None
    for record in session['observations']:
        path=cache.resolve(record)
        current=bool(version and record['source']==cache.source(version['workspace'],record['source']['view']))
        reference={'ref':record['ref'],'identity':record['identity'],'observation':record['observation'],'current_source':current}
        references.append(reference)
        content.extend([{'type':'text','text':'DIRECTOR OBSERVATION '+json.dumps(reference,ensure_ascii=False)},
                        manager.providers.image_content(path)])
    if len(references)==2:
        compatible=comparison_identity(references[0]['observation'])==comparison_identity(references[1]['observation'])
        for reference in references: reference['paired_comparison_compatible']=compatible
    return references,content

def working(manager, project_id):
    project=manager.get_project(project_id)
    selection=project.get('current_scene')
    view={'selection':copy.deepcopy(selection),'working_version':project['workspace']['working_version'],
          'project_revision':project['revision'],'images':[],'objects':[],'render_count':0}
    if not selection: return view,[]
    version=manager.scene_version(selection['run_id'],selection['revision_id'])
    if version['plan'].get('scene_kind','parametric')!='parametric':
        from .director_contract import reject
        reject('Director首阶段仅支持原生参数场景。','director_scene_unsupported',409)
    view.update(blend_sha256=file_sha256(version['files']['blend']),plan_sha256=file_sha256(version['files']['plan']),
        inspection_sha256=file_sha256(version['files']['inspection']),
        objects=copy.deepcopy(version['plan']['objects']),
        assets=copy.deepcopy(version['plan'].get('assets',{})),
        inspection=json.loads(version['files']['inspection'].read_text(encoding='utf-8')))
    if 'surface' in version['plan']:view['surface']=copy.deepcopy(version['plan']['surface'])
    content=[]
    for artifact in version['run']['artifacts']:
        if artifact.get('revision_id')!=selection['revision_id'] or artifact['kind']!='render' or artifact.get('view_id') not in {'main','auxiliary'}: continue
        path=manager._checked_artifact(version['run'],artifact)
        ref={'run_id':selection['run_id'],'revision_id':selection['revision_id'],'artifact_id':artifact['id'],
            'sha256':artifact['sha256'],'view_id':artifact['view_id'],
            'mode':(artifact.get('observation') or {}).get('observation_mode','appearance'),
            'observation':copy.deepcopy(artifact.get('observation')),'url':artifact['url']}
        view['images'].append(ref)
        content += [{'type':'text','text':'WORKING IMAGE '+json.dumps(ref,ensure_ascii=False)},manager.providers.image_content(path)]
    return view,content
