"""Publish a verified bundle from the dedicated GitHub Actions publication job."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from update import LISTS, PUBLISHED, UpdateError, digest, load_config, verify_bundle


PROTECTED = (*PUBLISHED, "update-config.json", "scripts", ".github", ".gitattributes", ".gitignore")


def git(repo, *args, check=True):
    # Rebase also creates commits and needs the same identity as the initial commit.
    command = ["git", "-c", "user.name=github-actions[bot]", "-c",
               "user.email=41898282+github-actions[bot]@users.noreply.github.com", *args]
    result = subprocess.run(command, cwd=repo, text=True, capture_output=True, timeout=30)
    if check and result.returncode:
        raise UpdateError(f"Git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def remote_matches(repo, candidate):
    for name in PUBLISHED:
        result = git(repo, "show", f"FETCH_HEAD:{name}", check=False)
        if result.returncode or result.stdout != (candidate / name).read_text(encoding="utf-8"):
            return False
    return True


def publish(repo, candidate, branch, *, sleep=time.sleep):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise UpdateError("Publication is restricted to GitHub Actions. Use update.py to generate a local preview.")
    git(repo, "check-ref-format", f"refs/heads/{branch}")
    if git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise UpdateError("Publication requires a clean checkout; tracked files were modified.")
    config = load_config(repo / "update-config.json")
    verify_bundle(candidate, repo, config)
    if all(digest(repo / name) == digest(candidate / name) for name in LISTS):
        return "No changes: both published lists already match. No commit was created. See this run's report for its validation time."
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    committed = False
    for attempt in range(1, 4):
        fetched = git(repo, "fetch", "--no-tags", "origin", f"refs/heads/{branch}", check=False)
        if fetched.returncode:
            if attempt == 3:
                raise UpdateError("Could not fetch the publication branch after 3 attempts. " + fetched.stderr.strip())
            sleep(attempt * 2)
            continue
        # A push can succeed remotely even if the connection drops before acknowledgement.
        if remote_matches(repo, candidate):
            return "Published bundle is already present on the remote branch. No further push is needed."
        compared = git(repo, "diff", "--quiet", base, "FETCH_HEAD", "--", *PROTECTED, check=False)
        if compared.returncode == 1:
            raise UpdateError("Lists or pipeline configuration changed on the remote branch during validation. Generate a fresh candidate; concurrent changes were preserved.")
        if compared.returncode != 0:
            raise UpdateError("Could not compare the remote branch with the validated revision.")
        if not committed:
            # The three files become visible together through one Git commit and one push.
            for name in PUBLISHED:
                (repo / name).write_bytes((candidate / name).read_bytes())
            git(repo, "add", "--", *PUBLISHED)
            git(repo, "commit", "-m", "Update validated DNS resolver lists", "--", *PUBLISHED)
            committed = True
        git(repo, "rebase", "FETCH_HEAD")
        pushed = git(repo, "push", "origin", f"HEAD:refs/heads/{branch}", check=False)
        if pushed.returncode == 0:
            return "Published resolvers.txt, top20.txt and metadata.json in one commit."
        print(f"Push attempt {attempt}/3 failed: {pushed.stderr.strip()}", file=sys.stderr)
        if attempt < 3:
            sleep(attempt * 2)
    raise UpdateError("Publication failed after 3 attempts. Check branch protection and contents: write permission; the candidate remains in the artifact.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        result = publish(repo, args.candidate.resolve(), args.branch)
    except (UpdateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Publication failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(f"## Publication\n\n{result}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
