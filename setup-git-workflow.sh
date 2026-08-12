#!/usr/bin/env bash
#
# Builds the develop + feature-branch history and pushes it to GitHub.
#
# HOW TO RUN (Windows): open "Git Bash" in this folder and run:
#     bash setup-git-workflow.sh
#
# It makes three commits on three feature branches, merges each into `develop`
# with --no-ff so every unit of work shows as its own merge commit, and pushes
# everything. It does NOT touch main until you merge the pull requests.
#
# Delete this file once you have run it - it is scaffolding, not part of the
# submission.

set -euo pipefail

echo "==> Checking we are in the right repository"
git rev-parse --is-inside-work-tree >/dev/null
test -f manage.py || { echo "ERROR: run this from the project root"; exit 1; }

echo "==> Recording commit identity"
git config user.name  "Anjali Solanki"
git config user.email "anjalisolanki0104@gmail.com"

echo "==> Clearing the staging area (working tree is left untouched)"
git reset -q

echo "==> Creating develop from main"
git checkout -q main
git checkout -q -b develop 2>/dev/null || git checkout -q develop

# ---------------------------------------------------------------------------
# 1. Line endings.  Must land first: once .gitattributes is committed, every
#    later `git add` normalises to LF automatically, so the content commits
#    below stay free of CRLF noise.
# ---------------------------------------------------------------------------
echo "==> Branch 1/3: chore/normalise-line-endings"
git checkout -q -b chore/normalise-line-endings
git add .gitattributes
git commit -q -m "chore: normalise line endings to LF via .gitattributes

The repository stores LF, but a Windows checkout rewrites every file with
CRLF. Without .gitattributes the next commit reports thousands of phantom
changed lines that bury the real diff. Pinning 'text=auto eol=lf' makes the
stored form canonical regardless of contributor operating system."
git checkout -q develop
git merge -q --no-ff chore/normalise-line-endings \
  -m "Merge branch 'chore/normalise-line-endings' into develop"

# ---------------------------------------------------------------------------
# 2. Author attribution.
# ---------------------------------------------------------------------------
echo "==> Branch 2/3: docs/author-attribution"
git checkout -q -b docs/author-attribution
git add \
  README.md \
  manage.py \
  requirements.txt \
  config/settings.py \
  apps/bookings/models.py \
  apps/bookings/views.py \
  apps/bookings/serializers.py \
  apps/bookings/filters.py \
  apps/bookings/urls.py \
  apps/bookings/services/booking_service.py \
  apps/bookings/services/payment_gateway.py \
  apps/bookings/services/webhook_service.py \
  apps/bookings/management/commands/seed_data.py \
  apps/common/exceptions.py \
  apps/demo/views.py \
  apps/demo/urls.py \
  apps/demo/templates/demo/console.html \
  docs/verify_demo.py \
  tests/conftest.py \
  tests/test_models.py \
  tests/test_booking_api.py \
  tests/test_lsa_search.py \
  tests/test_payment_gateway.py \
  tests/test_payment_webhook.py
git commit -q -m "docs: correct author attribution across the codebase

The README header credited one author while every module docstring and the
README footer credited another. Unified to Anjali Solanki
<anjalisolanki0104@gmail.com>, as the brief requires the codebase to carry the
candidate's name and contact details."
git checkout -q develop
git merge -q --no-ff docs/author-attribution \
  -m "Merge branch 'docs/author-attribution' into develop"

# ---------------------------------------------------------------------------
# 3. Unversioned endpoint aliases.
# ---------------------------------------------------------------------------
echo "==> Branch 3/3: feat/unversioned-endpoint-aliases"
git checkout -q -b feat/unversioned-endpoint-aliases
git add config/urls.py
git commit -q -m "feat(api): expose unversioned endpoint aliases

The brief names these endpoints two ways: /api/bookings/ and
/api/payments/webhook/ in the Outcome section, and /api/v1/bookings/ in the
Expected To Do section. Both now resolve, routed to the same view classes, so
the API answers whichever path a reviewer tries. No logic is duplicated."
git checkout -q develop
git merge -q --no-ff feat/unversioned-endpoint-aliases \
  -m "Merge branch 'feat/unversioned-endpoint-aliases' into develop"

# ---------------------------------------------------------------------------
# Push. main is deliberately left alone so the promotion happens via a PR.
# ---------------------------------------------------------------------------
echo "==> Pushing branches to origin"
git push -u origin chore/normalise-line-endings
git push -u origin docs/author-attribution
git push -u origin feat/unversioned-endpoint-aliases
git push -u origin develop

echo
echo "=============================================================="
echo "Done. Local history:"
git log --oneline --graph --all -12
echo
echo "NEXT STEP - open one pull request on GitHub:"
echo "  https://github.com/AnjaliSolanki04/LSA-Service-Booking-API/compare/main...develop"
echo
echo "Title:  Correct attribution, add unversioned endpoint aliases, pin line endings"
echo "Let CI run, then Merge pull request (choose 'Create a merge commit')."
echo "Afterwards run:  git checkout main && git pull"
echo "=============================================================="
