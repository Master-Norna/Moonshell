from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


def _git_output(root: Path, *arguments: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if result.returncode != 0:
        return False, ""
    return True, result.stdout.strip()


def collect_source_provenance(
    root: Path,
    expected_tag: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Collect fail-closed Git provenance for a release build."""
    environment = os.environ if environ is None else environ
    head_ok, head = _git_output(root, "rev-parse", "--verify", "HEAD")
    status_ok, status = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    tag_target_ok, tag_target_output = _git_output(
        root,
        "rev-parse",
        "--verify",
        f"refs/tags/{expected_tag}^{{commit}}",
    )
    main_target_ok, main_target_output = _git_output(
        root,
        "rev-parse",
        "--verify",
        "refs/remotes/origin/main^{commit}",
    )

    commit = head if head_ok and COMMIT.fullmatch(head) else ""
    tag_target = (
        tag_target_output
        if tag_target_ok and COMMIT.fullmatch(tag_target_output)
        else ""
    )
    main_target = (
        main_target_output
        if main_target_ok and COMMIT.fullmatch(main_target_output)
        else ""
    )
    source_on_main = False
    main_ancestor_ok = False
    if commit and main_target:
        main_ancestor_ok, _ = _git_output(
            root,
            "merge-base",
            "--is-ancestor",
            commit,
            main_target,
        )
        source_on_main = main_ancestor_ok
    source_tag = (
        expected_tag
        if commit
        and tag_target
        and tag_target.casefold() == commit.casefold()
        else ""
    )
    source_dirty = not status_ok or bool(status)

    environment_consistent = True
    github_sha = environment.get("GITHUB_SHA", "").strip()
    github_ref_type = environment.get("GITHUB_REF_TYPE", "").strip()
    github_ref_name = environment.get("GITHUB_REF_NAME", "").strip()
    github_context = bool(
        github_sha
        or github_ref_type
        or github_ref_name
        or environment.get("GITHUB_ACTIONS", "").strip()
    )
    if github_context:
        environment_consistent = (
            COMMIT.fullmatch(github_sha) is not None
            and bool(commit)
            and github_sha.casefold() == commit.casefold()
            and github_ref_type == "tag"
            and github_ref_name == expected_tag
            and source_tag == expected_tag
        )

    git_verified = (
        head_ok
        and status_ok
        and tag_target_ok
        and main_target_ok
        and main_ancestor_ok
        and bool(commit)
        and bool(tag_target)
        and bool(main_target)
        and source_tag == expected_tag
        and environment_consistent
    )
    release_eligible = (
        git_verified
        and not source_dirty
        and source_tag == expected_tag
    )
    return {
        "source_commit": commit,
        "source_tag": source_tag,
        "source_tag_target": tag_target,
        "source_main_target": main_target,
        "source_on_main": source_on_main,
        "source_dirty": source_dirty,
        "source_git_verified": git_verified,
        "release_eligible": release_eligible,
    }
