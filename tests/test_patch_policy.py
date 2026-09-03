"""Patch policy tests (指令8 §61 edit cases, §62 create cases).

All cases run against a disposable repository-like root; nothing is written
by the policy itself (it only reads to compute hashes/diffs).
"""

from __future__ import annotations

import pytest

from harness_agent.patch import (
    MAX_CREATE_CONTENT_CHARS,
    MAX_REPLACEMENTS,
    MAX_REPLACEMENT_TEXT_CHARS,
    OP_CREATE,
    OP_EDIT,
    PatchPolicyError,
    encode_proposed,
    prepare_patch,
)

from patch_test_helpers import (
    make_edit_file,
    make_root,
    replacement,
    try_symlink,
)


def edit_case(root, **kwargs):
    return prepare_patch(path="hello.py", operation=OP_EDIT, root=root, **kwargs)


# ---------------------------------------------------------------------------
# §61 Edit policy
# ---------------------------------------------------------------------------


def test_61_01_normal_edit(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    plan = edit_case(root, replacements=[replacement('"old"', '"new"')], summaries=["c"])
    assert plan.operation == OP_EDIT
    assert plan.repo_path == "hello.py"
    assert plan.proposed_sha256 != ""
    assert '"new"' in plan.proposed_content


def test_61_02_multiple_replacements(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='a = 1\nb = 2\nc = 3\n')
    plan = edit_case(
        root,
        replacements=[
            replacement("a = 1", "a = 10"),
            replacement("b = 2", "b = 20"),
            replacement("c = 3", "c = 30"),
        ],
        summaries=["c"],
    )
    assert "a = 10" in plan.proposed_content
    assert "b = 20" in plan.proposed_content
    assert "c = 30" in plan.proposed_content


def test_61_03_replacement_order_independent(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='a = 1\nb = 2\n')
    p1 = edit_case(
        root,
        replacements=[replacement("a = 1", "a = 9"), replacement("b = 2", "b = 8")],
        summaries=["c"],
    )
    p2 = edit_case(
        root,
        replacements=[replacement("b = 2", "b = 8"), replacement("a = 1", "a = 9")],
        summaries=["c"],
    )
    assert p1.proposed_sha256 == p2.proposed_sha256
    assert p1.diff == p2.diff


def test_61_04_zero_match_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="does not match"):
        edit_case(root, replacements=[replacement("zzz", "new")], summaries=["c"])


def test_61_05_duplicate_match_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text="sym\nsym\n")
    with pytest.raises(PatchPolicyError, match="not unique"):
        edit_case(root, replacements=[replacement("sym", "new")], summaries=["c"])


def test_61_06_overlapping_replacements_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "abcdef"\n')
    with pytest.raises(PatchPolicyError, match="[Oo]verlap"):
        edit_case(
            root,
            replacements=[
                replacement("abcdef", "AXYZF"),
                replacement("bcd", "B"),
            ],
            summaries=["c"],
        )


def test_61_07_empty_old_text_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="[Ee]mpty old_text"):
        edit_case(root, replacements=[replacement("", "x")], summaries=["c"])


def test_61_08_noop_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="no-op"):
        edit_case(root, replacements=[replacement('x = "old"', 'x = "old"')], summaries=["c"])


def test_61_09_blank_summary_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="[Ss]ummary"):
        edit_case(root, replacements=[replacement('"old"', '"new"')], summaries=["   "])


def test_61_10_nul_summary_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="NUL"):
        edit_case(root, replacements=[replacement('"old"', '"new"')], summaries=["a\x00b"])


def test_61_11_nul_path_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="NUL"):
        prepare_patch(
            path="hello\x00.py", operation=OP_EDIT, root=root,
            replacements=[replacement('"old"', '"new"')], summaries=["c"],
        )


def test_61_12_absolute_path_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError):
        prepare_patch(
            path=str(root / "hello.py"), operation=OP_EDIT, root=root,
            replacements=[replacement('"old"', '"new"')], summaries=["c"],
        )
    with pytest.raises(PatchPolicyError):
        prepare_patch(
            path="C:\\some\\where\\hello.py", operation=OP_EDIT, root=root,
            replacements=[replacement('"old"', '"new"')], summaries=["c"],
        )


def test_61_13_pardotdot_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="[Tt]raversal"):
        prepare_patch(
            path="../hello.py", operation=OP_EDIT, root=root,
            replacements=[replacement('"old"', '"new"')], summaries=["c"],
        )


def test_61_14_repo_escape_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    with pytest.raises(PatchPolicyError, match="[Tt]raversal"):
        prepare_patch(
            path="sub/../../out.py", operation=OP_EDIT, root=root,
            replacements=[replacement("a", "b")], summaries=["c"],
        )


def test_61_15_sensitive_path_rejected(tmp_path):
    root = make_root(tmp_path)
    (root / ".env").write_text("K=1\n", encoding="utf-8")
    with pytest.raises(PatchPolicyError, match="[Ss]ensitive|ignored"):
        prepare_patch(
            path=".env", operation=OP_EDIT, root=root,
            replacements=[replacement("K=1", "K=2")], summaries=["c"],
        )


def test_61_16_ignored_dir_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, rel="node_modules/x.py", text="a=1\n")
    with pytest.raises(PatchPolicyError):
        prepare_patch(
            path="node_modules/x.py", operation=OP_EDIT, root=root,
            replacements=[replacement("a=1", "a=2")], summaries=["c"],
        )


def test_61_17_symlink_target_rejected(tmp_path):
    root = make_root(tmp_path)
    real = root / "real.py"
    real.write_text("a=1\n", encoding="utf-8")
    link = root / "link.py"
    if not try_symlink(real, link):
        pytest.skip("symlinks not permitted on this platform")
    with pytest.raises(PatchPolicyError, match="[Ss]ymlink"):
        prepare_patch(
            path="link.py", operation=OP_EDIT, root=root,
            replacements=[replacement("a=1", "a=2")], summaries=["c"],
        )


def test_61_18_symlink_parent_escape_rejected(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("a=1\n", encoding="utf-8")
    link_dir = root / "escape"
    if not try_symlink(outside, link_dir):
        pytest.skip("symlinks not permitted on this platform")
    with pytest.raises(PatchPolicyError, match="[Ss]ymlink|escape|outside"):
        prepare_patch(
            path="escape/secret.py", operation=OP_EDIT, root=root,
            replacements=[replacement("a=1", "a=2")], summaries=["c"],
        )


def test_61_19_binary_rejected(tmp_path):
    root = make_root(tmp_path)
    (root / "img.png").write_bytes(b"\x89PNG\x00\x01")
    with pytest.raises(PatchPolicyError, match="[Bb]inary"):
        prepare_patch(
            path="img.png", operation=OP_EDIT, root=root,
            replacements=[replacement("a", "b")], summaries=["c"],
        )


def test_61_20_unsupported_encoding_rejected(tmp_path):
    root = make_root(tmp_path)
    # latin-1 "café = x" -- no NUL bytes, but not valid UTF-8 either
    (root / "hello.py").write_bytes(b"caf\xe9 = \"x\"\n")
    with pytest.raises(PatchPolicyError, match="[Ee]ncoding"):
        edit_case(root, replacements=[replacement("caf", "caf2")], summaries=["c"])


def test_61_21_large_file_rejected(tmp_path):
    root = make_root(tmp_path)
    big = root / "big.py"
    big.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(PatchPolicyError, match="size"):
        prepare_patch(
            path="big.py", operation=OP_EDIT, root=root,
            replacements=[replacement("x", "y")], summaries=["c"],
        )


def test_61_22_replacement_limit(tmp_path):
    root = make_root(tmp_path)
    lines = "\n".join(f"v{i} = {i}" for i in range(MAX_REPLACEMENTS + 5)) + "\n"
    make_edit_file(root, text=lines)
    reps = [replacement(f"v{i} = {i}", f"v{i} = {i + 1}") for i in range(MAX_REPLACEMENTS + 1)]
    with pytest.raises(PatchPolicyError, match="[Rr]eplacement limit"):
        edit_case(root, replacements=reps, summaries=["c"])


def test_61_23_replacement_text_size_limit(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text="x = 1\n")
    huge = "a" * (MAX_REPLACEMENT_TEXT_CHARS + 1)
    with pytest.raises(PatchPolicyError, match="[Rr]eplacement text"):
        edit_case(root, replacements=[replacement("x = 1", huge)], summaries=["c"])


def test_61_24_diff_too_large_rejected(tmp_path, monkeypatch):
    import harness_agent.patch.policy as policy_mod
    monkeypatch.setattr(policy_mod, "MAX_DIFF_CHARS", 8)
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    with pytest.raises(PatchPolicyError, match="diff is too large"):
        edit_case(root, replacements=[replacement('"old"', '"brand new value!"')], summaries=["c"])


def test_61_25_complete_diff_returned(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    plan = edit_case(root, replacements=[replacement('"old"', '"new"')], summaries=["c"])
    assert plan.diff.startswith("--- ")
    assert "+" in plan.diff or "-" in plan.diff
    assert '"old"' in plan.diff and '"new"' in plan.diff


def test_61_26_unicode_source(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text="greeting = \"héllo 世界\"\n", rel="u.py")
    plan = prepare_patch(
        path="u.py", operation=OP_EDIT, root=root,
        replacements=[replacement("héllo 世界", "héllo 世界！")], summaries=["c"],
    )
    assert "héllo 世界！" in plan.proposed_content


def test_61_27_spaces_in_filename(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, rel="my file.py", text="a=1\n")
    plan = prepare_patch(
        path="my file.py", operation=OP_EDIT, root=root,
        replacements=[replacement("a=1", "a=2")], summaries=["c"],
    )
    assert plan.repo_path == "my file.py"


def test_61_28_lf_preserved(tmp_path):
    root = make_root(tmp_path)
    (root / "hello.py").write_bytes(b"a = 1\nb = 2\n")
    plan = edit_case(root, replacements=[replacement("a = 1", "a = 9")], summaries=["c"])
    assert plan.proposed_content == "a = 9\nb = 2\n"
    assert encode_proposed(plan) == b"a = 9\nb = 2\n"


def test_61_29_crlf_preserved(tmp_path):
    root = make_root(tmp_path)
    (root / "hello.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    plan = edit_case(root, replacements=[replacement("a = 1", "a = 9")], summaries=["c"])
    assert encode_proposed(plan) == b"a = 9\r\nb = 2\r\n"


def test_61_30_bom_preserved(tmp_path):
    root = make_root(tmp_path)
    (root / "hello.py").write_bytes(b"\xef\xbb\xbf" + b"a = 1\n")
    plan = edit_case(root, replacements=[replacement("a = 1", "a = 9")], summaries=["c"])
    assert plan.has_bom is True
    assert encode_proposed(plan) == b"\xef\xbb\xbf" + b"a = 9\n"


def test_61_31_mixed_newline_rejected(tmp_path):
    root = make_root(tmp_path)
    (root / "hello.py").write_bytes(b"a = 1\r\nb = 2\n")
    with pytest.raises(PatchPolicyError, match="[Mm]ixed|newline"):
        edit_case(root, replacements=[replacement("a = 1", "a = 9")], summaries=["c"])


# ---------------------------------------------------------------------------
# §62 Create policy
# ---------------------------------------------------------------------------


def create_case(root, **kwargs):
    return prepare_patch(path="new.txt", operation=OP_CREATE, root=root, **kwargs)


def test_62_01_normal_create(tmp_path):
    root = make_root(tmp_path)
    plan = create_case(root, content="line1\nline2\n", summaries=["new file"])
    assert plan.operation == OP_CREATE
    assert plan.proposed_sha256 != ""
    assert plan.original_sha256 == ""


def test_62_02_target_already_exists_rejected(tmp_path):
    root = make_root(tmp_path)
    (root / "new.txt").write_text("exists\n", encoding="utf-8")
    with pytest.raises(PatchPolicyError, match="already exists"):
        create_case(root, content="a\n", summaries=["c"])


def test_62_03_parent_missing_rejected(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(PatchPolicyError, match="parent directory does not exist"):
        prepare_patch(path="missing/dir/new.txt", operation=OP_CREATE, root=root,
                      content="a\n", summaries=["c"])


def test_62_04_parent_not_directory_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, rel="afile.txt", text="x\n")
    with pytest.raises(PatchPolicyError, match="parent directory"):
        prepare_patch(path="afile.txt/child.txt", operation=OP_CREATE, root=root,
                      content="a\n", summaries=["c"])


def test_62_05_parent_symlink_escape_rejected(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link_dir = root / "createdir"
    if not try_symlink(outside, link_dir):
        pytest.skip("symlinks not permitted on this platform")
    with pytest.raises(PatchPolicyError):
        prepare_patch(path="createdir/x.txt", operation=OP_CREATE, root=root,
                      content="a\n", summaries=["c"])


def test_62_06_sensitive_new_path_rejected(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(PatchPolicyError):
        prepare_patch(path=".env", operation=OP_CREATE, root=root,
                      content="K=1\n", summaries=["c"])


def test_62_07_ignored_dir_rejected(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(PatchPolicyError):
        prepare_patch(path="dist/x.py", operation=OP_CREATE, root=root,
                      content="a=1\n", summaries=["c"])


def test_62_08_content_size_limit(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(PatchPolicyError, match="[Cc]ontent exceeds"):
        create_case(root, content="a" * (MAX_CREATE_CONTENT_CHARS + 1), summaries=["c"])


def test_62_09_unicode_content(tmp_path):
    root = make_root(tmp_path)
    plan = create_case(root, content="café ☕\n", summaries=["c"])
    assert "☕" in plan.proposed_content


def test_62_10_lf_deterministic(tmp_path):
    root = make_root(tmp_path)
    plan = create_case(root, content="a\r\nb\r\n", summaries=["c"])
    assert plan.proposed_content == "a\nb\n"


def test_62_11_no_bom_default(tmp_path):
    root = make_root(tmp_path)
    plan = create_case(root, content="a\n", summaries=["c"])
    assert plan.has_bom is False
    assert encode_proposed(plan) == b"a\n"


def test_62_12_prepare_does_not_create_file(tmp_path):
    root = make_root(tmp_path)
    create_case(root, content="a\nb\n", summaries=["c"])
    assert not (root / "new.txt").exists()


def test_62_13_diff_shows_dev_null(tmp_path):
    root = make_root(tmp_path)
    plan = create_case(root, content="line1\nline2\n", summaries=["c"])
    assert plan.diff.startswith("--- /dev/null")
    assert "+line1" in plan.diff


def test_62_14_no_directory_creation(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(PatchPolicyError):
        prepare_patch(path="nested/unknown/x.txt", operation=OP_CREATE, root=root,
                      content="a\n", summaries=["c"])
    assert not (root / "nested").exists()
