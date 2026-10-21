"""Bounded static GLB input and Scene-owned asset records; standard library only."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import struct

MAX_GLB_BYTES = 32 * 1024 * 1024
MAX_ASSETS = 16
MAX_NODES = 256
MAX_MESHES = 64
MAX_VERTICES = 250000
MAX_TRIANGLES = 500000
MAX_SCENE_ASSET_VERTICES = 500000
MAX_SCENE_ASSET_TRIANGLES = 1000000
MAX_PARAMETRIC_VERTICES = 50000
MAX_PARAMETRIC_TRIANGLES = 100000
NODE_IMPORT_KEY = "sceneloop_asset_import_index"


class AssetError(ValueError):
    code = "asset_validation"
    outcome = "rejected"


def instance_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise AssetError("Asset instance_id requires 1–80 letters/digits/underscore/hyphen")
    return value


def transform(value=None, *, partial=False):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"position", "rotation", "scale"} or (partial and not value):
        raise AssetError("Asset transform accepts position/rotation/scale only")
    result = {} if partial else {"position": [0., 0., 0.], "rotation": [0., 0., 0.], "scale": [1., 1., 1.]}
    for key, vector in value.items():
        if (not isinstance(vector, (list, tuple)) or len(vector) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
            raise AssetError(f"Asset {key} requires three finite numbers")
        bound = {"position": 10000, "rotation": 360, "scale": 100}[key]
        if any(abs(v) > bound or (key == "scale" and v <= 0) for v in vector):
            raise AssetError(f"Asset {key} is outside the supported range")
        result[key] = [float(v) for v in vector]
    return result


def measured_rotation(value):
    """Stabilize only a float32 Euler readback just beyond +/- one turn.

    At 2*pi the binary32 spacing is 2**-21 radians, so a legal boundary
    rounded to Blender's float32 storage can exceed it by half that spacing.
    Requests, registrations and receipts still use strict ``transform``.
    """
    if (not isinstance(value,(list,tuple)) or len(value)!=3
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in value)):
        raise AssetError("Measured asset rotation requires three finite numbers")
    half_ulp_degrees = math.degrees(2**-22)
    stable = [math.copysign(360.,v) if 360 < abs(v) <= 360+half_ulp_degrees else v for v in value]
    return transform({"rotation":stable})["rotation"]


def _reserved(value):
    if isinstance(value, dict):
        if any(key.startswith("sceneloop_") for key in value):
            raise AssetError("GLB extras cannot claim SceneLoop reserved identities")
        for item in value.values():
            _reserved(item)
    elif isinstance(value, list):
        for item in value:
            _reserved(item)


def _extensions(value, allowed):
    if isinstance(value, dict):
        extensions = value.get("extensions", {})
        if not isinstance(extensions,dict) or set(extensions)-allowed:
            raise AssetError("Unsupported GLB extension payload")
        for item in value.values():
            _extensions(item,allowed)
    elif isinstance(value,list):
        for item in value:
            _extensions(item,allowed)


def _parse_glb(raw):
    """Validate fixed bytes before Blender; only one embedded BIN and static scene."""
    if not isinstance(raw, bytes) or not 20 <= len(raw) <= MAX_GLB_BYTES:
        raise AssetError("GLB size must be 20 bytes–32 MiB")
    magic, version, length = struct.unpack_from("<III", raw)
    if magic != 0x46546c67 or version != 2 or length != len(raw):
        raise AssetError("Invalid GLB v2 header/length")
    chunks = []; offset = 12
    while offset < len(raw):
        if offset + 8 > len(raw):
            raise AssetError("Truncated GLB chunk")
        count, kind = struct.unpack_from("<II", raw, offset); offset += 8
        if count % 4 or offset + count > len(raw):
            raise AssetError("Invalid GLB chunk extent")
        chunks.append((kind, raw[offset:offset+count])); offset += count
    if [kind for kind, _ in chunks] != [0x4e4f534a, 0x004e4942] or len(chunks[0][1]) > 2 * 1024 * 1024:
        raise AssetError("GLB requires a bounded JSON chunk and one embedded BIN")
    try:
        doc = json.loads(chunks[0][1].decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise AssetError("Invalid GLB JSON") from error
    if not isinstance(doc, dict) or doc.get("asset", {}).get("version") != "2.0":
        raise AssetError("Only glTF 2.0 static assets are supported")
    _reserved(doc)
    allowed_extensions = {"KHR_materials_unlit", "KHR_texture_transform", "KHR_materials_ior", "KHR_materials_specular"}
    _extensions(doc,allowed_extensions)
    if set(doc.get("extensionsUsed", [])) - allowed_extensions or set(doc.get("extensionsRequired", [])) - allowed_extensions:
        raise AssetError("GLB uses an unsupported extension")
    if doc.get("animations") or doc.get("skins") or doc.get("cameras"):
        raise AssetError("Animation, skinning and cameras are not supported")
    buffers = doc.get("buffers", [])
    if (len(buffers) != 1 or "uri" in buffers[0] or type(buffers[0].get("byteLength")) is not int
            or not 0 < buffers[0]["byteLength"] <= len(chunks[1][1])):
        raise AssetError("GLB must contain one embedded buffer without external URI")
    views = doc.get("bufferViews", [])
    for view in views:
        start, size = view.get("byteOffset", 0), view.get("byteLength")
        if (view.get("buffer", 0) != 0 or type(start) is not int or type(size) is not int
                or start < 0 or size < 0 or start + size > buffers[0]["byteLength"]):
            raise AssetError("GLB bufferView outside embedded buffer")
    images = doc.get("images", [])
    if len(images) > 64 or len(doc.get("materials", [])) > 128:
        raise AssetError("GLB material/image limit exceeded")
    for image in images:
        if ("uri" in image or type(image.get("bufferView")) is not int
                or not 0 <= image["bufferView"] < len(views) or image.get("mimeType") not in {"image/png", "image/jpeg"}):
            raise AssetError("Images must be embedded PNG/JPEG bufferViews")
    nodes, meshes, scenes = doc.get("nodes", []), doc.get("meshes", []), doc.get("scenes", [])
    if not 1 <= len(nodes) <= MAX_NODES or not 1 <= len(meshes) <= MAX_MESHES or len(scenes) != 1 or doc.get("scene", 0) != 0:
        raise AssetError("GLB requires one scene, 1–256 nodes and 1–64 meshes")
    parents = {}; roots = scenes[0].get("nodes", [])
    for index, node in enumerate(nodes):
        _reserved(node.get("extras", {}))
        if "skin" in node or "camera" in node or node.get("weights") or node.get("extensions"):
            raise AssetError("Only static MESH/EMPTY nodes are supported")
        if "mesh" in node and (type(node["mesh"]) is not int or not 0 <= node["mesh"] < len(meshes)):
            raise AssetError("Invalid node mesh index")
        for key, count in (("matrix", 16), ("translation", 3), ("rotation", 4), ("scale", 3)):
            if key in node and (not isinstance(node[key], list) or len(node[key]) != count
                    or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 10000 for v in node[key])):
                raise AssetError("Invalid finite node transform")
        if "scale" in node and any(v <= 0 for v in node["scale"]):
            raise AssetError("Node scale must be positive")
        for child in node.get("children", []):
            if type(child) is not int or not 0 <= child < len(nodes) or child in parents:
                raise AssetError("Invalid or shared node hierarchy")
            parents[child] = index
    visited = set(); pending = list(roots)
    while pending:
        index = pending.pop()
        if type(index) is not int or not 0 <= index < len(nodes) or index in visited:
            raise AssetError("Invalid scene roots or cyclic hierarchy")
        visited.add(index); pending.extend(nodes[index].get("children", []))
    if len(visited) != len(nodes) or any(root in parents for root in roots):
        raise AssetError("All asset nodes must belong to the one static scene")
    accessors = doc.get("accessors", []); vertices = triangles = 0
    for accessor in accessors:
        if (accessor.get("sparse") or type(accessor.get("count")) is not int or not 0 < accessor["count"] <= MAX_TRIANGLES * 3
                or type(accessor.get("bufferView")) is not int or not 0 <= accessor["bufferView"] < len(views)):
            raise AssetError("Invalid or sparse mesh accessor")
        width = {"SCALAR":1,"VEC2":2,"VEC3":3,"VEC4":4}.get(accessor.get("type"))
        size = {5120:1,5121:1,5122:2,5123:2,5125:4,5126:4}.get(accessor.get("componentType"))
        view = views[accessor["bufferView"]]; start = accessor.get("byteOffset", 0)
        stride = view.get("byteStride", (width or 0)*(size or 0))
        if (not width or not size or type(start) is not int or start < 0 or type(stride) is not int
                or stride < width*size or start+(accessor["count"]-1)*stride+width*size > view["byteLength"]):
            raise AssetError("Accessor exceeds its embedded bufferView")
    for mesh in meshes:
        _reserved(mesh.get("extras", {}))
        if mesh.get("weights"):
            raise AssetError("Morph targets are not supported")
        for primitive in mesh.get("primitives", []):
            if primitive.get("mode", 4) != 4 or primitive.get("targets") or primitive.get("extensions"):
                raise AssetError("Only uncompressed static triangles are supported")
            position = primitive.get("attributes", {}).get("POSITION")
            if type(position) is not int or not 0 <= position < len(accessors):
                raise AssetError("Mesh POSITION accessor missing")
            if accessors[position]["type"] != "VEC3" or accessors[position]["componentType"] != 5126:
                raise AssetError("Mesh positions must be float VEC3")
            for name, index in primitive["attributes"].items():
                if (name not in {"POSITION","NORMAL","TANGENT","TEXCOORD_0","TEXCOORD_1","COLOR_0"}
                        or type(index) is not int or not 0 <= index < len(accessors)):
                    raise AssetError("Unsupported or invalid static mesh attribute")
            vertices += accessors[position]["count"]
            indices = primitive.get("indices", position)
            if type(indices) is not int or not 0 <= indices < len(accessors):
                raise AssetError("Invalid mesh index accessor")
            if accessors[indices]["count"] % 3 or ("indices" in primitive and
                    (accessors[indices]["type"] != "SCALAR" or accessors[indices]["componentType"] not in (5121,5123,5125))):
                raise AssetError("Triangle indices require an unsigned scalar accessor")
            triangles += accessors[indices]["count"] // 3
    # Charge mesh instances, including repeated nodes sharing the same mesh.
    mesh_costs = []
    for mesh in meshes:
        v = t = 0
        for primitive in mesh.get("primitives", []):
            position = primitive["attributes"]["POSITION"]
            v += accessors[position]["count"]
            t += accessors[primitive.get("indices", position)]["count"] // 3
        mesh_costs.append((v,t))
    vertices = sum(mesh_costs[node["mesh"]][0] for node in nodes if "mesh" in node)
    triangles = sum(mesh_costs[node["mesh"]][1] for node in nodes if "mesh" in node)
    if not vertices or vertices > MAX_VERTICES or triangles > MAX_TRIANGLES:
        raise AssetError("External asset exceeds 250000 vertices / 500000 triangles")
    for material in doc.get("materials", []):
        _reserved(material.get("extras", {}))
    return doc, chunks[1][1]


def parse_glb(raw):
    try:
        return _parse_glb(raw)
    except AssetError:
        raise
    except (AttributeError, TypeError, KeyError, IndexError, ValueError, RecursionError, struct.error) as error:
        raise AssetError("Malformed static GLB data") from error


def geometry_facts(raw):
    """Measured static contract costs, including repeated mesh instances."""
    doc, _ = parse_glb(raw)
    accessors = doc['accessors']
    costs = []
    for mesh in doc['meshes']:
        vertices = triangles = 0
        for primitive in mesh.get('primitives',[]):
            position = primitive['attributes']['POSITION']
            vertices += accessors[position]['count']
            triangles += accessors[primitive.get('indices',position)]['count']//3
        costs.append((vertices,triangles))
    return {'vertices':sum(costs[node['mesh']][0] for node in doc['nodes'] if 'mesh' in node),
            'triangles':sum(costs[node['mesh']][1] for node in doc['nodes'] if 'mesh' in node),
            'mesh_instances':sum('mesh' in node for node in doc['nodes']),
            'geometry_evidence':'static_glb_contract_only'}


def encode_glb(doc, binary):
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    raw += b" " * (-len(raw) % 4); binary += b"\0" * (-len(binary) % 4)
    return struct.pack("<III", 0x46546c67, 2, 28+len(raw)+len(binary)) + struct.pack("<II",len(raw),0x4e4f534a) + raw + struct.pack("<II",len(binary),0x004e4942) + binary


def fixed_glb(path):
    if str(path).startswith(("\\\\", "//")) or "://" in str(path):
        raise AssetError("Asset input must be a local file, not a network path")
    path = Path(path).resolve()
    if path.suffix.lower() != ".glb" or not path.is_file() or path.stat().st_size > MAX_GLB_BYTES:
        raise AssetError("Asset requires an existing local .glb of at most 32 MiB")
    raw = path.read_bytes(); parse_glb(raw)
    return raw, {"sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw)}


def registration(value, parametric_ids=()):
    if not isinstance(value, dict) or set(value) != {"schema_version", "instances"} or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise AssetError("Invalid Scene-owned asset registration")
    rows = value["instances"]
    if not isinstance(rows, list) or len(rows) > MAX_ASSETS:
        raise AssetError("Scene supports at most 16 asset instances")
    occupied = set(parametric_ids); root_ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"instance_id", "root_id", "source", "transform", "nodes"}:
            raise AssetError("Incomplete asset instance identity")
        identifier = instance_id(row["instance_id"])
        if identifier in occupied:
            raise AssetError("Asset id duplicates an instance or parametric id")
        occupied.add(identifier)
        if not isinstance(row["root_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", row["root_id"]) or row["root_id"] in root_ids:
            raise AssetError("Invalid or duplicate asset root identity")
        root_ids.add(row["root_id"])
        source = row["source"]
        if (not isinstance(source, dict) or set(source) != {"sha256", "byte_size"}
                or not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                or type(source["byte_size"]) is not int or not 20 <= source["byte_size"] <= MAX_GLB_BYTES):
            raise AssetError("Invalid fixed asset source identity")
        if not isinstance(row["transform"], dict) or set(row["transform"]) != {"position", "rotation", "scale"}:
            raise AssetError("Asset transform record is incomplete")
        transform(row["transform"])
        nodes = row["nodes"]
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
            raise AssetError("Asset node record limit exceeded")
        identities = set(); indices = set()
        for node in nodes:
            if (not isinstance(node, dict) or set(node) != {"node_id", "source_index", "parent_id", "type", "matrix_local"}
                    or not isinstance(node["node_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", node["node_id"])
                    or node["node_id"] in identities or type(node["source_index"]) is not int or not 0 <= node["source_index"] < MAX_NODES
                    or node["source_index"] in indices
                    or (node["parent_id"] is not None and not isinstance(node["parent_id"],str))
                    or node["type"] not in {"MESH", "EMPTY"}):
                raise AssetError("Invalid asset node identity")
            identities.add(node["node_id"])
            indices.add(node["source_index"])
            matrix = node["matrix_local"]
            if (not isinstance(matrix, list) or len(matrix) != 4 or any(not isinstance(line, list) or len(line) != 4
                    or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>10000 for v in line) for line in matrix)):
                raise AssetError("Invalid asset local matrix")
        if any(node["parent_id"] is not None and node["parent_id"] not in identities for node in nodes):
            raise AssetError("Unknown asset parent identity")
        parents = {node["node_id"]:node["parent_id"] for node in nodes}
        for identifier in parents:
            seen = set(); current = identifier
            while current is not None:
                if current in seen:
                    raise AssetError("Cyclic asset parent identities")
                seen.add(current); current = parents[current]
    return copy.deepcopy(value)


def receipt(value):
    registration({"schema_version":1,"instances":[value]})
    return value


def inspection(value, row):
    """Bind a worker success to complete measured facts for this exact instance."""
    receipt(row)
    if not isinstance(value, dict) or any(value.get(k) != row[k] for k in ("instance_id","root_id","source")):
        raise AssetError("Measured asset identity differs from receipt")
    if not isinstance(value.get("transform"),dict) or set(value["transform"])!={"position","rotation","scale"}:
        raise AssetError("Missing actual asset transform")
    actual = transform(value["transform"])
    if any(abs(a-b)>1e-5 for k in actual for a,b in zip(actual[k],row["transform"][k])):
        raise AssetError("Measured asset transform differs from receipt")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(row["nodes"]):
        raise AssetError("Measured asset nodes missing")
    expected = {n["node_id"]:n for n in row["nodes"]}; seen = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_id") not in expected or node["node_id"] in seen:
            raise AssetError("Measured node identity invalid")
        seen.add(node["node_id"])
        if any(node.get(k) != expected[node["node_id"]][k] for k in ("source_index","parent_id","type")):
            raise AssetError("Measured node hierarchy differs from receipt")
        _measured_matrix(node.get("matrix_world"))
    _measured_matrix(value.get("matrix_world"))
    for key, limit in (("mesh_count",MAX_NODES),("vertices",MAX_VERTICES),("polygons",MAX_TRIANGLES)):
        if type(value.get(key)) is not int or not 1 <= value[key] <= limit:
            raise AssetError("Missing or unbounded measured asset geometry")
    bounds = value.get("bounds")
    if (not isinstance(bounds,list) or len(bounds)!=2 or any(not isinstance(v,list) or len(v)!=3
            or any(type(x) not in (float,int) or not math.isfinite(x) for x in v) for v in bounds)
            or any(bounds[0][i]>bounds[1][i] for i in range(3))):
        raise AssetError("Missing finite measured asset bounds")


def _measured_matrix(matrix):
    if (not isinstance(matrix,list) or len(matrix)!=4 or any(not isinstance(v,list) or len(v)!=4
            or any(type(x) not in (float,int) or not math.isfinite(x) for x in v) for v in matrix)):
        raise AssetError("Missing finite measured world matrix")
