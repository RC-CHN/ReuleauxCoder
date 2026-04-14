from reuleauxcoder.extensions.skills.catalog import build_skills_catalog
from reuleauxcoder.extensions.skills.models import Skill


def test_build_skills_catalog_returns_empty_string_for_no_skills() -> None:
    assert build_skills_catalog(()) == ""


def test_build_skills_catalog_sorts_and_escapes_skill_fields() -> None:
    skills = (
        Skill(
            name="zeta",
            description="Use > later",
            location="/tmp/zeta",
            skill_dir="/tmp",
            body="",
        ),
        Skill(
            name="alpha & beta",
            description="Use <first>",
            location="/tmp/a&b",
            skill_dir="/tmp",
            body="",
        ),
    )

    catalog = build_skills_catalog(skills)

    assert "<available_skills>" in catalog
    assert catalog.index("alpha &amp; beta") < catalog.index("zeta")
    assert "Use &lt;first&gt;" in catalog
    assert "/tmp/a&amp;b" in catalog
