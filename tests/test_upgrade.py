"""
tests/test_upgrade.py — Tests for bootstrap/upgrade.py

Focus areas (per spec):
  1. Managed-block parsing and reassembly (round-trip invariant)
  2. Hash-and-marker mechanism (the core invariant — tested exhaustively)
     - Clean block with new template → update applied
     - Clean block with same template → skip (no-op)
     - User-edited block → conflict, never overwritten
     - Missing stored hash → treated as clean (safe default for legacy files)
  3. Content outside managed markers is byte-identical before and after upgrade
  4. Already-current repo → no-op with exit 0
  5. Receipt always written on non-dry-run
  6. Conflict entries written to upgrade-conflicts.yaml
  7. Dry-run: diffs printed, no files written, no receipt
  8. run_upgrade integration (with filesystem fixtures)
  9. wrap_in_managed_block round-trip
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import pytest
import yaml

from bootstrap.upgrade import (
    BlockConflict,
    BlockUpgradeDecision,
    FileChange,
    UpgradeOptions,
    UpgradeResult,
    _sha256_short,
    _write_upgrade_conflicts,
    _write_upgrade_receipt,
    build_begin_marker,
    decide_block_upgrade,
    parse_attributes,
    reassemble,
    run_upgrade,
    split_managed_blocks,
    upgrade_file,
    wrap_in_managed_block,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_managed_block(role: str, content: str) -> str:
    """Build a file snippet with one managed block around ``content``."""
    h = _sha256_short(content)
    begin = f"<!-- agentOS:managed:begin role={role} hash={h} -->"
    end = "<!-- agentOS:managed:end -->"
    return f"{begin}{content}{end}"


def _make_file_with_block(
    role: str,
    block_content: str,
    prefix: str = "preamble\n",
    suffix: str = "\npostamble",
) -> str:
    return prefix + _make_managed_block(role, block_content) + suffix


def _tamper(content: str) -> str:
    """Slightly modify content to simulate a user edit."""
    return content + "\n# user edit"


# ---------------------------------------------------------------------------
# 1. parse_attributes
# ---------------------------------------------------------------------------

class TestParseAttributes:
    def test_single_attr(self):
        attrs = parse_attributes("role=planner")
        assert attrs == {"role": "planner"}

    def test_multiple_attrs(self):
        attrs = parse_attributes("role=builder hash=abc12345")
        assert attrs["role"] == "builder"
        assert attrs["hash"] == "abc12345"

    def test_empty_string(self):
        assert parse_attributes("") == {}

    def test_extra_whitespace(self):
        attrs = parse_attributes("  role=watcher   hash=deadbeef  ")
        assert attrs["role"] == "watcher"
        assert attrs["hash"] == "deadbeef"

    def test_name_attr(self):
        attrs = parse_attributes("name=plan-orchestrator hash=ff001122")
        assert attrs["name"] == "plan-orchestrator"


# ---------------------------------------------------------------------------
# 2. build_begin_marker
# ---------------------------------------------------------------------------

class TestBuildBeginMarker:
    def test_basic(self):
        m = build_begin_marker({"role": "planner", "hash": "abc12345"})
        assert m == "<!-- agentOS:managed:begin role=planner hash=abc12345 -->"

    def test_round_trip(self):
        """parse_attributes(build_begin_marker) is a round-trip."""
        attrs = {"role": "builder", "hash": "deadbeef"}
        marker = build_begin_marker(attrs)
        # Strip to just the attribute string.
        inner = marker[len("<!-- agentOS:managed:begin "):].rstrip(" -->").rstrip(">").rstrip(" -")
        parsed = parse_attributes(inner)
        assert parsed["role"] == attrs["role"]
        assert parsed["hash"] == attrs["hash"]


# ---------------------------------------------------------------------------
# 3. split_managed_blocks
# ---------------------------------------------------------------------------

class TestSplitManagedBlocks:
    def test_no_blocks(self):
        content = "plain text with no markers"
        segments = split_managed_blocks(content)
        assert segments == [("plain text with no markers", None)]

    def test_single_block(self):
        block_content = "\nmanaged stuff\n"
        content = _make_file_with_block("planner", block_content)
        segments = split_managed_blocks(content)
        # Should be: prefix (non-managed), block (managed), suffix (non-managed)
        assert len(segments) == 3
        pre, block, post = segments
        assert pre[1] is None        # non-managed
        assert block[1] is not None  # managed
        assert block[1]["role"] == "planner"
        assert post[1] is None

    def test_block_content_preserved(self):
        block_content = "\nhello world\n"
        content = _make_managed_block("builder", block_content)
        segments = split_managed_blocks(content)
        managed = [s for s in segments if s[1] is not None]
        assert len(managed) == 1
        assert managed[0][0] == block_content

    def test_multiple_blocks(self):
        c1 = "\nblock one\n"
        c2 = "\nblock two\n"
        b1 = _make_managed_block("builder", c1)
        b2 = _make_managed_block("reviewer", c2)
        content = b1 + "\nfiller\n" + b2
        segments = split_managed_blocks(content)
        managed = [s for s in segments if s[1] is not None]
        assert len(managed) == 2
        assert managed[0][1] is not None  # type guard
        assert managed[1][1] is not None  # type guard
        assert managed[0][1]["role"] == "builder"
        assert managed[1][1]["role"] == "reviewer"

    def test_empty_block_content(self):
        content = _make_managed_block("watcher", "")
        segments = split_managed_blocks(content)
        managed = [s for s in segments if s[1] is not None]
        assert len(managed) == 1
        assert managed[0][0] == ""


# ---------------------------------------------------------------------------
# 4. reassemble — round-trip invariant
# ---------------------------------------------------------------------------

class TestReassemble:
    """Content outside managed markers MUST be byte-identical after reassemble."""

    def test_round_trip_no_blocks(self):
        content = "no managed blocks here"
        segments = split_managed_blocks(content)
        assert reassemble(segments) == content

    def test_round_trip_single_block_no_change(self):
        block_content = "\nsome content\n"
        content = _make_file_with_block("planner", block_content)
        segments = split_managed_blocks(content)
        reconstructed = reassemble(segments)
        assert reconstructed == content

    def test_round_trip_multiple_blocks(self):
        c1, c2 = "\nalpha\n", "\nbeta\n"
        content = (
            "header\n"
            + _make_managed_block("builder", c1)
            + "\nmiddle\n"
            + _make_managed_block("reviewer", c2)
            + "\nfooter"
        )
        segments = split_managed_blocks(content)
        assert reassemble(segments) == content

    def test_non_managed_content_byte_identical(self):
        """The key invariant: text outside markers is untouched."""
        outside_text = "outside MUST NOT change — special chars: äöü\n🎉\n"
        block_content = "\ninner\n"
        content = outside_text + _make_managed_block("planner", block_content) + outside_text
        segments = split_managed_blocks(content)
        # Simulate a block content change.
        new_segments = []
        for text, attrs in segments:
            if attrs is not None:
                new_content = "\nnew inner\n"
                new_hash = _sha256_short(new_content)
                new_attrs = dict(attrs)
                new_attrs["hash"] = new_hash
                new_segments.append((new_content, new_attrs))
            else:
                new_segments.append((text, attrs))
        result = reassemble(new_segments)
        # The outside_text must appear unchanged.
        assert outside_text in result
        # The new inner content must be present.
        assert "new inner" in result


# ---------------------------------------------------------------------------
# 5. _sha256_short
# ---------------------------------------------------------------------------

class TestSha256Short:
    def test_length(self):
        assert len(_sha256_short("anything")) == 8

    def test_consistency(self):
        assert _sha256_short("hello") == _sha256_short("hello")

    def test_different_inputs(self):
        assert _sha256_short("hello") != _sha256_short("world")

    def test_known_value(self):
        # sha256("") first 8 hex chars.
        expected = hashlib.sha256(b"").hexdigest()[:8]
        assert _sha256_short("") == expected


# ---------------------------------------------------------------------------
# 6. decide_block_upgrade — THE CORE INVARIANT (tested exhaustively)
# ---------------------------------------------------------------------------

class TestDecideBlockUpgrade:
    """
    The hash-and-marker mechanism is the core invariant of the upgrade system.
    Every decision branch is tested explicitly.
    """

    # ---- 6a. Clean block, new template differs → update ----

    def test_clean_block_new_content_triggers_update(self):
        """stored_hash == current_hash AND new template differs → action=update."""
        content = "\noriginal content\n"
        h = _sha256_short(content)
        new_content = "\nupdated content from spec\n"

        decision = decide_block_upgrade(
            file_path="agentOS.yaml",
            current_content=content,
            attrs={"role": "planner", "hash": h},
            new_template_content=new_content,
        )
        assert decision.action == "update"
        assert decision.new_content == new_content
        assert decision.new_attrs["hash"] == _sha256_short(new_content)
        assert decision.conflict is None

    # ---- 6b. Clean block, new template identical → skip (no-op) ----

    def test_clean_block_same_content_is_noop(self):
        """stored_hash == current_hash == new_hash → action=skip."""
        content = "\nsame content\n"
        h = _sha256_short(content)

        decision = decide_block_upgrade(
            file_path="agentOS.yaml",
            current_content=content,
            attrs={"role": "builder", "hash": h},
            new_template_content=content,  # identical
        )
        assert decision.action == "skip"
        assert decision.conflict is None

    # ---- 6c. User-edited block → conflict, NEVER overwritten ----

    def test_user_edited_block_produces_conflict(self):
        """current_hash != stored_hash → action=conflict, regardless of new content."""
        original = "\noriginal\n"
        stored_h = _sha256_short(original)
        user_edited = original + "\n# user added this\n"
        new_template = "\ncompletely new from spec\n"

        decision = decide_block_upgrade(
            file_path=".github/workflows/orchestrator.yml",
            current_content=user_edited,
            attrs={"role": "orchestrator", "hash": stored_h},
            new_template_content=new_template,
        )
        assert decision.action == "conflict"
        assert decision.conflict is not None
        assert decision.conflict.stored_hash == stored_h
        assert decision.conflict.current_hash == _sha256_short(user_edited)
        assert decision.conflict.file == ".github/workflows/orchestrator.yml"

    def test_user_edited_block_conflict_even_when_new_matches_stored(self):
        """A user edit is a conflict even if the new template == the original content.
        (The user's edit must not be silently reverted.)
        """
        original = "\noriginal\n"
        stored_h = _sha256_short(original)
        user_edited = original + "\n# user added\n"

        decision = decide_block_upgrade(
            file_path="agentOS.yaml",
            current_content=user_edited,
            attrs={"role": "planner", "hash": stored_h},
            new_template_content=original,  # new template == original
        )
        assert decision.action == "conflict"

    def test_user_edited_block_content_not_in_output(self):
        """Verifying the caller uses the conflict decision correctly: no update applied."""
        original = "\noriginal\n"
        h = _sha256_short(original)
        user_edited = original + "\n# user change\n"
        new_template = "\nbrand new content\n"

        decision = decide_block_upgrade(
            file_path="f.yaml",
            current_content=user_edited,
            attrs={"role": "x", "hash": h},
            new_template_content=new_template,
        )
        # Caller must NOT apply decision.new_content on conflict.
        assert decision.action == "conflict"
        assert decision.new_content == ""  # not populated for conflicts

    # ---- 6d. Missing stored hash → treat as clean (safe default) ----

    def test_missing_stored_hash_treated_as_clean(self):
        """No hash in marker → the block is treated as if it was just written.
        The current content is taken as authoritative (no conflict raised).
        If new template differs from current, an update is applied.
        """
        content = "\ncurrent content\n"
        new_content = "\nnew from spec\n"

        decision = decide_block_upgrade(
            file_path="agentOS.yaml",
            current_content=content,
            attrs={"role": "planner"},  # no hash
            new_template_content=new_content,
        )
        # Without a stored hash, current_hash != "" != stored_hash="", so the
        # condition `stored_hash and current_hash != stored_hash` is False
        # (stored_hash is falsy). Should proceed to update/skip logic.
        assert decision.action in ("update", "skip")
        assert decision.conflict is None

    # ---- 6e. Block being removed (new_template_content=None) ----

    def test_block_removal_clean(self):
        """Clean block + None new template → action=remove."""
        content = "\nto be removed\n"
        h = _sha256_short(content)

        decision = decide_block_upgrade(
            file_path="agentOS.yaml",
            current_content=content,
            attrs={"role": "legacy", "hash": h},
            new_template_content=None,
        )
        assert decision.action == "remove"
        assert decision.conflict is None

    def test_block_removal_with_user_edit_is_conflict(self):
        """User-edited block + None new template → conflict (not silently removed)."""
        original = "\ncontent\n"
        stored_h = _sha256_short(original)
        user_edited = original + "# user edit\n"

        decision = decide_block_upgrade(
            file_path="agentOS.yaml",
            current_content=user_edited,
            attrs={"role": "legacy", "hash": stored_h},
            new_template_content=None,
        )
        assert decision.action == "conflict"


# ---------------------------------------------------------------------------
# 7. upgrade_file — full file upgrade logic
# ---------------------------------------------------------------------------

class TestUpgradeFile:
    """Tests for the file-level merge operation."""

    def test_no_managed_blocks_returns_unchanged(self):
        """A file with no managed blocks is returned as-is (caller handles skip)."""
        content = "no managed blocks\njust plain text\n"
        new_template = "<!-- agentOS:managed:begin role=x hash=00000000 -->\nnew\n<!-- agentOS:managed:end -->"

        updated, changes, conflicts = upgrade_file(
            file_path="test.yaml",
            current_content=content,
            new_template_content=new_template,
        )
        # No managed blocks in current content → no changes made.
        assert updated == content
        assert changes.blocks_updated == 0
        assert conflicts == []

    def test_clean_update_applied(self):
        original_block = "\noriginal\n"
        new_block = "\nupdated\n"
        file_content = _make_file_with_block("planner", original_block,
                                              prefix="pre\n", suffix="\npost")
        new_template = _make_managed_block("planner", new_block)

        updated, changes, conflicts = upgrade_file(
            file_path="agentOS.yaml",
            current_content=file_content,
            new_template_content=new_template,
        )
        assert "updated" in updated
        assert changes.blocks_updated == 1
        assert changes.blocks_skipped == 0
        assert conflicts == []

    def test_non_managed_content_unchanged(self):
        """Content outside markers is byte-identical."""
        preamble = "PREAMBLE — must not change: äöü 🎉\n"
        postamble = "\nPOSTAMBLE — must not change: 123"
        original_block = "\noriginal\n"
        new_block = "\nupdated\n"
        file_content = _make_file_with_block("planner", original_block,
                                              prefix=preamble, suffix=postamble)
        new_template = _make_managed_block("planner", new_block)

        updated, _, _ = upgrade_file(
            file_path="f.yaml",
            current_content=file_content,
            new_template_content=new_template,
        )
        assert preamble in updated
        assert postamble in updated

    def test_skip_when_already_current(self):
        block_content = "\ncurrent and up to date\n"
        file_content = _make_file_with_block("builder", block_content)
        new_template = _make_managed_block("builder", block_content)  # same

        updated, changes, conflicts = upgrade_file(
            file_path="f.yaml",
            current_content=file_content,
            new_template_content=new_template,
        )
        assert updated == file_content  # byte-identical
        assert changes.blocks_updated == 0
        assert changes.blocks_skipped == 1
        assert conflicts == []

    def test_user_edit_produces_conflict(self):
        original = "\noriginal block\n"
        user_edited = original + "\n# user added\n"
        h = _sha256_short(original)
        begin = f"<!-- agentOS:managed:begin role=planner hash={h} -->"
        end = "<!-- agentOS:managed:end -->"
        file_content = f"pre\n{begin}{user_edited}{end}\npost"
        new_template = _make_managed_block("planner", "\nnew from spec\n")

        updated, changes, conflicts = upgrade_file(
            file_path="agentOS.yaml",
            current_content=file_content,
            new_template_content=new_template,
        )
        # Block not updated — conflict raised.
        assert changes.blocks_updated == 0
        assert len(conflicts) == 1
        assert conflicts[0].block_id == "planner"
        # Original content preserved in output.
        assert user_edited in updated

    def test_user_edit_never_silently_overwritten(self):
        """Critical invariant: a user-edited block must never disappear from the output."""
        original = "\nspec-written content\n"
        user_edit_marker = "# I ADDED THIS"
        user_edited = original + user_edit_marker + "\n"
        h = _sha256_short(original)
        begin = f"<!-- agentOS:managed:begin role=watcher hash={h} -->"
        end = "<!-- agentOS:managed:end -->"
        file_content = f"{begin}{user_edited}{end}"
        new_template = _make_managed_block("watcher", "\nbrand new spec content\n")

        updated, _, conflicts = upgrade_file(
            file_path="f.md",
            current_content=file_content,
            new_template_content=new_template,
        )
        assert user_edit_marker in updated, (
            "User edit was silently removed — this MUST NOT happen"
        )
        assert len(conflicts) == 1

    def test_multiple_blocks_independent(self):
        """Each block is evaluated independently — a conflict in one doesn't affect others."""
        clean_original = "\nclean original\n"
        clean_new = "\nclean new\n"
        dirty_original = "\ndirty original\n"
        dirty_h = _sha256_short(dirty_original)
        dirty_user = dirty_original + "# user edit\n"
        clean_h = _sha256_short(clean_original)

        file_content = (
            f"<!-- agentOS:managed:begin role=builder hash={clean_h} -->"
            f"{clean_original}"
            f"<!-- agentOS:managed:end -->"
            f"\nmiddle\n"
            f"<!-- agentOS:managed:begin role=reviewer hash={dirty_h} -->"
            f"{dirty_user}"
            f"<!-- agentOS:managed:end -->"
        )
        new_template = (
            _make_managed_block("builder", clean_new)
            + "\nmiddle\n"
            + _make_managed_block("reviewer", "\nbrand new\n")
        )

        updated, changes, conflicts = upgrade_file(
            file_path="f.yaml",
            current_content=file_content,
            new_template_content=new_template,
        )
        # Clean block updated.
        assert "clean new" in updated
        # Dirty block unchanged.
        assert "user edit" in updated
        assert changes.blocks_updated == 1
        assert len(conflicts) == 1
        assert conflicts[0].block_id == "reviewer"

    def test_unified_diff_produced_on_change(self):
        original_block = "\noriginal\n"
        new_block = "\nupdated\n"
        file_content = _make_file_with_block("planner", original_block)
        new_template = _make_managed_block("planner", new_block)

        _, changes, _ = upgrade_file(
            file_path="agentOS.yaml",
            current_content=file_content,
            new_template_content=new_template,
        )
        assert changes.unified_diff != ""
        assert "---" in changes.unified_diff
        assert "+++" in changes.unified_diff

    def test_no_diff_when_unchanged(self):
        block_content = "\nsame\n"
        file_content = _make_file_with_block("planner", block_content)
        new_template = _make_managed_block("planner", block_content)

        _, changes, _ = upgrade_file(
            file_path="f.yaml",
            current_content=file_content,
            new_template_content=new_template,
        )
        assert changes.unified_diff == ""


# ---------------------------------------------------------------------------
# 8. wrap_in_managed_block
# ---------------------------------------------------------------------------

class TestWrapInManagedBlock:
    def test_hash_matches_content(self):
        content = "\nhello world\n"
        wrapped = wrap_in_managed_block(content, role="planner")
        segments = split_managed_blocks(wrapped)
        managed = [s for s in segments if s[1] is not None]
        assert len(managed) == 1
        text, attrs = managed[0]
        assert attrs is not None  # type guard
        # wrap_in_managed_block pads with \n before and after content.
        # The stored hash is over the inner text (including padding).
        assert content in text
        assert attrs["hash"] == _sha256_short(text)
        assert attrs["role"] == "planner"

    def test_extra_attrs(self):
        content = "\nstuff\n"
        wrapped = wrap_in_managed_block(content, role="builder", extra_attrs={"name": "my-job"})
        segments = split_managed_blocks(wrapped)
        managed = [s for s in segments if s[1] is not None]
        _, attrs = managed[0]
        assert attrs is not None  # type guard
        assert attrs.get("name") == "my-job"

    def test_round_trip(self):
        """wrap → split → reassemble is identity."""
        content = "\ncontent\n"
        wrapped = wrap_in_managed_block(content, role="reviewer")
        segments = split_managed_blocks(wrapped)
        assert reassemble(segments) == wrapped

    def test_decide_block_upgrade_sees_hash_as_clean(self):
        """A freshly wrapped block's hash should be clean for decide_block_upgrade."""
        content = "\nfresh content\n"
        wrapped = wrap_in_managed_block(content, role="planner")
        segments = split_managed_blocks(wrapped)
        managed = [s for s in segments if s[1] is not None]
        text, attrs = managed[0]
        assert attrs is not None  # type guard
        new_content = "\ndifferent\n"
        decision = decide_block_upgrade(
            file_path="f.yaml",
            current_content=text,
            attrs=attrs,
            new_template_content=new_content,
        )
        assert decision.action == "update"

    def test_decide_block_upgrade_skip_when_same(self):
        content = "\nsame\n"
        wrapped = wrap_in_managed_block(content, role="builder")
        segments = split_managed_blocks(wrapped)
        managed = [s for s in segments if s[1] is not None]
        text, attrs = managed[0]
        assert attrs is not None  # type guard
        # Pass the actual inner text (as split_managed_blocks sees it) as new template,
        # so decide_block_upgrade correctly identifies it as already-current.
        decision = decide_block_upgrade(
            file_path="f.yaml",
            current_content=text,
            attrs=attrs,
            new_template_content=text,  # same as what's stored
        )
        assert decision.action == "skip"


# ---------------------------------------------------------------------------
# 9. Receipt and conflict file writing
# ---------------------------------------------------------------------------

class TestReceiptAndConflicts:
    def test_receipt_written_on_non_dry_run(self, tmp_path):
        result = UpgradeResult(
            from_version="1.0.0-alpha",
            to_version="1.1.0",
            files_changed=[FileChange(path="agentOS.yaml", blocks_updated=2)],
            dry_run=False,
        )
        _write_upgrade_receipt(result, tmp_path)
        receipt_path = tmp_path / ".agentOS" / "upgrade-receipt.yaml"
        assert receipt_path.exists()
        data = yaml.safe_load(receipt_path.read_text())
        assert data["from_version"] == "1.0.0-alpha"
        assert data["to_version"] == "1.1.0"
        assert data["dry_run"] is False
        assert len(data["files_changed"]) == 1

    def test_conflict_file_written_when_conflicts_present(self, tmp_path):
        conflicts = [
            BlockConflict(
                file="agentOS.yaml",
                block_id="planner",
                stored_hash="abc12345",
                current_hash="deadbeef",
            )
        ]
        _write_upgrade_conflicts(conflicts, tmp_path)
        conflicts_path = tmp_path / ".agentOS" / "upgrade-conflicts.yaml"
        assert conflicts_path.exists()
        data = yaml.safe_load(conflicts_path.read_text())
        assert len(data) == 1
        assert data[0]["block_id"] == "planner"
        assert data[0]["stored_hash"] == "abc12345"
        assert data[0]["current_hash"] == "deadbeef"

    def test_no_conflict_file_when_no_conflicts(self, tmp_path):
        _write_upgrade_conflicts([], tmp_path)
        conflicts_path = tmp_path / ".agentOS" / "upgrade-conflicts.yaml"
        assert not conflicts_path.exists()

    def test_receipt_contains_all_required_fields(self, tmp_path):
        result = UpgradeResult(
            from_version="1.0.0",
            to_version="1.1.0",
            files_changed=[],
            blocks_skipped=3,
            conflicts=[
                BlockConflict(file="f", block_id="x", stored_hash="a", current_hash="b")
            ],
            errors=[],
            dry_run=False,
        )
        _write_upgrade_receipt(result, tmp_path)
        data = yaml.safe_load((tmp_path / ".agentOS" / "upgrade-receipt.yaml").read_text())
        required_keys = {
            "from_version", "to_version", "completed_at", "dry_run",
            "files_changed", "blocks_skipped_total", "conflicts", "errors",
        }
        assert required_keys.issubset(data.keys())
        assert data["conflicts"] == 1


# ---------------------------------------------------------------------------
# 10. run_upgrade integration tests (filesystem)
# ---------------------------------------------------------------------------

class TestRunUpgrade:
    """Integration tests using real filesystem fixtures."""

    def _make_opts(self, tmp_path: Path, **kwargs) -> UpgradeOptions:
        return UpgradeOptions(
            target_dir=tmp_path,
            templates_dir=None,   # tests provide content directly
            from_version="1.0.0-alpha",
            to_version="1.1.0",
            dry_run=False,
            **kwargs,
        )

    def _write_spec(self, tmp_path: Path, version: str = "1.0.0-alpha") -> None:
        spec = {"specVersion": version, "runtime": {}}
        (tmp_path / "agentOS.yaml").write_text(
            yaml.dump(spec, default_flow_style=False), encoding="utf-8"
        )

    def test_already_current_is_noop(self, tmp_path):
        """Same from and to version → no files changed, receipt written."""
        self._write_spec(tmp_path, version="1.1.0")
        opts = UpgradeOptions(
            target_dir=tmp_path,
            from_version="1.1.0",
            to_version="1.1.0",
            dry_run=False,
            spec={"specVersion": "1.1.0", "runtime": {}},
        )
        result = run_upgrade(opts)
        assert result.ok
        assert result.files_changed == []
        receipt = tmp_path / ".agentOS" / "upgrade-receipt.yaml"
        assert receipt.exists(), "Receipt must be written even when no changes"

    def test_receipt_written_even_with_zero_changes(self, tmp_path):
        self._write_spec(tmp_path)
        opts = UpgradeOptions(
            target_dir=tmp_path,
            from_version="1.0.0-alpha",
            to_version="1.0.0-alpha",  # same version → no-op
            dry_run=False,
            spec={"specVersion": "1.0.0-alpha", "runtime": {}},
        )
        result = run_upgrade(opts)
        assert (tmp_path / ".agentOS" / "upgrade-receipt.yaml").exists()

    def test_dry_run_does_not_write_files(self, tmp_path):
        """--dry-run must not write any file changes or the receipt."""
        block_content = "\noriginal\n"
        managed_file = _make_file_with_block("planner", block_content)
        target_file = tmp_path / "agentOS.yaml"
        target_file.write_text(managed_file, encoding="utf-8")

        new_block = "\nupdated content\n"
        new_template = _make_managed_block("planner", new_block)

        # Use a custom templates_dir with the new template.
        tpl_dir = tmp_path / "templates"
        (tpl_dir).mkdir()
        (tpl_dir / "agentOS.yaml").write_text(new_template, encoding="utf-8")

        spec = {"specVersion": "1.0.0-alpha", "runtime": {}}
        opts = UpgradeOptions(
            target_dir=tmp_path,
            templates_dir=tpl_dir,
            from_version="1.0.0-alpha",
            to_version="1.1.0",
            dry_run=True,
            spec=spec,
        )
        run_upgrade(opts)

        # File must be unchanged.
        assert target_file.read_text(encoding="utf-8") == managed_file
        # Receipt must NOT be written on dry-run.
        assert not (tmp_path / ".agentOS" / "upgrade-receipt.yaml").exists()

    def test_conflict_written_to_yaml_on_user_edit(self, tmp_path):
        """A user-edited block produces a conflict entry in upgrade-conflicts.yaml."""
        original = "\noriginal content\n"
        h = _sha256_short(original)
        user_edited = original + "# user added\n"
        begin = f"<!-- agentOS:managed:begin role=planner hash={h} -->"
        end = "<!-- agentOS:managed:end -->"
        file_content = f"{begin}{user_edited}{end}"

        target_file = tmp_path / "agentOS.yaml"
        target_file.write_text(file_content, encoding="utf-8")

        new_block = "\nnew from spec\n"
        new_template = _make_managed_block("planner", new_block)
        tpl_dir = tmp_path / "tpl"
        tpl_dir.mkdir()
        (tpl_dir / "agentOS.yaml").write_text(new_template, encoding="utf-8")

        spec = {"specVersion": "1.0.0-alpha", "runtime": {}}
        opts = UpgradeOptions(
            target_dir=tmp_path,
            templates_dir=tpl_dir,
            from_version="1.0.0-alpha",
            to_version="1.1.0",
            dry_run=False,
            spec=spec,
        )
        result = run_upgrade(opts)

        assert len(result.conflicts) == 1
        assert result.conflicts[0].block_id == "planner"

        conflicts_file = tmp_path / ".agentOS" / "upgrade-conflicts.yaml"
        assert conflicts_file.exists()
        data = yaml.safe_load(conflicts_file.read_text())
        assert data[0]["block_id"] == "planner"

        # The user's edit must still be in the file.
        assert "user added" in target_file.read_text(encoding="utf-8")

    def test_file_without_managed_blocks_is_skipped(self, tmp_path):
        """Files with no managed markers are not touched (legacy / uninstrumented)."""
        content = "no managed blocks here\njust plain content\n"
        target_file = tmp_path / "agentOS.yaml"
        target_file.write_text(content, encoding="utf-8")

        new_template = _make_managed_block("planner", "\nnew\n")
        tpl_dir = tmp_path / "tpl"
        tpl_dir.mkdir()
        (tpl_dir / "agentOS.yaml").write_text(new_template, encoding="utf-8")

        spec = {"specVersion": "1.0.0-alpha", "runtime": {}}
        opts = UpgradeOptions(
            target_dir=tmp_path,
            templates_dir=tpl_dir,
            from_version="1.0.0-alpha",
            to_version="1.1.0",
            dry_run=False,
            spec=spec,
        )
        result = run_upgrade(opts)

        # File untouched.
        assert target_file.read_text(encoding="utf-8") == content
        assert "agentOS.yaml" in result.files_unchanged

    def test_missing_target_dir_agentosyaml_returns_error(self, tmp_path):
        """If agentOS.yaml is absent and spec not provided, result contains an error."""
        opts = UpgradeOptions(
            target_dir=tmp_path,
            dry_run=False,
            spec=None,  # force auto-load
        )
        result = run_upgrade(opts)
        assert not result.ok
        assert any("agentOS.yaml" in e for e in result.errors)


# ---------------------------------------------------------------------------
# 11. UpgradeResult.ok and print_summary smoke test
# ---------------------------------------------------------------------------

class TestUpgradeResult:
    def test_ok_with_no_errors(self):
        result = UpgradeResult()
        assert result.ok is True

    def test_not_ok_with_errors(self):
        result = UpgradeResult(errors=["something broke"])
        assert result.ok is False

    def test_print_summary_smoke(self, capsys):
        result = UpgradeResult(
            from_version="1.0.0",
            to_version="1.1.0",
            files_changed=[FileChange(path="agentOS.yaml", blocks_updated=1)],
            conflicts=[BlockConflict(file="f", block_id="x", stored_hash="a", current_hash="b")],
            dry_run=False,
        )
        result.print_summary()
        captured = capsys.readouterr()
        assert "1.0.0" in captured.out
        assert "1.1.0" in captured.out
        assert "agentOS.yaml" in captured.out
        assert "conflict" in captured.out.lower()

    def test_print_summary_dry_run_note(self, capsys):
        result = UpgradeResult(
            from_version="1.0",
            to_version="1.1",
            files_changed=[FileChange(path="f.yaml", blocks_updated=1)],
            dry_run=True,
        )
        result.print_summary()
        captured = capsys.readouterr()
        assert "dry-run" in captured.out.lower()
