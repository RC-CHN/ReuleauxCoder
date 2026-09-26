"""Install a release wheel with uv tool and exercise it outside the checkout."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


root = Path(__file__).resolve().parent.parent
directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
(wheel,) = directory.glob("*.whl")
uv = shutil.which("uv")
node = shutil.which("node")
assert uv and node, "The release smoke test needs uv and Node"
with tempfile.TemporaryDirectory(prefix="rcoder-install-") as temporary:
    work = Path(temporary)
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(work / "tools"),
        "UV_TOOL_BIN_DIR": str(work / "bin"),
    }
    env.pop("PYTHONPATH", None)
    subprocess.run(
        [
            uv,
            "tool",
            "install",
            "--default-index",
            "https://pypi.org/simple",
            "--python",
            sys.executable,
            str(wheel),
        ],
        cwd=work,
        env=env,
        check=True,
    )
    suffix = ".exe" if os.name == "nt" else ""
    python = (
        work
        / "tools/reuleauxcoder"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    for command in ("rcoder", "rcoder-cli", "rcoder-tui"):
        subprocess.run(
            [str(work / "bin" / (command + suffix)), "--version"],
            cwd=work,
            env={**env, "PATH": ""},
            check=True,
        )
    subprocess.run(
        [str(python), "-c", """
from pathlib import Path
import runpy
import subprocess
import sys
from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool

work = Path.cwd()
service = SkillsService(workspace_dir=work, home_dir=work / 'empty-home')
loaded = service.reload()
expected = {
    'rcoder-config', 'skill-creator', 'skill-installer', 'project-guide',
    'code-review', 'code-simplify', 'test-and-fix', 'github-ci', 'ui-acceptance',
    'docs-writing', 'docs-translate', 'text-polish', 'structured-data',
    'pptx', 'docx', 'xlsx', 'pdf',
}
assert {item.name for item in loaded.active_skills} == expected
assert not loaded.diagnostics
creator = next(item for item in loaded.active_skills if item.name == 'skill-creator')
validate = runpy.run_path(str(Path(creator.skill_dir) / 'scripts/validate_skill.py'))['validate']
for item in loaded.active_skills:
    assert item.scope == 'builtin'
    assert Path(item.location).is_relative_to(Path(sys.prefix).resolve())
    assert not validate(Path(item.skill_dir)), item.name
    for script in (Path(item.skill_dir) / 'scripts').glob('*.py'):
        subprocess.run([sys.executable, str(script), '--help'], check=True, capture_output=True, timeout=20)
skill = next(item for item in loaded.active_skills if item.name == 'rcoder-config')
assert skill.scope == 'builtin'
assert Path(skill.location).is_relative_to(Path(sys.prefix).resolve())
reader = ReadFileTool(LocalToolBackend(ExecutionContext(cwd=str(work))))
for name in ('SKILL.md', 'references/configuration.md', 'references/reasoning.md', 'references/workflows.md'):
    path = Path(skill.skill_dir) / name
    first_line = path.read_text(encoding='utf-8').splitlines()[0]
    assert first_line in reader.execute(str(path)).model_text
configuration = ConfigurationService.for_workspace(work, home=work / 'empty-home')
assert configuration.describe()['api_version'] == 2
checked = configuration.check(documents=[{
    'scope': 'workspace',
    'content': 'app:\\n  api_key: offline-smoke-key\\n  model: example-model\\n',
}])
assert checked['valid'], checked
assert all(check['status'] == 'passed' for check in checked['checks']), checked
assert not (work / '.rcoder').exists()
assert not (work / 'empty-home').exists()
print('All 17 installed skills, resource links, script entry points and offline configuration checks passed.')
"""],
        cwd=work,
        env=env,
        check=True,
        timeout=60,
    )
    bundle = subprocess.check_output(
        [
            str(python),
            "-c",
            "from importlib.resources import files; print(files('reuleauxcoder') / '_tui/cli.mjs')",
        ],
        cwd=work,
        env=env,
        text=True,
    ).strip()
    subprocess.run(
        [node, bundle, "--help"],
        cwd=work,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    if os.name != "nt":
        subprocess.run(
            [
                str(python),
                str(root / "reuleauxcoder-tui/test/terminal_smoke.py"),
                "--launcher",
                str(work / "bin/rcoder"),
            ],
            cwd=work,
            env=env,
            check=True,
            timeout=30,
        )
print(
    "uv tool installation, all entry points, bundled skill and standalone TUI passed outside the checkout."
)
