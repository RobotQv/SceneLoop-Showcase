"""Narrow, conservative native dependency qualification; never open resource paths."""
import hashlib
import json
from pathlib import Path
import time

SCHEMA=1
METHOD='native_library_metadata_preflight_and_scene_v1'
MAX_BLEND=256*1024*1024
RESOURCE_TYPES=('libraries','images','fonts','sounds','movieclips','cache_files','volumes','textures','texts','particles','pointclouds')

def sha(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''): value.update(chunk)
    return value.hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def inspector_identity():
    return digest({'helper':sha(Path(__file__)),'worker':sha(Path(__file__).with_name('blender_worker.py')),
                   'adapter':sha(Path(__file__).with_name('blender_adapter.py')),'schema':SCHEMA})

def valid(proof, binding):
    try:
        measured=proof['measurement']
        return (proof['binding']==binding and measured['schema_version']==SCHEMA and measured['method']==METHOD
                and type(measured['eligible']) is bool and proof['digest']==digest({'binding':binding,'measurement':measured}))
    except (ValueError,KeyError,TypeError): return False

def verify_reference(dependencies, source, state):
    from .director_contract import reject
    if not state or not isinstance(dependencies,dict) or not dependencies.get('proof_id'):
        reject('旧缓存没有实际依赖资格证明。','observation_dependency_proof_invalid',409)
    with state._connection() as c:
        row=c.execute('SELECT document FROM observation_dependency_proofs WHERE proof_id=?',(dependencies['proof_id'],)).fetchone()
    try: proof=json.loads(row[0]) if row else None
    except (ValueError,TypeError): proof=None
    binding={'project_id':source['project_id'],'blend_sha256':source['blend_sha256'],'plan_sha256':source['plan_sha256'],
             'renderer_digest':digest(source['renderer']),'inspector_digest':inspector_identity()}
    if (not valid(proof,binding) or not proof['measurement']['eligible']
        or dependencies.get('proof_digest')!=proof['digest'] or dependencies.get('proof_id')!=digest(binding)):
        reject('缓存依赖证明缺失/损坏/来源或检查器已变，不能继续使用。','observation_dependency_proof_invalid',409)

def inventory(path):
    import bpy
    size=Path(path).stat().st_size
    if size>MAX_BLEND: return {'eligible':False,'reasons':['blend_capacity_unknown'],'resources':{}}
    resources={}; complete=True; images=[]
    # Leave every destination empty: inspect the main file's datablock inventory
    # without linking/appending IDs or following library/image resource paths.
    # Blender itself handles its native compression; no host dependency is added.
    with bpy.data.libraries.load(str(path),link=False) as (source,destination):
        for name in RESOURCE_TYPES:
            values=getattr(source,name,None)
            if not isinstance(values,list): complete=False
            else: resources[name]=len(values)
        # Image IDs are loaded as metadata from this main .blend only. Never
        # call images.load(), pixels, reload(), or any resource filename.
        # Factory files retain VIEWER IDs; names alone cannot establish that.
        if complete and not any(count for name,count in resources.items() if name!='images'):
            destination.images=list(source.images)
            images=destination.images
    reasons=[]
    if not complete: reasons.append('incomplete_or_unknown_native_inventory')
    if any(count for name,count in resources.items() if name!='images'):
        reasons.append('resource_datablock_requires_external_or_packed_qualification')
    elif resources.get('images') and (len(images)!=resources['images'] or any(image is None or image.source!='VIEWER' for image in images)):
        reasons.append('non_viewer_image_resource_unqualified')
    resources['non_viewer_images']=sum(image is None or image.source!='VIEWER' for image in images)
    return {'eligible':not reasons,'reasons':reasons,'resources':resources}

def inspect_native(workspace):
    """Called inside the readonly worker, after no-external-resource preflight."""
    import bpy
    workspace=Path(workspace); blend=workspace/'scene.blend'; plan=workspace/'plan.json'
    before={'blend_sha256':sha(blend),'plan_sha256':sha(plan)}
    document=json.loads(plan.read_text(encoding='utf-8'));rows=document.get('assets',{}).get('instances',[])
    if rows:
        from asset_contract import registration
        registration(document['assets'],[row['id'] for row in document['objects']])
    measured=inventory(blend)
    if measured['eligible']:
        # No library/image/font/etc ID was present, so this cannot cause reads
        # of those external paths while opening the frozen source.
        bpy.ops.wm.open_mainfile(filepath=str(blend),load_ui=False,use_scripts=False)
        reasons=[]
        if bpy.data.libraries or any(image.source not in {'VIEWER'} for image in bpy.data.images):
            reasons.append('unexpected_native_resource')
        for scene in bpy.data.scenes:
            if scene.sequence_editor:
                strips=getattr(scene.sequence_editor,'strips',getattr(scene.sequence_editor,'sequences_all',None))
                if strips is None or len(strips): reasons.append('sequencer_unqualified')
            compositor=getattr(scene,'compositing_node_group',getattr(scene,'node_tree',None))
            if compositor and any(node.type not in {'R_LAYERS','COMPOSITE','VIEWER'} for node in compositor.nodes):
                reasons.append('compositor_unqualified')
        for tree in bpy.data.node_groups:
            if tree.bl_idname!='ShaderNodeTree': reasons.append('non_shader_node_tree_unqualified')
        trees=[mat.node_tree for mat in bpy.data.materials if mat.use_nodes]
        trees += [world.node_tree for world in bpy.data.worlds if world.use_nodes]
        trees += list(bpy.data.node_groups)
        if any(node.type=='SCRIPT' for tree in trees if tree for node in tree.nodes):
            reasons.append('shader_script_unqualified')
        risky={'NODES','FLUID','SMOKE','OCEAN','MESH_SEQUENCE_CACHE','PARTICLE_SYSTEM','PARTICLE_INSTANCE','DYNAMIC_PAINT'}
        for obj in bpy.data.objects:
            if any(modifier.type in risky for modifier in obj.modifiers) or getattr(obj,'particle_systems',()):
                reasons.append('dynamic_or_cached_geometry_unqualified')
            if obj.animation_data and obj.animation_data.drivers: reasons.append('driver_unqualified')
        if rows:
            from asset_runtime import inspect_assets
            from asset_contract import inspection
            actual=inspect_assets(bpy.context.scene,document)
            for value,row in zip(actual,rows):inspection(value,row)
            measured.update(asset_registration_digest=digest(document['assets']),asset_measurements=actual)
        measured.update(eligible=not reasons,reasons=sorted(set(reasons)))
    after={'blend_sha256':sha(blend),'plan_sha256':sha(plan)}
    if before!=after: measured.update(eligible=False,reasons=['source_changed_during_inspection'])
    return {'source':before,'dependency_inspection':{'schema_version':SCHEMA,'method':METHOD,**measured}}

def check_asset_measurements(measured, plan):
    from .director_contract import reject
    from .asset_contract import registration,inspection,AssetError
    rows=plan.get('assets',{}).get('instances',[])
    if not rows or not measured['eligible']:return
    try:
        registration(plan['assets'],[row['id'] for row in plan['objects']])
        actual=measured.get('asset_measurements')
        if measured.get('asset_registration_digest')!=digest(plan['assets']) or not isinstance(actual,list) or len(actual)!=len(rows):
            raise AssetError('Missing actual static asset proof')
        for value,row in zip(actual,rows):inspection(value,row)
    except (AssetError,KeyError,TypeError,ValueError):
        reject('静态资产缺少同源实际身份/来源/几何资格证明。','observation_dependency_proof_invalid',409)

def qualify(workspace, project_id, adapter, renderer, *, state=None):
    """Trusted SQLite proof receipt. No existing inspection/JSON file is proof."""
    from .director_contract import reject
    workspace=Path(workspace)
    binding={'project_id':project_id,'blend_sha256':sha(workspace/'scene.blend'),'plan_sha256':sha(workspace/'plan.json'),
             'renderer_digest':digest(renderer),'inspector_digest':inspector_identity()}
    key=digest(binding); proof=None
    if state is not None:
        with state._connection() as c:
            row=c.execute('SELECT document FROM observation_dependency_proofs WHERE proof_id=?',(key,)).fetchone()
        if row:
            try:
                proof=json.loads(row[0])
                if not valid(proof,binding): raise ValueError()
            except (ValueError,KeyError,TypeError):
                reject('依赖证明损坏/旧版本/跨来源，不能继续缓存。','observation_dependency_proof_invalid',409)
    if proof is None:
        inspector=getattr(adapter,'inspect_dependencies',None)
        if not callable(inspector): reject('没有真实依赖检查入口，缓存资格未知。','observation_dependencies_unknown',409)
        before_files={p for p in workspace.iterdir() if p.is_file()}
        log=workspace/'blender.log'; old_log=log.read_bytes() if log.exists() else None
        started=time.monotonic()
        try: result=inspector(workspace)
        finally:
            if log.exists():
                trace=workspace/('dep-'+key[:16]+'.log')
                if not trace.exists(): trace.write_bytes(log.read_bytes())
                if old_log is not None: log.write_bytes(old_log)
                else: log.unlink()
        measured=result.get('dependency_inspection')
        if (result.get('source')!={k:binding[k] for k in ('blend_sha256','plan_sha256')}
            or sha(workspace/'scene.blend')!=binding['blend_sha256'] or sha(workspace/'plan.json')!=binding['plan_sha256']
            or not isinstance(measured,dict) or measured.get('schema_version')!=SCHEMA or measured.get('method')!=METHOD
            or type(measured.get('eligible')) is not bool):
            reject('实际依赖检查的来源或协议不可确认。','observation_dependency_proof_invalid',409)
        check_asset_measurements(measured,json.loads((workspace/'plan.json').read_text(encoding='utf-8')))
        proof={'binding':binding,'measurement':measured,'digest':digest({'binding':binding,'measurement':measured}),
               'checks':1,'seconds':time.monotonic()-started,
               'new_file_bytes':sum(p.stat().st_size for p in workspace.iterdir() if p.is_file() and p not in before_files)}
        if state is not None:
            with state._write() as c:
                c.execute('INSERT INTO observation_dependency_proofs VALUES (?,?)',(key,json.dumps(proof,ensure_ascii=False)))
    plan=json.loads((workspace/'plan.json').read_text(encoding='utf-8'));rows=plan.get('assets',{}).get('instances',[])
    check_asset_measurements(proof['measurement'],plan)
    if not proof['measurement']['eligible']:
        reject('实际blend依赖为外部/打包/未知资源，本阶段不具备缓存资格。','observation_dependencies_unknown',409)
    return {'proof_id':key,'proof_digest':proof['digest'],'inspector_digest':binding['inspector_digest'],
            'method':METHOD,'kind':'native_measured_static_assets_no_resource_dependencies' if rows else 'native_measured_no_resource_dependencies'}
