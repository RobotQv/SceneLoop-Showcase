"""Project-owned image bytes and immutable identities; never model file paths."""
import base64
import binascii
import copy
import hashlib
import json
import os
import re
import struct
import uuid
import zlib
from .project_state_store import _not_link

MAX_BYTES = 4 * 1024 * 1024
MAX_BODY = 6 * 1024 * 1024


def reject(message, code='project_reference_invalid', status=400):
    from .runtime import AppError
    raise AppError(message, code, status)


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        offset=8; size=None; has_data=False; ended=False
        while offset+12<=len(data):
            length=struct.unpack('>I',data[offset:offset+4])[0]
            kind=data[offset+4:offset+8]; end=offset+8+length
            if end+4>len(data):break
            raw=data[offset+8:end]
            if zlib.crc32(kind+raw)&0xffffffff != struct.unpack('>I',data[end:end+4])[0]:break
            if offset==8:
                if kind!=b'IHDR' or length!=13:break
                size=struct.unpack('>II',raw[:8])
            if kind==b'IDAT':has_data=True
            offset=end+4
            if kind==b'IEND':
                ended=length==0 and offset==len(data)
                break
        if ended and has_data and size and all(1<=v<=4096 for v in size):return 'image/png','.png'
    elif data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):
        offset=2;size=None
        while offset+4<len(data):
            if data[offset]!=255:break
            marker=data[offset+1];offset+=2
            if marker==255:offset-=1;continue
            length=int.from_bytes(data[offset:offset+2],'big')
            if length<2 or offset+length>len(data):break
            if marker in {0xc0,0xc1,0xc2} and length>=8:
                height,width=struct.unpack('>HH',data[offset+3:offset+7])
                if 1<=height<=4096 and 1<=width<=4096:size=(width,height)
            if marker==0xda:
                if size and offset+length<len(data)-2:return 'image/jpeg','.jpg'
                break
            offset+=length
    reject('只接受完整且有界的PNG/JPEG图片。')


def upload(manager, project_id, body):
    if (not isinstance(body,dict) or set(body)!={'purpose','media_type','content_base64'}
            or body['purpose'] not in ('scene','asset') or body['media_type'] not in ('image/png','image/jpeg')
            or not isinstance(body['content_base64'],str) or len(body['content_base64'])>MAX_BODY):
        reject('附件需要purpose、media_type和content_base64，不接受文件路径。')
    try:data=base64.b64decode(body['content_base64'],validate=True)
    except (ValueError,binascii.Error):reject('图片编码无效。')
    if not 1<=len(data)<=MAX_BYTES:reject('图片需为1字节到4MiB。')
    media,extension=image_type(data)
    if media!=body['media_type']:reject('图片实际格式与声明不一致。')
    manager.get_project(project_id)
    state=manager._projects().state_store(project_id)
    sha=hashlib.sha256(data).hexdigest()
    aid=uuid.uuid5(uuid.UUID(project_id),body['purpose']+':'+sha).hex
    doc={'artifact_id':aid,'project_id':project_id,'purpose':body['purpose'],'sha256':sha,
         'media_type':media,'byte_size':len(data),'url':f'/api/projects/{project_id}/references/{aid}'}
    with manager._projects().lock:
        # Fixed bytes precede the metadata receipt; no image IO spans a SQL write.
        with state._connection():pass
        directory=state.metadata/'references'
        _not_link(directory)
        directory.mkdir(exist_ok=True)
        path=directory/(aid+extension)
        _not_link(path)
        if path.exists():
            if path.read_bytes()!=data:reject('参考原件发生变化。','project_reference_changed',409)
        else:
            temporary=directory/(uuid.uuid4().hex+'.tmp')
            try:
                with temporary.open('xb') as stream:stream.write(data)
                os.replace(temporary,path)
            finally:
                temporary.unlink(missing_ok=True)
        encoded=json.dumps(doc,sort_keys=True,separators=(',',':'))
        _not_link(directory);_not_link(path)
        with state._write() as c:
            c.execute('CREATE TABLE IF NOT EXISTS project_references (artifact_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            row=c.execute('SELECT document FROM project_references WHERE artifact_id=?',(aid,)).fetchone()
            if row and row[0]!=encoded:reject('参考身份冲突。','project_reference_changed',409)
            c.execute('INSERT OR IGNORE INTO project_references VALUES (?,?)',(aid,encoded))
    return {'reference':doc}


def resolve(manager, project_id, reference):
    if (not isinstance(reference,dict) or set(reference)!={'artifact_id','sha256','purpose'}
            or not re.fullmatch(r'[0-9a-f]{32}',str(reference.get('artifact_id','')))
            or not re.fullmatch(r'[0-9a-f]{64}',str(reference.get('sha256','')))
            or reference.get('purpose') not in ('scene','asset')):
        reject('采用参考需要准确的artifact_id、sha256和purpose。')
    state=manager._projects().state_store(project_id)
    with state._connection() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_references'").fetchone():
            reject('项目参考不存在。','project_reference_missing',404)
        row=c.execute('SELECT document FROM project_references WHERE artifact_id=?',(reference['artifact_id'],)).fetchone()
    if not row:reject('参考不属于本项目。','project_reference_missing',404)
    doc=json.loads(row[0])
    if doc['project_id']!=project_id or any(doc[k]!=v for k,v in reference.items()):
        reject('参考身份或摘要不一致。','project_reference_changed',409)
    extension='.png' if doc['media_type']=='image/png' else '.jpg'
    directory=state.metadata/'references';path=directory/(doc['artifact_id']+extension)
    _not_link(directory);_not_link(path)
    if (not path.is_file()
            or path.stat().st_size!=doc['byte_size'] or hashlib.sha256(path.read_bytes()).hexdigest()!=doc['sha256']):
        reject('参考原件发生变化。','project_reference_changed',409)
    return copy.deepcopy(doc),path


def adopted(manager, project_id, references):
    return [resolve(manager,project_id,reference)[0] for reference in references]


def attach_scene(manager, run_id):
    run=manager.get(run_id)
    reference=next((r for r in run.get('reference_snapshots',[]) if r['purpose']=='scene'),None)
    if not reference:return False
    doc,path=resolve(manager,run['project_id'],{k:reference[k] for k in ('artifact_id','sha256','purpose')})
    if run.get('reference'):
        artifact=next(a for a in run['artifacts'] if a['id']==run['reference']['artifact_id'])
        manager._checked_artifact(run,artifact)
        if artifact['sha256']!=doc['sha256']:reject('已采用参考不能重绑。','project_reference_changed',409)
        return True
    output=manager.run_directory(run_id)/('project-reference'+path.suffix)
    output.parent.mkdir(parents=True,exist_ok=True);_not_link(output)
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=doc['sha256']:reject('参考复制时发生变化。','project_reference_changed',409)
    if output.exists() and output.read_bytes()!=data:reject('已固定的参考副本发生变化。','project_reference_changed',409)
    if not output.exists():
        with output.open('xb') as stream:stream.write(data)
    artifact=manager._artifact(run_id,output,'concept')
    descriptor={'artifact_id':artifact['id'],'sha256':doc['sha256'],'basis':'explicit_project_scene_reference'}
    manager._update(run_id,'reference_adopted','已采用本项目场景评价参考；不生成概念图。',
                    reference=descriptor,available_reference=descriptor,evaluation={'mode':'reference'})
    return True


def reference_resolver(manager, run_id, reference_id):
    run=manager.get(run_id)
    authorization=manager._director_host().local_asset_authorization(run)
    ref=authorization.get('asset_reference')
    if not ref or ref['artifact_id']!=reference_id:reject('资产参考不属于原持续授权。','project_reference_missing',404)
    doc,path=resolve(manager,run['project_id'],{k:ref[k] for k in ('artifact_id','sha256','purpose')})
    state=manager._projects().state_store(run['project_id'])
    return {'project_id':run['project_id'],'reference_id':doc['artifact_id'],
            'path':path.relative_to(state.project_root).as_posix(),'sha256':doc['sha256'],
            'byte_size':doc['byte_size'],'purpose':'asset'}
