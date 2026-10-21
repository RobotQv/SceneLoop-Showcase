"""Small trusted Blender node recipes, derived from the local material studies.

Uses no images or external paths. Coordinates are world metres: direction is a
world axis and is not a UV/texture-paint promise. Bump changes shading only.
"""
import bpy


def linear(color):
    return tuple(v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in color) + (1,)


def material(spec):
    mat = bpy.data.materials.new("SceneLoop authored " + spec["recipe"])
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()

    def node(kind):
        obj = nodes.new(kind); obj.name = f"n{len(nodes):02d}"
        return obj

    output = node("ShaderNodeOutputMaterial")
    shader = node("ShaderNodeBsdfPrincipled")
    links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    shader.inputs["Roughness"].default_value = spec["roughness"]
    shader.inputs["Metallic"].default_value = 1 if spec["recipe"] == "metal" else 0
    if spec["recipe"] == "cloth":
        shader.inputs["Sheen Weight"].default_value = .25
    if spec["recipe"] == "paint":
        shader.inputs["Coat Weight"].default_value = .18
        shader.inputs["Coat Roughness"].default_value = .3
    geometry = node("ShaderNodeNewGeometry")
    mapping = node("ShaderNodeVectorMath"); mapping.operation = "MULTIPLY"
    axis = "XYZ".index(spec["direction"])
    factors = [1 / spec["scale_m"]] * 3
    if spec["recipe"] == "wood":
        factors[axis] *= .08
    elif spec["recipe"] == "metal":
        factors[axis] *= .15
    mapping.inputs[1].default_value = factors
    links.new(geometry.outputs["Position"], mapping.inputs[0])
    noise = node("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 1
    noise.inputs["Detail"].default_value = 3
    noise.inputs["Roughness"].default_value = .65
    links.new(mapping.outputs[0], noise.inputs["Vector"])
    height = noise.outputs["Fac"]
    if spec["recipe"] in {"stone", "ground", "metal"}:
        grains = node("ShaderNodeTexVoronoi")
        grains.inputs["Scale"].default_value = 8
        links.new(mapping.outputs[0], grains.inputs["Vector"])
        mix = node("ShaderNodeMath"); mix.operation = "MULTIPLY"
        links.new(height, mix.inputs[0]); links.new(grains.outputs["Distance"], mix.inputs[1])
        height = mix.outputs[0]
    elif spec["recipe"] == "cloth":
        waves = []
        for direction in [a for a in "XYZ" if a != spec["direction"]]:
            wave = node("ShaderNodeTexWave"); wave.bands_direction = direction
            wave.inputs["Scale"].default_value = 1
            wave.inputs["Distortion"].default_value = .05
            links.new(mapping.outputs[0], wave.inputs["Vector"]); waves.append(wave)
        interlace = node("ShaderNodeMath"); interlace.operation = "MULTIPLY"
        links.new(waves[0].outputs["Fac"], interlace.inputs[0]); links.new(waves[1].outputs["Fac"], interlace.inputs[1])
        height = interlace.outputs[0]
    colors = node("ShaderNodeValToRGB")
    base = linear(spec["color"])
    for i, multiplier in enumerate((1 - spec["variation"] * .7, 1 + spec["variation"] * .25)):
        colors.color_ramp.elements[i].color = tuple(min(1, c * multiplier) for c in base[:3]) + (1,)
    links.new(noise.outputs["Fac"], colors.inputs["Fac"])
    links.new(colors.outputs["Color"], shader.inputs["Base Color"])
    bump = node("ShaderNodeBump")
    bump.inputs["Distance"].default_value = spec["bump_m"]
    bump.inputs["Strength"].default_value = .65
    links.new(height, bump.inputs["Height"]); links.new(bump.outputs["Normal"], shader.inputs["Normal"])
    mat.diffuse_color = base
    return mat


def world(spec):
    result = bpy.data.worlds.new("SceneLoop authored environment")
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.clear()
    background = nodes.new("ShaderNodeBackground"); background.name = "background"
    output = nodes.new("ShaderNodeOutputWorld"); output.name = "output"
    links.new(background.outputs["Background"], output.inputs["Surface"])
    background.inputs["Strength"].default_value = spec["strength"]
    if spec["type"] == "color":
        background.inputs["Color"].default_value = linear(spec["color"])
    else:
        import math
        sky = nodes.new("ShaderNodeTexSky"); sky.name = "sky"
        modes = {item.identifier for item in sky.bl_rna.properties["sky_type"].enum_items}
        sky.sky_type = "MULTIPLE_SCATTERING" if "MULTIPLE_SCATTERING" in modes else "NISHITA"
        sky.sun_disc = False
        sky.sun_elevation = math.radians(spec["sun_elevation"])
        sky.sun_rotation = math.radians(spec["sun_rotation"])
        sky.air_density = 1
        if hasattr(sky, "aerosol_density"):
            sky.aerosol_density = .7
        else:
            sky.dust_density = .7
        links.new(sky.outputs["Color"], background.inputs["Color"])
    return result
