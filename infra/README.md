# Hosting re-dash.com

```
GitHub Actions (weekly) --build.py--> dist/ --deploy.sh (OIDC role)--> S3 (private) <--OAC-- CloudFront <-- Route 53 (re-dash.com, www)
```

Two CloudFormation stacks, both in `us-east-1` (CloudFront certificates must live there):

| Stack | Template | Contents |
|---|---|---|
| `re-dash-dns` | `dns.yaml` | Route 53 hosted zone, email records carried over from Hover |
| `re-dash-site` | `site.yaml` | ACM certificate, S3 bucket, CloudFront + www redirect, DNS aliases, GitHub deploy role |

They are separate because the certificate can't validate until Hover points the domain at Route 53.

## Caching

| Path | Cache-Control | Why |
|---|---|---|
| `app.<hash>.css`, `app.<hash>.js` | `max-age=31536000, immutable` | name changes when content changes |
| `data/<build>/<metro>.json` | `max-age=31536000, immutable` | each build writes a new folder |
| `index.html`, `data/manifest.json` | `max-age=60, s-maxage=300` | pointers to the current build; invalidated on each deploy |

`deploy.sh` keeps the newest 4 data folders and deletes older ones.

## One-time setup

Run from the repo root with AWS credentials for the target account (`aws configure` or `aws sso login`).

1. **Copy every Hover DNS record first.** Open Hover > re-dash.com > DNS and compare with `infra/dns.yaml`. It carries the MX record and `mail` A record found on 2026-09-13; add anything else listed there (TXT/SPF, CNAMEs) or it stops resolving after the switch.

2. **Create the hosted zone.**
   ```sh
   aws cloudformation deploy --region us-east-1 --stack-name re-dash-dns --template-file infra/dns.yaml
   aws cloudformation describe-stacks --region us-east-1 --stack-name re-dash-dns --query "Stacks[0].Outputs" --output table
   ```

3. **Switch nameservers at Hover** to the four `NameServers` from step 2 (Hover keeps the registration). Wait until this shows the Route 53 servers:
   ```sh
   dig +short NS re-dash.com
   ```

4. **Check for an existing GitHub OIDC provider.** If this prints a `token.actions.githubusercontent.com` ARN, use `CreateGitHubOidcProvider=false` in step 5.
   ```sh
   aws iam list-open-id-connect-providers
   ```

5. **Create the site stack** (certificate validation and CloudFront setup take several minutes). The deploy role trusts GitHub's OIDC subject, which embeds the owner and repo IDs, so the GitHub repo must exist first. Look up its prefix:
   ```sh
   gh api repos/MttJns/re-dash/actions/oidc/customization/sub --jq .sub_claim_prefix
   ```
   ```sh
   aws cloudformation deploy --region us-east-1 --stack-name re-dash-site --template-file infra/site.yaml \
     --capabilities CAPABILITY_IAM \
     --parameter-overrides HostedZoneId=<HostedZoneId from step 2> GitHubSubjectPrefix=<prefix> CreateGitHubOidcProvider=true
   aws cloudformation describe-stacks --region us-east-1 --stack-name re-dash-site --query "Stacks[0].Outputs" --output table
   ```

6. **First deploy from your machine.**
   ```sh
   CENSUS_API_KEY=<key> python3 build.py
   BUCKET=<SiteBucket> DISTRIBUTION_ID=<DistributionId> ./deploy.sh
   ```
   Then open https://re-dash.com.

7. **Hand deploys to GitHub Actions.** Create the `MttJns/re-dash` repo and push `main`, then:
   ```sh
   gh secret set AWS_DEPLOY_ROLE_ARN --body <DeployRoleArn>
   gh variable set SITE_BUCKET --body <SiteBucket>
   gh variable set DISTRIBUTION_ID --body <DistributionId>
   gh secret set CENSUS_API_KEY
   ```
   The role ARN is a secret, not a variable, because GitHub prints action inputs in public logs and only secrets are masked there. The `deploy` job skips until `DISTRIBUTION_ID` is set; after that every push to `main` and the weekly schedule rebuild and deploy.

## Notes

- The bucket and hosted zone are retained if their stacks are deleted, so a stack deletion never takes the domain or content down with it.
- The bucket is versioned and keeps replaced or deleted files for 30 days. To roll back a bad deploy, restore the previous versions of `index.html` and `data/manifest.json` (S3 console > object > Versions), then invalidate `/`, `/index.html` and `/data/manifest.json`. The older data and asset files they point to are still in the bucket.
- The deploy role can only list/write this bucket and invalidate this distribution, and only from `main` of the repository identified by `GitHubSubjectPrefix`. Because that prefix uses immutable IDs, a different repo later created under the same name can't deploy.
