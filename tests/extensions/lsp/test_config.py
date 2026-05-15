from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride


class TestLspConfigDefaults:
    def test_defaults_when_no_lsp_section(self) -> None:
        config = Config()  # No lsp attribute
        lsp = LspConfig.from_config(config)
        assert lsp.enabled is True
        assert lsp.poll_timeout_ms == 5000
        assert lsp.max_diagnostics == 20
        assert lsp.include_warnings is False
        assert lsp.server_overrides == {}

    def test_defaults_when_lsp_is_none(self) -> None:
        config = Config()
        config.lsp = None  # type: ignore[attr-defined]
        lsp = LspConfig.from_config(config)
        assert lsp.enabled is True
        assert lsp.server_overrides == {}

    def test_parse_basic(self) -> None:
        config = Config()
        config.lsp = {  # type: ignore[attr-defined]
            "enabled": False,
            "poll_timeout_ms": 10000,
            "max_diagnostics": 10,
            "include_warnings": True,
        }
        lsp = LspConfig.from_config(config)
        assert lsp.enabled is False
        assert lsp.poll_timeout_ms == 10000
        assert lsp.max_diagnostics == 10
        assert lsp.include_warnings is True

    def test_parse_server_overrides(self) -> None:
        config = Config()
        config.lsp = {  # type: ignore[attr-defined]
            "servers": {
                "cpp": {
                    "cmd": "clangd",
                    "args": ["--compile-commands-dir=build"],
                    "workspace_root": "build",
                },
                "python": {
                    "cmd": "pyright-langserver",
                    "args": ["--stdio"],
                    "init_opts": {
                        "python.analysis.extraPaths": ["./lib"],
                    },
                },
            }
        }
        lsp = LspConfig.from_config(config)
        assert len(lsp.server_overrides) == 2

        cpp = lsp.get_override("cpp")
        assert cpp is not None
        assert cpp.cmd == "clangd"
        assert cpp.args == ["--compile-commands-dir=build"]
        assert cpp.workspace_root == "build"
        assert cpp.init_opts is None

        py_ = lsp.get_override("python")
        assert py_ is not None
        assert py_.cmd == "pyright-langserver"
        assert py_.init_opts == {"python.analysis.extraPaths": ["./lib"]}

    def test_get_override_missing(self) -> None:
        config = Config()
        config.lsp = {"servers": {}}  # type: ignore[attr-defined]
        lsp = LspConfig.from_config(config)
        assert lsp.get_override("rust") is None

    def test_empty_servers_dict(self) -> None:
        config = Config()
        config.lsp = {"servers": {}}  # type: ignore[attr-defined]
        lsp = LspConfig.from_config(config)
        assert lsp.server_overrides == {}

    def test_servers_is_none(self) -> None:
        config = Config()
        config.lsp = {"servers": None}  # type: ignore[attr-defined]
        lsp = LspConfig.from_config(config)
        assert lsp.server_overrides == {}


class TestLspServerOverride:
    def test_default_fields(self) -> None:
        ov = LspServerOverride(language="python")
        assert ov.language == "python"
        assert ov.cmd is None
        assert ov.args is None
        assert ov.workspace_root is None
        assert ov.init_opts is None

    def test_partial_override(self) -> None:
        """Only set workspace_root, leave cmd/args as default."""
        ov = LspServerOverride(
            language="rust",
            workspace_root="/custom/root",
        )
        assert ov.cmd is None
        assert ov.workspace_root == "/custom/root"
