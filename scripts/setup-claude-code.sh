#!/bin/sh
# Claude Code permissions for unattended development of this Kairo checkout.
#
#   scripts/setup-claude-code.sh             write .claude/settings.local.json
#   scripts/setup-claude-code.sh --dry-run   show what would change; write nothing
#   scripts/setup-claude-code.sh --settings FILE   another settings file (for testing)
#
# Run it as the account that runs Claude Code (not root), once per server and
# after updating the rules below; then restart Claude Code from the project root.
# It is a human's tool: the rules deny Claude Code edits to this script and to
# .claude/, so the permission policy cannot rewrite itself.
#
# The model (Claude Code 2.1.x permission syntax):
#   permissions.defaultMode = "dontAsk": anything not pre-approved is denied
#     without waiting for a person.
#   Project files: Read and Edit under the project root (Edit(//<root>/**): the
#     double slash is Claude Code's absolute-path form); edits to .claude/ (this
#     policy), .git/ (hooks and config run code) and this script are denied.
#   Read-only commands (git status/diff/log/show/branch, ls, cat, ...) need no
#     rules: Claude Code's own validators allow them with vetted flags only. A
#     wildcard rule would also admit their write flags (git diff --output=FILE).
#   Git writes: add, commit, rm, mv (git refuses paths outside the repository),
#     switch -c, tag -a; network operations only as exact commands (fetch and
#     pull with free arguments can run commands via --upload-pack or ext::).
#   Tests: scripts/test.sh, not python3 with arbitrary arguments.
#   Kairo's services: status, is-active and restart of kairo and kairo-dashboard.
#
# Not a sandbox: the tests run project code, which Claude Code can edit, as this
# account. See README, "Developing with Claude Code".
set -eu

die() { echo "setup-claude-code: $*" >&2; exit 1; }

dry_run=0 settings=
while [ "$#" -gt 0 ]; do
	case $1 in
		--dry-run) dry_run=1 ;;
		--settings) [ "$#" -ge 2 ] || die "--settings needs a file"; settings=$2; shift ;;
		-h|--help) sed -n '2,8p' "$0"; exit 0 ;;
		*) die "unknown argument: $1" ;;
	esac
	shift
done

[ "$(id -u)" -ne 0 ] || die "run this as the account that runs Claude Code, not root"
command -v git >/dev/null || die "git is required"
command -v python3 >/dev/null || die "python3 is required"

here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel 2>/dev/null) || die "not inside a git checkout"
[ -f "$root/src/kairo/runtime.py" ] && grep -q '^name = "kairo"$' "$root/pyproject.toml" 2>/dev/null \
	|| die "$root is not a Kairo checkout"
case $root in
	/*) ;;
	*) die "project root is not absolute: $root" ;;
esac
# The root becomes part of permission patterns: only plain path characters.
printf '%s' "$root" | grep -Eq '^/[A-Za-z0-9._/-]+$' \
	|| die "project root contains characters unsafe in a permission rule: $root"

real_settings="$root/.claude/settings.local.json"
[ -n "$settings" ] || settings=$real_settings

ROOT="$root" SETTINGS="$settings" DRY_RUN="$dry_run" python3 - <<'PY'
import difflib, json, os, sys

root, path, dry_run = os.environ["ROOT"], os.environ["SETTINGS"], os.environ["DRY_RUN"] == "1"
record_path = os.path.join(os.path.dirname(path), "kairo-managed-permissions.json")

def fail(message):
    print(f"setup-claude-code: {message}", file=sys.stderr)
    sys.exit(1)

abs_ = "/" + root  # Claude Code's absolute path form: //<path>

ALLOW = [
    # Project files.
    f"Read({abs_}/**)",
    f"Edit({abs_}/**)",
    # Git: local writes (git itself refuses paths outside the repository).
    "Bash(git add *)",
    "Bash(git commit *)",
    "Bash(git rm *)",
    "Bash(git mv *)",
    "Bash(git switch main)",
    "Bash(git switch -c *)",
    "Bash(git tag -a *)",
    "Bash(git remote -v)",
    # Git: network, exact commands only.
    "Bash(git fetch origin)",
    "Bash(git pull --ff-only origin main)",
    "Bash(git push origin main)",
    # Tests and checks: fixed entry points, no interpreter wildcards.
    "Bash(scripts/test.sh)",
    "Bash(scripts/test.sh *)",
    "Bash(./scripts/test.sh)",
    "Bash(./scripts/test.sh *)",
    "Bash(node --check src/kairo/dashboard/static/app.js)",
    # Kairo's own services.
    "Bash(sudo systemctl status kairo)",
    "Bash(sudo systemctl is-active kairo)",
    "Bash(sudo systemctl restart kairo)",
    "Bash(sudo systemctl status kairo-dashboard)",
    "Bash(sudo systemctl is-active kairo-dashboard)",
    "Bash(sudo systemctl restart kairo-dashboard)",
]
DENY = [
    # The policy cannot rewrite itself; git internals run code (hooks, config).
    f"Edit({abs_}/.claude/**)",
    f"Edit({abs_}/.git/**)",
    f"Edit({abs_}/scripts/setup-claude-code.sh)",
    "Edit(~/.claude/**)",
    # Credentials and tokens, whatever the mode.
    "Read(//etc/kairo-runtime/**)",
    "Read(~/.claude/.credentials.json)",
    "Read(//var/lib/kairo/dashboard.token)",
]
# Written by the first, superseded bootstrap (setup-kairo-claude-permissions.sh):
# removed if present.
LEGACY = [f"Edit({root}/**)"] + [f"Bash({r})" for r in (
    "git status *", "git diff *", "git log *", "git show *", "git add *", "git commit *",
    "git push *", "git pull *", "git fetch *", "git branch *", "git switch *",
    "git checkout *", "git tag *", "git remote *", "python3 *", "python *", "pytest *",
    "pip *", "uv *", "ls *", f"find {root} *", "grep *", "rg *", f"cat {root}/*",
    f"mkdir -p {root}/*", f"cp {root}/*", f"mv {root}/*", f"rm {root}/*")]

def load(p, default):
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        fail(f"{p} is not readable JSON ({exc}); fix or move it, nothing was changed")

data = load(path, {})
if not isinstance(data, dict):
    fail(f"{path}: the top level must be a JSON object")
perms = data.get("permissions", {})
if not isinstance(perms, dict):
    fail(f"{path}: 'permissions' must be an object")
for key in ("allow", "deny", "ask"):
    if not isinstance(perms.get(key, []), list) or not all(isinstance(r, str) for r in perms.get(key, [])):
        fail(f"{path}: permissions.{key} must be a list of strings")
record = load(record_path, {})
managed_before = set(record.get("allow", [])) | set(record.get("deny", [])) \
    if isinstance(record, dict) else set()
replaceable = managed_before | set(LEGACY)

before = json.dumps(data, indent=2, ensure_ascii=False) + "\n" if os.path.exists(path) else ""
kept_allow = [r for r in perms.get("allow", []) if r not in replaceable]
kept_deny = [r for r in perms.get("deny", []) if r not in replaceable]

# Rules this bounded model must not coexist with: fail closed, a person decides.
BROAD = {"Bash", "Bash(*)", "Bash(:*)", "Edit", "Write", "Read", "Edit(**)", "Edit(//**)"}
INTERPRETERS = ("sudo", "su", "sh", "bash", "zsh", "python", "python3", "pip", "pip3",
                "uv", "node", "npx", "perl", "ruby", "env", "xargs", "find", "curl", "wget")
conflicts = [r for r in kept_allow if r in BROAD or any(
    r.startswith(f"Bash({cmd} ") or r.startswith(f"Bash({cmd}:") or r == f"Bash({cmd})"
    or r.startswith(f"Bash({cmd}*") for cmd in INTERPRETERS) and r not in ALLOW]
if conflicts:
    fail("existing allow rules would defeat the bounded permission model: "
         + ", ".join(conflicts) + f"\nremove them from {path} (or decide otherwise) and run again")

changes = []
if "defaultMode" in data:  # misplaced: Claude Code reads permissions.defaultMode only
    changes.append(f"removed top-level defaultMode={data['defaultMode']!r} (ignored by Claude Code)")
    del data["defaultMode"]
if perms.get("defaultMode") != "dontAsk":
    changes.append(f"permissions.defaultMode: {perms.get('defaultMode')!r} -> 'dontAsk'")
removed = sorted((set(perms.get("allow", [])) | set(perms.get("deny", []))) & replaceable
                 - set(ALLOW) - set(DENY))
perms["allow"] = kept_allow + [r for r in ALLOW if r not in kept_allow]
perms["deny"] = kept_deny + [r for r in DENY if r not in kept_deny]
perms["defaultMode"] = "dontAsk"
data["permissions"] = perms
after = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

preserved = [r for r in kept_allow + kept_deny if r not in ALLOW and r not in DENY]
print(f"settings:     {path}")
for c in changes:
    print(f"change:       {c}")
for r in removed:
    print(f"removed:      {r}")
for r in preserved:
    print(f"preserved:    {r}  (not managed by this script)")
other = sorted(k for k in data if k != "permissions")
if other:
    print(f"preserved:    other settings: {', '.join(other)}")
if before == after:
    print("result:       already up to date")
else:
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                     "before", "after", lineterm="", n=0))
    print(f"result:       {len(diff)} diff lines" + (" (dry run: nothing written)" if dry_run else ""))
    if dry_run:
        print("\n".join(diff))

if not dry_run:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if before and before != after:
        with open(path + ".previous", "w", encoding="utf-8") as f:
            f.write(before)
    for target, text in ((path, after),
                         (record_path, json.dumps({"allow": ALLOW, "deny": DENY}, indent=2) + "\n")):
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    # Validate what was written, as Claude Code will read it.
    check = load(path, None)
    p = check.get("permissions", {}) if isinstance(check, dict) else {}
    if "defaultMode" in check or p.get("defaultMode") != "dontAsk" \
            or not set(ALLOW) <= set(p.get("allow", [])) or not set(DENY) <= set(p.get("deny", [])):
        fail(f"{path} does not hold the expected configuration after writing")
    print("validated:    JSON parses; permissions.defaultMode = dontAsk; all rules present")
PY

if [ "$dry_run" -eq 0 ] && [ "$settings" = "$real_settings" ]; then
	cd "$root"
	for f in .claude/settings.local.json .claude/settings.local.json.previous \
			.claude/kairo-managed-permissions.json; do
		if ! git check-ignore -q "$f"; then
			printf '%s\n' "$f" >> .gitignore
			echo "gitignore:    added $f"
		fi
		git check-ignore -q "$f" || die "$f is still not ignored by git"
		if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
			die "$f is tracked by git: untrack it (git rm --cached $f)"
		fi
	done
	echo "gitignore:    .claude/settings.local.json is ignored and untracked"
fi

cat <<EOF

project root: $root
mode:         dontAsk (anything not allowed below is denied without asking)
allowed:      read and edit project files; git add, commit, rm, mv, switch -c, tag -a;
              git fetch origin / pull --ff-only origin main / push origin main;
              scripts/test.sh; status, is-active, restart of kairo and kairo-dashboard;
              read-only commands Claude Code itself vets (git status/diff/log, ls, ...)
denied:       edits to .claude/, .git/, this script and ~/.claude; credential files;
              other sudo, other systemctl actions, interpreters and package managers
              with free arguments, other git network operations, anything else
next:         restart Claude Code from $root so the settings load
EOF
