"""Read-only configuration client for runtime and independent connections."""


class ConfigurationClient:
    def __init__(self, peer):
        self.peer = peer

    def initialize(self):
        return self.peer.request("initialize", {"version": 1})

    def describe(self, section=None):
        return self.peer.request("config.describe", {"section": section} if section is not None else {})

    def inspect(self):
        return self.peer.request("config.inspect")

    def check(self, **parameters):
        return self.peer.request("config.check", parameters)
