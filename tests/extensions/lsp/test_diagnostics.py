from reuleauxcoder.extensions.lsp.diagnostics import (
    SEVERITY_ERROR,
    SEVERITY_HINT,
    SEVERITY_INFORMATION,
    SEVERITY_WARNING,
    Diagnostic,
    DiagnosticBlock,
    render_blocks,
)


class TestDiagnostic:
    def test_basic_properties(self) -> None:
        d = Diagnostic(
            line=12,
            character=8,
            message="unexpected indent",
            severity=SEVERITY_ERROR,
            code="E999",
        )
        assert d.line == 12
        assert d.character == 8
        assert d.message == "unexpected indent"
        assert d.severity == SEVERITY_ERROR
        assert d.code == "E999"

    def test_severity_label(self) -> None:
        assert Diagnostic(line=1, character=1, message="", severity=1).severity_label == "ERROR"
        assert Diagnostic(line=1, character=1, message="", severity=2).severity_label == "WARNING"
        assert Diagnostic(line=1, character=1, message="", severity=3).severity_label == "INFO"
        assert Diagnostic(line=1, character=1, message="", severity=4).severity_label == "HINT"
        assert Diagnostic(line=1, character=1, message="", severity=99).severity_label == "UNKNOWN"

    def test_is_error(self) -> None:
        assert Diagnostic(line=1, character=1, message="", severity=1).is_error is True
        assert Diagnostic(line=1, character=1, message="", severity=2).is_error is False
        assert Diagnostic(line=1, character=1, message="", severity=3).is_error is False

    def test_is_warning(self) -> None:
        assert Diagnostic(line=1, character=1, message="", severity=2).is_warning is True
        assert Diagnostic(line=1, character=1, message="", severity=1).is_warning is False

    def test_slots_no_dict(self) -> None:
        """Diagnostic uses __slots__ so no __dict__ overhead."""
        d = Diagnostic(line=1, character=1, message="x")
        assert not hasattr(d, "__dict__")


class TestDiagnosticBlock:
    def test_empty(self) -> None:
        block = DiagnosticBlock(file_path="src/main.py")
        assert block.is_empty() is True
        assert block.file_path == "src/main.py"

    def test_non_empty(self) -> None:
        block = DiagnosticBlock(
            file_path="src/main.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        assert block.is_empty() is False

    def test_slots(self) -> None:
        block = DiagnosticBlock(file_path="f.py")
        assert not hasattr(block, "__dict__")


class TestRenderBlocks:
    def test_single_error(self) -> None:
        block = DiagnosticBlock(
            file_path="src/main.py",
            items=[Diagnostic(line=12, character=8, message="IndentationError: unexpected indent")],
        )
        out = render_blocks([block])
        assert out is not None
        assert '<diagnostics file="src/main.py">' in out
        assert "ERROR [12:8] IndentationError:" in out
        assert "</diagnostics>" in out

    def test_multiline_message_trimmed(self) -> None:
        block = DiagnosticBlock(
            file_path="f.py",
            items=[Diagnostic(line=1, character=1, message="line1\nline2\nline3")],
        )
        out = render_blocks([block])
        assert out is not None
        assert "line1" in out
        assert "line2" not in out
        assert "line3" not in out

    def test_default_errors_only(self) -> None:
        block = DiagnosticBlock(
            file_path="f.py",
            items=[
                Diagnostic(line=1, character=1, message="err", severity=1),
                Diagnostic(line=2, character=1, message="warn", severity=2),
                Diagnostic(line=3, character=1, message="hint", severity=4),
            ],
        )
        out = render_blocks([block])  # include_warnings=False (default)
        assert out is not None
        assert "err" in out
        assert "warn" not in out
        assert "hint" not in out

    def test_include_warnings_true(self) -> None:
        block = DiagnosticBlock(
            file_path="f.py",
            items=[
                Diagnostic(line=1, character=1, message="err", severity=1),
                Diagnostic(line=2, character=1, message="warn", severity=2),
            ],
        )
        out = render_blocks([block], include_warnings=True)
        assert out is not None
        assert "err" in out
        assert "warn" in out

    def test_empty_block_returns_none(self) -> None:
        """Empty blocks produce None, not an empty string."""
        block = DiagnosticBlock(file_path="f.py", items=[])
        assert render_blocks([block]) is None
        assert render_blocks([block], include_warnings=True) is None

    def test_warnings_only_block_returns_none_when_filtered(self) -> None:
        """Block with only warnings → None when include_warnings=False."""
        block = DiagnosticBlock(
            file_path="f.py",
            items=[Diagnostic(line=1, character=1, message="warn", severity=2)],
        )
        assert render_blocks([block]) is None  # default: errors only

    def test_sorted_errors_first_by_severity_then_line(self) -> None:
        block = DiagnosticBlock(
            file_path="f.py",
            items=[
                Diagnostic(line=10, character=1, message="err2", severity=1),
                Diagnostic(line=5, character=1, message="err1", severity=1),
                Diagnostic(line=1, character=1, message="warn", severity=2),
            ],
        )
        out = render_blocks([block], include_warnings=True)
        assert out is not None
        # Errors before warnings
        err1_pos = out.index("err1")
        err2_pos = out.index("err2")
        warn_pos = out.index("warn")
        assert err1_pos < err2_pos  # line 5 before line 10
        assert err2_pos < warn_pos  # errors before warnings

    def test_max_diagnostics_cap(self) -> None:
        items = [
            Diagnostic(line=i, character=1, message=f"e{i}", severity=1)
            for i in range(1, 26)  # 25 items
        ]
        block = DiagnosticBlock(file_path="f.py", items=items)
        out = render_blocks([block], max_diagnostics=5)
        assert out is not None
        # Only 5 should appear
        assert out.count("ERROR") == 5

    def test_multiple_blocks(self) -> None:
        b1 = DiagnosticBlock(
            file_path="a.py",
            items=[Diagnostic(line=1, character=1, message="err_a")],
        )
        b2 = DiagnosticBlock(
            file_path="b.py",
            items=[Diagnostic(line=2, character=1, message="err_b")],
        )
        out = render_blocks([b1, b2])
        assert out is not None
        assert 'file="a.py"' in out
        assert 'file="b.py"' in out
        assert "err_a" in out
        assert "err_b" in out

    def test_all_blocks_empty_returns_none(self) -> None:
        assert render_blocks([
            DiagnosticBlock(file_path="a.py"),
            DiagnosticBlock(file_path="b.py"),
        ]) is None

    def test_mixed_empty_and_nonempty(self) -> None:
        """One empty block + one non-empty → only the non-empty rendered."""
        out = render_blocks([
            DiagnosticBlock(file_path="a.py"),
            DiagnosticBlock(
                file_path="b.py",
                items=[Diagnostic(line=1, character=1, message="err")],
            ),
        ])
        assert out is not None
        assert 'file="a.py"' not in out
        assert 'file="b.py"' in out
