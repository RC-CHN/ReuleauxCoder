"""Agent-independent configuration client for normal and recovery connections."""


class ConfigurationClient:
    def __init__(self, peer):
        self.peer = peer

    def initialize(self):
        """Use only with `rcoder config rpc`; normal runtime clients initialize themselves."""
        return self.peer.request("initialize", {"version": 1})

    def describe(self, section=None):
        return self.peer.request(
            "config.describe", {"section": section} if section is not None else {}
        )

    def inspect(self):
        return self.peer.request("config.inspect")

    def prepare(self, **parameters):
        return self.peer.request("config.prepare", parameters)

    def validate(self, **parameters):
        return self.peer.request("config.validate", parameters)

    def apply(self, change_id, *, allow_unverified_model=False):
        return self.peer.request(
            "config.apply",
            {"change_id": change_id, "allow_unverified_model": allow_unverified_model},
        )

    def history(self, limit=20):
        return self.peer.request("config.history", {"limit": limit})

    def revert(self, change_id):
        return self.peer.request("config.revert", {"change_id": change_id})

    def recover(self, change_id, base_revision, *, side="before"):
        return self.peer.request(
            "config.recover",
            {"change_id": change_id, "base_revision": base_revision, "side": side},
        )
