"""Route the frozen profile without making local authority transferable to cloud."""
from .asset_provider import Asset3DProvider, AssetJobError
from .hunyuan_provider import HunyuanProvider
from .tripo_provider import TripoV3Provider


class AssetProviderRouter(Asset3DProvider):
    def __init__(self, project_root):
        self.local = HunyuanProvider(project_root)
        self.cloud = TripoV3Provider(project_root)

    def choose(self, profile):
        if profile.get("provider_id") == "hunyuan" and profile.get("mode") == "local_process":
            return self.local
        if profile.get("provider_id") == "tripo" and profile.get("mode") == "cloud_api":
            return self.cloud
        raise AssetJobError("unsupported_asset_capability")

    def capabilities(self, profile):
        return self.choose(profile).capabilities(profile)

    def validate(self, profile, input_descriptor, parameters):
        return self.choose(profile).validate(profile, input_descriptor, parameters)

    def upload_input(self, identity, profile, credential, input_path):
        return self.choose(profile).upload_input(identity, profile, credential, input_path)

    def submit(self, identity, profile, credential, wire):
        return self.choose(profile).submit(identity, profile, credential, wire)

    def query(self, identity, profile, credential):
        return self.choose(profile).query(identity, profile, credential)

    def cancel(self, identity, profile, credential):
        return self.choose(profile).cancel(identity, profile, credential)

    def fetch_result(self, identity, profile, credential, output):
        return self.choose(profile).fetch_result(identity, profile, credential, output)

    def check_artifact(self, raw):
        # Both first-version providers produce untextured shape. Keep the old
        # image rejection; the common GLB guard does not decode output pixels.
        return self.cloud.check_artifact(raw)

    def resolve(self, reference, identity):
        return self.local.resolve(reference, identity)
