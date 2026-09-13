#!/usr/bin/env bash
# Upload dist/ to S3 and refresh CloudFront.
# Immutable files go first; the pointer files (index.html, data/manifest.json)
# go last so they never reference objects that aren't uploaded yet.
set -euo pipefail

: "${BUCKET:?set BUCKET to the site bucket name}"
: "${DISTRIBUTION_ID:?set DISTRIBUTION_ID to the CloudFront distribution id}"
DIST="${DIST:-dist}"
KEEP_VERSIONS="${KEEP_VERSIONS:-4}"

IMMUTABLE="public, max-age=31536000, immutable"
SHORT="public, max-age=60, s-maxage=300"

if [[ ! -f "$DIST/index.html" || ! -f "$DIST/data/manifest.json" ]]; then
  echo "no build found in $DIST; run build.py first" >&2
  exit 1
fi

# Content-hashed assets and versioned data never change once written.
aws s3 cp "$DIST" "s3://$BUCKET" --recursive --only-show-errors \
  --exclude "*" --include "app.*" --include "data/*/*" \
  --cache-control "$IMMUTABLE"

aws s3 cp "$DIST/data/manifest.json" "s3://$BUCKET/data/manifest.json" --only-show-errors \
  --cache-control "$SHORT" --content-type "application/json"
aws s3 cp "$DIST/index.html" "s3://$BUCKET/index.html" --only-show-errors \
  --cache-control "$SHORT" --content-type "text/html; charset=utf-8"

aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" \
  --paths "/" "/index.html" "/data/manifest.json" --query "Invalidation.Id" --output text

# Keep the newest KEEP_VERSIONS data versions (always including the one just deployed).
current="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["built_at"])' "$DIST/data/manifest.json")"
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "data/" --delimiter "/" \
  --query "CommonPrefixes[].Prefix" --output text \
  | tr '\t' '\n' | { grep -E '^data/[0-9]{8}T[0-9]{6}Z/$' || true; } | sort -r | tail -n +"$((KEEP_VERSIONS + 1))" \
  | while read -r prefix; do
      [[ "$prefix" == "data/$current/" ]] && continue
      aws s3 rm "s3://$BUCKET/$prefix" --recursive --only-show-errors
    done

echo "deployed build $current"
