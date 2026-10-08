#!/bin/bash
# Publish site/ + the current figures as GitHub Pages under alexjungaalto.
#   ./deploy_github.sh            # uses REPO=helsinki-traffic-health
#   REPO=other-name ./deploy_github.sh
set -euo pipefail
cd "$(dirname "$0")"
OWNER=alexjungaalto
REPO=${REPO:-helsinki-traffic-health}
DESC="Helsinki's chronic-disease index by district vs. road traffic measured by Fintraffic counters — and why the asthma question cannot be answered with open data"
TAG=${TAG:-2023_helsinki_300m_traffic}

# figures must exist; regenerate with: python3 helsinki_morbidity_highways.py --html
for f in "out/map_chronic_disease_index_${TAG}.png" \
         "out/scatter_chronic_disease_index_${TAG}.png" \
         "out/map_chronic_disease_index_${TAG}.html" \
         "out/helsinki_districts_${TAG}.csv"; do
  [ -s "$f" ] || { echo "missing $f -- run the script with --html first"; exit 1; }
done

rm -rf docs && mkdir -p docs
cp site/index.html docs/index.html
cp "out/map_chronic_disease_index_${TAG}.png"    docs/map.png
cp "out/scatter_chronic_disease_index_${TAG}.png" docs/scatter.png
cp "out/map_chronic_disease_index_${TAG}.html"   docs/map.html
cp "out/helsinki_districts_${TAG}.csv"           docs/districts.csv
touch docs/.nojekyll

[ -d .git ] || { git init -q && git branch -M main; }
cat > .gitignore <<'EOF'
cache/
out/
__pycache__/
.DS_Store
EOF

if ! gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  echo "creating $OWNER/$REPO"
  gh repo create "$OWNER/$REPO" --public --description "$DESC" --source . --remote origin >/dev/null
else
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$OWNER/$REPO.git"
fi

git add -A
git commit -q -m "Publish: $(date -u +%Y-%m-%d) run" \
  -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" || echo "nothing to commit"
git push -u origin main

# GitHub Pages from docs/ on main (idempotent)
gh api -X POST "repos/$OWNER/$REPO/pages" -f 'source[branch]=main' -f 'source[path]=/docs' >/dev/null 2>&1 \
  || gh api -X PUT "repos/$OWNER/$REPO/pages" -f 'source[branch]=main' -f 'source[path]=/docs' >/dev/null 2>&1 || true

echo "https://$OWNER.github.io/$REPO/"
